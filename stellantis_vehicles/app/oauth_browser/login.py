"""Headless-browser login to obtain the Stellantis OAuth authorization code.

Clean-room implementation (no code taken from the unlicensed worker-v2 repo).
Flow: open the OAuth authorize URL, fill the Gigya login form, confirm the
consent form, then catch the redirect to the app scheme (mym*://...?code=...)
which the browser cannot follow. Depending on the Chromium build that shows
up as a failed request, as a plain request event, as a 30x response with a
Location header or as a frame navigation - all four are watched.

Chromium is launched per call and closed afterwards to keep the add-on's
memory footprint low on small hosts. Every step has a timeout and the
browser teardown is bounded too, so a stuck Chromium cannot hang the flow.

Diagnostics without the add-on (asks for the password, prints every URL seen):

    python app/oauth_browser/login.py --app MyPeugeot --country DE --email me@example.org
"""
import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger(__name__)

# Selectors of the Stellantis/Gigya login page. Kept in one place because
# they are the part most likely to break on upstream UI changes.
SEL_EMAIL = "#gigya-login-form input[name='username']"
SEL_PASSWORD = "#gigya-login-form input[name='password']"
SEL_SUBMIT = "#gigya-login-form input[type='submit']"
SEL_AUTHORIZE = "#cvs_from input[type='submit']"

APP_SCHEME_PREFIX = "mym"
CLOSE_TIMEOUT_S = 10.0

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-zygote",
]


class OauthBrowserError(RuntimeError):
    pass


async def _login_response_failure(response) -> str | None:
    """Read only numeric login status; never expose response bodies or URLs."""
    if urlsplit(response.url).path.rstrip("/").lower() != "/accounts.login":
        return None
    if response.status >= 400:
        return f"Login endpoint returned HTTP {int(response.status)}"
    try:
        payload = await asyncio.wait_for(response.json(), 5)
    except Exception:  # Malformed/non-JSON response is not proof of login rejection.
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("errorCode")
    if type(code) is int and 0 < code <= 999999:
        return f"Login endpoint returned error {code}; manual sign-in may be required"
    return None


async def _wait_login_result(code, failure, consent, timeout_s):
    """Keep watching failures after consent; use a single remaining deadline."""
    watchers = {code, failure, consent}
    try:
        async with asyncio.timeout(timeout_s):
            while True:
                await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
                if failure.done():
                    raise OauthBrowserError(failure.result())
                if code.done():
                    return code.result()
                if consent.done():
                    consent.result()
                    watchers.discard(consent)
    finally:
        if not consent.done():
            consent.cancel()
        await asyncio.gather(consent, return_exceptions=True)


def _code_from_url(url: str) -> str | None:
    if not url or not url.lower().startswith(APP_SCHEME_PREFIX):
        return None
    query = urlsplit(url).query
    return parse_qs(query).get("code", [None])[0]


def _redact(url: str) -> str:
    """URL without query values for logging (the code must not end up in logs)."""
    if not url:
        return ""
    parts = urlsplit(url)
    keys = "&".join(f"{k}=…" for k in parse_qs(parts.query)) if parts.query else ""
    return parts._replace(query=keys, fragment="…" if parts.fragment else "").geturl()


async def _bounded(coro, timeout: float, what: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        _LOGGER.warning("%s did not finish within %.0fs, giving up on it", what, timeout)
    except Exception as err:  # noqa: BLE001 - teardown must never mask the real error
        _LOGGER.debug("%s failed: %s", what, err)


async def fetch_oauth_code(oauth_url: str, email: str, password: str,
                           timeout_s: float = 60.0, debug_dir: str | None = None,
                           on_event=None, locale: str | None = None) -> str:
    """Return the OAuth authorization code for the given credentials.

    ``locale``: browser locale (e.g. ``de-DE``), normally the one of the
    account's country from the app configuration. ``debug_dir``: if set, a
    screenshot and the final URL are written there when no code could be
    captured. ``on_event(source, url)`` is called for every URL seen
    (diagnostics CLI).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:  # pragma: no cover
        raise OauthBrowserError("playwright is not installed") from err

    loop = asyncio.get_running_loop()
    code_future: asyncio.Future[str] = loop.create_future()
    started = time.monotonic()

    def seen(source: str, url: str) -> None:
        if not url:
            return
        _LOGGER.debug("[%5.1fs] %-16s %s", time.monotonic() - started, source, _redact(url))
        if on_event:
            on_event(source, url)
        code = _code_from_url(url)
        if code and not code_future.done():
            code_future.set_result(code)

    failure_future: asyncio.Future[str] = loop.create_future()

    response_tasks = set()

    async def inspect_login_response(response):
        reason = await _login_response_failure(response)
        if reason and not failure_future.done():
            _LOGGER.warning("%s", reason)
            failure_future.set_result(reason)

    def on_response(response) -> None:
        if 300 <= response.status < 400:
            seen("response-location", response.headers.get("location", ""))
        if urlsplit(response.url).path.rstrip("/").lower() == "/accounts.login":
            task = loop.create_task(inspect_login_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

    def on_navigated(frame) -> None:
        seen("framenavigated", frame.url)
        if "failedlogin" in frame.url.lower() and not failure_future.done():
            failure_future.set_result("Identity provider rejected the login")

    phase = "browser_start"
    pw = await async_playwright().start()
    browser = None
    page = None
    try:
        browser = await _launch(pw)
        context = await browser.new_context(viewport={"width": 1280, "height": 720},
                                            locale=locale or "en-US")
        page = await context.new_page()
        page.on("request", lambda r: seen("request", r.url))
        page.on("requestfailed", lambda r: seen("requestfailed", r.url))
        page.on("response", on_response)
        page.on("framenavigated", on_navigated)


        ms = int(timeout_s * 1000)
        _LOGGER.debug("Opening OAuth page")
        phase = "open_authorization"
        await page.goto(oauth_url, wait_until="domcontentloaded", timeout=ms)
        phase = "wait_login_form"
        await page.wait_for_selector(SEL_EMAIL, timeout=ms)
        await page.fill(SEL_EMAIL, email)
        await page.fill(SEL_PASSWORD, password)
        phase = "submit_credentials"
        await page.click(SEL_SUBMIT)
        phase = "wait_login_result"
        _LOGGER.debug("Credentials submitted, waiting for consent form or app redirect")

        async def wait_consent() -> None:
            nonlocal phase
            await page.wait_for_selector(SEL_AUTHORIZE, timeout=ms)
            phase = "submit_consent"
            await page.click(SEL_AUTHORIZE)
            phase = "wait_app_redirect"
            _LOGGER.debug("Consent submitted, waiting for app redirect")

        consent_task = loop.create_task(wait_consent())
        try:
            return await _wait_login_result(
                code_future, failure_future, consent_task,
                max(0, timeout_s - (time.monotonic() - started)))
        except TimeoutError as err:
            raise OauthBrowserError("Login deadline reached") from err
    except BaseException:
        _LOGGER.warning("Login stopped in phase %s", phase)
        if debug_dir:
            await _dump_debug(page, debug_dir)
        raise
    finally:
        for task in response_tasks:
            task.cancel()
        await asyncio.gather(*response_tasks, return_exceptions=True)
        if browser is not None:
            await _bounded(browser.close(), CLOSE_TIMEOUT_S, "browser.close()")
        await _bounded(pw.stop(), CLOSE_TIMEOUT_S, "playwright.stop()")


async def _launch(pw):
    """Real Chromium in the new headless mode (same UA and fingerprint as a
    normal Chrome) - the headless shell is easy prey for bot detection.
    Falls back to the shell when the full browser is not installed."""
    try:
        browser = await pw.chromium.launch(channel="chromium", headless=True, args=CHROMIUM_ARGS)
        _LOGGER.debug("Chromium (new headless) %s", browser.version)
        return browser
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Full Chromium not available (%s), using headless shell",
                        (str(err).splitlines() or [repr(err)])[0])
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        _LOGGER.debug("Chromium headless shell %s", browser.version)
        return browser


_TOKEN_RE = re.compile(r'("(?:authId|tokenId|token|code|password|IDToken\d|input)"\s*:\s*")([^"]{6,})"')


def _redact_tokens(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: f'{m.group(1)}{m.group(2)[:6]}…"', text)


async def _page_text(page) -> str:
    try:
        text = await asyncio.wait_for(page.inner_text("body"), 10)
        return " ".join(text.split())[:400]
    except Exception as err:  # noqa: BLE001
        return f"<no page text: {err}>"


async def _dump_debug(page, debug_dir: str | None) -> None:
    if not page:
        return
    try:
        final_url = page.url
        _LOGGER.warning("Stuck on %s", _redact(final_url))
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            await asyncio.wait_for(page.screenshot(path=os.path.join(debug_dir, "oauth_last_page.png")), 10)
            with open(os.path.join(debug_dir, "oauth_last_page.txt"), "w", encoding="utf-8") as f:
                f.write(final_url + "\n")
                f.write(await asyncio.wait_for(page.content(), 10))
            _LOGGER.warning("Screenshot and page source written to %s", debug_dir)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not write debug output: %s", err)

def _cli() -> None:  # pragma: no cover - manual diagnostics
    import argparse
    import getpass
    import sys

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(app_dir, "hass_shim"))
    sys.path.insert(0, app_dir)
    from homeassistant.core import HomeAssistant  # noqa: E402
    from stellantis_vehicles.stellantis import StellantisOauth  # noqa: E402

    parser = argparse.ArgumentParser(description="Diagnose the headless Stellantis login")
    parser.add_argument("--app", default="MyPeugeot")
    parser.add_argument("--country", default="DE")
    parser.add_argument("--email", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--debug-dir", default=os.path.join(app_dir, "..", "data", "oauth_debug"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncio").setLevel(logging.INFO)
    password = getpass.getpass("Passwort: ")

    async def run() -> int:
        hass = HomeAssistant(config_dir=os.path.join(app_dir, "..", "data"))
        client = StellantisOauth(hass)
        client.set_mobile_app(args.app, args.country.upper())
        url = client.get_oauth_url()
        print("OAuth URL:", url)
        events: list[tuple[str, str]] = []
        try:
            code = await fetch_oauth_code(url, args.email, password, args.timeout, args.debug_dir,
                                          on_event=lambda s, u: events.append((s, u)),
                                          locale=client.get_config("locale"))
        except OauthBrowserError as err:
            print("FEHLER:", err)
            code = None
        print("\nGesehene URLs (Query-Werte maskiert):")
        for source, seen_url in events:
            print(f"  {source:16s} {_redact(seen_url)}")
        print("\nErgebnis:", "Code erhalten (%d Zeichen)" % len(code) if code else "kein Code")
        return 0 if code else 1

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    _cli()
