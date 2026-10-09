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
    ({'errorCode': 206001}, None),
    ({'errorCode': 206002}, None),
    ({'errorCode': 206006}, None),
    ({'errorCode': 403100}, None),
    ({'errorCode': 403101}, None),
    ({'errorCode': 403102}, None),
    ({'errorCode': 401020}, None),
    ({'errorCode': 999999}, None),
    ({'errorCode': 403042}, 'error 403042'),
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
async def test_consent_error_is_a_login_failure_without_raw_text():
    loop = asyncio.get_running_loop()
    code, failure = loop.create_future(), loop.create_future()
    async def consent():
        raise RuntimeError('Timeout waiting for selector on https://example.invalid/?token=secret')
    task = asyncio.create_task(consent())
    with pytest.raises(OauthBrowserError, match='consent form not found') as err:
        await asyncio.wait_for(_wait_login_result(code, failure, task, 60), .5)
    assert 'secret' not in str(err.value)
    from login import public_login_error
    assert public_login_error(err.value) == str(err.value)


def test_step_timeout_is_separate_from_attempt_deadline():
    import inspect
    from login import LOGIN_DEADLINE_S, fetch_oauth_code
    params = inspect.signature(fetch_oauth_code).parameters
    assert params['deadline_s'].default == LOGIN_DEADLINE_S
    # Deadline plus bounded teardown stays below the integration's 300 s.
    assert LOGIN_DEADLINE_S + 20 < 300


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


@pytest.mark.asyncio
async def test_pending_registration_can_still_complete():
    response = SimpleNamespace(url='https://example.invalid/accounts.login',
                               status=200, json=AsyncMock(return_value={'errorCode': 206001}))
    loop = asyncio.get_running_loop()
    code, failure = loop.create_future(), loop.create_future()
    async def consent():
        reason = await _login_response_failure(response)
        if reason:
            failure.set_result(reason)
        code.set_result('synthetic-code')
    assert await _wait_login_result(code, failure, asyncio.create_task(consent()), .5) == 'synthetic-code'


@pytest.mark.asyncio
async def test_stuck_url_logged_without_debug_directory(caplog):
    from login import _dump_debug
    page = SimpleNamespace(url='https://example.invalid/login?token=private-query#private-fragment',
                           screenshot=AsyncMock(), content=AsyncMock())
    await _dump_debug(page, None)
    assert 'Stuck on https://example.invalid/login' in caplog.text
    assert 'private' not in caplog.text
    page.screenshot.assert_not_awaited()
    page.content.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticate_and_console_diagnostics_omit_contents(caplog):
    from login import _log_authenticate_response, _log_console
    caplog.set_level('DEBUG')
    response = SimpleNamespace(status=401, json=AsyncMock(return_value={
        'code': 401, 'authId': 'private-auth-id', 'message': 'private-provider-message',
        'callbacks': [
            {'type': 'PasswordCallback', 'input': [{'value': 'private-password'}]},
            {'type': 'private-callback-name'}, {'type': ['private-malformed']},
        ],
    }))
    await _log_authenticate_response(response)
    _log_console(SimpleNamespace(type='error', text='private-console-token'))
    _log_console(SimpleNamespace(type='private-type', text='private-console-token'))
    assert 'authenticate HTTP 401' in caplog.text
    assert 'numeric code 401' in caplog.text
    assert 'PasswordCallback' in caplog.text
    assert 'Browser console error' in caplog.text
    assert 'private' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [[], {'code': 'private-code', 'callbacks': 'private-body'}])
async def test_malformed_diagnostics_are_ignored(payload, caplog):
    from login import _log_authenticate_response
    caplog.set_level('DEBUG')
    await _log_authenticate_response(SimpleNamespace(status=200, json=AsyncMock(return_value=payload)))
    assert 'private' not in caplog.text
