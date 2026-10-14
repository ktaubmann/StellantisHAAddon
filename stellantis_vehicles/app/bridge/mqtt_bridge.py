"""MQTT discovery bridge.

Publishes every vehicle of the account as one HA device with its entities via
MQTT discovery and keeps their state topics up to date from the coordinator.
Command topics are subscribed and forwarded to the entity handlers.

Topic layout (``<prefix>`` defaults to ``stellantis``):

  <prefix>/status                       bridge availability, LWT (online/offline)
  <prefix>/<vin>/available              vehicle availability (last poll ok?)
  <prefix>/<vin>/<component>/<key>/state       entity state (retained)
  <prefix>/<vin>/<component>/<key>/attributes  entity attributes as JSON (retained)
  <prefix>/<vin>/<component>/<key>/available   entity availability, only for
                                               entities with own rules (buttons, ...)
  <prefix>/<vin>/<component>/<key>/set         command topic (button/number/switch/text)

Discovery goes to ``homeassistant/<component>/stellantis_<vin>/<key>/config``.

paho callbacks run on the network thread; anything touching the coordinators
is handed over to the asyncio loop.
"""
import asyncio
import json
import logging
from datetime import date, datetime, time
from typing import Callable

import paho.mqtt.client as mqtt

from stellantis_vehicles.const import FIELD_MOBILE_APP

from homeassistant.exceptions import ServiceValidationError

from .entities import Entity, build_entities

_LOGGER = logging.getLogger(__name__)

SUPPORT_URL = "https://github.com/ktaubmann/StellantisHAAddon"


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class VehicleBinding:
    """Everything the bridge tracks for one vehicle."""

    def __init__(self, coordinator, entities: list[Entity]) -> None:
        self.coordinator = coordinator
        self.entities = entities
        self.by_command_topic: dict[str, Entity] = {}
        # last published (state, attributes json, available) per entity key
        self.published: dict[str, tuple] = {}
        self.vehicle_available: bool | None = None
        self.discovery_topics: set[str] = set()
        self.remote_commands: bool | None = None

    @property
    def vin(self) -> str:
        return self.coordinator.vin


class MqttBridge:
    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 loop: asyncio.AbstractEventLoop | None = None,
                 discovery_prefix: str = "homeassistant", topic_prefix: str = "stellantis",
                 addon_version: str = "0.0.0", client_id: str = "stellantis-addon") -> None:
        self._host, self._port = host, port
        self._loop = loop or asyncio.get_event_loop()
        self._discovery_prefix = discovery_prefix
        self._prefix = topic_prefix
        self._addon_version = addon_version
        self._bindings: dict[str, VehicleBinding] = {}
        # Commands are serialised so a bulk action cannot flood the Stellantis cloud
        self._command_lock = asyncio.Lock()
        self._connected = False
        self.on_connection_change: Callable[[bool], None] | None = None

        self._client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311, clean_session=True)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # --- topics --------------------------------------------------------------
    @property
    def status_topic(self) -> str:
        return f"{self._prefix}/status"

    def vehicle_available_topic(self, vin: str) -> str:
        return f"{self._prefix}/{vin}/available"

    def entity_topic(self, vin: str, entity: Entity, leaf: str) -> str:
        return f"{self._prefix}/{vin}/{entity.topic_key}/{leaf}"

    def discovery_topic(self, vin: str, entity: Entity) -> str:
        return f"{self._discovery_prefix}/{entity.component}/stellantis_{vin}/{entity.key}/config"

    # --- connection ----------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        _LOGGER.info("Connecting to MQTT broker %s:%s", self._host, self._port)
        # connect_async + loop_start: paho retries in the background until the
        # broker is reachable and reconnects on its own after drops.
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        # A clean DISCONNECT suppresses the last will, so the offline status
        # must be delivered before the network thread stops.
        if self._connected:
            try:
                info = self._client.publish(self.status_topic, "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=5)
            except (ValueError, RuntimeError) as err:
                _LOGGER.debug("Could not publish offline status: %s", err)
        self._client.disconnect()
        self._client.loop_stop()
        self._connected = False

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        if rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.error("MQTT connection refused: %s", mqtt.connack_string(rc))
            return
        _LOGGER.info("Connected to MQTT broker")
        self._connected = True
        client.publish(self.status_topic, "online", qos=1, retain=True)
        # Subscriptions and retained topics are re-established on every
        # (re)connect from the loop, where the coordinator state lives.
        self._loop.call_soon_threadsafe(self._loop.create_task, self._republish_all())
        if self.on_connection_change:
            self._loop.call_soon_threadsafe(self.on_connection_change, True)

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        self._connected = False
        if rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning("MQTT connection lost (%s), reconnecting", mqtt.error_string(rc))
        if self.on_connection_change:
            self._loop.call_soon_threadsafe(self.on_connection_change, False)

    def _on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        asyncio.run_coroutine_threadsafe(self._dispatch_command(msg.topic, payload), self._loop)

    def _publish(self, topic: str, payload: str | None, retain: bool = True) -> None:
        # payload None = clear a retained topic
        self._client.publish(topic, payload if payload is not None else "", qos=0, retain=retain)

    # --- vehicles ------------------------------------------------------------
    async def attach(self, coordinator) -> VehicleBinding:
        """Register a vehicle: build entities, publish discovery, follow updates."""
        vin = coordinator.vin
        remote_commands = coordinator.stellantis.remote_commands
        existing = self._bindings.get(vin)
        if existing is not None:
            if existing.remote_commands == remote_commands:
                return existing
            # Remote commands were switched on/off: rebuild the entity set
            # (upstream reloads the config entry for this).
            _LOGGER.info("Remote commands for %s now %s, rebuilding entities", vin, remote_commands)
            await self.detach(vin, remove=True)
        binding = VehicleBinding(coordinator, build_entities(coordinator, remote_commands))
        binding.remote_commands = remote_commands
        for entity in binding.entities:
            if entity.writable:
                binding.by_command_topic[self.entity_topic(vin, entity, "set")] = entity
        self._bindings[vin] = binding
        _LOGGER.info("Vehicle %s (%s): %d entities", vin, coordinator.vehicle_type, len(binding.entities))

        coordinator.add_listener(self.on_coordinator_update)
        if self._connected:
            self._publish_discovery(binding)
            self._subscribe(binding)
            self._publish_states(binding, force=True)
        return binding

    async def detach(self, vin: str, remove: bool = False) -> None:
        """Stop following a vehicle; ``remove`` also deletes it from HA."""
        binding = self._bindings.pop(vin, None)
        if not binding:
            return
        binding.coordinator.remove_listener(self.on_coordinator_update)
        for topic in binding.by_command_topic:
            self._client.unsubscribe(topic)
        if remove:
            for topic in binding.discovery_topics:
                self._publish(topic, None)
            for entity in binding.entities:
                for leaf in ("state", "attributes", "available"):
                    self._publish(self.entity_topic(vin, entity,leaf), None)
            self._publish(self.vehicle_available_topic(vin), None)

    async def _republish_all(self) -> None:
        for binding in list(self._bindings.values()):
            self._publish_discovery(binding)
            self._subscribe(binding)
            self._publish_states(binding, force=True)

    def _subscribe(self, binding: VehicleBinding) -> None:
        for topic in binding.by_command_topic:
            self._client.subscribe(topic, qos=0)

    # --- discovery -----------------------------------------------------------
    def _device_payload(self, coordinator) -> dict:
        vin = coordinator.vin
        vehicle_type = coordinator.vehicle_type
        type_label = coordinator.get_translation(
            f"component.stellantis_vehicles.entity.sensor.type.state.{vehicle_type.lower()}", vehicle_type)
        return {
            "identifiers": [f"stellantis_{vin}"],
            "name": vin,
            # API brand field (upstream 62b40fd), the app for older data
            "manufacturer": coordinator.vehicle.get("brand") or coordinator.config.get(FIELD_MOBILE_APP, "Stellantis"),
            "model": f"{type_label} - {vin}",
            "serial_number": vin,
        }

    def _discovery_payload(self, binding: VehicleBinding, entity: Entity) -> dict:
        vin = binding.vin
        availability = [
            {"topic": self.status_topic},
            {"topic": self.vehicle_available_topic(vin)},
        ]
        if entity.available is not None:
            availability.append({"topic": self.entity_topic(vin, entity, "available")})
        payload = {
            "name": entity.name,
            "unique_id": f"stellantis_{entity.unique_id}",
            "default_entity_id": f"{entity.component}.stellantis_{vin.lower()}_{entity.key}",
            "device": self._device_payload(binding.coordinator),
            "origin": {
                "name": "Stellantis Vehicles Add-on",
                "sw_version": self._addon_version,
                "support_url": SUPPORT_URL,
            },
            "availability": availability,
            "availability_mode": "all",
            "json_attributes_topic": self.entity_topic(vin, entity, "attributes"),
        }
        if entity.icon:
            payload["icon"] = entity.icon
        if entity.has_state:
            payload["state_topic"] = self.entity_topic(vin, entity, "state")
        if entity.writable:
            payload["command_topic"] = self.entity_topic(vin, entity, "set")
        payload.update(entity.discovery)
        return payload

    def _publish_discovery(self, binding: VehicleBinding) -> None:
        current_topics = set()
        for entity in binding.entities:
            topic = self.discovery_topic(binding.vin, entity)
            current_topics.add(topic)
            self._publish(topic, json.dumps(self._discovery_payload(binding, entity), default=_json_default))
        # Entities that disappeared (e.g. remote commands disabled meanwhile)
        for stale in binding.discovery_topics - current_topics:
            self._publish(stale, None)
        binding.discovery_topics = current_topics

    # --- state ---------------------------------------------------------------
    def on_coordinator_update(self, coordinator) -> None:
        binding = self._bindings.get(coordinator.vin)
        if not binding:
            return
        if not coordinator.data:
            # No successful poll yet (upstream creates its entities only after
            # the first refresh). Listener calls still arrive here: a failed
            # first poll, or the MQTT connect the first poll itself starts.
            # Updating on empty data would e.g. end a stored running charge.
            if self._connected:
                self._publish_states(binding)
            return
        for entity in binding.entities:
            try:
                entity.update()
            except Exception as err:  # noqa: BLE001 - one bad entity must not block the rest
                _LOGGER.exception("Updating %s/%s failed: %s", coordinator.vin, entity.key, err)
        if self._connected:
            self._publish_states(binding)

    def _publish_states(self, binding: VehicleBinding, force: bool = False) -> None:
        vin = binding.vin
        vehicle_available = binding.coordinator.last_update_success
        if force or vehicle_available != binding.vehicle_available:
            self._publish(self.vehicle_available_topic(vin), "online" if vehicle_available else "offline")
            binding.vehicle_available = vehicle_available

        for entity in binding.entities:
            state = entity.state
            attributes = json.dumps(entity.attributes, default=_json_default) if entity.attributes else None
            available = entity.available
            snapshot = (state, attributes, available)
            if not force and binding.published.get(entity.topic_key) == snapshot:
                continue
            previous = binding.published.get(entity.topic_key)
            if entity.has_state and (force or previous is None or previous[0] != state):
                self._publish(self.entity_topic(vin, entity, "state"), state)
            if attributes is not None and (force or previous is None or previous[1] != attributes):
                self._publish(self.entity_topic(vin, entity, "attributes"), attributes)
            if available is not None and (force or previous is None or previous[2] != available):
                self._publish(self.entity_topic(vin, entity, "available"), "online" if available else "offline")
            binding.published[entity.topic_key] = snapshot

    # --- commands ------------------------------------------------------------
    async def _dispatch_command(self, topic: str, payload: str) -> None:
        for binding in self._bindings.values():
            entity = binding.by_command_topic.get(topic)
            if entity is None:
                continue
            _LOGGER.debug("Command %s -> %s/%s", payload, binding.vin, entity.key)
            async with self._command_lock:
                try:
                    await entity.handle_command(payload)
                except ServiceValidationError as err:
                    # e.g. another command is still pending (upstream: toast in HA)
                    _LOGGER.warning("Command %r for %s/%s rejected: %s", payload, binding.vin, entity.key, err)
                except Exception as err:  # noqa: BLE001 - upstream raises into HA, we only log
                    _LOGGER.error("Command %r for %s/%s failed: %s", payload, binding.vin, entity.key, err)
            # command_status / command_pending changed
            self.on_coordinator_update(binding.coordinator)
            return
        _LOGGER.debug("Ignoring message on unknown topic %s", topic)
