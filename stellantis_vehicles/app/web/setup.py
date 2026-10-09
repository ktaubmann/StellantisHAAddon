"""Setup flow behind the ingress UI (replacement for the upstream config_flow.py).

Steps, same order and same stored data as upstream:
  login   OAuth code (headless Chromium or pasted manually) -> access token
  otp     only with remote commands: customer id, SMS code + PIN -> MQTT token
  done    everything the runtime needs is in the stored config

The flow owns no HTML; server.py renders ``snapshot()`` and calls the
``submit_*`` methods. Long-running work (Chromium login, token requests) runs
as a task so the ingress request returns immediately and the page polls.
"""
import asyncio
import logging
import os
from datetime import timedelta
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from homeassistant.helpers import translation

from stellantis_vehicles.const import (
    DOMAIN,
    FIELD_ANONYMIZE_LOGS,
    FIELD_COUNTRY_CODE,
    FIELD_MOBILE_APP,
    FIELD_NOTIFICATIONS,
    FIELD_REMOTE_COMMANDS,
    MOBILE_APPS,
    MQTT_REFRESH_TOKEN_TTL,
)
from stellantis_vehicles.utils import get_datetime

_LOGGER = logging.getLogger(__name__)

STEP_LOGIN = "login"
STEP_OTP = "otp"
STEP_DONE = "done"


class SetupError(Exception):
    """User facing error, message already translated."""


def extract_oauth_code(text: str) -> str | None:
    """Accept a bare code, the ``mym…://oauth2redirect/…?code=…`` URL or any
    URL carrying a ``code`` query parameter."""
    text = (text or "").strip()
    if not text:
        return None
    if "code=" in text:
        query = urlsplit(text).query or text.split("?", 1)[-1]
        code = parse_qs(query).get("code", [None])[0]
        return code.strip() if code else None
    if " " in text or "/" in text:
        return None
    return text


class SetupFlow:
    def __init__(self, hass, stellantis, options: dict,
                 on_complete: Callable[[], Awaitable[None]] | None = None) -> None:
        self._hass = hass
        self._stellantis = stellantis
        self._options = options
        self._on_complete = on_complete
        self._translations: dict = {}
        self._task: asyncio.Task | None = None
        self.error: str | None = None
        self.message: str | None = None
        # Set while remote commands are being reconfigured: the flag in the
        # stored config is already False, but the OTP step must still run.
        self._force_otp = False
        # Upstream option defaults; hass_notify() stays silent until
        # `notifications` is stored, so persist them before the first step.
        if FIELD_NOTIFICATIONS not in self.entry:
            self._store({FIELD_NOTIFICATIONS: True, FIELD_ANONYMIZE_LOGS: True})
        # The add-on option `remote_commands: false` wins over the stored flag;
        # `true` leaves the decision to the stored flag / the ingress page.
        if not self.remote_commands_wanted and self.entry.get(FIELD_REMOTE_COMMANDS) is not False:
            self._stellantis.disable_remote_commands()
        # Where the user is in the flow, derived from the stored config
        self.step = self._derive_step()

    # --- state ---------------------------------------------------------------
    @property
    def entry(self) -> dict:
        return self._hass.config_entries.entry.data

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def configured(self) -> bool:
        return bool(self.entry.get("oauth", {}).get("refresh_token"))

    @property
    def remote_commands_wanted(self) -> bool:
        return bool(self._options.get(FIELD_REMOTE_COMMANDS, True))

    @property
    def remote_commands_ready(self) -> bool:
        return self.entry.get(FIELD_REMOTE_COMMANDS) is True and bool(self.entry.get("mqtt", {}).get("access_token"))

    def _derive_step(self) -> str:
        if not self.configured:
            return STEP_LOGIN
        if self._force_otp:
            return STEP_OTP
        if self.remote_commands_wanted and not self.remote_commands_ready and self.entry.get(FIELD_REMOTE_COMMANDS) is not False:
            return STEP_OTP
        return STEP_DONE

    def snapshot(self) -> dict:
        oauth = self.entry.get("oauth", {})
        mqtt = self.entry.get("mqtt", {})
        return {
            "step": self.step,
            "busy": self.busy,
            "error": self.error,
            "message": self.message,
            "mobile_app": self.mobile_app,
            "country_code": self.country_code,
            "email": self._options.get("email", ""),
            "oauth_mode": self.oauth_mode,
            "oauth_url": self.oauth_url,
            "oauth_expires": oauth.get("expires_in"),
            "mqtt_expires": mqtt.get("expires_in"),
            "remote_commands": self.entry.get(FIELD_REMOTE_COMMANDS),
            "remote_commands_ready": self.remote_commands_ready,
            "customer_id": self.entry.get("customer_id"),
        }

    # --- options -------------------------------------------------------------
    @property
    def mobile_app(self) -> str:
        return self.entry.get(FIELD_MOBILE_APP) or self._options.get(FIELD_MOBILE_APP, "MyPeugeot")

    @property
    def country_code(self) -> str:
        return str(self.entry.get(FIELD_COUNTRY_CODE) or self._options.get(FIELD_COUNTRY_CODE, "DE")).upper()

    @property
    def oauth_mode(self) -> str:
        return "manual" if self._options.get("oauth_mode") == "manual" else "browser"

    @property
    def oauth_url(self) -> str | None:
        try:
            self._ensure_mobile_app()
            return self._stellantis.get_oauth_url()
        except SetupError:
            return None

    def _ensure_mobile_app(self) -> None:
        app, country = self.mobile_app, self.country_code
        if app not in MOBILE_APPS:
            raise SetupError(self._text("invalid_app", app=app, apps=", ".join(MOBILE_APPS)))
        if country not in MOBILE_APPS[app]["configs"]:
            raise SetupError(self._text("invalid_country", country=country, app=app))
        if self._stellantis.get_config("client_id") is None or self._stellantis.get_config(FIELD_MOBILE_APP) != app:
            self._stellantis.save_config({FIELD_MOBILE_APP: app, FIELD_COUNTRY_CODE: country})

    # --- translations --------------------------------------------------------
    async def load_translations(self) -> None:
        if not self._translations:
            self._translations = await translation.async_get_translations(
                self._hass, self._hass.config.language, "config", {DOMAIN})

    def _error_message(self, error: str, detail=None) -> str:
        result = str(self._translations.get(f"component.{DOMAIN}.config.error.{error}", error))
        if detail:
            result = f"{result}: {detail}"
        return result

    def _text(self, key: str, **placeholders) -> str:
        de = self._hass.config.language.startswith("de")
        texts = {
            "invalid_app": ("App {app} wird nicht unterstützt (möglich: {apps})",
                            "App {app} is not supported (available: {apps})"),
            "invalid_country": ("Ländercode {country} ist für {app} nicht verfügbar",
                                "Country code {country} is not available for {app}"),
            "no_credentials": ("E-Mail und Passwort fehlen (Add-on-Optionen oder Formular)",
                               "E-mail and password missing (add-on options or form)"),
            "no_code": ("Kein OAuth-Code erkannt", "No OAuth code recognised"),
            "busy": ("Es läuft bereits ein Vorgang", "Another step is still running"),
            "login_ok": ("Anmeldung erfolgreich", "Login successful"),
            "sms_sent": ("SMS-Code angefordert", "SMS code requested"),
            "otp_ok": ("Fernbefehle eingerichtet", "Remote commands configured"),
            "remote_disabled": ("Fernbefehle deaktiviert", "Remote commands disabled"),
        }
        return texts[key][0 if de else 1].format(**placeholders)

    # --- persistence ---------------------------------------------------------
    def _store(self, data: dict) -> None:
        """Mirror upstream: runtime config and stored entry data in sync."""
        self._stellantis.save_config(data)
        for key, value in data.items():
            self._stellantis.update_stored_config(key, value)

    # --- task plumbing -------------------------------------------------------
    def _start(self, coro) -> None:
        if self.busy:
            coro.close()
            raise SetupError(self._text("busy"))
        self.error = None
        self.message = None
        self._task = self._hass.loop.create_task(self._guard(coro))

    async def _guard(self, coro) -> None:
        try:
            await coro
        except SetupError as err:
            self.error = str(err)
        except Exception as err:  # noqa: BLE001 - shown to the user, nothing else to do
            _LOGGER.exception("Setup step failed: %s", err)
            self.error = str(err)
        finally:
            self.step = self._derive_step()

    async def wait(self) -> None:
        """Test helper: wait for the running step."""
        if self._task:
            await self._task

    # --- step: login -----------------------------------------------------------
    def start_login(self, email: str | None = None, password: str | None = None, code: str | None = None) -> None:
        """Browser login with credentials, or manual mode with a pasted code/URL."""
        self._ensure_mobile_app()
        if code is not None:
            oauth_code = extract_oauth_code(code)
            if not oauth_code:
                raise SetupError(self._text("no_code"))
            self._start(self._finish_login(oauth_code))
            return
        email = email or self._options.get("email") or ""
        password = password or self._options.get("password") or ""
        if not email or not password:
            raise SetupError(self._text("no_credentials"))
        self._start(self._browser_login(email, password))

    async def _browser_login(self, email: str, password: str) -> None:
        await self.load_translations()
        # imported late: playwright is optional locally
        from oauth_browser.login import LOGIN_BACKSTOP_S, fetch_oauth_code, public_login_error
        try:
            # fetch_oauth_code enforces its own deadline; this only catches a hung browser start.
            oauth_code = await asyncio.wait_for(
                fetch_oauth_code(self._stellantis.get_oauth_url(), email, password,
                                 debug_dir=self._hass.config.path("oauth_debug"),
                                 locale=self._stellantis.get_config("locale")),
                timeout=LOGIN_BACKSTOP_S)
        except asyncio.TimeoutError as err:
            raise SetupError(self._error_message("get_oauth_code", "timeout")) from err
        except Exception as err:  # noqa: BLE001
            # Browser errors can carry URLs or page text; show only our fixed messages.
            raise SetupError(self._error_message("get_oauth_code", public_login_error(err))) from err
        await self._finish_login(oauth_code)

    async def _finish_login(self, oauth_code: str) -> None:
        await self.load_translations()
        self._stellantis.logger_filter.add_custom_value(oauth_code)
        self._stellantis.save_config({"oauth_code": oauth_code})
        try:
            token_request = await self._stellantis.get_access_token()
            oauth = {
                "access_token": token_request["access_token"],
                "refresh_token": token_request["refresh_token"],
                "expires_in": (get_datetime() + timedelta(seconds=int(token_request["expires_in"]))).isoformat(),
            }
        except Exception as err:  # noqa: BLE001
            await self._stellantis.hass_notify("access_token_error")
            raise SetupError(self._error_message("get_access_token", err)) from err

        data = {
            FIELD_MOBILE_APP: self.mobile_app,
            FIELD_COUNTRY_CODE: self.country_code,
            "oauth": oauth,
            FIELD_NOTIFICATIONS: self.entry.get(FIELD_NOTIFICATIONS, True),
            FIELD_ANONYMIZE_LOGS: self.entry.get(FIELD_ANONYMIZE_LOGS, True),
        }
        if not self.remote_commands_wanted:
            data[FIELD_REMOTE_COMMANDS] = False
            if not self.entry.get("customer_id"):
                data["customer_id"] = "MN-" + str(uuid4()).replace("-", "")[:16]
        self._store(data)
        self.message = self._text("login_ok")
        if self.remote_commands_wanted and not self.remote_commands_ready:
            # Straight on to the OTP step: fetch customer id and trigger the SMS
            await self._request_sms()
        else:
            await self._complete()

    # --- step: otp -------------------------------------------------------------
    def start_sms(self) -> None:
        """(Re)send the OTP SMS, e.g. when the first one did not arrive."""
        self._start(self._request_sms())

    async def _request_sms(self) -> None:
        await self.load_translations()
        try:
            user_info = await self._stellantis.get_user_info()
        except Exception as err:  # noqa: BLE001
            raise SetupError(self._error_message("get_user_info", err)) from err
        if not user_info or "customer" not in user_info[0]:
            raise SetupError(self._error_message("missing_user_info"))
        self._store({"customer_id": user_info[0]["customer"]})
        try:
            await self._stellantis.get_otp_sms()
        except Exception as err:  # noqa: BLE001
            await self._stellantis.hass_notify("otp_error")
            raise SetupError(self._error_message("get_otp_sms", err)) from err
        self.message = self._text("sms_sent")

    def start_otp(self, sms_code: str, pin_code: str) -> None:
        sms_code, pin_code = (sms_code or "").strip(), (pin_code or "").strip()
        if not sms_code or not pin_code:
            raise SetupError(self._error_message("get_mqtt_access_token_nok_access"))
        self._start(self._finish_otp(sms_code, pin_code))

    async def _finish_otp(self, sms_code: str, pin_code: str) -> None:
        await self.load_translations()
        # get_otp_code() uses os.mkdir on <config>/.storage/<domain>: parent must exist
        os.makedirs(self._hass.config.path(".storage"), exist_ok=True)
        try:
            await self._hass.async_add_executor_job(self._stellantis.new_otp, sms_code, pin_code)
            token_request = await self._stellantis.get_mqtt_access_token()
        except Exception as err:  # noqa: BLE001
            await self._stellantis.hass_notify("otp_error")
            key = "get_mqtt_access_token_" + str(err).lower().replace(":", "_")
            message = self._translations.get(f"component.{DOMAIN}.config.error.{key}")
            raise SetupError(message or self._error_message("get_mqtt_access_token", err)) from err

        mqtt = {
            "access_token": token_request["access_token"],
            "refresh_token": token_request["refresh_token"],
            "expires_in": (get_datetime() + timedelta(seconds=int(token_request["expires_in"]))).isoformat(),
            "refresh_token_expires_at": (get_datetime() + timedelta(minutes=int(MQTT_REFRESH_TOKEN_TTL))).isoformat(),
        }
        self._store({"mqtt": mqtt, FIELD_REMOTE_COMMANDS: True})
        self._force_otp = False
        self.message = self._text("otp_ok")
        await self._complete()

    def disable_remote_commands(self) -> None:
        self._stellantis.disable_remote_commands()
        self._force_otp = False
        self.message = self._text("remote_disabled")
        self.step = self._derive_step()
        if self.step == STEP_DONE:
            self._start(self._complete())

    def reconfigure_remote_commands(self) -> None:
        """Upstream reconfigure -> remote_commands: drop the flag, redo OTP."""
        self._stellantis.disable_remote_commands()
        self._stellantis.otp = None
        self._force_otp = True
        self.step = STEP_OTP
        self.start_sms()

    def reauth(self) -> None:
        """Back to the login step, keeping everything else."""
        self.step = STEP_LOGIN

    # --- completion ------------------------------------------------------------
    async def _complete(self) -> None:
        self.step = self._derive_step()
        if self.step == STEP_DONE and self._on_complete:
            await self._on_complete()
