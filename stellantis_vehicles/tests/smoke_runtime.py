"""Offline smoke test for runtime.py (vehicle wiring) together with the bridge
and the setup flow: start -> coordinators poll -> bridge publishes; auth
failure -> flow back to login -> re-login restarts polling; retry on API
errors; no vehicles.

    .venv/Scripts/python tests/smoke_runtime.py
"""
import asyncio
import json
import os
import sys
import tempfile

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.join(APP_DIR, "hass_shim"))
sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402

import smoke_bridge as sb  # noqa: E402  (fixture + fakes)
from bridge.mqtt_bridge import MqttBridge  # noqa: E402
from runtime import Runtime  # noqa: E402
from web.setup import STEP_DONE, STEP_LOGIN, SetupFlow  # noqa: E402

VIN2 = "VR3TESTVIN0000002"


class RuntimeFakeStellantis(sb.FakeStellantis):
    def __init__(self, hass):
        super().__init__(hass)
        self.vehicles_error = None
        self.status_error = None
        self.refreshes = 0

    async def scheduled_tokens_refresh(self):
        self.refreshes += 1

    async def get_user_vehicles(self, force=False):
        if self.vehicles_error:
            raise self.vehicles_error
        return self._vehicles

    async def get_vehicle_status(self, vehicle):
        if self.status_error:
            raise self.status_error
        return self.status

    async def get_access_token(self):
        return {"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600}


def check(condition, message):
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        check.failures += 1


check.failures = 0


async def main():
    data_dir = tempfile.mkdtemp(prefix="stellantis_rt_")
    loop = asyncio.get_running_loop()
    hass = HomeAssistant(config_dir=data_dir, language="de", loop=loop)
    stellantis = RuntimeFakeStellantis(hass)
    stellantis.set_entry(hass.config_entries.entry)
    # Fully configured stored config, as left behind by the setup flow
    for key, value in {"mobile_app": "MyPeugeot", "country_code": "DE", "remote_commands": False,
                       "customer_id": "MN-1", "notifications": True, "anonymize_logs": True,
                       "oauth": {"access_token": "at", "refresh_token": "rt", "expires_in": "2099-01-01T00:00:00+00:00"}}.items():
        stellantis.update_stored_config(key, value)
    stellantis.save_config(hass.config_entries.entry.data)
    stellantis._vehicles = [{"vin": sb.VIN, "vehicle_id": "1", "type": "Electric"},
                            {"vin": VIN2, "vehicle_id": "2", "type": "Thermic"}]

    options = {"mobile_app": "MyPeugeot", "country_code": "DE", "remote_commands": False, "oauth_mode": "manual"}
    state = {"vehicles": [], "mqtt": "x"}
    bridge = MqttBridge("h", 1883, None, None, loop=loop, addon_version="t")
    recorder = sb.RecorderClient()
    bridge._client = recorder
    bridge._connected = True

    runtime = None

    async def on_configured():
        await runtime.start()

    flow = SetupFlow(hass, stellantis, options, on_configured)
    runtime = Runtime(hass, stellantis, bridge, flow, state)
    check(flow.step == STEP_DONE, "stored config complete -> done")

    print("start")
    await runtime.start()
    check(len(runtime.coordinators) == 2, "two coordinators")
    check(stellantis.refreshes == 1, "tokens refreshed once")
    check(all(c.running for c in runtime.coordinators.values()), "both polling")
    await asyncio.sleep(0.3)  # first poll of vehicle 1 (vehicle 2 is staggered)
    check(recorder.published.get(f"stellantis/{sb.VIN}/sensor/battery/state") == "67", "vehicle 1 published via bridge")
    check(runtime.coordinators[VIN2]._phase_offset == 30, "second vehicle staggered by half the interval")
    check(state["vehicles"][0]["ok"] is True and state["vehicles"][0]["vin"] == sb.VIN, f"state vehicles: {state['vehicles']}")
    thermic_configs = [t for t in recorder.published if f"stellantis_{VIN2}" in t and t.endswith("/config")]
    check(thermic_configs and not any("/battery/" in t for t in thermic_configs), "thermic vehicle without battery entities")
    check(not any("/button/" in t for t in recorder.published), "no buttons without remote commands")

    print("idempotent restart")
    await runtime.start()
    check(len(runtime.coordinators) == 2 and stellantis.refreshes == 2, "start() again: no duplicates")

    print("auth failure while polling")
    stellantis.status_error = ConfigEntryAuthFailed("expired")
    c1 = runtime.coordinators[sb.VIN]
    await asyncio.sleep(0)  # let the loop pick it up
    try:
        await c1.async_refresh()
    except ConfigEntryAuthFailed:
        pass
    c1.on_auth_failed()
    check(flow.step == STEP_LOGIN and "login required" in state.get("auth", ""), "flow back to login, state shows it")
    check(any(n.get("id") == "reauth" for n in hass.notifications), "reauth notification")
    check(runtime._relogin_handle is None, "manual mode: no automatic login")
    check(recorder.published[f"stellantis/{sb.VIN}/available"] == "offline", "vehicle offline after auth failure")

    print("re-login restarts polling")
    stellantis.status_error = None
    await c1.stop()  # simulate the loop having ended
    check(not c1.running, "coordinator stopped")
    flow.start_login(code="CODE123")
    await flow.wait()
    check(flow.step == STEP_DONE, f"login done (error={flow.error})")
    await asyncio.sleep(0.3)
    check(c1.running and "auth" not in state, "polling again, auth state cleared")
    check(stellantis.get_config("oauth")["access_token"] == "at2", "runtime config carries the new token")
    check(recorder.published[f"stellantis/{sb.VIN}/available"] == "online", "vehicle online again")
    with open(os.path.join(data_dir, "config_entry.json"), encoding="utf-8") as f:
        check(json.load(f)["oauth"]["access_token"] == "at2", "async_update_entry persists the new token")

    print("reauth requested by the upstream token timers")
    hass.config_entries.entry.async_start_reauth(hass)
    hass.config_entries.entry.async_start_reauth(hass)
    check(flow.step == STEP_LOGIN and "reauthentication requested" in state.get("auth", ""),
          "entry.async_start_reauth -> login required")
    check(sum(1 for n in hass.notifications if n.get("id") == "reauth") == 1, "same notification id replaces, no duplicate")
    hass.notifications.clear()  # user dismissed it
    hass.config_entries.entry.async_start_reauth(hass)  # upstream's 30-minute retry
    c1.on_auth_failed()
    check(not hass.notifications, "repeated auth failures while waiting for the login: no new notification")
    await runtime.stop()

    print("automatic login with the credentials from the options")
    import oauth_browser.login as browser_login
    from runtime import NOTIFY_REJECTED, NOTIFY_RELOGIN
    calls = []
    outcome = {"raise": None}

    async def fake_fetch(url, email, password, **kwargs):
        calls.append((email, password))
        if outcome["raise"]:
            raise outcome["raise"]
        return "AUTOCODE"

    browser_login.fetch_oauth_code = fake_fetch
    auto_options = dict(options, oauth_mode="browser", email="me@example.com", password="pw")
    auto_state = {"vehicles": [], "mqtt": "x"}
    auto_runtime = None

    async def auto_configured():
        await auto_runtime.start()

    auto_flow = SetupFlow(hass, stellantis, auto_options, auto_configured)
    auto_runtime = Runtime(hass, stellantis, None, auto_flow, auto_state)
    await auto_runtime.start()
    check(auto_flow.can_relogin and not flow.can_relogin, "automatic login only with browser mode and credentials")

    async def settle():
        for _ in range(50):
            await asyncio.sleep(0)
            task = auto_runtime._relogin_task
            if auto_runtime._relogin_handle is None and (task is None or task.done()):
                return
            if task is not None and not task.done():
                await task

    hass.notifications.clear()
    stellantis.oauth_calls_before = len(calls)
    hass.config_entries.entry.async_start_reauth(hass)
    check(any(n["id"] == "reauth" and n["message"] == NOTIFY_RELOGIN for n in hass.notifications),
          "notification says the add-on logs in again by itself")
    await settle()
    check(calls == [("me@example.com", "pw")], f"browser login with the option credentials: {calls}")
    check(not auto_runtime._login_required and "auth" not in auto_state and auto_flow.step == STEP_DONE,
          "login restored without the user")
    check(not any(n["id"] == "reauth" for n in hass.notifications), "login notification dismissed after success")
    check(all(c.running for c in auto_runtime.coordinators.values()), "polling again after the automatic login")

    print("automatic login: transient failure retries later")
    from oauth_browser.login import OauthBrowserError, OauthLoginRejected
    outcome["raise"] = OauthBrowserError("Login deadline reached", public=True)
    calls.clear()
    previous_task = auto_runtime._relogin_task
    hass.config_entries.entry.async_start_reauth(hass)
    for _ in range(20):
        await asyncio.sleep(0)
        if auto_runtime._relogin_task is not previous_task:
            break
    await auto_runtime._relogin_task
    check(len(calls) == 1 and auto_runtime._login_required and auto_runtime._relogin_handle is not None,
          "failed attempt schedules the next one")
    check(auto_runtime._relogin_attempt == 2, "backoff advances")
    hass.config_entries.entry.async_start_reauth(hass)
    check(len(calls) == 1, "repeated reauth requests do not start extra logins")

    print("automatic login: rejection stops")
    outcome["raise"] = OauthLoginRejected("Login endpoint returned error 403042; manual sign-in may be required",
                                          public=True)
    auto_runtime._relogin_handle.cancel()
    auto_runtime._start_relogin()
    await auto_runtime._relogin_task
    check(len(calls) == 2 and auto_runtime._relogin_handle is None, "no further attempts after a rejection")
    check(any(n["id"] == "reauth" and n["message"] == NOTIFY_REJECTED for n in hass.notifications),
          "notification asks for a manual login")
    check(auto_flow.step == STEP_LOGIN and "403042" in (auto_flow.error or ""), "login page shows the rejection")
    await auto_runtime.stop()

    print("retry on API error")
    state2 = {"vehicles": []}
    st2 = RuntimeFakeStellantis(hass)
    st2.set_entry(hass.config_entries.entry)
    st2.save_config(hass.config_entries.entry.data)
    st2.vehicles_error = RuntimeError("api down")
    rt2 = Runtime(hass, st2, None, flow, state2)
    await rt2.start()
    check(not rt2.coordinators and "api down" in state2.get("vehicles_error", "") and rt2._retry is not None, "retry scheduled")
    await rt2.stop()
    check(rt2._retry is None, "retry cancelled on stop")

    print("no vehicles")
    st2.vehicles_error = None
    st2._vehicles = []
    await rt2.start()
    check(state2["vehicles"] == [] and any("Fahrzeug" in str(n.get("title")) or "vehicle" in str(n.get("message")).lower() or n for n in hass.notifications), "no-vehicles notification")

    print(f"\n{check.failures} failures")
    return check.failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
