"""bundler_cluster live source over MockTransport. No network.

A clean launch passes. A same-slot bundle fails. A shared funder fails.
RPC failure, an unfinished call budget, and an unfinished time budget are
BLIND, and a blind required check forces KILL on the score report.
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from typing import Any

import httpx

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.core.ids import CorrelationId, IdFactory, bind_correlation
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools import build_registry, register_read_tools
from pumpgrok.tools.onchain.bundler_cluster import (
    BUNDLER_CLUSTER_SPEC,
    BundlerClusterIn,
    build_bundler_cluster_tool,
    run_bundler_cluster,
)
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.score_report import ScoreReportIn, run_score_report

START = 1469918176.385
MINT = "So11111111111111111111111111111111111111112"
CURVE = "8" * 44
CREATOR = "9" * 44
FUNDER = "F" * 44
ALICE = "A" * 44
BOB = "B" * 44
CAROL = "C" * 44
SUPPLY = 1_000_000_000_000


class _Script:
    """One in-memory RPC. ``fail`` methods return a JSON-RPC error."""

    def __init__(self) -> None:
        self.sigs: dict[str, list[dict[str, Any]]] = {}
        self.txs: dict[str, dict[str, Any] | None] = {}
        self.supply = str(SUPPLY)
        self.fail: set[str] = set()
        self.force: dict[str, Any] = {}
        self.calls: list[str] = []
        self.tx_configs: list[dict[str, Any]] = []
        self.tx_sigs: list[str] = []
        # None: this RPC has no getTransactionsForAddress. A dict is the result body.
        self.oldest: dict[str, Any] | None = None
        # Signature pages keyed by the `before` cursor. None uses ``sigs``.
        self.by_before: dict[str | None, list[dict[str, Any]]] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = str(body["method"])
        params = body["params"]
        self.calls.append(method)
        if method in self.fail:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})
        if method in self.force:
            result = self.force[method]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        if method == "getTransactionsForAddress":
            if self.oldest is None:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": -32601, "message": "Method not found"},
                    },
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": self.oldest})
        if method == "getSignaturesForAddress":
            result: Any = self._signatures(params)
        elif method == "getTransaction":
            signature = str(params[0])
            self.tx_sigs.append(signature)
            if len(params) > 1 and isinstance(params[1], dict):
                self.tx_configs.append(params[1])
            result = self.txs.get(signature)
        elif method == "getTokenSupply":
            result = {"context": {"slot": 1}, "value": {"amount": self.supply, "decimals": 6}}
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _signatures(self, params: list[Any]) -> list[dict[str, Any]]:
        if self.by_before is None:
            return self.sigs.get(str(params[0]), [])
        before = None
        if len(params) > 1 and isinstance(params[1], dict):
            before = params[1].get("before")
        return self.by_before.get(before if isinstance(before, str) else None, [])


def _rpc(script: _Script, *, start: float = START) -> tuple[RpcClient, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
    rpc = RpcClient(["https://rpc.invalid"], client, FixtureClock(start=start), random.Random(4))
    return rpc, client


def _sig(signature: str, slot: int, ts: int) -> dict[str, Any]:
    return {"signature": signature, "slot": slot, "blockTime": ts, "err": None}


def _buy(slot: int, ts: int, buyers: list[tuple[str, int]]) -> dict[str, Any]:
    keys: list[dict[str, Any]] = [
        {"pubkey": wallet, "signer": True, "writable": True} for wallet, _amount in buyers
    ]
    keys.append({"pubkey": CURVE, "signer": False, "writable": True})
    balances: list[dict[str, Any]] = [
        {
            "accountIndex": index,
            "mint": MINT,
            "owner": wallet,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }
        for index, (wallet, amount) in enumerate(buyers)
    ]
    rest = SUPPLY - sum(amount for _wallet, amount in buyers)
    balances.append(
        {
            "accountIndex": len(buyers),
            "mint": MINT,
            "owner": CURVE,
            "uiTokenAmount": {"amount": str(rest), "decimals": 6},
        }
    )
    return {
        "slot": slot,
        "blockTime": ts,
        "meta": {
            "err": None,
            "preBalances": [1_000_000_000 for _key in keys],
            "postBalances": [900_000_000 for _key in keys],
            "preTokenBalances": [],
            "postTokenBalances": balances,
        },
        "transaction": {"message": {"accountKeys": keys}},
    }


def _fund(slot: int, ts: int, source: str, dest: str) -> dict[str, Any]:
    return {
        "slot": slot,
        "blockTime": ts,
        "meta": {
            "err": None,
            "preBalances": [2_000_000_000, 0],
            "postBalances": [1_000_000_000, 1_000_000_000],
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": source, "signer": True, "writable": True},
                    {"pubkey": dest, "signer": False, "writable": True},
                ],
                "instructions": [
                    {
                        "program": "system",
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": source,
                                "destination": dest,
                                "lamports": 1_000_000_000,
                            },
                        },
                    }
                ],
            }
        },
    }


def _inp() -> BundlerClusterIn:
    return BundlerClusterIn(mint=MINT, creator=CREATOR, curve_account=CURVE)


async def _run(script: _Script, **threshold_overrides: float) -> dict[str, Any]:
    thresholds = SafetyThresholds(**threshold_overrides) if threshold_overrides else None
    rpc, client = _rpc(script)
    try:
        return await run_bundler_cluster(_inp(), rpc, thresholds=thresholds)
    finally:
        await client.aclose()


async def test_clean_token_passes() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyB", 30, 1_000_200), _sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(CREATOR, 1_000)])
    script.txs["buyB"] = _buy(30, 1_000_200, [(ALICE, 2_000)])
    script.sigs[CREATOR] = []
    script.sigs[ALICE] = []
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal(0)
    assert out["flagged_wallets"] == 0
    assert out["signals"] == []
    assert out["blind_reason"] is None
    assert CURVE not in {member["wallet"] for member in out["members"]}
    assert out["meta"]["source"] == "solana_rpc"


async def test_bundled_token_fails_with_same_slot_evidence() -> None:
    script = _Script()
    amount = 200_000_000_000
    script.sigs[MINT] = [_sig("bundle", 7, 1_000_000)]
    script.txs["bundle"] = _buy(7, 1_000_000, [(CREATOR, amount), (ALICE, amount), (BOB, amount)])
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.6")
    assert out["flagged_wallets"] == 3
    assert "same_slot" in out["signals"]
    assert "same_transaction" in out["signals"]
    assert "deployer_overlap" in out["signals"]
    assert "funding hop skipped" in out["evidence_summary"]
    assert script.calls.count("getTransaction") == 1
    assert "getSignaturesForAddress" not in script.calls[2:]
    report = await _report(script, checks=["bundler_cluster"])
    row = next(item for item in report["checks"] if item["id"] == "bundler_cluster")
    assert row["status"] == "fail"
    assert row["value"] == 60.0


async def test_funding_cluster_fails() -> None:
    script = _Script()
    amount = 200_000_000_000
    script.sigs[MINT] = [_sig("buyB", 40, 1_000_400), _sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, amount)])
    script.txs["buyB"] = _buy(40, 1_000_400, [(BOB, amount)])
    script.sigs[ALICE] = [_sig("buyA", 10, 1_000_000), _sig("fundA", 8, 999_700)]
    script.sigs[BOB] = [_sig("buyB", 40, 1_000_400), _sig("fundB", 38, 1_000_100)]
    script.txs["fundA"] = _fund(8, 999_700, FUNDER, ALICE)
    script.txs["fundB"] = _fund(38, 1_000_100, FUNDER, BOB)
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.4")
    assert out["signals"] == ["common_funder"]
    assert {member["funder"] for member in out["members"]} == {FUNDER}
    assert "same_slot" not in out["signals"]


async def test_deployer_funded_buyer_is_flagged() -> None:
    script = _Script()
    amount = 300_000_000_000
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, amount)])
    script.sigs[ALICE] = [_sig("fundA", 9, 999_800)]
    script.txs["fundA"] = _fund(9, 999_800, CREATOR, ALICE)
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.3")
    assert "deployer_funded" in out["signals"]
    assert "common_funder" not in out["signals"]


async def test_lone_dev_buy_is_not_a_cluster() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("dev", 4, 1_000_000)]
    script.txs["dev"] = _buy(4, 1_000_000, [(CREATOR, 400_000_000_000)])
    script.sigs[CREATOR] = []
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal(0)
    assert out["flagged_wallets"] == 0


async def test_rpc_failure_is_blind_and_kills_the_report() -> None:
    script = _Script()
    script.fail.add("getSignaturesForAddress")
    out = await _run(script)
    assert out["complete"] is False
    assert out["blind_reason"] == "rpc failed for bundler_cluster"
    assert out["cluster_share"] == Decimal(0)
    report = await _report(script, checks=["bundler_cluster"])
    row = next(item for item in report["checks"] if item["id"] == "bundler_cluster")
    assert row["status"] == "blind"
    assert row["reason"] == "rpc failed for bundler_cluster"
    assert report["verdict_recommendation"] == "KILL"
    assert "bundler_cluster" in report["blind_checks"]


async def test_call_budget_exhaustion_is_blind() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, 1_000)])
    out = await _run(script, bundler_max_rpc_calls=1)
    assert out["complete"] is False
    assert out["blind_reason"] == "rpc call budget exhausted"
    assert out["rpc_calls"] == 1
    report = await _report(script, checks=["bundler_cluster"], bundler_max_rpc_calls=1)
    row = next(item for item in report["checks"] if item["id"] == "bundler_cluster")
    assert row["status"] == "blind"
    assert row["status"] != "pass"
    assert report["verdict_recommendation"] == "KILL"


async def test_time_budget_exhaustion_is_blind() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    out = await _run(script, bundler_time_budget_s=0)
    assert out["complete"] is False
    assert out["blind_reason"] == "time budget exhausted"
    assert out["rpc_calls"] == 0
    assert script.calls == []


async def test_incomplete_history_is_blind() -> None:
    script = _Script()
    # One full 1000-signature page and no older page. The fallback cap for a
    # tight call budget is a single page, so the launch is still unseen.
    script.sigs[MINT] = [
        _sig(f"s{index}", 5000 - index, 3_000_000 - index) for index in range(1000)
    ]
    out = await _run(script, bundler_max_rpc_calls=9)
    assert out["complete"] is False
    assert out["blind_reason"] == "launch window incomplete: history exceeds the signature page cap"
    assert "getTransaction" not in script.calls


async def test_history_longer_than_one_page_uses_the_oldest_page() -> None:
    script = _Script()
    newest = [_sig(f"new{index}", 9000 - index, 4_000_000 - index) for index in range(1000)]
    oldest_newest_first = [
        _sig("third", 82, 1_000_020),
        _sig("second", 81, 1_000_010),
        _sig("create", 80, 1_000_000),
    ]
    # getSignaturesForAddress is newest-first. The short page's last entry is
    # the create.
    script.by_before = {None: newest, newest[-1]["signature"]: oldest_newest_first}
    amount = 200_000_000_000
    script.txs["create"] = _buy(80, 1_000_000, [(ALICE, amount), (BOB, amount), (CAROL, amount)])
    script.txs["create"]["version"] = 1
    script.txs["second"] = _buy(81, 1_000_010, [(CREATOR, 1_000)])
    script.txs["third"] = _buy(82, 1_000_020, [(CREATOR, 1_000)])
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.6")
    assert "same_transaction" in out["signals"]
    assert script.tx_sigs[0] == "create"
    assert "new0" not in script.tx_sigs
    assert script.tx_configs
    assert all(cfg.get("maxSupportedTransactionVersion") == 1 for cfg in script.tx_configs)
    assert all(cfg.get("encoding") == "jsonParsed" for cfg in script.tx_configs)


async def test_helius_oldest_signatures_skip_later_history() -> None:
    script = _Script()
    amount = 200_000_000_000
    script.oldest = {
        "data": [
            _sig("create", 7, 1_000_000),
            _sig("later", 40, 1_000_500),
        ],
        "paginationToken": "40:1",
    }
    script.txs["create"] = _buy(7, 1_000_000, [(CREATOR, amount), (ALICE, amount), (BOB, amount)])
    script.txs["create"]["version"] = 0
    script.txs["later"] = _buy(40, 1_000_500, [(CAROL, 1_000)])
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.6")
    assert "deployer_overlap" in out["signals"]
    assert script.calls.count("getSignaturesForAddress") == 0
    assert "getTransactionsForAddress" in script.calls
    assert script.tx_sigs[:2] == ["create", "later"]
    assert all(cfg.get("maxSupportedTransactionVersion") == 1 for cfg in script.tx_configs)


async def test_unreadable_launch_transaction_is_blind() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    tx = _buy(10, 1_000_000, [(ALICE, 1_000)])
    tx["meta"]["postTokenBalances"][0].pop("owner")
    script.txs["buyA"] = tx
    out = await _run(script)
    assert out["complete"] is False
    assert out["blind_reason"] == "launch transaction unreadable"


async def test_warn_band_on_a_small_same_slot_cluster() -> None:
    script = _Script()
    amount = 40_000_000_000
    script.sigs[MINT] = [_sig("bundle", 7, 1_000_000)]
    script.txs["bundle"] = _buy(7, 1_000_000, [(ALICE, amount), (BOB, amount), (CAROL, amount)])
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.12")
    report = await _report(script, checks=["bundler_cluster"])
    row = next(item for item in report["checks"] if item["id"] == "bundler_cluster")
    assert row["status"] == "warn"
    assert row["value"] == 12.0


async def test_balance_fallback_finds_a_funder_without_parsed_instructions() -> None:
    script = _Script()
    amount = 300_000_000_000
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, amount)])
    script.sigs[ALICE] = [_sig("fundA", 9, 999_800)]
    funded = _fund(9, 999_800, CREATOR, ALICE)
    funded["transaction"]["message"].pop("instructions")
    script.txs["fundA"] = funded
    out = await _run(script)
    assert out["complete"] is True
    assert out["signals"] == ["deployer_funded"]
    assert out["members"][0]["funder"] == CREATOR


async def test_inner_instruction_funder_counts() -> None:
    script = _Script()
    amount = 150_000_000_000
    script.sigs[MINT] = [_sig("buyB", 20, 1_000_200), _sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, amount)])
    script.txs["buyB"] = _buy(20, 1_000_200, [(BOB, amount)])
    for wallet, sig, slot, ts in (
        (ALICE, "fundA", 9, 999_800),
        (BOB, "fundB", 19, 1_000_050),
    ):
        script.sigs[wallet] = [_sig(sig, slot, ts)]
        funded = _fund(slot, ts, FUNDER, wallet)
        transfer = funded["transaction"]["message"].pop("instructions")
        funded["meta"]["innerInstructions"] = [{"index": 0, "instructions": transfer}]
        script.txs[sig] = funded
    out = await _run(script)
    assert out["complete"] is True
    assert out["signals"] == ["common_funder"]
    assert out["cluster_share"] == Decimal("0.3")


async def test_empty_history_null_tx_bad_supply_and_malformed_sigs_are_blind() -> None:
    empty = _Script()
    out = await _run(empty)
    assert out["blind_reason"] == "no signatures for mint"

    missing = _Script()
    missing.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    out = await _run(missing)
    assert out["blind_reason"] == "launch transaction unavailable"

    untimed = _Script()
    untimed.sigs[MINT] = [{"signature": "buyA"}]
    untimed.txs["buyA"] = {"meta": {"err": None}, "transaction": {"message": {}}}
    out = await _run(untimed)
    assert out["blind_reason"] == "launch transaction missing slot or time"

    broke = _Script()
    broke.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    broke.supply = "0"
    out = await _run(broke)
    assert out["blind_reason"] == "token supply unreadable"

    malformed = _Script()
    malformed.force["getSignaturesForAddress"] = {"not": "a list"}
    out = await _run(malformed)
    assert out["blind_reason"] == "launch signatures malformed"


async def test_failed_transaction_is_not_a_buyer() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    failed = _buy(10, 1_000_000, [(ALICE, 500_000_000_000)])
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    script.txs["buyA"] = failed
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal(0)
    assert out["flagged_wallets"] == 0


async def test_header_signers_count_without_parsed_signer_flags() -> None:
    script = _Script()
    amount = 200_000_000_000
    script.sigs[MINT] = [_sig("bundle", 7, 1_000_000)]
    tx = _buy(7, 1_000_000, [(ALICE, amount), (BOB, amount), (CAROL, amount)])
    message = tx["transaction"]["message"]
    message["accountKeys"] = [ALICE, BOB, CAROL, CURVE]
    message["header"] = {"numRequiredSignatures": 3}
    script.txs["bundle"] = tx
    out = await _run(script)
    assert out["complete"] is True
    assert out["cluster_share"] == Decimal("0.6")
    assert "same_transaction" in out["signals"]


async def test_missing_funding_transaction_is_blind() -> None:
    script = _Script()
    script.sigs[MINT] = [_sig("buyA", 10, 1_000_000)]
    script.txs["buyA"] = _buy(10, 1_000_000, [(ALICE, 1_000)])
    script.sigs[ALICE] = [_sig("fundA", 9, 999_800)]
    out = await _run(script)
    assert out["complete"] is False
    assert out["blind_reason"] == "funding transaction unavailable"


async def test_tool_spec_uses_the_shared_rpc_limiter() -> None:
    assert BUNDLER_CLUSTER_SPEC.name == "onchain.bundler_cluster"
    assert BUNDLER_CLUSTER_SPEC.rate_limit_key == "solana_rpc"
    assert BUNDLER_CLUSTER_SPEC.side_effect.value == "read"
    assert BUNDLER_CLUSTER_SPEC.timeout_s == 12.0
    reg, _audits = build_registry(clock=FixtureClock(start=START), rng=random.Random(1))
    script = _Script()
    rpc, client = _rpc(script)
    try:
        register_read_tools(reg, rpc)
        assert "onchain.bundler_cluster" in reg.names()
        out = await build_bundler_cluster_tool(rpc)(_inp())
        assert out.complete is False
        assert out.blind_reason == "no signatures for mint"
    finally:
        await client.aclose()


async def _report(script: _Script, **overrides: Any) -> dict[str, Any]:
    checks = overrides.pop("checks", None)
    thresholds = overrides or None
    rpc, client = _rpc(script)
    factory = IdFactory(FixtureClock(start=START), random.Random(2))
    try:
        with bind_correlation(CorrelationId.new(factory)):
            return await run_score_report(
                ScoreReportIn(
                    mint=MINT,
                    curve_account=CURVE,
                    creator=CREATOR,
                    checks=checks,
                    thresholds=thresholds,
                ),
                rpc,
                thresholds=SafetyThresholds(),
            )
    finally:
        await client.aclose()
