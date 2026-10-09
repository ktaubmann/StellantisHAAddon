"""No browser, credentials or provider network required."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import server


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


CREDS = {'url': 'https://example.invalid/authorize',
         'email': 'test@example.invalid', 'password': 'synthetic-secret'}


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    monkeypatch.setattr(server, '_login_lock', asyncio.Lock())
    monkeypatch.setattr(server, 'fetch_oauth_code', AsyncMock(return_value='fake-code'))


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [None, [], 'text', 7, {'url': []},
                                    dict(CREDS, password=42), dict(CREDS, email=' ')])
async def test_invalid_input_never_starts_browser(payload):
    response = await server.handle_login(Request(payload))
    assert response.status == 400
    server.fetch_oauth_code.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
async def test_nonfinite_deadline_rejected(value):
    response = await server.handle_login(Request(dict(CREDS, timeout_page=value)))
    assert response.status == 400
    server.fetch_oauth_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_worker_does_not_queue_credentials():
    async with server._login_lock:
        response = await server.handle_login(Request(CREDS))
    assert response.status == 429
    assert response.headers['Retry-After'] == '10'
    server.fetch_oauth_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_whole_attempt_deadline_cleans_up_and_allows_next_login(monkeypatch):
    cleaned = asyncio.Event()
    async def stuck(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    original_timeout = asyncio.timeout
    monkeypatch.setattr(server.asyncio, 'timeout', lambda seconds: original_timeout(0.01))
    monkeypatch.setattr(server, 'fetch_oauth_code', stuck)
    response = await server.handle_login(Request(CREDS))
    assert response.status == 504
    assert cleaned.is_set()
    assert not server._login_lock.locked()
    monkeypatch.setattr(server, 'fetch_oauth_code', AsyncMock(return_value='next-code'))
    assert (await server.handle_login(Request(CREDS))).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError, server.OauthBrowserError])
async def test_errors_do_not_disclose_browser_contents(error, caplog):
    server.fetch_oauth_code.side_effect = error('synthetic-secret code=private-code')
    response = await server.handle_login(Request(CREDS))
    assert response.status in (400,500)
    assert 'synthetic-secret' not in response.text + caplog.text
    assert 'private-code' not in response.text + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [None, '', 12])
async def test_missing_code_is_a_failure(code):
    server.fetch_oauth_code.return_value = code
    response = await server.handle_login(Request(CREDS))
    assert response.status == 502


@pytest.mark.asyncio
async def test_cancellation_releases_lock(monkeypatch):
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    async def stuck(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    monkeypatch.setattr(server, 'fetch_oauth_code', stuck)
    task = asyncio.create_task(server.handle_login(Request(CREDS)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert not server._login_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'Identity provider rejected the login',
    'Login deadline reached',
    'Login endpoint returned HTTP 429',
    'Login endpoint returned error 403042; manual sign-in may be required',
])
async def test_known_login_errors_are_actionable(message, caplog):
    server.fetch_oauth_code.side_effect = server.OauthBrowserError(message)
    response = await server.handle_login(Request(CREDS))
    assert response.status == 400
    assert json.loads(response.text)['message'] == message
    assert message in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'Identity provider rejected the login private-secret',
    'Login endpoint returned HTTP 429\nprivate-secret',
    'Login endpoint returned error 999999; manual sign-in may be required',
])
async def test_error_messages_must_match_known_reasons_exactly(message, caplog):
    server.fetch_oauth_code.side_effect = server.OauthBrowserError(message)
    response = await server.handle_login(Request(CREDS))
    assert 'Login did not complete' in response.text
    assert message not in response.text + caplog.text
