"""One read path for bonding curves and DAMM v2 pools.

``onchain.lp_status`` and ``market.liquidity`` both call
:func:`read_market_pool`. A standard curve decodes in place. A completed curve,
or an account that is not a curve, is resolved through DAMM v2 discovery.
DAMM v1 and DLMM are not read; when no v2 pool can be decoded the result is
unreadable rather than a guessed reserve.
"""

from __future__ import annotations

from dataclasses import dataclass

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.curve_layout import (
    CurveDecodeError,
    CurveState,
    decode_curve_account,
)
from pumpgrok.tools.onchain.damm_v2 import (
    DAMM_V2_PROGRAM,
    DammPool,
    LpLockRead,
    account_data,
    decode_damm_pool,
    discover_damm_pools,
    fill_vault_amounts,
    read_lp_lock,
    select_pool,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


@dataclass(frozen=True, slots=True)
class MarketPool:
    """What the liquidity and LP readers share."""

    kind: str
    curve: CurveState | None
    pool: DammPool | None
    lock: LpLockRead | None
    real_sol_lamports: int | None
    creator: str | None

    @property
    def graduated(self) -> bool:
        if self.kind == "damm_v2":
            return True
        return bool(self.curve and self.curve.complete)


async def _account(rpc: RpcClient, address: str) -> tuple[bytes, str] | None:
    try:
        result = await rpc.call("getAccountInfo", [address, {"encoding": "base64"}])
    except RpcError as exc:
        raise DataStale("pool account unreadable") from exc
    return account_data(result)


async def _resolve_damm(
    rpc: RpcClient, mint: str, hinted: DammPool | None
) -> tuple[DammPool, LpLockRead]:
    pool = hinted
    if pool is not None and mint not in (pool.token_a_mint, pool.token_b_mint):
        pool = None
    if pool is None:
        pools = await discover_damm_pools(rpc, mint)
        pool = select_pool(pools)
    if pool is None:
        raise DataStale(f"no readable DAMM v2 pool mint={mint}")
    pool = await fill_vault_amounts(rpc, pool)
    if pool.sol_side() is None:
        raise DataStale(f"DAMM v2 pool is not SOL-quoted mint={mint}")
    lock = await read_lp_lock(rpc, pool)
    return pool, lock


async def read_market_pool(rpc: RpcClient, mint: str, account: str) -> MarketPool:
    """Read ``account``, then a DAMM v2 pool when the curve is complete or absent.

    Raises :class:`DataStale` when neither a known curve nor a SOL-quoted v2
    pool can be read. An open curve with zero real SOL is a successful read.
    """
    fetched = await _account(rpc, account)
    if fetched is None:
        raise DataStale(f"pool account missing mint={mint}")
    raw, owner = fetched
    curve: CurveState | None = None
    if owner != DAMM_V2_PROGRAM:
        try:
            curve = decode_curve_account(raw)
        except CurveDecodeError:
            curve = None
    if curve is not None and not curve.complete:
        return _open_curve(mint, curve)
    hinted = _hinted_pool(raw, owner, account)
    if curve is not None and curve.complete:
        return await _graduated_curve(rpc, mint, curve, hinted)
    pool, lock = await _resolve_damm(rpc, mint, hinted)
    return _damm_market(pool, lock, curve=None, creator=None)


def _open_curve(mint: str, curve: CurveState) -> MarketPool:
    if not curve.quote_is_sol():
        raise DataStale(f"curve quote is not SOL mint={mint}")
    return MarketPool(
        kind="bonding_curve",
        curve=curve,
        pool=None,
        lock=None,
        real_sol_lamports=curve.real_sol_reserves_lamports,
        creator=curve.creator,
    )


def _hinted_pool(raw: bytes, owner: str, account: str) -> DammPool | None:
    if owner != DAMM_V2_PROGRAM:
        return None
    try:
        return decode_damm_pool(raw, address=account)
    except ValueError:
        return None


async def _graduated_curve(
    rpc: RpcClient, mint: str, curve: CurveState, hinted: DammPool | None
) -> MarketPool:
    try:
        pool, lock = await _resolve_damm(rpc, mint, hinted)
    except DataStale:
        if not curve.quote_is_sol():
            raise
        return MarketPool(
            kind="bonding_curve",
            curve=curve,
            pool=None,
            lock=None,
            real_sol_lamports=None,
            creator=curve.creator,
        )
    return _damm_market(pool, lock, curve=curve, creator=curve.creator)


def _damm_market(
    pool: DammPool, lock: LpLockRead, *, curve: CurveState | None, creator: str | None
) -> MarketPool:
    side = pool.sol_side()
    return MarketPool(
        kind="damm_v2",
        curve=curve,
        pool=pool,
        lock=lock,
        real_sol_lamports=None if side is None else side[1],
        creator=creator,
    )
