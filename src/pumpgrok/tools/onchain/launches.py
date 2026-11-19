"""What counts as a pump.fun launch.

A launch is one bonding-curve account owned by the pump program whose
``creator`` field equals the wallet. The creator sits at byte offset 49 on
the 81, 82, 83 and 166-byte layouts. It is the curve's on-chain creator, not
the wallet that signed the create transaction and not the wallet's signature
count. A bundler can sign the create while naming this wallet as creator;
those curves still count. The legacy 49-byte layout has no creator field and
is not attributed.

The scan is ``getProgramAccounts`` with a memcmp on that offset. A failed or
malformed scan raises :class:`DataStale`. An empty successful scan is zero
launches, which is an observation, not a guess. :func:`count_creator_launches`
is the only implementation. ``onchain.deployer_history`` (SAFE-8) and the
SCOUT liquidity read both call it.
"""

from __future__ import annotations

import base64
from typing import Final

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.curve_layout import (
    CURVE_CREATOR_OFFSET,
    CURVE_LAYOUTS_WITH_CREATOR,
    decode_curve_account,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

PUMP_PROGRAM: Final[str] = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def creator_curve_accounts(rpc: RpcClient, creator: str) -> list[str]:
    """Bonding-curve addresses whose creator field is ``creator``.

    Raises :class:`DataStale` when the scan cannot be read. Does not return an
    empty list for a transport or RPC failure.
    """
    try:
        result = await rpc.call(
            "getProgramAccounts",
            [
                PUMP_PROGRAM,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "filters": [
                        {"memcmp": {"offset": CURVE_CREATOR_OFFSET, "bytes": creator}},
                    ],
                },
            ],
        )
    except RpcError as exc:
        raise DataStale(f"creator launch scan unreadable creator={creator}") from exc
    if not isinstance(result, list):
        raise DataStale(f"creator launch scan malformed creator={creator}")
    addresses: list[str] = []
    for row in result:
        parsed = _row(row)
        if parsed is None:
            continue
        address, raw = parsed
        if len(raw) not in CURVE_LAYOUTS_WITH_CREATOR:
            continue
        try:
            state = decode_curve_account(raw)
        except ValueError:
            continue
        if state.creator == creator:
            addresses.append(address)
    return addresses


async def count_creator_launches(rpc: RpcClient, creator: str) -> int:
    """Number of pump.fun bonding curves whose creator is ``creator``."""
    return len(await creator_curve_accounts(rpc, creator))


def _row(row: object) -> tuple[str, bytes] | None:
    if not isinstance(row, dict):
        return None
    pubkey = row.get("pubkey")
    account = row.get("account")
    if not isinstance(pubkey, str) or not isinstance(account, dict):
        return None
    data = account.get("data")
    if (
        not isinstance(data, list)
        or len(data) != 2
        or data[1] != "base64"
        or not isinstance(data[0], str)
    ):
        return None
    try:
        return pubkey, base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError):
        return None
