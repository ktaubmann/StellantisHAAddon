"""HTTP front end for the headless Stellantis login.

Speaks the same wire format as the community "worker-v2" service, so this
add-on is a drop-in replacement for the shared Render.com instance that
homeassistant-stellantis-vehicles uses by default:

    POST /          {"url": ..., "email": ..., "password": ...}
                    -> 200 {"code": "<authorization code>"}
                    -> 400 {"message": "...", "code": 400}
    GET  /health    -> {"status": "ok"}

Only the wire format is shared. The login itself is our own clean-room
implementation in login.py; no code was taken from the (unlicensed)
worker-v2 repository.

Credentials arrive in the request body and are handed straight to the
browser. They are never logged, and neither is the resulting code.
"""
import asyncio
import logging
import math
import os

from aiohttp import web

import discovery
from login import LOGIN_DEADLINE_S, OauthBrowserError, fetch_oauth_code

_LOGGER = logging.getLogger("loginworker")

PORT = int(os.environ.get("WORKER_PORT", "3000"))
DEFAULT_TIMEOUT_S = float(os.environ.get("WORKER_TIMEOUT", "60"))

# One Chromium at a time. The add-on is meant to run on small hosts, and two
# concurrent browsers are a reliable way to run a Raspberry Pi out of memory.
_login_lock = asyncio.Lock()


def _error(message: str, status: int = 400) -> web.Response:
    """Error shape of worker-v2: a message plus the status repeated as "code"."""
    return web.json_response({"message": message, "code": status}, status=status)


async def handle_login(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any parse problem is the same to the caller
        return _error("Body is not valid JSON")

    if not isinstance(payload, dict):
        return _error("Body must be a JSON object")

    url = payload.get("url")
    email = payload.get("email")
    password = payload.get("password")
    if not all(isinstance(value, str) and value.strip() for value in (url, email, password)):
        return _error("Missing required params")

    # worker-v2 clients send milliseconds; keep accepting them.
    try:
        timeout_s = float(payload.get("timeout_page", DEFAULT_TIMEOUT_S * 1000)) / 1000
    except (TypeError, ValueError):
        return _error("timeout_page is not a number")
    if not math.isfinite(timeout_s):
        return _error("timeout_page must be finite")
    # Per page step, as in worker-v2; the whole attempt is bounded separately.
    timeout_s = min(max(timeout_s, 10.0), LOGIN_DEADLINE_S)

    if _login_lock.locked():
        response = _error("A login is already running. Wait for it to finish before retrying.", 429)
        response.headers["Retry-After"] = "10"
        return response

    async with _login_lock:
        _LOGGER.info("Starting login (step timeout %.0fs, deadline %.0fs)",
                     timeout_s, LOGIN_DEADLINE_S)
        try:
            # One cancellation deadline covers startup and every login stage.
            # fetch_oauth_code still runs its bounded cleanup after cancellation.
            async with asyncio.timeout(LOGIN_DEADLINE_S):
                code = await fetch_oauth_code(url, email, password, timeout_s=timeout_s,
                                              locale=payload.get("locale"))
        except TimeoutError:
            _LOGGER.warning("Login deadline reached")
            return _error("Login timed out. No automatic retry was made.", 504)
        except OauthBrowserError as err:
            # Browser exceptions can include URLs, form values or page text.
            # Never return or log their raw contents.
            _LOGGER.warning("Login did not complete")
            return _error("Login did not complete. Check the official app or sign in manually.")
        except Exception as err:  # noqa: BLE001 - never leak a stack trace to the caller
            _LOGGER.error("Login worker failed (%s)", type(err).__name__)
            return _error("The local login worker failed. No automatic retry was made.", 500)

    if not isinstance(code, str) or not code.strip():
        return _error("The login returned no authorization code.", 502)
    _LOGGER.info("Authorization code captured")
    return web.json_response({"code": code})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def announce_on_startup(app: web.Application) -> None:
    # Best effort: without discovery the URL can still be entered by hand.
    try:
        await discovery.announce(PORT)
    except Exception as err:  # noqa: BLE001 - never keep the worker from starting
        _LOGGER.warning("Supervisor discovery failed: %s", err)


def create_app(announce: bool = False) -> web.Application:
    app = web.Application()
    app.router.add_post("/", handle_login)
    app.router.add_get("/health", handle_health)
    if announce:
        app.on_startup.append(announce_on_startup)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOGGER.info("Stellantis Login Worker %s listening on port %d",
                 os.environ.get("ADDON_VERSION", "dev"), PORT)
    web.run_app(create_app(announce=True), host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
