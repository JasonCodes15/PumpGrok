"""market.liquidity: pool depth from real reserves (TOOL-001, B9).

``liquidity_lamports`` is the real SOL in the pool: the bonding curve's real
quote reserve, or the WSOL side of a Meteora DAMM v2 pool. Virtual reserves
are reported on their own fields and are not the liquidity figure, and they
are not what ``order_to_liquidity_ratio`` or the safety depth check compare
to the floor. Spot impact on an open curve still uses the virtual pair,
because that is the constant-product price. A graduated curve or a v2 pool
uses the real SOL reserve for impact. DAMM v1 and DLMM are not read; an
unreadable pool is ``DataStale``, never a guessed depth.

``creator_launches`` uses :func:`count_creator_launches`, the same count as
``onchain.deployer_history``. ``None`` means the scan was not run or could
not be read. Zero means the scan succeeded and found no curves.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J4, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.curve_layout import curve_progress
from pumpgrok.tools.onchain.launches import count_creator_launches
from pumpgrok.tools.onchain.pool_read import read_market_pool
from pumpgrok.tools.onchain.rpc import RpcClient


class LiquidityIn(ToolInput):
    """Depth for ``mint`` plus impact for a hypothetical order size."""

    mint: MintStr
    curve_account: MintStr
    order_size_lamports: int = Field(default=0, ge=0)


class LiquidityOut(ToolOutput):
    """Reserves and impact ratios. ``liquidity_lamports`` is real SOL."""

    virtual_sol_reserves_lamports: int = Field(ge=0)
    virtual_token_reserves_units: int = Field(ge=0)
    real_sol_reserves_lamports: int = Field(ge=0)
    liquidity_lamports: int = Field(ge=0)
    liquidity_basis: Literal["bonding_curve_real", "damm_v2_quote"]
    curve_progress: Decimal = Field(ge=0, le=1)
    price_impact: Decimal = Field(ge=0)
    order_to_liquidity_ratio: Decimal = Field(ge=0)
    graduated: bool
    creator: str | None = None
    creator_launches: int | None = Field(default=None, ge=0)


LIQUIDITY_SPEC = ToolSpec(
    name="market.liquidity",
    description=(
        "Pool depth from real SOL reserves (bonding curve or DAMM v2). "
        "Virtual reserves are labelled separately and are not the liquidity "
        "figure compared to the floor."
    ),
    input_model=LiquidityIn,
    output_model=LiquidityOut,
    side_effect=SideEffect.READ,
    timeout_s=10.0,
    retry=J4,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/curve_layout.py via solana_rpc getAccountInfo",
)


def _ratio(order: Decimal, real: Decimal) -> Decimal:
    """Order divided by real SOL. Zero real SOL yields 0; the depth check uses the lamports."""
    if real > 0:
        return order / real
    return Decimal(0)


async def run_liquidity(inp: LiquidityIn, rpc: RpcClient) -> dict[str, Any]:
    """Read real SOL depth. Virtual reserves stay on their own fields."""
    try:
        market = await read_market_pool(rpc, inp.mint, inp.curve_account)
    except DataStale as exc:
        raise DataStale(f"market.liquidity unreadable mint={inp.mint}") from exc
    if market.real_sol_lamports is None:
        raise DataStale(f"market.liquidity real reserves unreadable mint={inp.mint}")
    real = market.real_sol_lamports
    order = Decimal(inp.order_size_lamports)
    real_dec = Decimal(real)
    curve = market.curve
    if market.kind == "bonding_curve" and curve is not None:
        virtual_sol = curve.virtual_sol_reserves_lamports
        virtual_token = curve.virtual_token_reserves_units
        progress = curve_progress(curve)
        basis = "bonding_curve_real"
        impact_base = Decimal(virtual_sol)
    else:
        virtual_sol = 0
        virtual_token = 0
        progress = Decimal(1) if market.graduated else Decimal(0)
        basis = "damm_v2_quote"
        impact_base = real_dec
    impact = order / (impact_base + order) if order > 0 and impact_base > 0 else Decimal(0)
    launches: int | None = None
    if market.creator is not None:
        try:
            launches = await count_creator_launches(rpc, market.creator)
        except DataStale:
            launches = None
    out = {
        "virtual_sol_reserves_lamports": virtual_sol,
        "virtual_token_reserves_units": virtual_token,  # pragma: allowlist secret: field
        "real_sol_reserves_lamports": real,
        "liquidity_lamports": real,
        "liquidity_basis": basis,
        "curve_progress": progress,
        "price_impact": impact,
        "order_to_liquidity_ratio": _ratio(order, real_dec),
        "graduated": market.graduated,
        "creator": market.creator,
        "creator_launches": launches,
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return LiquidityOut.model_validate(out).model_dump()


def build_liquidity_tool(rpc: RpcClient) -> Tool:
    """Bind ``market.liquidity`` to one injected RPC client (no globals)."""

    @tool(LIQUIDITY_SPEC)
    async def _liquidity(payload: LiquidityIn) -> dict[str, Any]:
        return await run_liquidity(payload, rpc)

    return _liquidity
