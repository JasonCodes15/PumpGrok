"""Meteora DAMM v2 (cp-amm) pool decode and LP lock/burn.

Program ``cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG``. The pool account is
the zero-copy ``Pool`` (8-byte discriminator + 1104 bytes, 1112 total). Offsets
below match that C layout and were checked against mainnet pool
``FzD7dEQXoyJeUfJwmh1UchazP2TZVm1nXQrbGXbipfTG`` for mint
``J5iPnrg8ZvRx3uGp8ZmsuJHxr74jYWV4fcRuNQDveXFb``.

Reserves come from ``token_a_amount`` / ``token_b_amount`` when
``layout_version`` is at least 1. Those fields are not tracked on version 0;
the caller then has to read the vaults, and a failed vault read is unreadable,
not zero. SOL liquidity is the WSOL side only. A pool quoted in another mint
does not report a lamport figure.

LP lock is ``permanent_lock_liquidity / liquidity``. Burn is the share of
position liquidity whose position NFT has supply 0. An empty position scan
that succeeded means no position NFTs exist, so none are burned. A failed
scan leaves the burn percent unset. Vesting is not treated as a permanent lock.

DAMM v1 (``Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB``) and DLMM
(``LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo``) are not decoded. A mint that
only has one of those pools stays unreadable here.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.curve_layout import encode_pubkey
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

DAMM_V2_PROGRAM: Final[str] = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
WSOL_MINT: Final[str] = "So11111111111111111111111111111111111111112"
POOL_DISCRIMINATOR: Final[bytes] = bytes.fromhex("f19a6d0411b16dbc")
POSITION_DISCRIMINATOR: Final[bytes] = bytes.fromhex("aabc8fe47a40f7d0")
POOL_ACCOUNT_SIZE: Final[int] = 1112
POSITION_ACCOUNT_SIZE: Final[int] = 408

# Account offsets (discriminator included). Pool::INIT_SPACE layout.
_TOKEN_A_MINT = 168
_TOKEN_B_MINT = 200
_TOKEN_A_VAULT = 232
_TOKEN_B_VAULT = 264
_LIQUIDITY = 360
_PERMANENT_LOCK = 552
_TOKEN_A_AMOUNT = 680
_TOKEN_B_AMOUNT = 688
_LAYOUT_VERSION = 696

# Position offsets.
_POSITION_POOL = 8
_POSITION_NFT = 40
_UNLOCKED = 152
_VESTED = 168
_POSITION_PERMANENT = 184

_MAX_POSITIONS = 25


@dataclass(frozen=True, slots=True)
class DammPool:
    """One decoded DAMM v2 pool. Amounts are integer base units."""

    address: str
    token_a_mint: str
    token_b_mint: str
    token_a_vault: str
    token_b_vault: str
    liquidity: int
    permanent_lock_liquidity: int
    token_a_amount: int | None
    token_b_amount: int | None
    layout_version: int

    def sol_side(self) -> tuple[str, int] | None:
        """``(mint, amount)`` of the WSOL side, or None when the pool is not SOL-quoted.

        ``None`` amounts (layout version 0 without a vault read) stay unknown.
        """
        if self.token_a_mint == WSOL_MINT and self.token_a_amount is not None:
            return self.token_a_mint, self.token_a_amount
        if self.token_b_mint == WSOL_MINT and self.token_b_amount is not None:
            return self.token_b_mint, self.token_b_amount
        return None


@dataclass(frozen=True, slots=True)
class PositionView:
    """One position NFT and its liquidity buckets."""

    address: str
    nft_mint: str
    unlocked_liquidity: int
    vested_liquidity: int
    permanent_locked_liquidity: int

    def total(self) -> int:
        return self.unlocked_liquidity + self.vested_liquidity + self.permanent_locked_liquidity


@dataclass(frozen=True, slots=True)
class LpLockRead:
    """Lock and burn percents. ``None`` means that half could not be read."""

    locked_pct: float | None
    burned_pct: float | None
    positions: int
    checked: tuple[str, ...]


def _u64(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<Q", data, offset)[0])


def _u128(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 16], "little")


def account_data(result: object) -> tuple[bytes, str] | None:
    """``(bytes, owner)`` from a ``getAccountInfo`` result, or None when absent."""
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    data = value.get("data")
    if not isinstance(owner, str) or not isinstance(data, list) or len(data) != 2:
        return None
    if data[1] != "base64" or not isinstance(data[0], str):
        return None
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError):
        return None
    return raw, owner


def decode_damm_pool(raw: bytes, *, address: str) -> DammPool:
    """Decode one pool account. Raises ValueError when the bytes are not a pool."""
    if len(raw) != POOL_ACCOUNT_SIZE or raw[:8] != POOL_DISCRIMINATOR:
        raise ValueError("account is not a DAMM v2 pool")
    version = raw[_LAYOUT_VERSION]
    amount_a: int | None = _u64(raw, _TOKEN_A_AMOUNT) if version >= 1 else None
    amount_b: int | None = _u64(raw, _TOKEN_B_AMOUNT) if version >= 1 else None
    return DammPool(
        address=address,
        token_a_mint=encode_pubkey(raw[_TOKEN_A_MINT : _TOKEN_A_MINT + 32]),
        token_b_mint=encode_pubkey(raw[_TOKEN_B_MINT : _TOKEN_B_MINT + 32]),
        token_a_vault=encode_pubkey(raw[_TOKEN_A_VAULT : _TOKEN_A_VAULT + 32]),
        token_b_vault=encode_pubkey(raw[_TOKEN_B_VAULT : _TOKEN_B_VAULT + 32]),
        liquidity=_u128(raw, _LIQUIDITY),
        permanent_lock_liquidity=_u128(raw, _PERMANENT_LOCK),
        token_a_amount=amount_a,
        token_b_amount=amount_b,
        layout_version=version,
    )


def decode_position(raw: bytes, *, address: str) -> PositionView:
    """Decode one position. Raises ValueError when the bytes are not a position."""
    if len(raw) != POSITION_ACCOUNT_SIZE or raw[:8] != POSITION_DISCRIMINATOR:
        raise ValueError("account is not a DAMM v2 position")
    return PositionView(
        address=address,
        nft_mint=encode_pubkey(raw[_POSITION_NFT : _POSITION_NFT + 32]),
        unlocked_liquidity=_u128(raw, _UNLOCKED),
        vested_liquidity=_u128(raw, _VESTED),
        permanent_locked_liquidity=_u128(raw, _POSITION_PERMANENT),
    )


def _pct(part: int, whole: int) -> float | None:
    """Percent of ``whole`` held by ``part``.

    Both zero is 0. A positive part of an empty whole is unread.
    """
    if whole < 0 or part < 0:
        return None
    if whole == 0:
        return 0.0 if part == 0 else None
    quant = (Decimal(part) * Decimal(100) / Decimal(whole)).quantize(Decimal("0.0001"))
    return float(quant)


def lock_from_pool(pool: DammPool) -> float | None:
    """Permanent-lock percent from the pool fields. Unreadable when the ratio is inconsistent."""
    return _pct(pool.permanent_lock_liquidity, pool.liquidity)


async def _program_accounts(rpc: RpcClient, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        result = await rpc.call(
            "getProgramAccounts",
            [
                DAMM_V2_PROGRAM,
                {"encoding": "base64", "commitment": "confirmed", "filters": filters},
            ],
        )
    except RpcError as exc:
        raise DataStale("DAMM v2 program scan failed") from exc
    if not isinstance(result, list):
        raise DataStale("DAMM v2 program scan malformed")
    rows: list[dict[str, Any]] = []
    for row in result:
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_bytes(row: dict[str, Any]) -> tuple[str, bytes] | None:
    pubkey = row.get("pubkey")
    account = row.get("account")
    if not isinstance(pubkey, str) or not isinstance(account, dict):
        return None
    data = account.get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        return None
    if not isinstance(data[0], str):
        return None
    try:
        return pubkey, base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError):
        return None


async def discover_damm_pools(rpc: RpcClient, mint: str) -> list[DammPool]:
    """Pools that quote ``mint`` on either side.

    A failed scan raises :class:`DataStale`. An empty successful scan is no v2
    pool, which the caller must not fill in from another venue.
    """
    found: dict[str, DammPool] = {}
    for offset in (_TOKEN_A_MINT, _TOKEN_B_MINT):
        rows = await _program_accounts(rpc, [{"memcmp": {"offset": offset, "bytes": mint}}])
        for row in rows:
            parsed = _row_bytes(row)
            if parsed is None:
                continue
            address, raw = parsed
            try:
                pool = decode_damm_pool(raw, address=address)
            except ValueError:
                continue
            if mint in (pool.token_a_mint, pool.token_b_mint):
                found[address] = pool
    return list(found.values())


def _parsed_token_amount(result: object) -> int | None:
    """Token amount from a jsonParsed token account, or None when it is not there."""
    node: object = result
    for key in ("value", "data", "parsed", "info", "tokenAmount"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, dict):
        return None
    amount = node.get("amount")
    if isinstance(amount, str) and amount.isdecimal():
        return int(amount)
    return None


async def _vault_amount(rpc: RpcClient, vault: str) -> int | None:
    """Vault token balance. A failed read is None, not zero."""
    try:
        result = await rpc.call(
            "getAccountInfo", [vault, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
    except RpcError:
        return None
    return _parsed_token_amount(result)


async def fill_vault_amounts(rpc: RpcClient, pool: DammPool) -> DammPool:
    """Read vault token balances when layout version 0 did not store amounts.

    A failed read leaves the amount unset. It is not replaced with zero.
    """
    tracked = pool.layout_version >= 1
    if tracked and pool.token_a_amount is not None and pool.token_b_amount is not None:
        return pool
    amount_a = pool.token_a_amount
    amount_b = pool.token_b_amount
    if amount_a is None:
        amount_a = await _vault_amount(rpc, pool.token_a_vault)
    if amount_b is None:
        amount_b = await _vault_amount(rpc, pool.token_b_vault)
    return DammPool(
        address=pool.address,
        token_a_mint=pool.token_a_mint,
        token_b_mint=pool.token_b_mint,
        token_a_vault=pool.token_a_vault,
        token_b_vault=pool.token_b_vault,
        liquidity=pool.liquidity,
        permanent_lock_liquidity=pool.permanent_lock_liquidity,
        token_a_amount=amount_a,
        token_b_amount=amount_b,
        layout_version=pool.layout_version,
    )


async def _nft_supply(rpc: RpcClient, mint: str) -> int | None:
    try:
        result = await rpc.call("getTokenSupply", [mint])
    except RpcError:
        return None
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    amount = value.get("amount")
    if isinstance(amount, str) and amount.isdecimal():
        return int(amount)
    return None


async def read_lp_lock(rpc: RpcClient, pool: DammPool) -> LpLockRead:
    """Permanent-lock percent from the pool, burn percent from position NFTs.

    More than ``_MAX_POSITIONS`` positions, or any NFT supply that cannot be
    read, leaves the burn percent unset. The lock percent still comes from the
    pool fields when those are consistent.
    """
    locked = lock_from_pool(pool)
    try:
        rows = await _program_accounts(
            rpc, [{"memcmp": {"offset": _POSITION_POOL, "bytes": pool.address}}]
        )
    except DataStale:
        return LpLockRead(
            locked_pct=locked, burned_pct=None, positions=0, checked=("damm_v2_pool",)
        )
    positions: list[PositionView] = []
    for row in rows:
        parsed = _row_bytes(row)
        if parsed is None:
            continue
        address, raw = parsed
        try:
            positions.append(decode_position(raw, address=address))
        except ValueError:
            continue
    if len(positions) > _MAX_POSITIONS:
        return LpLockRead(
            locked_pct=locked,
            burned_pct=None,
            positions=len(positions),
            checked=("damm_v2_pool", "damm_v2_positions_truncated"),
        )
    burned_liquidity = 0
    total_liquidity = 0
    for position in positions:
        total_liquidity += position.total()
        supply = await _nft_supply(rpc, position.nft_mint)
        if supply is None:
            return LpLockRead(
                locked_pct=locked,
                burned_pct=None,
                positions=len(positions),
                checked=("damm_v2_pool", "damm_v2_positions"),
            )
        if supply == 0:
            burned_liquidity += position.total()
    burned: float | None
    if not positions:
        burned = 0.0
    else:
        burned = _pct(burned_liquidity, total_liquidity if total_liquidity else pool.liquidity)
    return LpLockRead(
        locked_pct=locked,
        burned_pct=burned,
        positions=len(positions),
        checked=("damm_v2_pool", "damm_v2_positions"),
    )


def select_pool(pools: list[DammPool]) -> DammPool | None:
    """The only pool, or the one WSOL reserve when several exist and one dominates.

    Two pools with the same positive SOL reserve are ambiguous and return None
    so the caller stays blind instead of picking one.
    """
    if not pools:
        return None
    if len(pools) == 1:
        return pools[0]
    ranked: list[tuple[int, DammPool]] = []
    for pool in pools:
        side = pool.sol_side()
        ranked.append((side[1] if side is not None else -1, pool))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked[0][0] < 0:
        return None
    if ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]
