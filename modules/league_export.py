"""Shared adapters for native and retained-prefix league exports."""

from __future__ import annotations

import io

import discord

import settings
from modules import house_show, league_export_workers as workers


def access_error(member, guild_id: int) -> str | None:
    if not house_show._league_scope(int(guild_id)):
        return 'League exports are available only in the configured league server.'
    if not settings.is_staff(member):
        return 'League exports require staff access.'
    return None


def request(*, member, guild, include_logs: bool) -> workers.LeagueExportRequest:
    return workers.LeagueExportRequest(
        guild_id=int(guild.id),
        requester_id=int(member.id),
        requester_is_staff=bool(settings.is_staff(member)),
        league_scope=bool(house_show._league_scope(int(guild.id))),
        include_logs=bool(include_logs),
        attachment_limit=int(
            getattr(guild, 'filesize_limit', workers.DEFAULT_ATTACHMENT_LIMIT)
        ),
    )


def discord_file(result: workers.LeagueExportResult) -> discord.File:
    return discord.File(io.BytesIO(result.payload), filename=result.filename)


async def run_export(
    request: workers.LeagueExportRequest,
) -> workers.LeagueExportResult:
    return await workers.run_league_export(request)
