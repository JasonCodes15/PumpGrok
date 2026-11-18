"""Solana JSON-RPC client: one injected httpx client, ordered failover (STRUCT-002).

Tenacity ``AsyncRetrying`` drives retries with ``sleep=clock.sleep`` and a
seeded wait, so replays reproduce every delay from the fixture alone (R27).
Endpoints stay keyless. A paid-provider key travels in ``api_key`` and is
applied when the request is sent. A Helius host (``*.helius-rpc.com``) receives
it as the documented ``api-key`` query parameter. Any other host receives it
as an ``x-api-key`` header, unless ``auth`` selects one mode for the whole
list. The key is not stored on the endpoint.

Nothing raised or logged here carries an endpoint URL, a header value, a
query value, or ``str(exc)`` from httpx: errors name the method and the
endpoint count only (T-02-01, T-02-02). Redirects are not followed, so a
keyed URL is not forwarded to another host.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
)

from pumpgrok.core.errors import DataStale
from pumpgrok.core.tool import RetryPolicy
from pumpgrok.replay.clock import Clock
from pumpgrok.replay.rng import make_rng

RpcAuth = Literal["auto", "header", "helius"]
_HELIUS_HOST = "helius-rpc.com"


class RpcError(RuntimeError):
    """Redacted RPC failure: method plus endpoint count, never URLs or keys."""


def _require_auth(auth: str) -> RpcAuth:
    """Return ``auth`` when it is a known mode. The value is never echoed."""
    if auth in {"auto", "header", "helius"}:
        return cast(RpcAuth, auth)
    raise RpcError("rpc misconfigured: auth")


class _Failover(Exception):
    """Internal: an RPC-level error on this endpoint; try the next one."""


class _BadBody(Exception):
    """Internal: an endpoint returned malformed JSON; try the next one."""


def _is_helius_host(endpoint: str) -> bool:
    """True for the documented Helius RPC hosts, and no lookalike suffix."""
    host = urlsplit(endpoint).hostname
    if not host:
        return False
    normalized = host.lower().rstrip(".")
    return normalized == _HELIUS_HOST or normalized.endswith("." + _HELIUS_HOST)


def _with_api_key(endpoint: str, key: str) -> str:
    """Return ``endpoint`` with one ``api-key`` query parameter set to ``key``.

    Any ``api-key`` already on the URL is dropped first. The fragment is
    dropped too: it is not sent, and it must not keep a second copy of a key.
    """
    parts = urlsplit(endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() != "api-key"
    ]
    query.append(("api-key", key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _retriable_rpc_error(exc: BaseException) -> bool:
    """Retry only timeouts, HTTP 5xx and 429 (catalog section 0).

    4xx (including unknown-method -32601 surfaced as HTTP), redirects and
    connection errors fail over to the next endpoint immediately, without
    retry delay.
    """
    if isinstance(exc, _TransportFailure):
        return exc.retry
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status <= 599
    return False


class _TransportFailure(Exception):
    """Redacted transport failure. ``str`` is a fixed kind, never a URL or key."""

    def __init__(self, kind: str, *, retry: bool) -> None:
        super().__init__(kind)
        self.retry = retry


def _transport_failure(exc: httpx.HTTPError) -> _TransportFailure:
    """Map an httpx error to a redacted failure. The original text is discarded."""
    if isinstance(exc, httpx.TimeoutException):
        return _TransportFailure("rpc timeout", retry=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry = status == 429 or 500 <= status <= 599
        return _TransportFailure(f"rpc http {status}", retry=retry)
    return _TransportFailure("rpc connection", retry=False)


class _SeededWait:
    """Exponential backoff with seeded jitter (never global ``random``).

    Tenacity ``wait_random_exponential`` draws from the global ``random``
    module, which breaks replay determinism (R27). This wait draws from the
    injected ``random.Random`` instead. Tenacity calls it with one
    ``RetryCallState`` argument and uses the returned float as seconds.
    """

    def __init__(
        self, rng: random.Random, base: float = 0.1, factor: float = 2.0, cap: float = 5.0
    ) -> None:
        self._rng = rng
        self._base = base
        self._factor = factor
        self._cap = cap

    def __call__(self, retry_state: Any) -> float:
        attempt = max(int(getattr(retry_state, "attempt_number", 1)), 1)
        ceiling = min(self._base * (self._factor ** (attempt - 1)), self._cap)
        return ceiling + self._rng.uniform(0, ceiling)


class RpcClient:
    """Ordered-failover JSON-RPC over an injected httpx client."""

    def __init__(
        self,
        endpoints: Sequence[str],
        client: httpx.AsyncClient,
        clock: Clock,
        rng: random.Random | None = None,
        *,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 15.0,
        max_attempts_per_endpoint: int = 3,
        retry: RetryPolicy | None = None,
        auth: str = "auto",
    ) -> None:
        if not endpoints:
            raise RpcError("rpc misconfigured: no endpoints")
        self._endpoints = list(endpoints)
        self._client = client
        self._clock = clock
        # INT-05: production composition roots always pass an explicit rng seeded
        # from the composition-root seed; the default below is test-only.
        self._rng = rng if rng is not None else make_rng()
        self._api_key = api_key if api_key is not None else SecretStr("")
        self._auth = _require_auth(auth)
        self._helius_in_list = any(_is_helius_host(endpoint) for endpoint in self._endpoints)
        self._timeout_seconds = timeout_seconds
        if retry is None:
            retry = RetryPolicy(
                attempts=max(1, min(max_attempts_per_endpoint, 10)),
                base_s=0.1,
                max_s=5.0,
            )
        self._retry = retry

    @property
    def clock(self) -> Clock:
        """The injected replay clock (envelope stamps read this, never wall time)."""
        return self._clock

    @property
    def endpoint_count(self) -> int:
        return len(self._endpoints)

    def __repr__(self) -> str:
        return f"RpcClient(endpoints={self.endpoint_count}, auth={self._auth!r})"

    def _request_parts(self, endpoint: str) -> tuple[str, dict[str, str]]:
        """URL and headers for one send. The stored endpoint list is not changed.

        ``auto`` uses the Helius query parameter on a Helius host and the
        header elsewhere. A list that already contains a Helius host does not
        attach that key to the other hosts. ``header`` always sends
        ``x-api-key``. ``helius`` always sends the query parameter, including
        on a custom hostname that fronts Helius.
        """
        key = self._api_key.get_secret_value()
        if not key:
            return endpoint, {}
        if self._auth == "auto" and self._helius_in_list and not _is_helius_host(endpoint):
            return endpoint, {}
        if self._auth == "helius" or (self._auth == "auto" and _is_helius_host(endpoint)):
            return _with_api_key(endpoint, key), {}
        return endpoint, {"x-api-key": key}

    async def _post_once(self, endpoint: str, method: str, params: list[Any]) -> Any:
        url, headers = self._request_parts(endpoint)
        try:
            resp = await self._client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _transport_failure(exc) from None
        try:
            data = resp.json()
        except ValueError:
            raise _BadBody(f"rpc bad body method={method}") from None
        if isinstance(data, dict) and "error" in data:
            raise RpcError(f"rpc error for method={method}")
        if isinstance(data, dict):
            return data.get("result")
        return data

    async def call(self, method: str, params: list[Any]) -> Any:
        """POST ``method`` to each endpoint in order; first success wins.

        Timeouts, HTTP 5xx and 429 retry per endpoint from the calling tool's
        RetryPolicy (j4/j5/j8); 4xx and connection errors fail over immediately.
        An RPC-level ``error`` fails over immediately; a malformed body fails
        over and becomes :class:`DataStale` when every endpoint is malformed.
        Failures raise :class:`RpcError` with no URL, header, or exception text.
        """
        bad_bodies = 0
        for endpoint in self._endpoints:
            try:
                async for attempt in AsyncRetrying(
                    sleep=self._clock.sleep,
                    wait=_SeededWait(self._rng, base=self._retry.base_s, cap=self._retry.max_s),
                    stop=stop_after_attempt(self._retry.attempts),
                    retry=retry_if_exception(_retriable_rpc_error),
                    reraise=True,
                ):
                    with attempt:
                        try:
                            return await self._post_once(endpoint, method, params)
                        except RpcError as exc:
                            raise _Failover from exc
            except _Failover:
                continue
            except _BadBody:
                bad_bodies += 1
                continue
            except (_TransportFailure, httpx.HTTPError, RetryError):
                continue
        if bad_bodies == len(self._endpoints):
            raise DataStale(f"rpc bad body method={method} endpoints={len(self._endpoints)}")
        raise RpcError(f"rpc failed method={method} endpoints={len(self._endpoints)}") from None
