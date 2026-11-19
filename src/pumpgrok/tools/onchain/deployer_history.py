"""onchain.deployer_history: creator launch history from RPC (TOOL-002).

WHALE-only grant (R35); ``safety.score_report`` composes it in-process for
RUG. ``tokens_seen`` is the pump.fun launch count from
:func:`pumpgrok.tools.onchain.launches.count_creator_launches`: bonding-curve
accounts whose creator field is this wallet. It is not a signature count.
``recent_signatures`` is still the signature window, kept as evidence of
activity, and is not the launch count. ``rugs`` and ``worst_pnl_pct`` stay
unwired. A failed launch scan is ``DataStale`` (BLIND), never zero.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now, utcnow
from pumpgrok.tools.onchain.launches import count_creator_launches
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

_SIG_LIMIT = 100
_DAY_S = 86_400


class DeployerHistoryIn(ToolInput):
    """History for ``creator`` over the trailing ``lookback_days``."""

    creator: MintStr
    lookback_days: int = Field(default=30, ge=1, le=90)


class DeployerHistoryOut(ToolOutput):
    """Signature-window history; adjudicated fields stay unwired."""

    tokens_seen: int = Field(ge=0)
    tokens_bought: int = Field(ge=0)
    rugs: int = Field(ge=0)
    worst_pnl_pct: Decimal | None = None
    is_known_bad: bool = False
    first_seen_at: datetime | None = None
    recent_signatures: list[str] = Field(default_factory=list)


DEPLOYER_HISTORY_SPEC = ToolSpec(
    name="onchain.deployer_history",
    description=(
        "Creator launch count: pump.fun bonding curves whose creator field "
        "is this wallet. Signature rows are evidence, not the count."
    ),
    input_model=DeployerHistoryIn,
    output_model=DeployerHistoryOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/deployer_history.py via getSignaturesForAddress",
)


async def run_deployer_history(inp: DeployerHistoryIn, rpc: RpcClient) -> dict[str, Any]:
    """Count launches, then attach the signature window as evidence."""
    try:
        launches = await count_creator_launches(rpc, inp.creator)
    except DataStale as exc:
        raise DataStale(
            f"onchain.deployer_history launches unreadable creator={inp.creator}"
        ) from exc
    try:
        sigs = await rpc.call("getSignaturesForAddress", [inp.creator, {"limit": _SIG_LIMIT}])
    except RpcError as exc:
        raise DataStale(f"onchain.deployer_history upstream failed creator={inp.creator}") from exc
    if not isinstance(sigs, list):
        raise DataStale(f"onchain.deployer_history malformed creator={inp.creator}")
    cutoff = utcnow(rpc.clock).timestamp() - inp.lookback_days * _DAY_S
    kept: list[dict[str, Any]] = []
    for entry in sigs:
        if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
            continue
        block_time = entry.get("blockTime")
        if isinstance(block_time, int) and block_time < cutoff:
            continue
        kept.append(entry)
    stamps = [e["blockTime"] for e in kept if isinstance(e.get("blockTime"), int)]
    out = {
        "tokens_seen": launches,
        "tokens_bought": 0,
        "rugs": 0,
        "worst_pnl_pct": None,
        "is_known_bad": False,
        "first_seen_at": datetime.fromtimestamp(min(stamps), UTC) if stamps else None,
        "recent_signatures": [e["signature"] for e in kept[:10]],
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return DeployerHistoryOut.model_validate(out).model_dump()


def build_deployer_history_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.deployer_history`` to one injected RPC client."""

    @tool(DEPLOYER_HISTORY_SPEC)
    async def _deployer_history(payload: DeployerHistoryIn) -> dict[str, Any]:
        return await run_deployer_history(payload, rpc)

    return _deployer_history
