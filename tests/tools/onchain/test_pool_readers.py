"""Curve, DAMM v2, and launch-count readers over captured or built accounts.

No test opens a socket. The 166-byte curve and the 1112-byte DAMM v2 pool are
mainnet account bytes stored under tests/fixtures/rpc. RPC calls are mocked.
"""

from __future__ import annotations

import base64
import json
import random
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from pumpgrok.core.errors import DataStale
from pumpgrok.execution.paper_adapter.models import CurveSnapshot
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.market.liquidity import LiquidityIn, run_liquidity
from pumpgrok.tools.onchain.curve_layout import (
    CURVE_ACCOUNT_SIZE,
    CURVE_ACCOUNT_SIZE_CURRENT,
    decode_curve_account,
    encode_curve_fixture,
)
from pumpgrok.tools.onchain.damm_v2 import decode_damm_pool, lock_from_pool
from pumpgrok.tools.onchain.deployer_history import DeployerHistoryIn, run_deployer_history
from pumpgrok.tools.onchain.launches import count_creator_launches
from pumpgrok.tools.onchain.lp_status import LpStatusIn, run_lp_status
from pumpgrok.tools.onchain.rpc import RpcClient
from pumpgrok.tools.safety.checks.readers import check_lp_status

ROOT = Path(__file__).resolve().parents[2]
CURVE_FIXTURE = json.loads(
    (ROOT / "fixtures" / "rpc" / "bonding_curve" / "mainnet_166.json").read_text(encoding="utf-8")
)
POOL_FIXTURE = json.loads(
    (ROOT / "fixtures" / "rpc" / "damm_v2" / "pool_1112.json").read_text(encoding="utf-8")
)
MINT = CURVE_FIXTURE["mint"]
CURVE = CURVE_FIXTURE["account"]
POOL_MINT = POOL_FIXTURE["mint"]
POOL = POOL_FIXTURE["pool"]
CREATOR = "4k5uD5YgY3ifGHe5ofTA35cYFFJcTtGQyw251ubP3Kmy"
START = 1_700_000_000.0


def _rpc(handler) -> RpcClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RpcClient(["https://rpc.invalid"], client, FixtureClock(start=START), random.Random(1))


def _curve_raw() -> bytes:
    raw = base64.b64decode(CURVE_FIXTURE["data_b64"])
    assert len(raw) == CURVE_ACCOUNT_SIZE_CURRENT
    return raw


def _pool_raw() -> bytes:
    raw = base64.b64decode(POOL_FIXTURE["data_b64"])
    assert len(raw) == POOL_FIXTURE["len"]
    return raw


def test_mainnet_curve_decodes_real_sol_near_zero() -> None:
    state = decode_curve_account(_curve_raw())
    assert state.creator == CREATOR
    assert state.complete is False
    assert state.virtual_sol_reserves_lamports == 30_000_000_062
    assert state.real_sol_reserves_lamports == 62
    assert state.quote_is_sol() is True
    assert state.initial_virtual_quote_lamports == 30_000_000_000


def test_legacy_and_creator_layouts_round_trip() -> None:
    legacy = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_073_000_000_000_000,
        real_sol_reserves_lamports=0,
        real_token_reserves_units=793_100_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
    )
    assert len(legacy) == CURVE_ACCOUNT_SIZE
    state = decode_curve_account(legacy)
    assert state.real_sol_reserves_lamports == 0
    assert state.virtual_sol_reserves_lamports == 30_000_000_000
    assert state.creator is None
    with_creator = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_073_000_000_000_000,
        real_sol_reserves_lamports=62,
        real_token_reserves_units=793_100_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
        creator=CREATOR,
    )
    assert len(with_creator) == 81
    assert decode_curve_account(with_creator).creator == CREATOR


def test_unknown_curve_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a known layout"):
        decode_curve_account(b"\x00" * 50)


def test_floor_compares_real_reserves() -> None:
    snap = CurveSnapshot(
        mint=MINT,
        s_lamports=30_000_000_062,
        t_units=1_072_999_999_949_415,
        real_sol_reserves_lamports=62,
    )
    assert snap.liquidity_lamports() == 62
    untouched = CurveSnapshot(mint=MINT, s_lamports=42_000_000_000, t_units=1)
    assert untouched.liquidity_lamports() == 42_000_000_000


def test_damm_pool_reserves_and_zero_permanent_lock() -> None:
    pool = decode_damm_pool(_pool_raw(), address=POOL)
    assert pool.token_b_mint == POOL_MINT
    assert pool.token_a_mint == "So11111111111111111111111111111111111111112"
    side = pool.sol_side()
    assert side is not None
    assert side[1] == 3804
    assert pool.token_b_amount == 2001
    assert pool.liquidity == 0
    assert pool.permanent_lock_liquidity == 0
    assert lock_from_pool(pool) == 0.0


async def test_lp_and_liquidity_read_the_166_byte_curve() -> None:
    raw = _curve_raw()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getAccountInfo":
            result = {
                "value": {
                    "data": [base64.b64encode(raw).decode("ascii"), "base64"],
                    "owner": CURVE_FIXTURE["owner"],
                    "lamports": 1,
                    "executable": False,
                }
            }
        elif method == "getProgramAccounts":
            result = [
                {
                    "pubkey": CURVE,
                    "account": {
                        "data": [base64.b64encode(raw).decode("ascii"), "base64"],
                        "owner": CURVE_FIXTURE["owner"],
                    },
                }
            ]
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    rpc = _rpc(handler)
    lp = await run_lp_status(LpStatusIn(mint=MINT, curve_account=CURVE), rpc)
    assert lp["stage"] == "bonding_curve"
    assert lp["curve_complete"] is False
    liq = await run_liquidity(LiquidityIn(mint=MINT, curve_account=CURVE), rpc)
    assert liq["liquidity_lamports"] == 62
    assert liq["real_sol_reserves_lamports"] == 62
    assert liq["virtual_sol_reserves_lamports"] == 30_000_000_062
    assert liq["liquidity_basis"] == "bonding_curve_real"
    assert liq["creator_launches"] == 1
    assert liq["creator"] == CREATOR


async def test_damm_v2_reports_real_sol_and_unlocked_lp() -> None:
    raw = _pool_raw()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getAccountInfo":
            result = {
                "value": {
                    "data": [base64.b64encode(raw).decode("ascii"), "base64"],
                    "owner": POOL_FIXTURE["owner"],
                    "lamports": 1,
                    "executable": False,
                }
            }
        elif method == "getProgramAccounts":
            result = []
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    rpc = _rpc(handler)
    lp = await run_lp_status(LpStatusIn(mint=POOL_MINT, curve_account=POOL), rpc)
    assert lp["stage"] == "graduated"
    assert lp["lp_locked_pct"] == 0.0
    assert lp["lp_burned_pct"] == 0.0
    assert lp["locker"] == "meteora-damm-v2"
    verdict = check_lp_status(lp)
    assert verdict.status == "fail"
    liq = await run_liquidity(
        LiquidityIn(mint=POOL_MINT, curve_account=POOL, order_size_lamports=1_000), rpc
    )
    assert liq["liquidity_lamports"] == 3804
    assert liq["liquidity_basis"] == "damm_v2_quote"
    assert liq["virtual_sol_reserves_lamports"] == 0
    assert liq["order_to_liquidity_ratio"] == Decimal(1000) / Decimal(3804)


async def test_launch_scan_failure_is_blind_not_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32010, "message": "excluded"}}
        )

    rpc = _rpc(handler)
    with pytest.raises(DataStale):
        await count_creator_launches(rpc, CREATOR)
    with pytest.raises(DataStale):
        await run_deployer_history(DeployerHistoryIn(creator=CREATOR), rpc)


async def test_signature_count_is_not_the_launch_count() -> None:
    raw = encode_curve_fixture(
        virtual_sol_reserves_lamports=30_000_000_000,
        virtual_token_reserves_units=1_073_000_000_000_000,
        real_sol_reserves_lamports=62,
        real_token_reserves_units=793_100_000_000_000,
        total_supply_units=1_000_000_000_000_000,
        complete=False,
        creator=CREATOR,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body["method"]
        if method == "getProgramAccounts":
            result = [
                {
                    "pubkey": CURVE,
                    "account": {
                        "data": [base64.b64encode(raw).decode("ascii"), "base64"],
                        "owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                    },
                }
            ]
        elif method == "getSignaturesForAddress":
            result = [
                {"signature": f"sig{i}", "slot": i, "blockTime": 1_700_000_000 - i}
                for i in range(5)
            ]
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    out = await run_deployer_history(DeployerHistoryIn(creator=CREATOR), _rpc(handler))
    assert out["tokens_seen"] == 1
    assert len(out["recent_signatures"]) == 5
