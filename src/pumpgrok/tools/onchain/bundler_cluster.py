"""onchain.bundler_cluster: launch-window insider cluster share (check 7).

Reads a new mint's earliest transactions through the shared :class:`RpcClient`
(``getSignaturesForAddress`` + ``getTransaction`` + ``getTokenSupply``). Flags
a cluster when several buyers share the create slot or the same transaction,
when buyers were funded by one wallet inside the funding window (one hop
back), or when a buyer was funded by the deployer. A lone deployer buy is the
normal pump.fun dev buy and is not a cluster.

The scan stops at ``bundler_max_rpc_calls`` and ``bundler_time_budget_s``.
On Helius, the oldest signatures come from one ``getTransactionsForAddress``
call (``sortOrder=asc``), so a mint with thousands of signatures still
reaches its launch. Other endpoints page ``getSignaturesForAddress`` at
limit 1000 until the start of history or the call budget. RPC failure, a
history that does not fit in that budget, an unreadable transaction, or an
exhausted time budget returns ``complete=False``. Callers must treat that
as BLIND. A finished scan with no flagged wallets is the only pass.

Standard JSON-RPC only. No enhanced transaction API and no new HTTP client.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now
from pumpgrok.tools.onchain.rpc import TRANSACTION_CONFIG, RpcClient, RpcError

_SIG_PAGE = 1000
_FUNDING_SIG_LIMIT = 8
_SKIPPED_FUNDING = "funding hop skipped; same-slot cluster already meets the fail threshold"


class _Blind(Exception):
    """The check cannot be finished. The reason is safe to surface."""

    def __init__(self, reason: str, *, code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class _Budget:
    """Counts logical RPC calls and stops at the call cap or the time budget."""

    def __init__(self, rpc: RpcClient, *, max_calls: int, time_budget_s: float) -> None:
        self._rpc = rpc
        self._left = max(0, max_calls)
        self._deadline = rpc.clock.monotonic() + max(0.0, time_budget_s)
        self.calls = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        """One logical RPC call. Retries stay inside :meth:`RpcClient.call`."""
        self._charge()
        try:
            return await self._rpc.call(method, params)
        except RpcError as exc:
            raise _Blind("rpc failed for bundler_cluster", code=exc.code) from None

    def _charge(self) -> None:
        if self._left <= 0:
            raise _Blind("rpc call budget exhausted")
        if self._rpc.clock.monotonic() >= self._deadline:
            raise _Blind("time budget exhausted")
        self._left -= 1
        self.calls += 1


@dataclass(frozen=True, slots=True)
class _Buyer:
    wallet: str
    post_units: int
    slot: int
    block_time: int
    signature: str


class ClusterMember(BaseModel):
    """One flagged wallet and the signals that put it in the cluster."""

    model_config = ConfigDict(frozen=True)

    wallet: str
    token_units: int = Field(ge=0)
    share: Decimal = Field(ge=0, le=1)
    slot: int = Field(ge=0)
    signals: list[str]
    funder: str | None = None
    signature: str


class BundlerClusterIn(ToolInput):
    """Launch-window cluster scan for ``mint``. ``creator`` is the deployer."""

    mint: MintStr
    creator: MintStr | None = None
    curve_account: MintStr | None = None


class BundlerClusterOut(ToolOutput):
    """Cluster share of supply. ``complete`` false is BLIND, never a pass."""

    complete: bool
    blind_reason: str | None = None
    cluster_share: Decimal = Field(ge=0, le=1)
    supply_units: int = Field(ge=0)
    flagged_wallets: int = Field(ge=0)
    signals: list[str] = Field(default_factory=list)
    members: list[ClusterMember] = Field(default_factory=list)
    rpc_calls: int = Field(ge=0)
    slot: int | None = None
    evidence_summary: str = ""


BUNDLER_CLUSTER_SPEC = ToolSpec(
    name="onchain.bundler_cluster",
    description=(
        "Launch-window insider cluster share from earliest mint transactions: "
        "same-slot buyers, one-hop shared funding, and deployer overlap. "
        "Incomplete data is blind, never a pass."
    ),
    input_model=BundlerClusterIn,
    output_model=BundlerClusterOut,
    side_effect=SideEffect.READ,
    timeout_s=12.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/bundler_cluster.py via signatures+transactions",
)


@dataclass(frozen=True, slots=True)
class _Found:
    complete: bool
    blind_reason: str | None
    cluster_share: Decimal
    supply_units: int
    members: list[ClusterMember]
    signals: list[str]
    rpc_calls: int
    slot: int | None
    evidence_summary: str


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _message(tx: dict[str, Any]) -> dict[str, Any]:
    outer = tx.get("transaction")
    if not isinstance(outer, dict):
        return {}
    message = outer.get("message")
    return message if isinstance(message, dict) else {}


def _one_key(key: object, header_signer: bool) -> tuple[str, bool] | None:
    if isinstance(key, str):
        return key, header_signer
    if isinstance(key, dict) and isinstance(key.get("pubkey"), str):
        return str(key["pubkey"]), bool(key.get("signer")) or header_signer
    return None


def _account_keys(tx: dict[str, Any]) -> list[tuple[str, bool]]:
    message = _message(tx)
    raw = message.get("accountKeys")
    header = message.get("header")
    declared = 0
    if isinstance(header, dict):
        declared = _as_int(header.get("numRequiredSignatures")) or 0
    if not isinstance(raw, list):
        return []
    keys: list[tuple[str, bool]] = []
    for index, key in enumerate(raw):
        parsed = _one_key(key, index < declared)
        if parsed is not None:
            keys.append(parsed)
    return keys


def _token_amount(entry: dict[str, Any]) -> int | None:
    raw = entry.get("uiTokenAmount")
    if not isinstance(raw, dict):
        return None
    return _as_int(raw.get("amount"))


def _owner_amounts(balances: object, mint: str) -> tuple[dict[str, int], bool]:
    """Sum ``mint`` units by owner. The bool is true when a row has no owner."""
    totals: dict[str, int] = {}
    if not isinstance(balances, list):
        return totals, False
    unreadable = False
    for entry in balances:
        if not isinstance(entry, dict) or entry.get("mint") != mint:
            continue
        amount = _token_amount(entry)
        owner = entry.get("owner")
        if amount is None or not isinstance(owner, str) or not owner:
            unreadable = True
            continue
        totals[owner] = totals.get(owner, 0) + amount
    return totals, unreadable


def _signer_buys(
    pre: dict[str, int],
    post: dict[str, int],
    signers: set[str],
    *,
    slot: int,
    block_time: int,
    signature: str,
    curve: str | None,
    mint: str,
) -> list[_Buyer]:
    buyers: list[_Buyer] = []
    for owner in set(pre) | set(post):
        if owner not in signers or owner in {curve, mint}:
            continue
        gained = post.get(owner, 0) - pre.get(owner, 0)
        if gained <= 0:
            continue
        buyers.append(_Buyer(owner, post.get(owner, 0), slot, block_time, signature))
    return buyers


def _buyers_from_tx(
    mint: str,
    tx: dict[str, Any],
    *,
    slot: int,
    block_time: int,
    signature: str,
    curve: str | None,
) -> list[_Buyer] | None:
    """Buyers in one transaction. ``None`` means the transaction is unreadable."""
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("err") is not None:
        return []
    pre, pre_bad = _owner_amounts(meta.get("preTokenBalances"), mint)
    post, post_bad = _owner_amounts(meta.get("postTokenBalances"), mint)
    if pre_bad or post_bad:
        return None
    signers = {pubkey for pubkey, is_signer in _account_keys(tx) if is_signer}
    positive = [
        owner for owner in set(pre) | set(post) if post.get(owner, 0) - pre.get(owner, 0) > 0
    ]
    if positive and not signers:
        return None
    return _signer_buys(
        pre,
        post,
        signers,
        slot=slot,
        block_time=block_time,
        signature=signature,
        curve=curve,
        mint=mint,
    )


def _instruction_lists(tx: dict[str, Any]) -> list[object]:
    found: list[object] = [_message(tx).get("instructions")]
    meta = tx.get("meta")
    if not isinstance(meta, dict) or not isinstance(meta.get("innerInstructions"), list):
        return found
    for group in meta["innerInstructions"]:
        if isinstance(group, dict):
            found.append(group.get("instructions"))
    return found


def _transfer_source(ix: object, buyer: str) -> str | None:
    if not isinstance(ix, dict):
        return None
    parsed = ix.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
        return None
    info = parsed.get("info")
    if not isinstance(info, dict):
        return None
    source = info.get("source")
    if info.get("destination") == buyer and isinstance(source, str) and source != buyer:
        return source
    return None


def _parsed_funder(tx: dict[str, Any], buyer: str) -> str | None:
    for raw in _instruction_lists(tx):
        if not isinstance(raw, list):
            continue
        for ix in raw:
            source = _transfer_source(ix, buyer)
            if source is not None:
                return source
    return None


def _largest_sol_drop(
    keys: list[str], pre: list[object], post: list[object], buyer: str
) -> str | None:
    best_wallet = ""
    best_drop = 0
    for index, wallet in enumerate(keys):
        if wallet == buyer or index >= len(pre) or index >= len(post):
            continue
        before = _as_int(pre[index])
        after = _as_int(post[index])
        if before is None or after is None:
            continue
        drop = before - after
        if drop > best_drop:
            best_drop = drop
            best_wallet = wallet
    return best_wallet or None


def _balance_funder(tx: dict[str, Any], buyer: str) -> str | None:
    keys = [pubkey for pubkey, _signer in _account_keys(tx)]
    if buyer not in keys:
        return None
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return None
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return None
    index = keys.index(buyer)
    if index >= len(pre) or index >= len(post):
        return None
    before = _as_int(pre[index])
    after = _as_int(post[index])
    if before is None or after is None or after - before <= 0:
        return None
    return _largest_sol_drop(keys, pre, post, buyer)


def _funder_of(tx: dict[str, Any], buyer: str) -> str | None:
    """Wallet that sent SOL to ``buyer`` in ``tx``, parsed transfer first."""
    parsed = _parsed_funder(tx, buyer)
    if parsed is not None:
        return parsed
    return _balance_funder(tx, buyer)


def _candidate(entry: object, buy_sig: str, buy_time: int, window_s: int) -> tuple[int, str] | None:
    if not isinstance(entry, dict):
        return None
    signature = entry.get("signature")
    stamp = entry.get("blockTime")
    if not isinstance(signature, str) or signature == buy_sig or not isinstance(stamp, int):
        return None
    if stamp > buy_time or buy_time - stamp > window_s:
        return None
    return stamp, signature


def _prior_signature(
    entries: list[object], *, buy_sig: str, buy_time: int, window_s: int
) -> str | None:
    best_time = -1
    best_sig: str | None = None
    for entry in entries:
        chosen = _candidate(entry, buy_sig, buy_time, window_s)
        if chosen is None:
            continue
        stamp, signature = chosen
        if stamp >= best_time:
            best_time = stamp
            best_sig = signature
    return best_sig


def _over_minimum(groups: dict[Any, set[str]], minimum: int) -> set[str]:
    flagged: set[str] = set()
    for wallets in groups.values():
        if len(wallets) >= minimum:
            flagged.update(wallets)
    return flagged


def _grouped(buyers: list[_Buyer], minimum: int) -> tuple[set[str], set[str]]:
    by_slot: dict[int, set[str]] = {}
    by_sig: dict[str, set[str]] = {}
    for buyer in buyers:
        by_slot.setdefault(buyer.slot, set()).add(buyer.wallet)
        by_sig.setdefault(buyer.signature, set()).add(buyer.wallet)
    return _over_minimum(by_slot, minimum), _over_minimum(by_sig, minimum)


def _funder_flags(
    funders: dict[str, str], creator: str | None, minimum: int
) -> dict[str, set[str]]:
    counts: dict[str, int] = {}
    for source in funders.values():
        counts[source] = counts.get(source, 0) + 1
    flags: dict[str, set[str]] = {}
    for wallet, source in funders.items():
        wallet_flags: set[str] = set()
        if counts[source] >= minimum:
            wallet_flags.add("common_funder")
        if creator is not None and source == creator:
            wallet_flags.add("deployer_funded")
        if wallet_flags:
            flags[wallet] = wallet_flags
    return flags


def _flagged(
    buyers: list[_Buyer],
    funders: dict[str, str],
    *,
    minimum_slot: int,
    minimum_funder: int,
    creator: str | None,
) -> dict[str, set[str]]:
    slot_flagged, tx_flagged = _grouped(buyers, minimum_slot)
    flags: dict[str, set[str]] = {}
    for wallet in slot_flagged:
        flags.setdefault(wallet, set()).add("same_slot")
    for wallet in tx_flagged:
        flags.setdefault(wallet, set()).add("same_transaction")
    for wallet, wallet_flags in _funder_flags(funders, creator, minimum_funder).items():
        flags.setdefault(wallet, set()).update(wallet_flags)
    if creator is not None and creator in flags:
        flags[creator].add("deployer_overlap")
    return flags


def _latest(buyers: list[_Buyer]) -> dict[str, _Buyer]:
    latest: dict[str, _Buyer] = {}
    for buyer in buyers:
        previous = latest.get(buyer.wallet)
        if previous is None or (buyer.slot, buyer.signature) >= (previous.slot, previous.signature):
            latest[buyer.wallet] = buyer
    return latest


def _cluster_share(latest: dict[str, _Buyer], wallets: set[str], supply: int) -> Decimal:
    held = sum(latest[wallet].post_units for wallet in wallets if wallet in latest)
    if supply <= 0:
        return Decimal(0)
    return min(Decimal(1), Decimal(held) / Decimal(supply))


def _meets_fail(share: Decimal, active: SafetyThresholds) -> bool:
    return share * Decimal(100) >= Decimal(str(active.bundler_cluster_fail_pct))


def _members(
    latest: dict[str, _Buyer],
    flags: dict[str, set[str]],
    funders: dict[str, str],
    supply: int,
) -> list[ClusterMember]:
    members: list[ClusterMember] = []
    ordered = sorted(flags, key=lambda wallet: (latest[wallet].slot, wallet))
    for wallet in ordered:
        buyer = latest[wallet]
        share = min(Decimal(1), Decimal(buyer.post_units) / Decimal(supply))
        members.append(
            ClusterMember(
                wallet=wallet,
                token_units=buyer.post_units,
                share=share,
                slot=buyer.slot,
                signals=sorted(flags[wallet]),
                funder=funders.get(wallet),
                signature=buyer.signature,
            )
        )
    return members


def _summary(members: list[ClusterMember], *, skipped_funding: bool) -> str:
    parts: list[str] = []
    for member in members[:5]:
        fund = f" funder={member.funder}" if member.funder else ""
        parts.append(f"{member.wallet} slot={member.slot}{fund} {'+'.join(member.signals)}")
    text = "; ".join(parts)
    if not skipped_funding:
        return text
    return f"{text}; {_SKIPPED_FUNDING}" if text else _SKIPPED_FUNDING


def _min_slot(window: list[dict[str, Any]]) -> int | None:
    slots = [item["slot"] for item in window if isinstance(item.get("slot"), int)]
    return min(slots) if slots else None


def _page(result: object) -> list[dict[str, Any]] | None:
    if not isinstance(result, list):
        return None
    page: list[dict[str, Any]] = []
    for item in result:
        if isinstance(item, dict) and isinstance(item.get("signature"), str):
            page.append(item)
    return page


def _as_oldest(result: object) -> list[dict[str, Any]] | None:
    """Oldest-first signature rows from ``getTransactionsForAddress``.

    ``None`` means the body is not that shape. An empty ``data`` list is a
    real empty history.
    """
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, list):
        return None
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("signature"), str):
            return None
        rows.append(item)
    return rows


def _fallback_page_cap(calls_left: int, launch_txs: int) -> int:
    """Signature pages that still leave supply plus one read per launch tx."""
    reserve = 1 + max(1, launch_txs)
    return max(1, calls_left - reserve)


async def _page_oldest(budget: _Budget, mint: str, *, max_pages: int) -> list[dict[str, Any]]:
    """Oldest-first signatures by walking ``before`` until a short page."""
    before: str | None = None
    oldest: list[dict[str, Any]] = []
    for _page_index in range(max(1, max_pages)):
        params: dict[str, Any] = {"limit": _SIG_PAGE}
        if before is not None:
            params["before"] = before
        result = await budget.call("getSignaturesForAddress", [mint, params])
        page = _page(result)
        if page is None or not isinstance(result, list):
            raise _Blind("launch signatures malformed")
        if not page:
            return list(reversed(oldest))
        oldest = page
        if len(result) < _SIG_PAGE:
            return list(reversed(oldest))
        before = str(page[-1]["signature"])
    raise _Blind("launch window incomplete: history exceeds the signature page cap")


async def _launch_signatures(
    budget: _Budget, mint: str, *, limit: int, max_pages: int
) -> list[dict[str, Any]]:
    """Oldest signatures. Helius returns them directly; other RPCs page."""
    capped = min(1000, max(1, limit))
    try:
        result = await budget.call(
            "getTransactionsForAddress",
            [
                mint,
                {
                    "transactionDetails": "signatures",
                    "sortOrder": "asc",
                    "limit": capped,
                },
            ],
        )
    except _Blind as exc:
        # Missing method and HTTP 429 both surface as an RPC failure here.
        # A call or time budget must not turn into a second scan.
        if exc.reason != "rpc failed for bundler_cluster":
            raise
        return await _page_oldest(budget, mint, max_pages=max_pages)
    parsed = _as_oldest(result)
    if parsed is None:
        raise _Blind("launch signatures malformed")
    return parsed[:capped]


def _parse_supply(result: object) -> int | None:
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    return _as_int(value.get("amount"))


async def _supply_units(budget: _Budget, mint: str) -> int:
    result = await budget.call("getTokenSupply", [mint])
    supply = _parse_supply(result)
    if supply is None or supply <= 0:
        raise _Blind("token supply unreadable")
    return supply


def _stamp(tx: dict[str, Any], entry: dict[str, Any]) -> tuple[int, int] | None:
    slot = tx.get("slot") if isinstance(tx.get("slot"), int) else entry.get("slot")
    raw_time = tx.get("blockTime")
    block_time = raw_time if isinstance(raw_time, int) else entry.get("blockTime")
    if isinstance(slot, int) and isinstance(block_time, int):
        return slot, block_time
    return None


async def _buyers_from_entry(
    budget: _Budget, inp: BundlerClusterIn, entry: dict[str, Any]
) -> list[_Buyer]:
    signature = str(entry["signature"])
    tx = await budget.call("getTransaction", [signature, dict(TRANSACTION_CONFIG)])
    if not isinstance(tx, dict):
        raise _Blind("launch transaction unavailable")
    stamp = _stamp(tx, entry)
    if stamp is None:
        raise _Blind("launch transaction missing slot or time")
    slot, block_time = stamp
    parsed = _buyers_from_tx(
        inp.mint,
        tx,
        slot=slot,
        block_time=block_time,
        signature=signature,
        curve=inp.curve_account,
    )
    if parsed is None:
        raise _Blind("launch transaction unreadable")
    return parsed


async def _load_buyers(
    budget: _Budget, inp: BundlerClusterIn, window: list[dict[str, Any]]
) -> list[_Buyer]:
    buyers: list[_Buyer] = []
    for entry in window:
        buyers.extend(await _buyers_from_entry(budget, inp, entry))
    return buyers


async def _one_hop(budget: _Budget, buyer: _Buyer, window_s: int) -> str | None:
    result = await budget.call(
        "getSignaturesForAddress", [buyer.wallet, {"limit": _FUNDING_SIG_LIMIT}]
    )
    if not isinstance(result, list):
        raise _Blind("funding signatures malformed")
    prior = _prior_signature(
        result, buy_sig=buyer.signature, buy_time=buyer.block_time, window_s=window_s
    )
    if prior is None:
        return None
    tx = await budget.call("getTransaction", [prior, dict(TRANSACTION_CONFIG)])
    if not isinstance(tx, dict):
        raise _Blind("funding transaction unavailable")
    meta = tx.get("meta")
    if isinstance(meta, dict) and meta.get("err") is not None:
        return None
    return _funder_of(tx, buyer.wallet)


async def _funding_map(
    budget: _Budget, latest: dict[str, _Buyer], active: SafetyThresholds
) -> dict[str, str]:
    window_s = max(0, int(active.bundler_funding_window_s))
    funders: dict[str, str] = {}
    for wallet in sorted(latest):
        source = await _one_hop(budget, latest[wallet], window_s)
        if source is not None:
            funders[wallet] = source
    return funders


def _assemble(
    *,
    latest: dict[str, _Buyer],
    flags: dict[str, set[str]],
    funders: dict[str, str],
    supply: int,
    calls: int,
    window: list[dict[str, Any]],
    skipped_funding: bool,
) -> _Found:
    members = _members(latest, flags, funders, supply)
    signals = sorted({signal for wallet_flags in flags.values() for signal in wallet_flags})
    return _Found(
        complete=True,
        blind_reason=None,
        cluster_share=_cluster_share(latest, set(flags), supply),
        supply_units=supply,
        members=members,
        signals=signals,
        rpc_calls=calls,
        slot=_min_slot(window),
        evidence_summary=_summary(members, skipped_funding=skipped_funding),
    )


async def _scan(inp: BundlerClusterIn, budget: _Budget, active: SafetyThresholds) -> _Found:
    launch = max(1, int(active.bundler_launch_txs))
    signatures = await _launch_signatures(
        budget,
        inp.mint,
        limit=launch,
        max_pages=_fallback_page_cap(int(active.bundler_max_rpc_calls) - 1, launch),
    )
    if not signatures:
        raise _Blind("no signatures for mint")
    supply = await _supply_units(budget, inp.mint)
    window = signatures[: max(1, int(active.bundler_launch_txs))]
    buyers = await _load_buyers(budget, inp, window)
    latest = _latest(buyers)
    slot_min = max(1, int(active.bundler_same_slot_min_buyers))
    funder_min = max(1, int(active.bundler_common_funder_min_buyers))
    preliminary = _flagged(
        buyers, {}, minimum_slot=slot_min, minimum_funder=funder_min, creator=inp.creator
    )
    skipped = _meets_fail(_cluster_share(latest, set(preliminary), supply), active)
    funders = {} if skipped else await _funding_map(budget, latest, active)
    flags = _flagged(
        buyers, funders, minimum_slot=slot_min, minimum_funder=funder_min, creator=inp.creator
    )
    return _assemble(
        latest=latest,
        flags=flags,
        funders=funders,
        supply=supply,
        calls=budget.calls,
        window=window,
        skipped_funding=skipped,
    )


def _blind_found(reason: str, calls: int) -> _Found:
    return _Found(
        complete=False,
        blind_reason=reason,
        cluster_share=Decimal(0),
        supply_units=0,
        members=[],
        signals=[],
        rpc_calls=calls,
        slot=None,
        evidence_summary="",
    )


def _dump(inp: BundlerClusterIn, rpc: RpcClient, found: _Found) -> dict[str, Any]:
    correlation = inp.correlation_id or (str(a) if (a := ambient_correlation()) else None)
    out = {
        "complete": found.complete,
        "blind_reason": found.blind_reason,
        "cluster_share": found.cluster_share,
        "supply_units": found.supply_units,
        "flagged_wallets": len(found.members),
        "signals": found.signals,
        "members": [member.model_dump() for member in found.members],
        "rpc_calls": found.rpc_calls,
        "slot": found.slot,
        "evidence_summary": found.evidence_summary,
        "meta": meta_now(SOLANA_RPC_SOURCE, correlation, rpc.clock),
    }
    return BundlerClusterOut.model_validate(out).model_dump()


async def run_bundler_cluster(
    inp: BundlerClusterIn,
    rpc: RpcClient,
    *,
    thresholds: SafetyThresholds | None = None,
) -> dict[str, Any]:
    """Scan the launch window and return a validated payload. Never raises for BLIND."""
    active = thresholds if thresholds is not None else SafetyThresholds()
    budget = _Budget(
        rpc,
        max_calls=int(active.bundler_max_rpc_calls),
        time_budget_s=active.bundler_time_budget_s,
    )
    try:
        found = await _scan(inp, budget, active)
    except _Blind as exc:
        found = _blind_found(exc.reason, budget.calls)
    return _dump(inp, rpc, found)


def build_bundler_cluster_tool(rpc: RpcClient, thresholds: SafetyThresholds | None = None) -> Tool:
    """Bind ``onchain.bundler_cluster`` to one injected RPC client."""

    @tool(BUNDLER_CLUSTER_SPEC)
    async def _bundler_cluster(payload: BundlerClusterIn) -> dict[str, Any]:
        return await run_bundler_cluster(payload, rpc, thresholds=thresholds)

    return _bundler_cluster
