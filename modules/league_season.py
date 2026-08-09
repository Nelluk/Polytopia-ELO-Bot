"""Shared service and renderers for league season records."""

from __future__ import annotations

import discord

import settings
from modules import house_show, league_season_workers as workers


NATIVE_PAGE_ROWS = 12


def _league_scope(guild_id: int) -> bool:
    return bool(house_show._league_scope(int(guild_id)))


def _channel_allowed(member, guild_id: int, channel_id: int | None) -> bool:
    return bool(house_show._channel_allowed(member, int(guild_id), channel_id))


def _tier_labels() -> tuple[tuple[int, str], ...]:
    return tuple(
        (int(value[0]), str(value[1]))
        for value in settings.league_tiers
        if len(value) > 1
    )


def native_access_error(member, guild_id: int, channel_id: int | None) -> str | None:
    if not _league_scope(guild_id):
        return 'League season records are available only in the configured league server.'
    if not _channel_allowed(member, guild_id, channel_id):
        return 'This command can only be used in a designated ELO bot channel.'
    return None


def build_request(*, member, guild_id: int, channel_id: int | None, season):
    return workers.LeagueSeasonRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        season=(int(season) if season is not None else None),
        league_scope=_league_scope(guild_id),
        channel_allowed=_channel_allowed(member, guild_id, channel_id),
        tier_labels=_tier_labels(),
    )


def _escape(value) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


def team_line(row: workers.LeagueSeasonTeamRow) -> str:
    return (
        f'{row.team_emoji} **{_escape(row.team_name)}**\n'
        f'`{str(row.regular_wins) + "W":.<3} '
        f'{str(row.regular_losses) + "L":.<3} '
        f'{str(row.regular_incomplete) + "I":.<3} - '
        f'{str(row.postseason_wins) + "W":.<3} '
        f'{str(row.postseason_losses) + "L":.<3} '
        f'{row.postseason_incomplete}I`'
    ).replace('.', '\u200b ')


def legacy_text(result: workers.LeagueSeasonResult) -> str:
    if result.historical_note:
        return result.historical_note
    output = [f'__**{result.title}**__']
    for tier in result.tiers:
        output.append(
            f'\n__**{tier.tier_name} Tier**__\n'
            '`Regular \u200b \u200b \u200b \u200b \u200b Post-Season`'
        )
        output.extend(team_line(row) for row in tier.teams)
    if not result.tiers:
        output.append('\n*No league records were found for this selection.*')
    if result.rows_truncated:
        output.append('\n*Result truncated at the configured safety limit.*')
    return '\n'.join(output)


def native_pages(result: workers.LeagueSeasonResult) -> tuple[str, ...]:
    if result.historical_note:
        return (f'# {result.title}\n{result.historical_note}',)
    pages = []
    for tier in result.tiers:
        rows = tier.teams or ()
        for start in range(0, max(1, len(rows)), NATIVE_PAGE_ROWS):
            page_rows = rows[start:start + NATIVE_PAGE_ROWS]
            body = '\n'.join(team_line(row) for row in page_rows)
            if not body:
                body = '*No teams were found in this tier.*'
            pages.append(
                f'# {result.title}\n'
                f'## {tier.tier_name} Tier\n'
                '`Regular \u200b \u200b \u200b \u200b \u200b Post-Season`\n'
                f'{body}'
            )
    if not pages:
        pages.append(
            f'# {result.title}\n*No league records were found for this selection.*'
        )
    if result.rows_truncated:
        pages[-1] += '\n\n*Result truncated at the configured safety limit.*'
    return tuple(pages)
