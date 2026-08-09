"""Discord-facing service for native league trade-price reads."""

from __future__ import annotations

import discord

from modules import house_show, interaction_lifecycle
from modules import league_trade_price_workers as workers


LEADERSHIP_ROLES = frozenset({'House Leader', 'House Co-Leader'})


def access_error(guild_id: int) -> str | None:
    if not house_show._league_scope(int(guild_id)):
        return 'Trade prices are available only in the configured league server.'
    return None


def request(*, guild, player, ending_season: int | None) -> workers.TradePriceRequest:
    role_names = {
        str(getattr(role, 'name', ''))
        for role in tuple(getattr(player, 'roles', ()) or ())
    }
    return workers.TradePriceRequest(
        guild_id=int(guild.id),
        player_discord_id=int(player.id),
        player_display_name=str(player.display_name),
        ending_season=(int(ending_season) if ending_season is not None else None),
        leadership_adjustment=bool(role_names & LEADERSHIP_ROLES),
    )


def public_message(actor, result: workers.TradePriceResult) -> str:
    if result.inference == 'explicit':
        season_note = f'Ending season: **{result.ending_season}** (selected)'
    elif result.inference == 'previous_due_to_incomplete':
        season_note = (
            f'Ending season: **{result.ending_season}** because the player has '
            f'an incomplete/unconfirmed Season {result.current_season} game'
        )
    else:
        season_note = f'Ending season: **{result.ending_season}** (current)'

    lines = [
        f'{actor.mention} calculated the trade price for '
        f'<@{result.player_discord_id}>.',
        f'## Trade price: **{result.price}**',
        season_note,
        'Leadership adjustment: '
        + ('**applied**' if result.leadership_adjustment else 'not applied'),
        '',
        '**Three-season inputs**',
    ]
    for row in result.seasons:
        if row.tier is None or row.games == 0:
            lines.append(f'- Season {row.season}: no qualifying games')
        else:
            lines.append(
                f'- Season {row.season}: Tier {row.tier}, '
                f'{row.wins}-{row.losses} ({row.games} games)'
            )
    return '\n'.join(lines)


public_interaction_sender = interaction_lifecycle.public_interaction_sender
run_trade_price = workers.run_trade_price
