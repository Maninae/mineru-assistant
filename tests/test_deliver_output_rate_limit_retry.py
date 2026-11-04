"""deliver-output.py retries a Telegram HTTP 429 once, honoring `retry_after`.

Every test drives `send_telegram` through a fake `urlopen` that replays a scripted
response sequence, and patches `time.sleep` so no test actually waits.
"""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "deliver-output.py"
module_spec = importlib.util.spec_from_file_location("deliver_output_rate_limit", MODULE_PATH)
deliver_output = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(deliver_output)


class FakeTelegramResponse(io.BytesIO):
    """Context-manager body that urlopen returns on success."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def make_telegram_http_error(status_code: int, error_body: dict) -> urllib.error.HTTPError:
    """Build an HTTPError whose `.read()` returns the given JSON body."""
    return urllib.error.HTTPError(
        "https://api.telegram.org/botX/sendMessage", status_code, "err", {},
        io.BytesIO(json.dumps(error_body).encode()),
    )


def run_send_telegram_against_response_sequence(scripted_responses: list):
    """Call send_telegram once; return (ok, urlopen call count, sleep calls)."""
    remaining_responses = list(scripted_responses)
    urlopen_call_count = 0

    def fake_urlopen(req, timeout=None):
        nonlocal urlopen_call_count
        urlopen_call_count += 1
        next_response = remaining_responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return FakeTelegramResponse(json.dumps(next_response).encode())

    with mock.patch.object(deliver_output.urllib.request, "urlopen", fake_urlopen), \
            mock.patch.object(deliver_output.time, "sleep") as fake_sleep:
        delivered_ok = deliver_output.send_telegram("hello", "TOKEN", "123")
    return delivered_ok, urlopen_call_count, [call.args[0] for call in fake_sleep.call_args_list]


RATE_LIMITED_BODY = {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}


def test_429_then_200_delivers_after_retry_after_sleep():
    delivered_ok, calls, sleeps = run_send_telegram_against_response_sequence(
        [make_telegram_http_error(429, RATE_LIMITED_BODY), {"ok": True}]
    )
    assert delivered_ok is True
    assert calls == 2
    assert sleeps == [7]


def test_429_twice_falls_back_to_failure():
    delivered_ok, calls, sleeps = run_send_telegram_against_response_sequence(
        [make_telegram_http_error(429, RATE_LIMITED_BODY), make_telegram_http_error(429, RATE_LIMITED_BODY)]
    )
    assert delivered_ok is False
    assert calls == 2
    assert sleeps == [7]


def test_retry_after_is_capped_and_defaulted():
    huge_wait_body = {"ok": False, "parameters": {"retry_after": 3600}}
    assert deliver_output.parse_telegram_retry_after_seconds(json.dumps(huge_wait_body)) == 60
    assert deliver_output.parse_telegram_retry_after_seconds('{"ok": false}') == 5
    assert deliver_output.parse_telegram_retry_after_seconds("not json") == 5


def test_non_429_error_is_not_retried():
    delivered_ok, calls, sleeps = run_send_telegram_against_response_sequence(
        [make_telegram_http_error(400, {"ok": False}), {"ok": True}]
    )
    assert delivered_ok is False
    assert calls == 1
    assert sleeps == []
