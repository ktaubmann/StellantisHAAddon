"""Login failures and task lifetime without accessing Peugeot or a real browser."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from login import OauthBrowserError, _login_response_failure, _wait_login_result


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,expected', [
    ({'errorCode': 0}, None),
    ({'errorCode': 401021, 'errorDetails': 'secret-value'}, 'error 401021'),
    ({'errorCode': 'secret-value'}, None),
    ({'errorCode': True}, None),
    ([], None),
])
async def test_provider_json_reports_only_numeric_error(payload, expected):
    response = SimpleNamespace(url='https://accounts.example.invalid/accounts.login?token=secret',
                               status=200, json=AsyncMock(return_value=payload))
    result = await _login_response_failure(response)
    if expected is None:
        assert result is None
    else:
        assert expected in result
        assert 'secret' not in result


@pytest.mark.asyncio
async def test_http_failure_does_not_read_body():
    response = SimpleNamespace(url='https://accounts.example.invalid/accounts.login',
                               status=429, json=AsyncMock())
    assert 'HTTP 429' in await _login_response_failure(response)
    response.json.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_response_ignored():
    response = SimpleNamespace(url='https://example.invalid/analytics',
                               status=500, json=AsyncMock())
    assert await _login_response_failure(response) is None
    response.json.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_json_response_is_not_misreported_as_bad_credentials():
    response = SimpleNamespace(url='https://example.invalid/accounts.login',
                               status=200, json=AsyncMock(side_effect=ValueError('private-body')))
    assert await _login_response_failure(response) is None


@pytest.mark.asyncio
async def test_failure_after_consent_is_detected_immediately():
    loop = asyncio.get_running_loop()
    code, failure = loop.create_future(), loop.create_future()
    async def consent():
        loop.call_soon(failure.set_result, 'provider rejected login')
    task = asyncio.create_task(consent())
    with pytest.raises(OauthBrowserError, match='provider rejected'):
        await asyncio.wait_for(_wait_login_result(code, failure, task, 60), .5)
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['code', 'failure', 'timeout', 'cancel'])
async def test_pending_consent_is_always_drained(outcome):
    loop = asyncio.get_running_loop()
    code, failure = loop.create_future(), loop.create_future()
    entered, cleaned = asyncio.Event(), asyncio.Event()
    async def consent():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    task = asyncio.create_task(consent())
    await entered.wait()
    runner = asyncio.create_task(_wait_login_result(code, failure, task, .02 if outcome == 'timeout' else 60))
    await asyncio.sleep(0)
    if outcome == 'code':
        code.set_result('synthetic-code')
        assert await runner == 'synthetic-code'
    else:
        if outcome == 'failure':
            failure.set_result('rejected')
            error = OauthBrowserError
        elif outcome == 'cancel':
            runner.cancel()
            error = asyncio.CancelledError
        else:
            error = TimeoutError
        with pytest.raises(error):
            await runner
    assert task.done()
    assert cleaned.is_set()


def test_url_log_redacts_query_and_fragment_secrets():
    from login import _redact
    redacted = _redact('mymap://oauth2redirect/de?code=secret-query#token=secret-fragment')
    assert 'secret' not in redacted
    assert 'code=' in redacted


def test_browser_implementations_stay_in_sync():
    root = Path(__file__).resolve().parents[2]
    worker = (root / 'stellantis_login_worker/app/login.py').read_text()
    bridge = (root / 'stellantis_vehicles/app/oauth_browser/login.py').read_text()
    assert worker[worker.index('import asyncio'):].strip() == bridge[bridge.index('import asyncio'):bridge.index('def _cli()')].strip()
