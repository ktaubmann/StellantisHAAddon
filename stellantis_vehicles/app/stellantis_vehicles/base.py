"""Own replacement for the upstream DataUpdateCoordinator.

Port of the coordinator half of the upstream ``base.py`` (develop 7f75d7d,
first ported from 69fddda) without anything Home-Assistant-specific. Not
ported: the supported-features lookup (upstream shows it in the diagnostics
only). stellantis.py imports
``StellantisVehicleCoordinator`` from here and touches:
  - coordinator._vehicle / coordinator._commands_history (per action_id:
    name, updates, service, message, retried, sent_at - the MQTT 400 retry
    resends the entry's own service/message)
  - coordinator.async_refresh()
  - coordinator.update_command_history(correlation_id, result_code)

Entities do not exist here; the MQTT bridge registers a listener and reads
``data`` / ``_sensors`` exactly like the upstream entity classes did. The
``_sensors`` dict is shared state: the bridge writes the converted entity
values into it and the coordinator reads them back (charge limit, ABRP sync,
last trip detection) - same one-cycle lag as upstream.
"""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from homeassistant.components import persistent_notification
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import translation

from .const import (
    COMMAND_HISTORY_LIMIT,
    COMMAND_STATUS_STILL_IN_PROGRESS,
    DOMAIN,
    EMPTY_STATUS_LIMIT,
    PENDING_ACTION_TIMEOUT,
    UPDATE_INTERVAL,
    VEHICLE_TYPE_ELECTRIC,
    VEHICLE_TYPE_HYBRID,
)
from .utils import (
    SENSITIVE_DATA_FILTER,
    get_datetime,
    log_call,
    rate_limit,
    time_from_pt_string,
    vehicle_removed_issue_id,
)

_LOGGER = logging.getLogger(__name__)
# Shared filter instance, attached once at import (upstream issue #414).
_LOGGER.addFilter(SENSITIVE_DATA_FILTER)

Listener = Callable[["StellantisVehicleCoordinator"], Awaitable[None] | None]


class StellantisVehicleCoordinator:
    def __init__(self, hass, config, vehicle, stellantis, translations, config_entry=None) -> None:
        self._hass = hass
        self._config = config
        self._vehicle = vehicle
        self._stellantis = stellantis
        self._translations = translations
        self._config_entry = config_entry
        self.name = DOMAIN
        self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
        self.last_update_success = False

        self._data: dict = {}
        self._sensors: dict = {}
        self._commands_history: dict = {}
        self._disabled_commands: list[str] = []
        # action_id of the command currently blocking further remote commands,
        # or None; cleared once that command reaches a final status.
        self._pending_action_id: str | None = None
        self._last_trip = None
        self._manage_charge_limit_sent = False
        self._phase_offset = 0
        self._privacy_full_logged = False
        self._empty_status_count = 0
        self._vehicle_removed = False
        # Set once the maintenance endpoint returned nothing, so it is not
        # polled again for this coordinator (upstream issue #623).
        self._maintenance_unsupported = False

        self._listeners: list[Listener] = []
        self._task: asyncio.Task | None = None
        # Strong references for fire-and-forget listener notifications.
        self._notify_tasks: set[asyncio.Task] = set()
        self._refresh_lock = asyncio.Lock()
        # Called once when polling stops because the token is dead
        # (upstream: config_entry.async_start_reauth). Set by main.py.
        self.on_auth_failed: Callable[[], None] | None = None

    # --- public API used by the bridge -------------------------------------
    @property
    def data(self) -> dict:
        return self._data

    @property
    def vehicle(self) -> dict:
        return self._vehicle

    @property
    def vin(self) -> str:
        return self._vehicle.get("vin", "")

    @property
    def vehicle_type(self) -> str:
        return self._vehicle["type"]

    @property
    def stellantis(self):
        return self._stellantis

    @property
    def config(self) -> dict:
        return self._config

    @property
    def disabled_commands(self) -> list[str]:
        return self._disabled_commands

    @property
    def last_trip(self):
        return self._last_trip

    def get_translation(self, path, default=None):
        return self._translations.get(path, default)

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def start(self) -> None:
        """Start polling; also restarts a loop that ended after an auth failure."""
        if self._task is None or self._task.done():
            self._task = self._hass.loop.create_task(self._run(), name=f"{DOMAIN}:{self.vin}")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def stagger_first_poll(self, offset_seconds) -> None:
        """Push this vehicle's first poll back once so several vehicles do not all
        poll at the same instant after a restart."""
        offset_seconds = int(offset_seconds)
        if offset_seconds <= 0:
            return
        self._phase_offset = offset_seconds

    def async_update_listeners(self) -> None:
        """Upstream name: schedule a listener notification from sync code."""
        task = self._hass.loop.create_task(self._notify())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    # --- polling -------------------------------------------------------------
    async def _run(self) -> None:
        if self._phase_offset:
            await asyncio.sleep(self._phase_offset)
            self._phase_offset = 0
        while True:
            try:
                await self.async_refresh()
            except ConfigEntryAuthFailed:
                _LOGGER.error("Authentication failed for %s, stopping polling", self.vin)
                if self.on_auth_failed:
                    self.on_auth_failed()
                return
            await asyncio.sleep(self._current_interval())

    def _current_interval(self) -> float:
        refresh_interval = self._sensors.get("number_refresh_interval")
        if refresh_interval and refresh_interval > 0:
            self.update_interval = timedelta(seconds=refresh_interval)
        else:
            self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
        return self.update_interval.total_seconds()

    async def async_refresh(self) -> None:
        async with self._refresh_lock:
            try:
                await self._async_update_data()
            except ConfigEntryAuthFailed:
                self.last_update_success = False
                await self._notify()
                raise
            except Exception as err:  # noqa: BLE001 - upstream raises UpdateFailed here
                _LOGGER.debug("Error communicating with Stellantis API: %s", err)
                self.last_update_success = False
                await self._notify()
                return
            self.last_update_success = True
            await self._notify()

    @log_call
    async def _async_update_data(self) -> None:
        """Update vehicle data from Stellantis (upstream logic, minus UpdateFailed)."""
        new_data = await self._stellantis.get_vehicle_status(self._vehicle)

        if not new_data:
            # Keep the last known data instead of blanking every entity on a
            # single empty response (404 / empty body).
            self._empty_status_count += 1
            _LOGGER.debug("Empty vehicle status response (%s in a row), keeping last known data",
                          self._empty_status_count)
            if self._empty_status_count < EMPTY_STATUS_LIMIT:
                return
            if self._empty_status_count % EMPTY_STATUS_LIMIT == 0 and not self._vehicle_removed:
                await self._reconcile_vehicle()
            raise RuntimeError("Empty vehicle status response")

        await self._add_maintenance(new_data)
        self._empty_status_count = 0
        self._clear_vehicle_removed()
        self._log_privacy_mode(new_data.get("privacy", {}).get("state"))

        if self._is_stale(new_data):
            return

        self._data = new_data
        await self.after_async_update_data()

    async def _add_maintenance(self, new_data: dict) -> None:
        """Merge the maintenance endpoint into the status (upstream ``_fetch_new_data``).

        Deviation from upstream: an error on this optional endpoint keeps the
        last known maintenance values instead of failing the whole update.
        """
        if self._maintenance_unsupported:
            return
        try:
            maintenance = await self._stellantis.get_vehicle_maintenance(self._vehicle)
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not fetch maintenance data for %s: %s", self.vin, err)
            if "maintenance" in self._data:
                new_data["maintenance"] = self._data["maintenance"]
            return
        if maintenance:
            new_data["maintenance"] = {
                "mileageBeforeMaintenance": maintenance.get("mileageBeforeMaintenance"),
                "daysBeforeMaintenance": maintenance.get("daysBeforeMaintenance"),
                "updatedAt": maintenance.get("updatedAt"),
            }
        else:
            _LOGGER.debug("Vehicle maintenance data not found - disabling further maintenance polling")
            self._maintenance_unsupported = True

    def _is_stale(self, new_data: dict) -> bool:
        """Whether new_data's updatedAt is not newer than the data already held."""
        if "updatedAt" not in new_data or "updatedAt" not in self._data:
            return False
        try:
            current_dt = datetime.fromisoformat(self._data["updatedAt"])
            new_dt = datetime.fromisoformat(new_data["updatedAt"])
        except (ValueError, TypeError):
            _LOGGER.debug("Invalid updatedAt values, proceeding without timestamp comparison")
            return False
        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=UTC)
        if new_dt.tzinfo is None:
            new_dt = new_dt.replace(tzinfo=UTC)
        if new_dt <= current_dt:
            _LOGGER.debug("API did not return updated vehicle data, skipping sensor update")
            return True
        return False

    def _log_privacy_mode(self, state) -> None:
        active = state == "Full"
        if active and not self._privacy_full_logged:
            self._privacy_full_logged = True
            _LOGGER.info("Private mode is enabled on vehicle %s, Stellantis has paused live data updates", self.vin)
        elif not active and self._privacy_full_logged:
            self._privacy_full_logged = False
            _LOGGER.info("Private mode is disabled on vehicle %s, live data updates resumed", self.vin)

    async def _reconcile_vehicle(self) -> None:
        """Re-fetch the account vehicle list to check whether this vehicle was unpaired."""
        try:
            live = await self._stellantis.get_user_vehicles(force=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not refresh the vehicle list for %s: %s", self.vin, err)
            return
        live_vins = {vehicle.get("vin") for vehicle in live}
        if not live_vins or self.vin in live_vins:
            return
        self._vehicle_removed = True
        _LOGGER.warning("Vehicle %s is no longer linked to this Stellantis account", self.vin)
        # No repair issues without HA core: surface it as a notification instead.
        persistent_notification.async_create(
            self._hass,
            f"Vehicle {self.vin} is no longer linked to this Stellantis account.",
            title="Stellantis Vehicles",
            notification_id=vehicle_removed_issue_id(self.vin),
        )

    def _clear_vehicle_removed(self) -> None:
        if not self._vehicle_removed:
            return
        self._vehicle_removed = False
        _LOGGER.info("Vehicle %s is reachable again", self.vin)
        # Upstream deletes its repair issue here.
        persistent_notification.async_dismiss(self._hass, vehicle_removed_issue_id(self.vin))

    async def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                result = listener(self)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 - one broken listener must not stop polling
                _LOGGER.exception("Coordinator listener failed for %s: %s", self.vin, err)

    # --- command history -----------------------------------------------------
    @property
    def command_history(self) -> dict:
        if not self._commands_history:
            return {}
        history_items = []
        for action_id in self._commands_history:
            action_name = self._commands_history[action_id]["name"]
            for update in self._commands_history[action_id]["updates"]:
                status = update["info"]
                translation_path = f"component.stellantis_vehicles.entity.sensor.command_status.state.{status}"
                status = self.get_translation(translation_path, status)
                history_items.append((update["date"], f"{action_name}: {status}"))
        history_items.sort(key=lambda x: x[0], reverse=True)
        return {
            item[0].strftime("%d/%m/%y %H:%M:%S:%f")[:-4]: item[1]
            for item in history_items
        }

    @property
    def pending_action(self) -> bool:
        """A sent command without final status blocks further commands, but
        only for PENDING_ACTION_TIMEOUT seconds after its last activity."""
        if self._pending_action_id is None:
            return False
        pending_command = self._commands_history.get(self._pending_action_id)
        if not pending_command:
            return False
        last_activity_at = pending_command["updates"][-1]["date"] if pending_command["updates"] else pending_command["sent_at"]
        return (get_datetime() - last_activity_at).total_seconds() < PENDING_ACTION_TIMEOUT

    def _prune_command_history(self) -> None:
        """Drop the oldest entries beyond COMMAND_HISTORY_LIMIT."""
        excess = len(self._commands_history) - COMMAND_HISTORY_LIMIT
        for _ in range(max(0, excess)):
            del self._commands_history[next(iter(self._commands_history))]

    async def update_command_history(self, action_id, update=None) -> None:
        if action_id not in self._commands_history:
            return
        if update:
            self._commands_history[action_id]["updates"].append({"info": update, "date": get_datetime()})
            if update == "not_compatible":
                disabled_name = self._commands_history[action_id]["name"]
                if disabled_name not in self._disabled_commands:
                    self._disabled_commands.append(disabled_name)
            if update not in COMMAND_STATUS_STILL_IN_PROGRESS and self._pending_action_id == action_id:
                self._pending_action_id = None
        await self._notify()

    def update_command_history_rate_limit(self, name) -> None:
        current_datetime = get_datetime()
        self._commands_history.update({
            current_datetime.time(): {"name": name, "updates": [{"info": "rate_limit", "date": current_datetime}]}
        })
        self._prune_command_history()
        self.async_update_listeners()

    # --- commands ------------------------------------------------------------
    async def _raise_command_pending(self, name) -> None:
        pending_name = self._commands_history[self._pending_action_id]["name"]
        placeholders = {"name": name, "pending_name": pending_name}
        translations = await translation.async_get_translations(
            self._hass, self._hass.config.language, "exceptions", {DOMAIN})
        template = translations.get(f"component.{DOMAIN}.exceptions.command_already_pending.message",
                                    'Cannot send "{name}": "{pending_name}" is still waiting for a response from the vehicle.')
        raise ServiceValidationError(
            template.format(**placeholders),
            translation_domain=DOMAIN,
            translation_key="command_already_pending",
            translation_placeholders=placeholders,
        )

    async def send_command(self, name, service, message) -> None:
        if self.pending_action:
            await self._raise_command_pending(name)
        try:
            action_id = await self._stellantis.send_mqtt_message(service, message, self._vehicle)
            if action_id is not None:
                # service/message: a 400 "invalid token" answer for this
                # action_id is retried with its own payload (stellantis.py);
                # sent_at: pending_action fallback before any update arrived.
                self._commands_history.update({action_id: {
                    "name": name, "updates": [], "service": service, "message": message,
                    "retried": False, "sent_at": get_datetime(),
                }})
                self._prune_command_history()
                self._pending_action_id = action_id
                await self._notify()
        except ConfigEntryAuthFailed as e:
            _LOGGER.warning("Authentication failed while sending command '%s' to vehicle '%s': %s", name, self.vin, e)
            if self.on_auth_failed:
                self.on_auth_failed()
        except Exception as e:
            _LOGGER.error("Failed to send command %s: %s", name, e)
            raise

    @rate_limit(6, 1200)  # 6 per 20 min
    async def send_wakeup_command(self, button_name) -> None:
        await self.send_command(button_name, "/VehCharge/state", {"action": "state"})

    async def send_doors_command(self, button_name, action) -> None:
        await self.send_command(button_name, "/Doors", {"action": action})

    async def send_horn_command(self, button_name) -> None:
        await self.send_command(button_name, "/Horn", {"nb_horn": "2", "action": "activate"})

    async def send_lights_command(self, button_name) -> None:
        await self.send_command(button_name, "/Lights", {"duration": "10", "action": "activate"})

    async def send_charge_command(self, button_name, update_only_time=False, action="immediate") -> None:
        current_hour = self._sensors.get("time_battery_charging_start")
        if current_hour is None:
            _LOGGER.warning("Charge start time unknown, cannot send charge command")
            return
        if update_only_time:
            if self._sensors.get("battery_charging") != "InProgress":
                action = "delayed"
        await self.send_command(button_name, "/VehCharge", {
            "program": {"hour": current_hour.hour, "minute": current_hour.minute},
            "type": action,
        })

    def get_programs(self) -> dict:
        """Current preconditioning programs (API key spelled "preconditionning" upstream)."""
        default_programs = {
            "program1": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
            "program2": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
            "program3": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
            "program4": {"day": [0, 0, 0, 0, 0, 0, 0], "hour": 34, "minute": 7, "on": 0},
        }
        air_conditioning = (self._data.get("preconditionning") or {}).get("airConditioning") or {}
        for program in air_conditioning.get("programs") or []:
            if not program:
                continue
            occurence = program.get("occurence")
            if occurence and occurence.get("day") and program.get("start"):
                date = time_from_pt_string(program["start"])
                if date is None:
                    continue
                default_programs["program" + str(program["slot"])] = {
                    "day": [int(day in occurence["day"]) for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")],
                    "hour": date.hour,
                    "minute": date.minute,
                    "on": int(program["enabled"]),
                }
        return default_programs

    async def send_charge_limit_command(self, button_name, action) -> None:
        """Native 80 % charge limit: ``daily`` = on, ``trip`` = off."""
        await self.send_command(button_name, "/VehCharge/limit", {"action": action})

    async def send_preconditioning_command(self, button_name, action) -> None:
        await self.send_command(button_name, "/ThermalPrecond", {"asap": action, "programs": self.get_programs()})

    async def send_abrp_data(self) -> None:
        tlm = {
            "utc": int(get_datetime().astimezone(UTC).timestamp()),
            "soc": None,
            "power": None,
            "speed": None,
            "lat": None,
            "lon": None,
            "is_charging": False,
            "is_dcfc": False,
            "is_parked": False,
        }
        sensors = self._sensors
        position = self._data.get("lastPosition") or {}
        coordinates = (position.get("geometry") or {}).get("coordinates") or []
        if sensors.get("battery") is not None:
            tlm["soc"] = sensors.get("battery")
        if sensors.get("speed") is not None:
            tlm["speed"] = sensors.get("speed")
        if len(coordinates) >= 2:
            tlm["lat"] = float(coordinates[1])
            tlm["lon"] = float(coordinates[0])
        if sensors.get("battery_charging") is not None:
            tlm["is_charging"] = sensors.get("battery_charging") == "InProgress"
        if sensors.get("battery_charging_type") is not None:
            tlm["is_dcfc"] = tlm["is_charging"] and sensors.get("battery_charging_type") == "Quick"
        if sensors.get("engine") is not None:
            tlm["is_parked"] = sensors.get("engine") == "Stop"
        if sensors.get("battery_health_resistance") is not None:
            tlm["soh"] = float(sensors.get("battery_health_resistance"))
        if sensors.get("battery_health_capacity") is not None:
            tlm["soh"] = float(sensors.get("battery_health_capacity"))
        if (position.get("properties") or {}).get("heading") is not None:
            tlm["heading"] = float(position["properties"]["heading"])
        if len(coordinates) == 3:
            tlm["elevation"] = float(coordinates[2])
        if sensors.get("temperature") is not None:
            tlm["ext_temp"] = sensors.get("temperature")
        if sensors.get("mileage") is not None:
            tlm["odometer"] = sensors.get("mileage")
        if sensors.get("autonomy") is not None:
            tlm["est_battery_range"] = sensors.get("autonomy")

        params = {"tlm": json.dumps(tlm), "token": sensors.get("text_abrp_token")}
        await self._stellantis.send_abrp_data(params)

    async def after_async_update_data(self) -> None:
        """Apply changes and do actions after vehicle data update.

        Reads ``_sensors`` as filled by the bridge on the *previous* update,
        exactly like upstream (entities update after the coordinator).
        """
        if self.vehicle_type in [VEHICLE_TYPE_ELECTRIC, VEHICLE_TYPE_HYBRID]:
            await self._auto_stop_charge_at_limit()
            await self._sync_abrp_if_enabled()
        await self._fetch_last_trip_on_engine_stop()

    async def _auto_stop_charge_at_limit(self) -> None:
        """Send a delayed charge-stop command once the configured limit is reached."""
        sensors = self._sensors
        if "battery_charging" not in sensors:
            return
        if sensors.get("battery_charging_limit") == "Partial":
            # The vehicle's native 80 % limit is active, it stops on its own
            return
        if sensors.get("battery_charging") != "InProgress":
            self._manage_charge_limit_sent = False
            return
        if self._manage_charge_limit_sent:
            return
        charge_limit_on = sensors.get("switch_battery_charging_limit", False)
        charge_limit = sensors.get("number_battery_charging_limit")
        current_battery = sensors.get("battery")
        if not (charge_limit_on and charge_limit and current_battery is not None):
            return
        if int(float(current_battery)) < int(charge_limit):
            return
        button_name = self.get_translation("component.stellantis_vehicles.entity.button.charge_stop.name", "charge_stop")
        try:
            await self.send_charge_command(button_name, False, "delayed")
        except ServiceValidationError as err:
            # Another command is still pending; must not fail the poll (upstream
            # would), the next cycle tries again.
            _LOGGER.debug("Charge limit stop postponed: %s", err)
            return
        self._manage_charge_limit_sent = True

    async def _sync_abrp_if_enabled(self) -> None:
        """Push the current status to ABRP if sync is enabled with a valid token."""
        if not self._sensors.get("switch_abrp_sync"):
            return
        token = self._sensors.get("text_abrp_token")
        if not token or len(token) != 36:
            return
        await self.send_abrp_data()

    async def _fetch_last_trip_on_engine_stop(self) -> None:
        """Fetch the last trip once the engine transitions from running to Stop."""
        current_engine_status = self._sensors.get("engine")
        new_engine_status = self._data.get("ignition", {}).get("type")
        if new_engine_status != "Stop":
            return
        if current_engine_status in (None, "Stop"):
            _LOGGER.debug("No last-trip fetch needed (engine %s -> Stop)", current_engine_status)
            return
        _LOGGER.debug("Engine status changed from %s to %s, fetching last trip data", current_engine_status, new_engine_status)
        await self.get_vehicle_last_trip()

    async def get_vehicle_last_trip(self) -> None:
        try:
            trips = await self._stellantis.get_vehicle_last_trip(self._vehicle)
            embedded = (trips or {}).get("_embedded", {}).get("trips")
            if embedded:
                if not self._last_trip or self._last_trip["id"] != embedded[-1]["id"]:
                    self._last_trip = embedded[-1]
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Failed to fetch last trip data: %s", e)
