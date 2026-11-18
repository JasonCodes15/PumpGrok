"""RPC discipline for TOOL-002: retry predicate, policy values, decode, redaction.

httpx.MockTransport fixtures only (B13: no respx). WallClock with a tiny
policy keeps retry sleeps in the tens of milliseconds; no test asserts on
elapsed time. No test opens a socket.
"""

from __future__ import annotations

import io
import logging
import random
import sys
import traceback
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import httpx
import pytest
from pydantic import SecretStr

from pumpgrok.core.errors import DataStale
from pumpgrok.core.tool import RetryPolicy
from pumpgrok.logging import setup_logging
from pumpgrok.replay.clock import WallClock
from pumpgrok.tools.onchain.rpc import (
    RpcClient,
    RpcError,
    _is_helius_host,
    _retriable_rpc_error,
    _SeededWait,
)

# Placeholder-shaped and short, so the secrets gate does not treat it as a credential.
HELIUS_KEY = "fake_helius_key"

TINY = RetryPolicy(attempts=3, base_s=0.01, max_s=0.02)


def _rpc(
    handler: object,
    endpoints: tuple[str, ...] = ("https://rpc.invalid",),
    *,
    retry: RetryPolicy | None = TINY,
    api_key: SecretStr | None = None,
    auth: str = "auto",
    seed: int = 7,
) -> RpcClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport)
    return RpcClient(
        list(endpoints),
        client,
        WallClock(),
        rng=random.Random(seed),
        retry=retry,
        api_key=api_key,
        auth=auth,
    )


def _query_key(request: httpx.Request) -> str | None:
    values = parse_qs(urlsplit(str(request.url)).query).get("api-key")
    if not values:
        return None
    return values[0]


async def test_4xx_fails_over_without_retry_delay() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": {"code": -32601, "message": "nope"}})

    rpc = _rpc(handler, endpoints=("https://rpc.invalid/a", "https://rpc.invalid/b"))
    with pytest.raises(RpcError, match="rpc failed method=getFoo endpoints=2"):
        await rpc.call("getFoo", [])
    assert len(calls) == 2  # one attempt per endpoint, no retries


async def test_single_4xx_endpoint_raises_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": -32601, "message": "nope"}})

    rpc = _rpc(handler)
    with pytest.raises(RpcError) as excinfo:
        await rpc.call("getFoo", [])
    assert "rpc.invalid" not in str(excinfo.value)


async def test_500_retries_then_fails_over_in_order() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 42})

    rpc = _rpc(handler, endpoints=("https://rpc.invalid/a", "https://rpc.invalid/b"))
    assert await rpc.call("getFoo", []) == 42
    assert [u.rsplit("/", 1)[-1] for u in calls] == ["a", "a", "a", "b"]


async def test_429_retried_per_policy() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = _rpc(handler)
    assert await rpc.call("getFoo", []) == "ok"
    assert calls == 3


async def test_timeout_retried_then_typed_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("too slow")

    rpc = _rpc(handler, retry=RetryPolicy(attempts=2, base_s=0.01, max_s=0.02))
    with pytest.raises(RpcError, match="rpc failed"):
        await rpc.call("getFoo", [])
    assert calls == 2


async def test_rpc_error_object_fails_over_immediately() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "fine"})

    rpc = _rpc(handler, endpoints=("https://rpc.invalid/a", "https://rpc.invalid/b"))
    assert await rpc.call("getFoo", []) == "fine"
    assert [u.rsplit("/", 1)[-1] for u in calls] == ["a", "b"]


async def test_malformed_body_yields_data_stale() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    rpc = _rpc(handler)
    with pytest.raises(DataStale, match="rpc bad body"):
        await rpc.call("getFoo", [])


async def test_malformed_first_endpoint_fails_over_to_good() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "recovered"})

    rpc = _rpc(handler, endpoints=("https://rpc.invalid/a", "https://rpc.invalid/b"))
    assert await rpc.call("getFoo", []) == "recovered"


async def test_api_key_travels_in_header_only() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = _rpc(handler, api_key=SecretStr("s3cret"))
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["header"] == "s3cret"
    assert "s3cret" not in seen["url"]


async def test_no_key_sends_no_auth_header() -> None:
    seen: dict[str, bool] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["has_header"] = "x-api-key" in request.headers
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = _rpc(handler)
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["has_header"] is False


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})


async def test_helius_key_is_query_parameter_not_header() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query_key(request)
        seen["header"] = request.headers.get("x-api-key")
        seen["stored"] = str(request.url).split("?", 1)[0]
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com",),
        api_key=SecretStr(HELIUS_KEY),
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["query"] == HELIUS_KEY
    assert seen["header"] is None
    assert seen["stored"] == "https://mainnet.helius-rpc.com"
    assert HELIUS_KEY not in repr(rpc)
    assert HELIUS_KEY not in str(rpc)


async def test_helius_devnet_and_apex_host_use_query_auth() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = urlsplit(str(request.url)).hostname or ""
        hosts.append(host)
        assert _query_key(request) == HELIUS_KEY
        assert "x-api-key" not in request.headers
        if host == "devnet.helius-rpc.com":
            return httpx.Response(400, json={"error": {"code": -32601}})
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://devnet.helius-rpc.com/path", "https://helius-rpc.com"),
        api_key=SecretStr(HELIUS_KEY),
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert hosts == ["devnet.helius-rpc.com", "helius-rpc.com"]


async def test_lookalike_helius_host_keeps_header_auth() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query_key(request)
        seen["header"] = request.headers.get("x-api-key")
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com.evil.example",),
        api_key=SecretStr(HELIUS_KEY),
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["query"] is None
    assert seen["header"] == HELIUS_KEY


async def test_auto_does_not_send_helius_key_to_failover_host() -> None:
    seen: list[tuple[str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = urlsplit(str(request.url)).hostname or ""
        seen.append((host, _query_key(request), request.headers.get("x-api-key")))
        if host.endswith("helius-rpc.com"):
            return httpx.Response(500, json={"error": "down"})
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=(
            "https://mainnet.helius-rpc.com",
            "https://api.mainnet-beta.solana.com",
        ),
        api_key=SecretStr(HELIUS_KEY),
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert seen[0][0] == "mainnet.helius-rpc.com"
    assert seen[0][1] == HELIUS_KEY
    assert seen[0][2] is None
    assert seen[-1][0] == "api.mainnet-beta.solana.com"
    assert seen[-1][1] is None
    assert seen[-1][2] is None
    assert all(item[1] != HELIUS_KEY or item[0].endswith("helius-rpc.com") for item in seen)


async def test_header_mode_stays_on_header_for_helius() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query_key(request)
        seen["header"] = request.headers.get("x-api-key")
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com",),
        api_key=SecretStr(HELIUS_KEY),
        auth="header",
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["query"] is None
    assert seen["header"] == HELIUS_KEY


async def test_helius_mode_uses_query_on_custom_host() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = _query_key(request)
        seen["header"] = request.headers.get("x-api-key")
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://rpc.example.test/solana",),
        api_key=SecretStr(HELIUS_KEY),
        auth="helius",
    )
    assert await rpc.call("getFoo", []) == "ok"
    assert seen["query"] == HELIUS_KEY
    assert seen["header"] is None


async def test_existing_query_is_kept_and_prior_api_key_replaced() -> None:
    planted = "prior-key"
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com/rpc?commitment=confirmed&api-key=" + planted,),
        api_key=SecretStr(HELIUS_KEY),
    )
    assert await rpc.call("getFoo", []) == "ok"
    query = parse_qs(urlsplit(seen["url"]).query)
    assert query["commitment"] == ["confirmed"]
    assert query["api-key"] == [HELIUS_KEY]
    assert planted not in seen["url"]
    assert query["api-key"].count(HELIUS_KEY) == 1


async def test_query_key_with_reserved_characters_stays_one_parameter() -> None:
    key = "fake&key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert _query_key(request) == key
        assert str(request.url).count("api-key=") == 1
        assert key not in str(request.url)
        return _ok(request)

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com",),
        api_key=SecretStr(key),
    )
    assert await rpc.call("getFoo", []) == "ok"


async def test_empty_key_sends_no_helius_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert _query_key(request) is None
        assert "x-api-key" not in request.headers
        return _ok(request)

    rpc = _rpc(handler, endpoints=("https://mainnet.helius-rpc.com",), api_key=SecretStr(""))
    assert await rpc.call("getFoo", []) == "ok"


async def test_helius_failure_redacts_key_from_errors_and_logs() -> None:
    buffer = io.StringIO()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    setup_logging(mode="paper", profile="paper", environ={}, stream=buffer)
    root.setLevel(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        assert _query_key(request) == HELIUS_KEY
        return httpx.Response(401, text=f"rejected {HELIUS_KEY}")

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com",),
        api_key=SecretStr(HELIUS_KEY),
    )
    try:
        with pytest.raises(RpcError) as excinfo:
            await rpc.call("getFoo", [])
        rendered = "".join(traceback.format_exception(excinfo.value))
        assert HELIUS_KEY not in str(excinfo.value)
        assert HELIUS_KEY not in rendered
        assert "mainnet.helius-rpc.com" not in str(excinfo.value)
        logged = buffer.getvalue()
        assert HELIUS_KEY not in logged
    finally:
        for handler_ in list(root.handlers):
            if handler_ not in saved_handlers:
                root.removeHandler(handler_)
        root.setLevel(saved_level)


async def test_connection_error_fails_over_without_retry() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("refused")

    rpc = _rpc(handler, endpoints=("https://rpc.invalid/a", "https://rpc.invalid/b"))
    with pytest.raises(RpcError, match="rpc failed method=getFoo endpoints=2"):
        await rpc.call("getFoo", [])
    assert calls == ["/a", "/b"]


async def test_redirect_is_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://collector.example/steal"})

    rpc = _rpc(
        handler,
        endpoints=("https://mainnet.helius-rpc.com",),
        api_key=SecretStr(HELIUS_KEY),
    )
    with pytest.raises(RpcError) as excinfo:
        await rpc.call("getFoo", [])
    assert len(calls) == 1
    assert "collector.example" not in calls[0]
    assert HELIUS_KEY not in str(excinfo.value)


async def test_non_object_result_is_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["raw"])

    rpc = _rpc(handler)
    assert await rpc.call("getFoo", []) == ["raw"]


def test_empty_endpoint_list_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    with pytest.raises(RpcError, match="no endpoints"):
        _rpc(handler, endpoints=())


def test_blank_host_is_not_helius() -> None:
    assert _is_helius_host("not a url") is False
    assert _is_helius_host("https://") is False


def test_httpx_errors_keep_retry_classification() -> None:
    assert _retriable_rpc_error(httpx.ConnectTimeout("slow")) is True
    request = httpx.Request("POST", "https://rpc.invalid")
    limited = httpx.Response(429, request=request)
    assert (
        _retriable_rpc_error(httpx.HTTPStatusError("limited", request=request, response=limited))
        is True
    )
    rejected = httpx.Response(400, request=request)
    assert (
        _retriable_rpc_error(httpx.HTTPStatusError("rejected", request=request, response=rejected))
        is False
    )
    assert _retriable_rpc_error(RuntimeError("other")) is False


def test_unknown_auth_is_refused_without_echo() -> None:
    planted = "fake_helius_key"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    with pytest.raises(RpcError, match="rpc misconfigured: auth") as excinfo:
        _rpc(handler, api_key=SecretStr(planted), auth=planted)
    assert planted not in str(excinfo.value)


def test_seeded_wait_uses_policy_bounds() -> None:
    wait = _SeededWait(random.Random(0), base=0.5, cap=4.0)
    first = wait(types.SimpleNamespace(attempt_number=1))
    assert 0.5 <= first <= 1.0
    second = wait(types.SimpleNamespace(attempt_number=2))
    assert 1.0 <= second <= 2.0
    late = wait(types.SimpleNamespace(attempt_number=10))
    assert 4.0 <= late <= 8.0
