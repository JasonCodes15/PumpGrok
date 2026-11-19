"""Bonding-curve account decode over Solana RPC (STRUCT-002, B9).

The pump.fun ``BondingCurve`` account is an 8-byte Anchor discriminator
followed by the IDL field order: virtual token, virtual quote, real token,
real quote, token total supply, the completion flag, then the creator and
the trailing flags added since the creator field landed. Quote reserves are
lamports on a standard SOL pool.

Known sizes, all sharing that prefix:

- 49 bytes: the original account (no creator).
- 81, 82 and 83 bytes: creator, then ``is_mayhem_mode``, then ``is_cashback_coin``.
- 166 bytes: the current account (quote mint, fee fields, initial virtual quote).

A size outside that set does not decode. Nothing is guessed from a truncated
or unfamiliar tail. ``LAYOUT_STATUS`` stays ``ASSUMED`` until a human review
marks it verified; readers still decode these sizes, and RPC-derived paper
entries stay fail-closed while the status is not verified.

The 166-byte layout was checked against mainnet mint
``5Jy82TkEQJ9yByqU8aTEQrN7hgpBxRwzGK6g7Vcat7CE`` (real quote about 0 lamports,
virtual quote 30 SOL). Fixtures built by :func:`encode_curve_fixture` use the
same field order with a test discriminator.
"""

from __future__ import annotations

import base64
import struct
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from pumpgrok.core.units import lamports_to_sol, sol_to_lamports
from pumpgrok.tools._common import UNITS_PER_TOKEN

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Anchor account discriminator ``sha256("account:BondingCurve")[:8]``.
CURVE_DISCRIMINATOR: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")
#: Test-only discriminator written by :func:`encode_curve_fixture`.
FIXTURE_DISCRIMINATOR: Final[bytes] = b"ASSUMED\x00"

#: Byte offset of the creator pubkey. Present on every layout except the 49-byte one.
CURVE_CREATOR_OFFSET: Final[int] = 49

#: Layouts this decoder accepts. Anything else fails closed.
CURVE_KNOWN_SIZES: Final[frozenset[int]] = frozenset({49, 81, 82, 83, 166})
#: Layouts that store a creator pubkey at :data:`CURVE_CREATOR_OFFSET`.
CURVE_LAYOUTS_WITH_CREATOR: Final[frozenset[int]] = frozenset({81, 82, 83, 166})

#: Legacy fixture size (discriminator, five u64s, completion flag).
CURVE_ACCOUNT_SIZE = 49
#: Current mainnet bonding-curve account size.
CURVE_ACCOUNT_SIZE_CURRENT = 166

_BASE = struct.Struct("<8sQQQQQ")
_NATIVE_QUOTE = "1" * 32
_WSOL = "So11111111111111111111111111111111111111112"

#: Bonding-curve layout verification state (R65). A mainnet 166-byte account
#: decodes under this module, and the status stays ASSUMED until a human
#: reviews it. While ASSUMED, no paper entry may consume an RPC-derived price.
LAYOUT_STATUS: Final[str] = "ASSUMED"

#: Lamports of real SOL at which the curve is considered complete (catalog:
#: 85 SOL). Used only as the progress denominator.
COMPLETION_SOL_LAMPORTS = sol_to_lamports(Decimal(85))


class CurveDecodeError(ValueError):
    """The account bytes do not match a known bonding-curve layout."""


class CurveState(BaseModel):
    """Decoded bonding-curve counters. Money is int; ratios stay Decimal.

    ``virtual_*`` is the constant-product quote. ``real_sol_reserves_lamports``
    is the SOL actually sitting in the curve and is the liquidity figure.
    """

    model_config = ConfigDict(frozen=True)

    virtual_sol_reserves_lamports: int
    virtual_token_reserves_units: int
    real_sol_reserves_lamports: int
    real_token_reserves_units: int
    total_supply_units: int
    complete: bool
    creator: str | None = None
    is_mayhem_mode: bool | None = None
    is_cashback_coin: bool | None = None
    quote_mint: str | None = None
    creator_fee_bps: int | None = None
    can_edit_creator_fee: bool | None = None
    is_holder_reward: bool | None = None
    creator_fee_lamports: int | None = None
    protocol_fees_lamports: int | None = None
    depth: int | None = None
    initial_virtual_quote_lamports: int | None = None
    post_complete_base_out: int | None = None
    post_complete_quote_in: int | None = None

    def quote_is_sol(self) -> bool:
        """True when the quote reserve is SOL (legacy layout, native, or WSOL)."""
        if self.quote_mint is None:
            return True
        return self.quote_mint in {_NATIVE_QUOTE, _WSOL}


def encode_pubkey(raw: bytes) -> str:
    """Encode 32 bytes as a base58 pubkey."""
    if len(raw) != 32:
        raise CurveDecodeError(f"pubkey is {len(raw)} bytes")
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_BASE58[remainder])
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    # A zero integer adds no digits. Leading zero bytes are the leading ones.
    return "1" * pad + "".join(reversed(digits))


def decode_pubkey(text: str) -> bytes:
    """Decode a base58 pubkey to 32 bytes."""
    try:
        number = 0
        for char in text:
            number = number * 58 + _BASE58.index(char)
    except ValueError as exc:
        raise CurveDecodeError("pubkey is not base58") from exc
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    raw = b"\x00" * pad + body
    if len(raw) != 32:
        raise CurveDecodeError(f"pubkey decodes to {len(raw)} bytes")
    return raw


def _flag(data: bytes, offset: int, name: str) -> bool:
    value = data[offset]
    if value not in (0, 1):
        raise CurveDecodeError(f"curve flag {name} is not boolean")
    return value == 1


def _u64(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<Q", data, offset)[0])


def decode_curve_account(data: bytes) -> CurveState:
    """Decode a known bonding-curve layout.

    Raises :class:`CurveDecodeError` on a wrong size, discriminator, or flag.
    A real quote of zero is valid (a curve nobody has bought). A zero virtual
    reserve or supply is not: the spot price is undefined.
    """
    size = len(data)
    if size not in CURVE_KNOWN_SIZES:
        raise CurveDecodeError(
            f"curve account size {size} is not a known layout {sorted(CURVE_KNOWN_SIZES)}"
        )
    if data[:8] not in (CURVE_DISCRIMINATOR, FIXTURE_DISCRIMINATOR):
        raise CurveDecodeError("curve discriminator is not a bonding-curve account")
    try:
        (_, vtok, vsol, rtok, rsol, supply) = _BASE.unpack_from(data, 0)
    except struct.error as exc:
        raise CurveDecodeError(f"curve account unpack failed: {type(exc).__name__}") from exc
    complete = _flag(data, 48, "complete")
    for name, value in (
        ("virtual_token", vtok),
        ("virtual_sol", vsol),
        ("supply", supply),
    ):
        if value == 0:
            raise CurveDecodeError(f"curve reserve {name} is zero; price undefined")
    creator: str | None = None
    is_mayhem: bool | None = None
    is_cashback: bool | None = None
    quote_mint: str | None = None
    creator_fee_bps: int | None = None
    can_edit: bool | None = None
    is_holder: bool | None = None
    creator_fee: int | None = None
    protocol_fees: int | None = None
    depth: int | None = None
    initial_virtual: int | None = None
    post_base: int | None = None
    post_quote: int | None = None
    if size >= 81:
        creator = encode_pubkey(data[CURVE_CREATOR_OFFSET : CURVE_CREATOR_OFFSET + 32])
    if size >= 82:
        is_mayhem = _flag(data, 81, "is_mayhem_mode")
    if size >= 83:
        is_cashback = _flag(data, 82, "is_cashback_coin")
    if size == 166:
        quote_mint = encode_pubkey(data[83:115])
        creator_fee_bps = _u64(data, 115)
        can_edit = _flag(data, 123, "can_edit_creator_fee")
        is_holder = _flag(data, 124, "is_holder_reward")
        creator_fee = _u64(data, 125)
        protocol_fees = _u64(data, 133)
        depth = data[141]
        initial_virtual = _u64(data, 142)
        post_base = _u64(data, 150)
        post_quote = _u64(data, 158)
    return CurveState(
        virtual_sol_reserves_lamports=vsol,
        virtual_token_reserves_units=vtok,
        real_sol_reserves_lamports=rsol,
        real_token_reserves_units=rtok,
        total_supply_units=supply,
        complete=complete,
        creator=creator,
        is_mayhem_mode=is_mayhem,
        is_cashback_coin=is_cashback,
        quote_mint=quote_mint,
        creator_fee_bps=creator_fee_bps,
        can_edit_creator_fee=can_edit,
        is_holder_reward=is_holder,
        creator_fee_lamports=creator_fee,
        protocol_fees_lamports=protocol_fees,
        depth=depth,
        initial_virtual_quote_lamports=initial_virtual,
        post_complete_base_out=post_base,
        post_complete_quote_in=post_quote,
    )


def decode_curve_account_base64(data: object) -> CurveState:
    """Decode the ``data`` field of a base64 ``getAccountInfo`` value.

    Accepts only the ``["<base64>", "base64"]`` pair form. The ``jsonParsed``
    object form carries no curve bytes and is rejected as malformed.
    """
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise CurveDecodeError("curve account is not base64-encoded bytes")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise CurveDecodeError("curve account base64 is malformed") from exc
    return decode_curve_account(raw)


def encode_curve_fixture(
    *,
    virtual_sol_reserves_lamports: int,
    virtual_token_reserves_units: int,
    real_sol_reserves_lamports: int,
    real_token_reserves_units: int,
    total_supply_units: int,
    complete: bool,
    creator: str | None = None,
) -> bytes:
    """Build account bytes for tests.

    Without ``creator`` the bytes are the 49-byte layout. With ``creator`` they
    are the 81-byte layout. Field order matches the pump IDL (virtual token
    first). Never used in production.
    """
    body = _BASE.pack(
        FIXTURE_DISCRIMINATOR,
        virtual_token_reserves_units,
        virtual_sol_reserves_lamports,
        real_token_reserves_units,
        real_sol_reserves_lamports,
        total_supply_units,
    ) + bytes((1 if complete else 0,))
    if creator is None:
        return body
    return body + decode_pubkey(creator)


def curve_price_sol_per_token(state: CurveState) -> Decimal:
    """SOL per token from virtual reserves (the constant-product spot).

    Liquidity and the floor use real reserves. The spot stays on the virtual
    pair: that is the price the curve actually quotes.
    """
    return lamports_to_sol(
        Decimal(state.virtual_sol_reserves_lamports)
        / Decimal(state.virtual_token_reserves_units)
        * Decimal(UNITS_PER_TOKEN)
    )


def curve_progress(state: CurveState) -> Decimal:
    """Completion ratio 0..1 from real SOL against the 85 SOL bar."""
    ratio = Decimal(state.real_sol_reserves_lamports) / Decimal(COMPLETION_SOL_LAMPORTS)
    return min(Decimal(1), max(Decimal(0), ratio))


def curve_market_cap_lamports(state: CurveState) -> int:
    """Market cap in lamports: virtual SOL scaled by supply over virtual tokens."""
    return (
        state.virtual_sol_reserves_lamports * state.total_supply_units
    ) // state.virtual_token_reserves_units


def rpc_curve_price_allowed(*, curve_source: str) -> bool:
    """True when a price from ``curve_source`` may price a paper entry.

    RPC-derived reserves stay untrusted for entries while ``LAYOUT_STATUS`` is
    not VERIFIED. Fixture, rest and pumpportal ticks never pass through this
    layout, so replay is unaffected.
    """
    return not (curve_source == "rpc" and LAYOUT_STATUS != "VERIFIED")
