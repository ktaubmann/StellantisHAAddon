"""Offline smoke test for the ingress setup flow (web/setup.py + web/server.py).

Stubs every Stellantis call; drives the aiohttp app with a test client:
manual login -> OTP (SMS + PIN) -> status page, plus error paths.

    .venv/Scripts/python tests/smoke_web.py
"""
import asyncio
import os
import sys
import tempfile

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, os.path.join(APP_DIR, "hass_shim"))
sys.path.insert(0, APP_DIR)

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryAuthFailed  # noqa: E402

from stellantis_vehicles.stellantis import StellantisVehicles  # noqa: E402
from web import server as web_server  # noqa: E402
from web.setup import STEP_DONE, STEP_LOGIN, STEP_OTP, SetupFlow, extract_oauth_code  # noqa: E402


class FakeStellantis(StellantisVehicles):
    def __init__(self, hass):
        super().__init__(hass)
        self.calls = []
        self.fail_token = False
        self.fail_otp = None

    async def get_access_token(self):
        self.calls.append("get_access_token")
        if self.fail_token:
            raise RuntimeError("boom")
        assert self.get_config("oauth_code") == "CODE123"
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    async def get_user_info(self):
        self.calls.append("get_user_info")
        return [{"customer": "CUST-1"}]

    async def get_otp_sms(self):
        self.calls.append("get_otp_sms")
        return {}

    def new_otp(self, sms_code, pin_code):
        self.calls.append(f"new_otp:{sms_code}:{pin_code}")
        if self.fail_otp:
            raise self.fail_otp

    async def get_mqtt_access_token(self):
        self.calls.append("get_mqtt_access_token")
        return {"access_token": "mat", "refresh_token": "mrt", "expires_in": 600}


def check(condition, message):
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        check.failures += 1


check.failures = 0


async def main():
    data_dir = tempfile.mkdtemp(prefix="stellantis_web_")
    loop = asyncio.get_running_loop()
    hass = HomeAssistant(config_dir=data_dir, language="de", loop=loop)
    stellantis = FakeStellantis(hass)
    stellantis.set_entry(hass.config_entries.entry)
    options = {"mobile_app": "MyPeugeot", "country_code": "de", "email": "me@example.org",
               "password": "secret", "oauth_mode": "manual", "remote_commands": True}
    completed = []

    async def on_complete():
        completed.append(True)

    flow = SetupFlow(hass, stellantis, options, on_complete)
    state = {"vehicles": [], "mqtt": "not connected"}
    client = TestClient(TestServer(web_server.build_app(state, flow, hass)))
    await client.start_server()

    print("helpers")
    check(extract_oauth_code("mymap://oauth2redirect/de-DE?code=ABC&scope=openid") == "ABC", "code from redirect URL")
    check(extract_oauth_code("  XYZ  ") == "XYZ", "bare code")
    check(extract_oauth_code("https://x/?a=1") is None and extract_oauth_code("") is None, "no code")

    print("login page")
    check(flow.step == STEP_LOGIN, "starts at login")
    page = await (await client.get("/")).text()
    check("Anmeldung beim Stellantis-Konto" in page and "name='code'" in page, "manual login form rendered")
    check("MyPeugeot · DE" in page, "app/country from options, upper-cased")
    check("oauth2/authorize" in page and "client_id=" in page, "oauth url rendered")

    print("bad code")
    resp = await client.post("/login", data={"code": "https://nothing/here"}, allow_redirects=False)
    check(resp.status == 303 and resp.headers["Location"] == "./", "redirects back")
    page = await (await client.get("/")).text()
    check("Kein OAuth-Code erkannt" in page, "error shown")

    print("token failure")
    stellantis.fail_token = True
    await client.post("/login", data={"code": "mym://oauth2redirect/de?code=CODE123"}, allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_LOGIN and "Zugriffstokens" in (flow.error or ""), f"token error surfaced: {flow.error}")
    check(any(n["title"] and "Stellantis" in n["title"] for n in hass.notifications), "notification created")
    stellantis.fail_token = False

    print("manual login")
    await client.post("/login", data={"code": "mym://oauth2redirect/de?code=CODE123"}, allow_redirects=False)
    await flow.wait()
    entry = hass.config_entries.entry.data
    check(flow.step == STEP_OTP, f"login done -> otp (error={flow.error})")
    check(entry["oauth"]["refresh_token"] == "rt" and entry["mobile_app"] == "MyPeugeot" and entry["country_code"] == "DE", "oauth + app stored")
    check(entry["customer_id"] == "CUST-1" and "get_otp_sms" in stellantis.calls, "customer id stored, SMS requested")
    check(entry.get("notifications") is True and entry.get("anonymize_logs") is True, "upstream option defaults stored")
    check(os.path.isfile(os.path.join(data_dir, "config_entry.json")), "config_entry.json written")
    check(not completed, "not completed before OTP")
    page = await (await client.get("/")).text()
    check("SMS-Code" in page and "name='pin_code'" in page, "otp form rendered")

    print("otp wrong pin")
    stellantis.fail_otp = RuntimeError("NOK:ACCESS")
    await client.post("/otp", data={"sms_code": "1234", "pin_code": "0000"}, allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_OTP and flow.error == "Ungültiger SMS-Code oder PIN-Code", f"translated otp error: {flow.error}")
    stellantis.fail_otp = None

    print("otp ok")
    await client.post("/otp", data={"sms_code": "1234", "pin_code": "5678"}, allow_redirects=False)
    await flow.wait()
    entry = hass.config_entries.entry.data
    check(flow.step == STEP_DONE, f"done (error={flow.error})")
    check(entry["mqtt"]["access_token"] == "mat" and entry["remote_commands"] is True, "mqtt token stored")
    check("new_otp:1234:5678" in stellantis.calls, "new_otp called with sms + pin")
    check(os.path.isdir(os.path.join(data_dir, ".storage")), ".storage created for otp pickle")
    check(completed == [True], "on_complete called once")
    page = await (await client.get("/")).text()
    check("<h2>Status</h2>" in page and "Fernbefehle</th><td>aktiv" in page, "status page shows remote active")
    check("Noch keine Fahrzeuge" in page, "empty vehicle list")
    state["vehicles"] = [{"vin": "VR3TEST", "type": "Electric", "ok": True, "updated_at": "2026-09-05T10:00:00+00:00"}]
    page = await (await client.get("/")).text()
    check("VR3TEST" in page and "✓" in page, "vehicle row rendered")

    print("restart derives step")
    flow2 = SetupFlow(hass, stellantis, options)
    check(flow2.step == STEP_DONE, "fully configured -> done")
    flow3 = SetupFlow(hass, stellantis, {**options, "remote_commands": False})
    check(flow3.step == STEP_DONE, "remote not wanted -> done")
    check(hass.config_entries.entry.data["remote_commands"] is False and stellantis.remote_commands is False,
          "option remote_commands=false overrides the stored flag at startup")
    stellantis.update_stored_config("remote_commands", True)
    stellantis.save_config({"remote_commands": True})
    check(SetupFlow(hass, stellantis, options).step == STEP_DONE and stellantis.remote_commands, "option true leaves stored flag alone")

    print("disable / reconfigure remote")
    await client.post("/remote/disable", allow_redirects=False)
    await flow.wait()
    check(hass.config_entries.entry.data["remote_commands"] is False and flow.step == STEP_DONE, "remote disabled, still done")
    check(SetupFlow(hass, stellantis, options).step == STEP_DONE, "explicitly disabled remote is not asked again on restart")
    await client.post("/remote/reconfigure", allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_OTP and stellantis.calls.count("get_otp_sms") == 2, "reconfigure -> new SMS, otp step")
    await client.post("/remote/disable", allow_redirects=False)
    await flow.wait()

    print("reauth")
    await client.post("/reauth", allow_redirects=False)
    check(flow.step == STEP_LOGIN, "reauth -> login")
    stellantis.fail_token = True
    await client.post("/login", data={"code": "CODE123"}, allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_DONE, "failed voluntary reauth returns to the status page")
    stellantis.fail_token = False
    flow.reauth(required=True)
    stellantis.fail_token = True
    await client.post("/login", data={"code": "CODE123"}, allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_LOGIN, "failed required reauth keeps the login step")
    stellantis.fail_token = False
    sms_before, completed_before = stellantis.calls.count("get_otp_sms"), len(completed)
    await client.post("/login", data={"code": "CODE123"}, allow_redirects=False)
    await flow.wait()
    check(flow.step == STEP_DONE and hass.config_entries.entry.data["remote_commands"] is False, "reauth keeps disabled remote commands")
    check(stellantis.calls.count("get_otp_sms") == sms_before, "no SMS when remote commands are switched off in the stored config")
    check(len(completed) == completed_before + 1, "login with disabled remote commands restarts the runtime")

    print("browser mode without credentials")
    flow_b = SetupFlow(hass, stellantis, {**options, "oauth_mode": "browser", "email": "", "password": ""})
    flow_b.step = STEP_LOGIN
    try:
        flow_b.start_login()
        check(False, "should raise")
    except Exception as err:  # noqa: BLE001
        check("E-Mail und Passwort" in str(err), f"credentials error: {err}")

    print("misc endpoints")
    health = await (await client.get("/health")).json()
    check(health["status"] == "ok" and health["step"] == STEP_DONE, "health")
    api = await (await client.get("/api/state")).json()
    check(api["flow"]["customer_id"] == "CUST-1", "api/state")
    await client.post("/notifications/dismiss", allow_redirects=False)
    check(hass.notifications == [], "notifications dismissed")

    await client.close()
    print(f"\n{check.failures} failures")
    return check.failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
