"""Entity definitions for the MQTT discovery bridge.

Replaces the upstream platform files (sensor.py, binary_sensor.py, button.py,
number.py, switch.py, text.py, time.py, device_tracker.py) and the entity
half of the upstream base.py (commit da32364, first ported from 69fddda). Every class here knows

  - how to describe itself for MQTT discovery (``component``, ``discovery``),
  - how to derive its state / attributes / availability from the coordinator
    (``update()``), writing converted values into ``coordinator._sensors``
    exactly like the upstream entities did,
  - and, for writable entities, how to act on a command payload
    (``handle_command()``).

Deviations from upstream, forced by MQTT discovery:
  - there is no MQTT ``time`` platform, so ``time.battery_charging_start``
    becomes a ``text`` entity with a ``HH:MM`` pattern;
  - number/switch/text values are restored from the stored config instead of
    HA's RestoreEntity, ``last_charge`` keeps its data in the stored config.
"""
import logging
import re
from copy import deepcopy
from datetime import date, datetime, time
from time import gmtime, strftime

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTime,
    UnitOfVolume,
)

from stellantis_vehicles.const import (
    BINARY_SENSORS_DEFAULT,
    KWH_CORRECTION,
    MS_TO_KMH_CONVERSION,
    SENSORS_DEFAULT,
    UPDATE_INTERVAL,
    VEHICLE_TYPE_ELECTRIC,
    VEHICLE_TYPE_HYBRID,
)
from stellantis_vehicles.exceptions import RateLimitException
from stellantis_vehicles.utils import date_from_pt_string, get_datetime, sort_dict, time_from_pt_string

_LOGGER = logging.getLogger(__name__)

TRANSLATION_ROOT = "component.stellantis_vehicles.entity"
LAST_UPDATED_KEY = f"{TRANSLATION_ROOT}.sensor.mileage.state_attributes.last_updated.name"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
PAYLOAD_PRESS = "PRESS"
PAYLOAD_NONE = "None"  # HA MQTT: sets sensor / binary_sensor to unknown


def format_state(value) -> str:
    """Render a python value as MQTT state payload the way HA expects it."""
    if value is None:
        return PAYLOAD_NONE
    if isinstance(value, bool):
        return PAYLOAD_ON if value else PAYLOAD_OFF
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def get_value_from_map(vehicle_data: dict, value_map: list):
    """Walk the vehicle status along ``value_map`` (upstream logic)."""
    value = None
    for key in value_map:
        if value is None:  # first key in the map
            if key in vehicle_data:
                value = vehicle_data[key]
        else:
            if isinstance(key, dict):
                if isinstance(value, list):
                    # dict in the map = {field: wanted} lookup inside a list
                    key_field, key_value = next(iter(key.items()))
                    value = next((item for item in value if item.get(key_field) == key_value), None)
                else:
                    value = None
            elif isinstance(key, int) or key in value:
                try:
                    value = value[key]
                except (IndexError, KeyError, TypeError):
                    value = None
            else:
                value = None
        if value is None:
            break
    return value


def _stellantis_mqtt_connected(stellantis) -> bool:
    """Upstream check: paho still reports is_connected() while on_disconnect
    runs, so the client's own _mqtt_connected flag has to agree as well."""
    return bool(stellantis and stellantis._mqtt and stellantis._mqtt.is_connected()
                and stellantis._mqtt_connected)


class Entity:
    """Base for everything the bridge publishes for one vehicle."""

    component = "sensor"
    # translation section, defaults to component (differs for the time text)
    translation_platform: str | None = None
    has_state = True
    writable = False

    def __init__(self, coordinator, key: str, icon: str | None = None, **discovery) -> None:
        self.coordinator = coordinator
        self.key = key
        self.icon = icon
        # Extra MQTT discovery fields (device_class, unit_of_measurement, ...)
        self.discovery = {k: v for k, v in discovery.items() if v is not None}
        self.native_value = None
        self.attributes: dict = {}

    # --- helpers -------------------------------------------------------------
    @property
    def sensors(self) -> dict:
        return self.coordinator._sensors

    @property
    def data(self) -> dict:
        return self.coordinator.data

    @property
    def vin(self) -> str:
        return self.coordinator.vin

    @property
    def stellantis(self):
        return self.coordinator.stellantis

    @property
    def platform(self) -> str:
        return self.translation_platform or self.component

    @property
    def name(self) -> str:
        return self.coordinator.get_translation(f"{TRANSLATION_ROOT}.{self.platform}.{self.key}.name", self.key)

    @property
    def topic_key(self) -> str:
        """``<component>/<key>``: keys alone are not unique (number and switch
        ``battery_charging_limit``), MQTT topics and unique_ids must be."""
        return f"{self.component}/{self.key}"

    @property
    def unique_id(self) -> str:
        key_formatted = re.sub(r"(?<!^)(?=[A-Z])", "_", self.key).lower()
        return f"{self.vin}_{self.component}_{key_formatted}"

    @property
    def last_updated_label(self) -> str:
        return self.coordinator.get_translation(LAST_UPDATED_KEY, "last_updated")

    @property
    def state(self) -> str | None:
        return format_state(self.native_value) if self.has_state else None

    @property
    def available(self) -> bool | None:
        """None = no entity level availability rules (only bridge/vehicle)."""
        return None

    @property
    def available_command(self) -> bool:
        """Base availability for remote commands (upstream). A pending command
        no longer makes buttons unavailable: the coordinator rejects a second
        command and ``binary_sensor.command_pending`` shows the state."""
        mqtt_is_connected = _stellantis_mqtt_connected(self.stellantis)
        command_is_enabled = self.name not in self.coordinator.disabled_commands
        return mqtt_is_connected and command_is_enabled

    def update(self) -> None:
        """Recompute state/attributes from the coordinator."""
        raise NotImplementedError

    async def handle_command(self, payload: str) -> None:
        raise NotImplementedError

    # --- value conversion (port of upstream StellantisBaseEntity.get_value) ---
    def get_value(self, value_map: list, key: str | None = None):
        key = key or self.key
        sensors = self.sensors
        value = get_value_from_map(self.data, value_map)

        if key == "mileage":
            if (not value or float(value) == 0) and sensors.get("mileage") and float(sensors.get("mileage")) > 0:
                value = sensors.get("mileage")

        if value is not None or key not in sensors:
            sensors[key] = value

        if value is None:
            return None

        if key == "fuel_consumption_total":
            value = float(value) / 100

        if key == "time_battery_charging_start":
            value = time_from_pt_string(value)
            sensors[key] = value

        if key == "battery_charging_end":
            new_updated_at = get_datetime()
            value = date_from_pt_string(value, new_updated_at)
            charge_limit_on = sensors.get("switch_battery_charging_limit", False)
            charge_limit = sensors.get("number_battery_charging_limit")
            current_battery = sensors.get("battery")
            if value and charge_limit_on and charge_limit and current_battery is not None and int(float(current_battery)) < 100:
                now_timestamp = datetime.timestamp(new_updated_at)
                value_timestamp = datetime.timestamp(value)
                diff = value_timestamp - now_timestamp
                limit_diff = (diff / (100 - int(float(current_battery)))) * (int(charge_limit) - int(float(current_battery)))
                value = get_datetime(datetime.fromtimestamp(now_timestamp + limit_diff))

        if key in ["battery_capacity", "battery_residual"]:
            if int(value) < 1:
                value = None
            else:
                value = float(value) / 1000
            # https://github.com/andreadegiovine/homeassistant-stellantis-vehicles/issues/272
            if value and sensors.get("switch_battery_values_correction", False):
                value = value * KWH_CORRECTION

        if key in ["coolant_temperature", "oil_temperature", "air_temperature"]:
            value = float(value)
            country = str(self.coordinator.config.get("country_code", "")).upper()
            fahrenheit_countries = {"US", "BS", "BZ", "KY", "PW", "FM", "MH", "GU", "MP", "AS", "VI", "LR", "MM", "GB"}
            if country in fahrenheit_countries:
                value = (value - 32) * 5.0 / 9.0

        if key == "battery":
            value = int(value)
            # Autonomy from the current payload, not the retained sensor value:
            # that keeps an old 0 while the field is missing (sleeping car) and
            # would zero a fresh battery level (upstream 50c28de).
            autonomy = get_value_from_map(self.data, ["energies", {"type": "Electric"}, "autonomy"])
            if value > 90 and autonomy is not None and autonomy == 0:
                # https://github.com/andreadegiovine/homeassistant-stellantis-vehicles/pull/476
                value = 0

        if isinstance(value, str):
            value = value.lower()

        return value


class MappedEntity(Entity):
    """Entity whose value comes from a value_map into the vehicle status."""

    def __init__(self, coordinator, key, value_map, updated_at_map, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, **discovery)
        self._value_map = deepcopy(value_map or [])
        self._updated_at_map = deepcopy(updated_at_map or [])

    def value_was_updated(self) -> bool:
        current_value = self.sensors.get(self.key)
        self.get_value(self._value_map)
        return current_value != self.sensors.get(self.key)

    def updated_at(self):
        return get_value_from_map(self.data, self._updated_at_map)


# --- sensor -----------------------------------------------------------------
class Sensor(MappedEntity):
    component = "sensor"

    def __init__(self, coordinator, key, value_map, updated_at_map, available=None, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, value_map, updated_at_map, icon, **discovery)
        self._available_rules = available

    @property
    def available(self) -> bool | None:
        if not self._available_rules:
            return None
        result = True
        for rule in self._available_rules:
            if not result:
                break
            for key in rule:
                if not result:
                    break
                if key in self.sensors:
                    if isinstance(rule[key], list):
                        result = self.sensors.get(key) in rule[key]
                    else:
                        result = rule[key] == self.sensors.get(key)
        return result

    def update(self) -> None:
        if self.value_was_updated():
            self.attributes[self.last_updated_label] = self.updated_at()
            self.native_value = self.get_value(self._value_map)


class TypeSensor(Entity):
    component = "sensor"

    def update(self) -> None:
        self.native_value = self.coordinator.vehicle_type.lower()


class CommandStatusSensor(Entity):
    component = "sensor"

    def update(self) -> None:
        history = self.coordinator.command_history
        if history:
            self.native_value = history[next(iter(history))]
            # Newest 20 entries, oldest first, as attributes
            self.attributes = dict(list(history.items())[:20])


class LastTripSensor(Entity):
    component = "sensor"

    def update(self) -> None:
        last_trip = self.coordinator.last_trip
        if not last_trip:
            return
        self.native_value = last_trip.get("distance")

        attributes = {}
        if "duration" in last_trip and float(last_trip["duration"]) > 0:
            attributes["duration"] = strftime("%H:%M:%S", gmtime(last_trip["duration"]))
        if "startMileage" in last_trip:
            attributes["start_mileage"] = f"{last_trip['startMileage']} {UnitOfLength.KILOMETERS}"
        kinetic = last_trip.get("kinetic") or {}
        if "avgSpeed" in kinetic and float(kinetic["avgSpeed"]) > 0:
            avg_speed_kmh = float(kinetic["avgSpeed"]) * MS_TO_KMH_CONVERSION
            attributes["avg_speed"] = f"{round(avg_speed_kmh, 2)} {UnitOfSpeed.KILOMETERS_PER_HOUR}"
        if "maxSpeed" in kinetic and float(kinetic["maxSpeed"]) > 0:
            attributes["max_speed"] = f"{round(float(kinetic['maxSpeed']), 2)} {UnitOfSpeed.KILOMETERS_PER_HOUR}"
        for consumption in last_trip.get("energyConsumptions") or []:
            if "type" not in consumption:
                continue
            if consumption["type"] == VEHICLE_TYPE_ELECTRIC:
                unit = UnitOfEnergy.KILO_WATT_HOUR
                avg_unit = f"{UnitOfEnergy.KILO_WATT_HOUR}/100{UnitOfLength.KILOMETERS}"
                divide = 1000
                if self.sensors.get("switch_battery_values_correction", False):
                    divide = divide / KWH_CORRECTION
            else:
                unit = UnitOfVolume.LITERS
                avg_unit = f"{UnitOfVolume.LITERS}/100{UnitOfLength.KILOMETERS}"
                divide = 100
            prefix = consumption["type"].lower()
            if "consumption" in consumption and round(float(consumption["consumption"]) / divide, 2) > 0:
                attributes[f"{prefix}_consumption"] = f"{round(float(consumption['consumption']) / divide, 2)} {unit}"
            if "avgConsumption" in consumption and round(float(consumption["avgConsumption"]) / divide, 2) > 0:
                attributes[f"{prefix}_avg_consumption"] = f"{round(float(consumption['avgConsumption']) / divide, 2)} {avg_unit}"
        self.attributes = attributes


class LastChargeSensor(Entity):
    """Charge session tracker. State/attributes survive restarts via the
    stored config (upstream used RestoreSensor)."""

    component = "sensor"
    STORE_KEY = "last_charge"
    UNITS = {
        "initial_percentage": PERCENTAGE,
        "final_percentage": PERCENTAGE,
        "recharged_percent": PERCENTAGE,
        "initial_energy": UnitOfEnergy.KILO_WATT_HOUR,
        "final_energy": UnitOfEnergy.KILO_WATT_HOUR,
        "recharged_energy": UnitOfEnergy.KILO_WATT_HOUR,
        "avg_power": UnitOfPower.KILO_WATT,
        "initial_autonomy": UnitOfLength.KILOMETERS,
        "final_autonomy": UnitOfLength.KILOMETERS,
        "recharged_autonomy": UnitOfLength.KILOMETERS,
        "mileage": UnitOfLength.KILOMETERS,
    }
    ORDERED_KEYS = [
        "in_progress", "mileage", "duration", "final_time",
        "initial_percentage", "final_percentage", "recharged_percent",
        "initial_energy", "final_energy", "recharged_energy",
        "initial_autonomy", "final_autonomy", "recharged_autonomy", "avg_power",
    ]

    def __init__(self, coordinator, key, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, **discovery)
        stored = self.stellantis.get_vehicle_stored_config(self.vin, self.STORE_KEY) or {}
        if stored.get("state"):
            try:
                self.native_value = datetime.fromisoformat(stored["state"])
            except ValueError:
                self.native_value = None
        self.attributes = dict(stored.get("attributes") or {})

    def _persist(self) -> None:
        state = self.native_value.isoformat() if isinstance(self.native_value, datetime) else None
        stored = {"state": state, "attributes": self.attributes}
        if self.stellantis.get_vehicle_stored_config(self.vin, self.STORE_KEY) != stored:
            self.stellantis.update_vehicle_stored_config(self.vin, self.STORE_KEY, stored)

    def update(self) -> None:
        sensors = self.sensors
        in_progress = sensors.get("battery_charging") == "InProgress"
        attributes = deepcopy(self.attributes)

        # Strip units so the values can be used in calculations
        for attribute in attributes:
            if attribute in self.UNITS and isinstance(attributes[attribute], str):
                attributes[attribute] = attributes[attribute].replace(f" {self.UNITS[attribute]}", "")

        prev_in_progress = bool(attributes.get("in_progress"))
        if prev_in_progress and not isinstance(self.native_value, datetime):
            # Stored data is inconsistent (in_progress without a start time):
            # drop it, otherwise neither the end nor the next start is detected.
            _LOGGER.warning("Dropping inconsistent last_charge data for %s", self.vin)
            attributes = {}
            prev_in_progress = False

        divide = 1000
        if sensors.get("switch_battery_values_correction", False):
            divide = divide / KWH_CORRECTION

        if in_progress and not prev_in_progress:
            # Start of charging detected
            self.native_value = get_datetime()
            attributes = {"in_progress": True}
            if sensors.get("mileage") is not None:
                attributes["mileage"] = round(sensors.get("mileage"))
            if sensors.get("battery") is not None:
                attributes["initial_percentage"] = round(sensors.get("battery"))
            if sensors.get("battery_residual") is not None:
                attributes["initial_energy"] = round(float(sensors.get("battery_residual")) / divide, 2)
            if sensors.get("autonomy") is not None:
                attributes["initial_autonomy"] = sensors.get("autonomy")

        elif prev_in_progress and not in_progress and isinstance(self.native_value, datetime):
            # End of charging detected
            del attributes["in_progress"]
            final_time = get_datetime()
            attributes["final_time"] = final_time.isoformat()
            if sensors.get("battery") is not None:
                attributes["final_percentage"] = round(sensors.get("battery"))
            if sensors.get("battery_residual") is not None:
                attributes["final_energy"] = round(float(sensors.get("battery_residual")) / divide, 2)
            if sensors.get("autonomy") is not None:
                attributes["final_autonomy"] = sensors.get("autonomy")

            duration = final_time - self.native_value
            attributes["duration"] = strftime("%H:%M:%S", gmtime(duration.total_seconds()))

            if "initial_percentage" in attributes and "final_percentage" in attributes:
                attributes["recharged_percent"] = round(float(attributes["final_percentage"]) - float(attributes["initial_percentage"]))
            if "initial_energy" in attributes and "final_energy" in attributes:
                recharged_energy = float(attributes["final_energy"]) - float(attributes["initial_energy"])
                attributes["recharged_energy"] = round(recharged_energy, 2)
                hours = duration.total_seconds() / 3600
                if hours > 0:
                    attributes["avg_power"] = round(recharged_energy / hours, 2)
            if "initial_autonomy" in attributes and "final_autonomy" in attributes:
                attributes["recharged_autonomy"] = round(float(attributes["final_autonomy"]) - float(attributes["initial_autonomy"]))

        # Restore units
        for attribute in attributes:
            if attribute in self.UNITS:
                attributes[attribute] = f"{attributes[attribute]} {self.UNITS[attribute]}"

        self.attributes = sort_dict(attributes, self.ORDERED_KEYS)
        self._persist()


# --- binary sensor ------------------------------------------------------------
class BinarySensor(MappedEntity):
    component = "binary_sensor"

    def __init__(self, coordinator, key, value_map, updated_at_map, on_value=None, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, value_map, updated_at_map, icon, **discovery)
        self._on_value = on_value

    def update(self) -> None:
        if self.value_was_updated():
            self.attributes[self.last_updated_label] = self.updated_at()
            value = self.get_value(self._value_map)
            if value is None:
                return
            if isinstance(value, list):
                self.native_value = self._on_value in value
            else:
                self.native_value = str(value).lower() == str(self._on_value).lower()


class RemoteCommandsBinarySensor(Entity):
    component = "binary_sensor"

    def update(self) -> None:
        self.native_value = _stellantis_mqtt_connected(self.stellantis)


class CommandPendingBinarySensor(Entity):
    component = "binary_sensor"

    def update(self) -> None:
        self.native_value = self.coordinator.pending_action


# --- device tracker -----------------------------------------------------------
class DeviceTracker(Entity):
    component = "device_tracker"
    has_state = False

    def __init__(self, coordinator, key, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, **discovery)
        # Without a www folder the upstream client keeps the manufacturer's
        # absolute picture URL - exactly what HA's entity_picture accepts.
        picture = str(coordinator.vehicle.get("picture") or "")
        if picture.startswith("http"):
            self.discovery["entity_picture"] = picture

    @property
    def _last_position(self) -> dict:
        last_position = self.data.get("lastPosition")
        return last_position if isinstance(last_position, dict) else {}

    def update(self) -> None:
        geometry = self._last_position.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        coordinates = coordinates if isinstance(coordinates, list) else []
        properties = self._last_position.get("properties")
        properties = properties if isinstance(properties, dict) else {}

        attributes = {}
        if len(coordinates) >= 2:
            attributes["latitude"] = float(coordinates[1])
            attributes["longitude"] = float(coordinates[0])
            attributes["gps_accuracy"] = 10
        attributes["altitude"] = float(coordinates[2]) if len(coordinates) == 3 else None
        attributes["fix_status"] = properties.get("fixStatus")
        attributes["signal_quality"] = properties.get("signalQuality")
        attributes["position_updated_at"] = properties.get("createdAt")
        self.attributes = attributes


# --- buttons ------------------------------------------------------------------
class Button(Entity):
    component = "button"
    has_state = False
    writable = True

    def __init__(self, coordinator, key, icon=None, action=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, payload_press=PAYLOAD_PRESS, **discovery)
        self._action = action

    @property
    def available(self) -> bool | None:
        return self.available_command

    def update(self) -> None:
        return

    async def handle_command(self, payload: str) -> None:
        if payload != PAYLOAD_PRESS:
            _LOGGER.warning("Ignoring unexpected payload %r for button %s", payload, self.key)
            return
        await self.press()

    async def press(self) -> None:
        raise NotImplementedError


class WakeUpButton(Button):
    async def press(self) -> None:
        try:
            await self.coordinator.send_wakeup_command(self.name)
        except RateLimitException:
            self.coordinator.update_command_history_rate_limit(self.name)


class DoorButton(Button):
    async def press(self) -> None:
        await self.coordinator.send_doors_command(self.name, self._action)


class HornButton(Button):
    async def press(self) -> None:
        await self.coordinator.send_horn_command(self.name)


class LightsButton(Button):
    async def press(self) -> None:
        await self.coordinator.send_lights_command(self.name)


class ChargingStartStopButton(Button):
    @property
    def available(self) -> bool:
        charging = self.sensors.get("battery_charging")
        if charging == "InProgress" and self.key == "charge_start":
            return False
        if charging in ["Finished", "Stopped"] and self.key == "charge_stop":
            return False
        charging_inprogress_stopped = charging in ["InProgress", "Stopped"]
        charging_finished = charging == "Finished"
        current_battery = self.sensors.get("battery")
        return bool(
            self.available_command
            and self.sensors.get("time_battery_charging_start")
            and (charging_inprogress_stopped
                 or (charging_finished and current_battery and int(float(current_battery)) < 100))
        )

    async def press(self) -> None:
        await self.coordinator.send_charge_command(self.name, False, self._action)


class PreconditioningButton(Button):
    @property
    def available(self) -> bool:
        if self.coordinator.vehicle_type not in [VEHICLE_TYPE_ELECTRIC, VEHICLE_TYPE_HYBRID]:
            return False
        doors = self.sensors.get("doors")
        doors_locked = doors is None or "Locked" in doors
        min_charge = 20
        battery = self.sensors.get("battery")
        check_battery_level = bool(battery and int(float(battery)) >= min_charge)
        check_battery_charging = self.sensors.get("battery_charging") == "InProgress"
        return bool(self.available_command and doors_locked and (check_battery_level or check_battery_charging))

    async def press(self) -> None:
        await self.coordinator.send_preconditioning_command(self.name, self._action)


class ChargingLimitButton(Button):
    """Native 80 % charge limit of the vehicle (``daily`` on / ``trip`` off)."""

    async def press(self) -> None:
        await self.coordinator.send_charge_limit_command(self.name, self._action)


def native_charge_limit_active(sensors: dict) -> bool:
    return sensors.get("battery_charging_limit") == "Partial"


# --- stored-config backed entities (number / switch / text) ------------------
class StoredEntity(Entity):
    """Value lives in the per-vehicle stored config; mirrored into _sensors."""

    writable = True
    sensor_prefix = ""

    def __init__(self, coordinator, key, icon=None, default_value=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, **discovery)
        self.sensor_key = f"{self.sensor_prefix}_{key}"
        self._default_value = default_value

    def _convert(self, value):
        return value

    def stored_value(self):
        value = self.stellantis.get_vehicle_stored_config(self.vin, self.sensor_key)
        if value is not None:
            value = self._convert(value)
            self.sensors[self.sensor_key] = value
            return value
        if self.sensor_key in self.sensors:
            return self.sensors.get(self.sensor_key)
        return self._default_value

    def update(self) -> None:
        self.native_value = self.stored_value()

    async def set_value(self, value) -> None:
        value = self._convert(value)
        self.native_value = value
        self.sensors[self.sensor_key] = value
        self.stellantis.update_vehicle_stored_config(self.vin, self.sensor_key, value)
        await self.coordinator.async_refresh()


class Number(StoredEntity):
    component = "number"
    sensor_prefix = "number"

    def _convert(self, value):
        return float(value)

    async def handle_command(self, payload: str) -> None:
        try:
            value = float(payload)
        except ValueError:
            _LOGGER.warning("Ignoring non-numeric payload %r for number %s", payload, self.key)
            return
        # min/max from the discovery payload are enforced by the HA UI only;
        # raw publishes on the command topic must not bypass them.
        low, high = self.discovery.get("min"), self.discovery.get("max")
        clamped = value
        if low is not None:
            clamped = max(float(low), clamped)
        if high is not None:
            clamped = min(float(high), clamped)
        if clamped != value:
            _LOGGER.warning("Value %s for number %s outside %s..%s, using %s", value, self.key, low, high, clamped)
        await self.set_value(clamped)


class Switch(StoredEntity):
    component = "switch"
    sensor_prefix = "switch"

    def __init__(self, coordinator, key, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, default_value=False,
                         payload_on=PAYLOAD_ON, payload_off=PAYLOAD_OFF, **discovery)

    def _convert(self, value):
        return bool(value)

    async def handle_command(self, payload: str) -> None:
        if payload not in (PAYLOAD_ON, PAYLOAD_OFF):
            _LOGGER.warning("Ignoring unexpected payload %r for switch %s", payload, self.key)
            return
        await self.set_value(payload == PAYLOAD_ON)


class BatteryChargingLimitNumber(Number):
    @property
    def available(self) -> bool:
        return not native_charge_limit_active(self.sensors)


class BatteryChargingLimitSwitch(Switch):
    @property
    def available(self) -> bool:
        return bool(self.sensors.get("number_battery_charging_limit", False)) and not native_charge_limit_active(self.sensors)


class AbrpSyncSwitch(Switch):
    @property
    def available(self) -> bool:
        token = self.sensors.get("text_abrp_token")
        return bool(token and len(token) == 36)


class Text(StoredEntity):
    component = "text"
    sensor_prefix = "text"

    def __init__(self, coordinator, key, icon=None, **discovery) -> None:
        super().__init__(coordinator, key, icon, default_value="", **discovery)

    def _convert(self, value):
        return str(value)

    async def handle_command(self, payload: str) -> None:
        await self.set_value(payload)


class ChargingStartTimeText(MappedEntity):
    """Upstream ``time.battery_charging_start``; MQTT has no time platform, so
    it is exposed as a text entity taking ``HH:MM``."""

    component = "text"
    translation_platform = "time"
    writable = True

    def __init__(self, coordinator, key, icon=None, **discovery) -> None:
        super().__init__(
            coordinator, key,
            ["energies", {"type": "Electric"}, "extension", "electric", "charging", "nextDelayedTime"],
            ["energy", {"type": "Electric"}, "updatedAt"],
            icon, pattern=TIME_PATTERN, min=5, max=5, mode="text", **discovery,
        )
        self.sensor_key = f"time_{key}"

    def value_was_updated(self) -> bool:
        current_value = self.sensors.get(self.sensor_key)
        self.get_value(self._value_map, self.sensor_key)
        return current_value != self.sensors.get(self.sensor_key)

    @property
    def available(self) -> bool:
        return self.available_command

    def update(self) -> None:
        if self.value_was_updated():
            self.attributes[self.last_updated_label] = self.updated_at()
            self.native_value = self.get_value(self._value_map, self.sensor_key)

    async def handle_command(self, payload: str) -> None:
        if not re.match(TIME_PATTERN, payload):
            _LOGGER.warning("Ignoring payload %r for %s, expected HH:MM", payload, self.key)
            return
        hour, minute = payload.split(":")
        value = time(int(hour), int(minute))
        self.native_value = value
        self.sensors[self.sensor_key] = value
        await self.coordinator.send_charge_command(self.name, True)
        await self.coordinator.async_refresh()


# --- factory (replaces the platform async_setup_entry functions) -------------
def build_entities(coordinator, remote_commands: bool) -> list[Entity]:
    """Entities for one vehicle, in upstream PLATFORMS order.

    The order matters: entities update sequentially and later ones read
    ``_sensors`` values written by earlier ones (last_charge needs
    battery_charging from the binary sensors, buttons need the charge time).
    """
    vehicle_type = coordinator.vehicle_type
    is_ev = vehicle_type in [VEHICLE_TYPE_ELECTRIC, VEHICLE_TYPE_HYBRID]
    entities: list[Entity] = []

    # binary_sensor.py
    for key, default in BINARY_SENSORS_DEFAULT.items():
        engine_limit = default.get("engine", [])
        if engine_limit and vehicle_type not in engine_limit:
            continue
        if not (default.get("value_map") and default.get("updated_at_map")):
            continue
        entities.append(BinarySensor(
            coordinator, key, default["value_map"], default["updated_at_map"],
            on_value=default.get("on_value"),
            icon=default.get("icon"),
            device_class=default.get("device_class"),
            entity_category=default.get("entity_category"),
        ))
    if remote_commands:
        entities.append(RemoteCommandsBinarySensor(coordinator, "remote_commands", icon="mdi:broadcast",
                                                   device_class=BinarySensorDeviceClass.CONNECTIVITY,
                                                   entity_category=EntityCategory.DIAGNOSTIC))
        entities.append(CommandPendingBinarySensor(coordinator, "command_pending", icon="mdi:progress-clock",
                                                   entity_category=EntityCategory.DIAGNOSTIC))

    # device_tracker.py
    entities.append(DeviceTracker(coordinator, "vehicle", icon=SENSORS_DEFAULT["vehicle"]["icon"], source_type="gps"))

    # button.py
    if remote_commands:
        entities.append(WakeUpButton(coordinator, "wakeup", icon="mdi:sleep"))
        entities.append(DoorButton(coordinator, "doors_lock", icon="mdi:car-door-lock", action="lock"))
        entities.append(DoorButton(coordinator, "doors_unlock", icon="mdi:car-door-lock-open", action="unlock"))
        entities.append(HornButton(coordinator, "horn", icon="mdi:bullhorn"))
        entities.append(LightsButton(coordinator, "lights", icon="mdi:car-parking-lights"))
        entities.append(PreconditioningButton(coordinator, "preconditioning_start", icon="mdi:fan", action="activate"))
        entities.append(PreconditioningButton(coordinator, "preconditioning_stop", icon="mdi:fan-off", action="deactivate"))
        if is_ev:
            entities.append(ChargingStartStopButton(coordinator, "charge_start", icon="mdi:battery-charging", action="immediate"))
            entities.append(ChargingStartStopButton(coordinator, "charge_stop", icon="mdi:battery-off", action="delayed"))
            entities.append(ChargingLimitButton(coordinator, "charge_limit_on", icon="mdi:battery-lock", action="daily"))
            entities.append(ChargingLimitButton(coordinator, "charge_limit_off", icon="mdi:battery-lock-open", action="trip"))

    # number.py
    if is_ev and remote_commands:
        entities.append(BatteryChargingLimitNumber(coordinator, "battery_charging_limit", icon="mdi:battery-charging-60",
                               unit_of_measurement=PERCENTAGE, min=15, max=95, step=1, mode="slider",
                               entity_category=EntityCategory.CONFIG))
    entities.append(Number(coordinator, "refresh_interval", icon="mdi:sync", default_value=float(UPDATE_INTERVAL),
                           unit_of_measurement=UnitOfTime.SECONDS, min=30, max=3600, step=5, mode="box",
                           entity_category=EntityCategory.CONFIG))

    # switch.py
    if is_ev:
        if remote_commands:
            entities.append(BatteryChargingLimitSwitch(coordinator, "battery_charging_limit", icon="mdi:battery-charging-60",
                                                       entity_category=EntityCategory.CONFIG))
        entities.append(AbrpSyncSwitch(coordinator, "abrp_sync", icon="mdi:source-branch-sync",
                                       entity_category=EntityCategory.CONFIG))
        entities.append(Switch(coordinator, "battery_values_correction", icon="mdi:auto-fix",
                               entity_category=EntityCategory.CONFIG))

    # sensor.py
    for key, default in SENSORS_DEFAULT.items():
        engine_limit = default.get("engine", [])
        if engine_limit and vehicle_type not in engine_limit:
            continue
        if not (default.get("value_map") and default.get("updated_at_map")):
            continue
        entities.append(Sensor(
            coordinator, key, default["value_map"], default["updated_at_map"],
            available=default.get("available"),
            icon=default.get("icon"),
            unit_of_measurement=default.get("unit_of_measurement"),
            device_class=default.get("device_class"),
            state_class=default.get("state_class"),
            suggested_display_precision=default.get("suggested_display_precision"),
        ))
    if is_ev:
        entities.append(LastChargeSensor(coordinator, "last_charge", icon="mdi:ev-station",
                                         device_class=SensorDeviceClass.TIMESTAMP,
                                         entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(TypeSensor(coordinator, "type", icon="mdi:car-info",
                               entity_category=EntityCategory.DIAGNOSTIC))
    if remote_commands:
        entities.append(CommandStatusSensor(coordinator, "command_status", icon="mdi:format-list-bulleted-type",
                                            entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(LastTripSensor(coordinator, "last_trip", icon="mdi:map-marker-path",
                                   unit_of_measurement=UnitOfLength.KILOMETERS,
                                   device_class=SensorDeviceClass.DISTANCE,
                                   entity_category=EntityCategory.DIAGNOSTIC))

    # text.py
    if is_ev:
        entities.append(Text(coordinator, "abrp_token", icon="mdi:source-branch", max=100,
                             entity_category=EntityCategory.CONFIG))

    # time.py
    if is_ev and remote_commands:
        entities.append(ChargingStartTimeText(coordinator, "battery_charging_start", icon="mdi:battery-clock"))

    return entities
