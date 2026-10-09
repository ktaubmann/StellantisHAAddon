"""Offline smoke test for the login worker's HTTP contract (app/server.py).

Stubs the browser login; drives the aiohttp app with a test client and checks
the wire format the homeassistant-stellantis-vehicles integration expects:
success carries "code", failures carry "message", /health answers "ok".

    ../.venv/Scripts/python tests/smoke_worker.py
"""
import asyncio
import os
import sys

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP_DIR)

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import discovery  # noqa: E402
import server  # noqa: E402
from login import OauthBrowserError  # noqa: E402

OAUTH_URL = "https://idpcvs.peugeot.com/am/oauth2/authorize?client_id=x"
CREDS = {"url": OAUTH_URL, "email": "me@example.org", "password": "s3cret"}

failures: list[str] = []


def check(condition: bool, what: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + what)
    if not condition:
        failures.append(what)


async def discovery_checks() -> None:
    """Announce against a fake Supervisor that mimics /addons/self/info and
    /discovery, including its {"result": ..., "data": ...} envelope."""
    seen: list[dict] = []
    refuse = False

    async def self_info(request):
        seen.append({"path": "info", "auth": request.headers.get("Authorization")})
        return web.json_response({"result": "ok", "data": {
            "slug": "0e0578fd_stellantis_login_worker",
            "hostname": "0e0578fd-stellantis-login-worker"}})

    async def post_discovery(request):
        seen.append({"path": "discovery", "auth": request.headers.get("Authorization"),
                     "body": await request.json()})
        if refuse:
            return web.json_response({"result": "error", "message": "not listed"}, status=403)
        return web.json_response({"result": "ok", "data": {"uuid": "abc123"}})

    fake = web.Application()
    fake.router.add_get("/addons/self/info", self_info)
    fake.router.add_post("/discovery", post_discovery)

    print("supervisor discovery")
    os.environ.pop("SUPERVISOR_TOKEN", None)
    async with TestServer(fake) as supervisor:
        base = str(supervisor.make_url("/"))

        check(await discovery.announce(3000, base_url=base) is None, "no token -> skipped")
        check(not seen, "no token -> Supervisor not contacted")

        config = await discovery.announce(3000, token="tkn", base_url=base)
        check(config == {"host": "0e0578fd-stellantis-login-worker", "port": 3000},
              "announced config is internal hostname + port")
        check(all(s["auth"] == "Bearer tkn" for s in seen), "Supervisor token sent as bearer")
        body = seen[-1].get("body", {})
        check(body.get("service") == "stellantis_vehicles", "service is the integration's domain")
        check(body.get("config") == config, "discovery body carries the config")

        refuse = True
        try:
            await discovery.announce(3000, token="tkn", base_url=base)
            check(False, "refused discovery raises")
        except discovery.DiscoveryError as err:
            check("403" in str(err), "refused discovery raises with status")

        # The startup hook must swallow the error so the worker still serves.
        os.environ["SUPERVISOR_TOKEN"] = "tkn"
        os.environ["SUPERVISOR_URL"] = base
        try:
            await server.announce_on_startup(web.Application())
            check(True, "startup hook survives a refused discovery")
        finally:
            del os.environ["SUPERVISOR_TOKEN"], os.environ["SUPERVISOR_URL"]


async def main() -> int:
    calls: list[dict] = []

    async def fake_login(url, email, password, timeout_s=60.0, debug_dir=None,
                         on_event=None, locale=None):
        calls.append({"url": url, "email": email, "password": password,
                      "timeout_s": timeout_s, "locale": locale})
        if password == "wrong":
            raise OauthBrowserError("Stellantis IdP rejected the login: bad credentials")
        if password == "boom":
            raise RuntimeError("chromium vanished")
        return "AUTH-CODE-123"

    server.fetch_oauth_code = fake_login

    async with TestClient(TestServer(server.create_app())) as client:
        print("health")
        res = await client.get("/health")
        check(res.status == 200, "GET /health -> 200")
        check((await res.json()) == {"status": "ok"}, 'body is {"status": "ok"}')

        print("successful login")
        res = await client.post("/", json=CREDS)
        body = await res.json()
        check(res.status == 200, "POST / -> 200")
        check(body.get("code") == "AUTH-CODE-123", 'body carries the code')
        check(calls[-1]["url"] == OAUTH_URL, "OAuth URL passed through unchanged")
        check(calls[-1]["timeout_s"] == server.DEFAULT_TIMEOUT_S, "default timeout applied")

        print("timeout_page is milliseconds")
        await client.post("/", json=dict(CREDS, timeout_page=90000))
        check(calls[-1]["timeout_s"] == 90.0, "90000 ms -> 90 s")
        await client.post("/", json=dict(CREDS, timeout_page=5))
        check(calls[-1]["timeout_s"] == 10.0, "absurdly small value clamped to 10 s")
        await client.post("/", json=dict(CREDS, timeout_page=9_000_000))
        check(calls[-1]["timeout_s"] == 240.0, "absurdly large value clamped to 240 s")

        print("bad requests")
        for missing in ("url", "email", "password"):
            payload = {k: v for k, v in CREDS.items() if k != missing}
            res = await client.post("/", json=payload)
            body = await res.json()
            check(res.status == 400 and body.get("code") == 400, f"missing {missing} -> 400")
            check("message" in body, f"missing {missing} -> body carries a message")
        res = await client.post("/", data="not json", headers={"Content-Type": "application/json"})
        check(res.status == 400, "invalid JSON -> 400")

        print("login errors")
        res = await client.post("/", json=dict(CREDS, password="wrong"))
        body = await res.json()
        check(res.status == 400, "rejected login -> 400")
        check("did not complete" in body.get("message", ""), "safe failure message returned")
        check("code" not in body or body["code"] == 400, "no authorization code in an error body")

        res = await client.post("/", json=dict(CREDS, password="boom"))
        body = await res.json()
        check(res.status == 500, "unexpected error -> 500")
        check("chromium vanished" not in body.get("message", ""), "raw exception text not disclosed")

        print("concurrency")
        gate = asyncio.Event()
        entered = asyncio.Event()
        overlap = False

        async def slow_login(*args, **kwargs):
            nonlocal overlap
            if entered.is_set():
                overlap = True
            entered.set()
            await gate.wait()
            entered.clear()
            return "SLOW-CODE"

        server.fetch_oauth_code = slow_login
        first = asyncio.ensure_future(client.post("/", json=CREDS))
        second = asyncio.ensure_future(client.post("/", json=CREDS))
        await asyncio.sleep(0.1)
        gate.set()
        results = await asyncio.gather(first, second)
        check(sorted(res.status for res in results) == [200, 429], "concurrent login is rejected without queueing")
        check(not overlap, "only one login runs at a time")

    await discovery_checks()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for what in failures:
            print("  -", what)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
