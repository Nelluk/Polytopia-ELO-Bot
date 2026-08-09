"""Shared native service for league-token balance and history workflows."""

from __future__ import annotations

from dataclasses import replace
import logging

import discord

import settings
from modules import house_show, league_tokens_workers as workers, team_emoji


logger = logging.getLogger('polybot.' + __name__)

capture_actor = team_emoji.capture_actor
public_interaction_sender = team_emoji.public_interaction_sender


def _requester_level(member) -> int:
    try:
        return int(settings.get_user_level(member))
    except Exception:
        return 0


def native_access_error(member, guild_id: int) -> str | None:
    del member
    if not house_show._league_scope(guild_id):
        return 'League token commands are available only in the configured league server.'
    return None


def build_read_request(
    *,
    member,
    guild_id: int,
    house_lookup: str | None,
) -> workers.LeagueTokensReadRequest:
    return workers.LeagueTokensReadRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        league_scope=house_show._league_scope(guild_id),
        house_lookup=(str(house_lookup) if house_lookup is not None else None),
    )


def selected_house(result: workers.LeagueTokensReadResult):
    return next(
        (
            house for house in result.houses
            if house.house_id == result.selected_house_id
        ),
        None,
    )


def build_mutation_request(
    *,
    member,
    current: workers.LeagueTokensReadResult,
    new_balance: int,
    note: str | None,
) -> workers.LeagueTokensMutationRequest:
    house = selected_house(current)
    if house is None:
        raise workers.LeagueTokensValidationError(
            'Choose a House when supplying a new token balance.'
        )
    return workers.LeagueTokensMutationRequest(
        guild_id=int(current.guild_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        league_scope=house_show._league_scope(current.guild_id),
        house_id=int(house.house_id),
        expected_house_name=str(house.name),
        expected_balance=int(house.balance),
        new_balance=int(new_balance),
        note=(str(note) if note is not None else None),
        requester_description=capture_actor(member).identity,
    )


def apply_mutation(
    current: workers.LeagueTokensReadResult,
    mutation: workers.LeagueTokensMutationResult,
) -> workers.LeagueTokensReadResult:
    houses = tuple(
        replace(house, balance=mutation.new_balance)
        if house.house_id == mutation.house_id
        else house
        for house in current.houses
    )
    log = workers.LeagueTokenLog(
        log_id=mutation.log_id,
        house_id=mutation.house_id,
        timestamp=mutation.timestamp,
        message=mutation.audit_message,
    )
    existing = tuple(row for row in current.logs if row.log_id != mutation.log_id)
    return replace(
        current,
        houses=houses,
        logs=(log, *existing)[:workers.MAX_LOG_ROWS],
        selected_house_id=mutation.house_id,
    )


async def run_read(request):
    return await workers.run_league_tokens_read(request)


async def run_mutation(request):
    return await workers.run_league_tokens_mutation(request)


def _display(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


def mutation_banner(result, *, actor) -> str:
    note = f' · Note: {_display(result.note)}' if result.note else ''
    return (
        f'{actor.label} changed **{_display(result.house_name)}** league tokens '
        f'from `{result.old_balance}` to `{result.new_balance}`{note}.'
    )
