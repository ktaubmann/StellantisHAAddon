"""Vehicle runtime: the part of upstream async_setup_entry that runs after the
config flow - token refresh, vehicle list, one coordinator per vehicle, all
of them attached to the MQTT bridge.

Started once the setup flow reports "done" (at boot when the stored config is
complete, or later from the ingress UI). Survives re-logins: a coordinator
whose polling stopped on an auth failure is simply started again.
"""
import asyncio
import logging

from homeassistant.components import persistent_notification
from homeassistant.exceptions import ConfigEntryAuthFailed

from stellantis_vehicles.const import UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

RETRY_SECONDS = 60
# Delays before each automatic login attempt after the login expired; the
# last one repeats. The browser login takes minutes and each attempt is a
# login at the provider, so this stays slow.
RELOGIN_DELAYS = (0, 15 * 60, 60 * 60, 6 * 3600, 24 * 3600)
NOTIFY_RELOGIN = "Anmeldung abgelaufen – das Add-on meldet sich automatisch neu an."
NOTIFY_MANUAL = "Anmeldung abgelaufen – bitte auf der Add-on-Seite neu anmelden."
NOTIFY_REJECTED = ("Automatische Anmeldung von Stellantis abgelehnt – bitte Zugangsdaten prüfen "
                   "und auf der Add-on-Seite neu anmelden.")


class Runtime:
    def __init__(self, hass, stellantis, bridge, flow, state: dict) -> None:
        self._hass = hass
        self._stellantis = stellantis
        self._bridge = bridge
        self._flow = flow
        self._state = state
        self._coordinators: dict = {}
        self._lock = asyncio.Lock()
        self._retry: asyncio.TimerHandle | None = None
        # Set on the first auth failure, cleared once tokens and vehicle list
        # work again (after a new login). Upstream keeps retrying a rejected
        # refresh token every 30 minutes and requests reauth each time; HA
        # dedupes that into one open flow, here it would mean a new error and
        # notification every half hour until the user logs in again.
        self._login_required = False
        self._relogin_attempt = 0
        self._relogin_handle: asyncio.TimerHandle | None = None
        self._relogin_task: asyncio.Task | None = None
        # Upstream starts HA's reauth flow from the token refresh timers
        # (entry.async_start_reauth); here that means "login required".
        hass.config_entries.entry.on_reauth = lambda: self._auth_failed("reauthentication requested")

    @property
    def coordinators(self) -> dict:
        return self._coordinators

    # --- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Idempotent: (re)starts whatever is not running yet."""
        async with self._lock:
            if self._retry:
                self._retry.cancel()
                self._retry = None
            try:
                await self._stellantis.scheduled_tokens_refresh()
                vehicles = await self._stellantis.get_user_vehicles()
            except ConfigEntryAuthFailed as err:
                self._auth_failed(str(err))
                return
            except Exception as err:  # noqa: BLE001 - upstream: ConfigEntryNotReady -> retry
                _LOGGER.warning("Could not fetch the vehicle list, retrying in %ss: %s", RETRY_SECONDS, err)
                self._state["vehicles_error"] = str(err)
                self._retry = self._hass.loop.call_later(
                    RETRY_SECONDS, lambda: self._hass.loop.create_task(self.start()))
                return
            self._state.pop("vehicles_error", None)
            if self._login_required:
                self._login_required = False
                self._cancel_relogin_timer()
                persistent_notification.async_dismiss(self._hass, "reauth")

            if not vehicles:
                _LOGGER.warning("No vehicles found for this account")
                await self._stellantis.hass_notify("no_vehicles_found")
                self._state["vehicles"] = []
                return

            self._stellantis.prune_stored_vehicle_configs({vehicle["vin"] for vehicle in vehicles})
            for index, vehicle in enumerate(vehicles):
                coordinator = await self._stellantis.async_get_coordinator(vehicle)
                if vehicle["vin"] not in self._coordinators:
                    self._coordinators[vehicle["vin"]] = coordinator
                    coordinator.on_auth_failed = lambda: self._auth_failed("token rejected while polling")
                    coordinator.add_listener(self._update_state)
                    if index and len(vehicles) > 1:
                        # Spread the polls of several vehicles across the interval
                        coordinator.stagger_first_poll(index * UPDATE_INTERVAL / len(vehicles))
                if self._bridge:
                    # Idempotent; rebuilds the entities when remote commands were toggled
                    await self._bridge.attach(coordinator)
                if not coordinator.running:
                    coordinator.start()
            self._update_state(None)
            _LOGGER.info("Polling %d vehicle(s)", len(self._coordinators))

    async def stop(self) -> None:
        if self._retry:
            self._retry.cancel()
            self._retry = None
        self._cancel_relogin_timer()
        if self._relogin_task and not self._relogin_task.done():
            self._relogin_task.cancel()
        for coordinator in self._coordinators.values():
            await coordinator.stop()

    # --- callbacks -----------------------------------------------------------
    def _auth_failed(self, reason: str) -> None:
        if self._login_required:
            _LOGGER.debug("Still waiting for a new login (%s)", reason)
            return
        self._login_required = True
        self._relogin_attempt = 0
        auto = self._flow.can_relogin
        _LOGGER.error("Authentication failed (%s), %s", reason,
                      "logging in again with the credentials from the add-on options" if auto
                      else "login required via the add-on page")
        self._state["auth"] = f"login required: {reason}"
        self._notify_login(NOTIFY_RELOGIN if auto else NOTIFY_MANUAL)
        self._flow.reauth()
        if auto:
            self._schedule_relogin()

    def _notify_login(self, message: str) -> None:
        persistent_notification.async_create(
            self._hass, message, title="Stellantis Vehicles", notification_id="reauth")

    # --- automatic login -------------------------------------------------------
    def _schedule_relogin(self) -> None:
        delay = RELOGIN_DELAYS[min(self._relogin_attempt, len(RELOGIN_DELAYS) - 1)]
        self._relogin_attempt += 1
        if delay:
            _LOGGER.info("Next automatic login attempt in %d minutes", delay // 60)
        self._relogin_handle = self._hass.loop.call_later(delay, self._start_relogin)

    def _cancel_relogin_timer(self) -> None:
        if self._relogin_handle:
            self._relogin_handle.cancel()
            self._relogin_handle = None

    def _start_relogin(self) -> None:
        self._relogin_handle = None
        self._relogin_task = self._hass.loop.create_task(self._relogin())

    async def _relogin(self) -> None:
        if not self._login_required:
            return
        _LOGGER.info("Automatic login attempt %d", self._relogin_attempt)
        result = await self._flow.relogin()
        # On success the flow has already restarted the runtime (on_complete),
        # which clears _login_required once tokens and vehicle list work.
        if not self._login_required:
            _LOGGER.info("Automatic login successful, polling again")
            return
        if result == "rejected":
            _LOGGER.error("Stellantis rejected the automatic login, login via the add-on page required")
            self._notify_login(NOTIFY_REJECTED)
            return
        if result == "ok":
            # New tokens, but the restart did not get the vehicle list yet
            # (API error: start() retries on its own and clears this then).
            _LOGGER.warning("Automatic login done, vehicles not reachable yet")
        else:
            _LOGGER.warning("Automatic login did not succeed (%s)", self._flow.error or result)
        self._schedule_relogin()

    def _update_state(self, _coordinator) -> None:
        self._state["vehicles"] = [
            {
                "vin": coordinator.vin,
                "type": coordinator.vehicle_type,
                "ok": coordinator.last_update_success,
                "updated_at": coordinator.data.get("updatedAt"),
            }
            for coordinator in self._coordinators.values()
        ]
        # Only once every vehicle polls again; a single failed one must keep the hint
        if self._coordinators and all(c.running for c in self._coordinators.values()):
            self._state.pop("auth", None)
