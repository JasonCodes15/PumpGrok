"""onchain.lp_status: curve stage and, once graduated, DAMM v2 lock/burn.

An open bonding curve has no LP to lock. A completed curve is read from the
Meteora DAMM v2 pool (permanent lock on the pool, burn from position NFTs).
Lock or burn left unset means that half could not be read. DAMM v1 and DLMM
are not decoded; a missing v2 pool does not invent a lock percent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.pool_read import read_market_pool
from pumpgrok.tools.onchain.rpc import RpcClient


class LpStatusIn(ToolInput):
    """Liquidity stage for ``mint`` from its curve account."""

    mint: MintStr
    curve_account: MintStr


class LpStatusOut(ToolOutput):
    """Stage plus completion flag; LP-mint fields are unwired (null)."""

    stage: Literal["bonding_curve", "graduated", "unknown"]
    curve_complete: bool | None = None
    lp_mint: str | None = None
    lp_burned_pct: float | None = None
    lp_locked_pct: float | None = None
    locker: str | None = None
    lock_expires_at: datetime | None = None
    checked: list[str] = Field(default_factory=list)


LP_STATUS_SPEC = ToolSpec(
    name="onchain.lp_status",
    description=(
        "Liquidity stage from the bonding curve, plus DAMM v2 permanent "
        "lock and position-NFT burn once the curve is complete."
    ),
    input_model=LpStatusIn,
    output_model=LpStatusOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/curve_layout.py via solana_rpc getAccountInfo",
)


async def run_lp_status(inp: LpStatusIn, rpc: RpcClient) -> dict[str, Any]:
    """Read the curve or DAMM v2 pool and return a validated payload."""
    try:
        market = await read_market_pool(rpc, inp.mint, inp.curve_account)
    except DataStale as exc:
        raise DataStale(f"onchain.lp_status unreadable mint={inp.mint}") from exc
    burned = None if market.lock is None else market.lock.burned_pct
    locked = None if market.lock is None else market.lock.locked_pct
    checked = ["curve_account"]
    if market.lock is not None:
        checked.extend(market.lock.checked)
    stage = "graduated" if market.graduated else "bonding_curve"
    out = {
        "stage": stage,
        "curve_complete": market.graduated,
        "lp_mint": None if market.pool is None else market.pool.address,
        "lp_burned_pct": burned,
        "lp_locked_pct": locked,
        "locker": "meteora-damm-v2" if market.kind == "damm_v2" else None,
        "lock_expires_at": None,
        "checked": checked,
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return LpStatusOut.model_validate(out).model_dump()


def build_lp_status_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.lp_status`` to one injected RPC client."""

    @tool(LP_STATUS_SPEC)
    async def _lp_status(payload: LpStatusIn) -> dict[str, Any]:
        return await run_lp_status(payload, rpc)

    return _lp_status
