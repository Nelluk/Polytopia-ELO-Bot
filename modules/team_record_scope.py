"""Explicit ownership scope for persistent Team records.

PCPLUS is a PolyChampions event venue, not an independent Team registry. Keep
this exception narrow: it is not a configurable federation mechanism and does
not alter the invoking Discord guild used for permissions or output.
"""

from __future__ import annotations


POLYCHAMPIONS_GUILD_ID = 447883341463814144
PCPLUS_GUILD_ID = 1289762588346814495


def persistent_team_guild_id(invoking_guild_id: int) -> int:
    """Return the guild that owns persistent Team rows for one invocation."""

    guild_id = int(invoking_guild_id)
    if guild_id == PCPLUS_GUILD_ID:
        return POLYCHAMPIONS_GUILD_ID
    return guild_id
