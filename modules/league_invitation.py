"""Discord orchestration for the production-only PolyChampions invitation task."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
import logging

import discord

import settings
from modules import league_invitation_workers as workers, models


logger = logging.getLogger('polybot.' + __name__)

INVITATION_MESSAGE = (
    'You have met the qualifications to be invited to the **PolyChampions** '
    'discord server! PolyChampions is a competitive Polytopia server '
    'organized into a league, with a focus on team (2v2 and 3v3) games.'
    '\n To join use this invite link: https://discord.gg/YcvBheS'
)


@dataclass(frozen=True)
class LeagueInvitationCycleResult:
    scanned_count: int
    eligible_count: int
    delivered_count: int
    already_recorded_count: int
    missing_member_count: int
    discord_failure_count: int
    persistence_failure_count: int
    truncated: bool
    next_after_member_id: int | None


def build_eligibility_request(*, as_of, after_member_id=None):
    era_start, era_end = models.moonrise_or_air_date_range()
    return workers.LeagueInvitationEligibilityRequest(
        as_of=as_of,
        polychampions_guild_id=int(settings.server_ids['polychampions']),
        global_guild_ids=tuple(
            int(guild_id)
            for guild_id in settings.servers_included_in_global_lb()
        ),
        era_start=era_start,
        era_end=era_end,
        after_member_id=(
            int(after_member_id) if after_member_id is not None else None
        ),
    )


async def run_invitation_cycle(*, bot, as_of=None, after_member_id=None):
    """Load eligibility, perform DMs, then record only successful deliveries."""

    as_of = as_of or datetime.datetime.now()
    guild = bot.get_guild(int(settings.server_ids['main']))
    if guild is None:
        logger.warning('Could not load the configured main guild for invitations.')
        return LeagueInvitationCycleResult(0, 0, 0, 0, 0, 0, 0, False, None)

    batch = await workers.run_load_invitation_eligibility(
        build_eligibility_request(
            as_of=as_of,
            after_member_id=after_member_id,
        )
    )
    logger.info(
        'Evaluated %s PolyChampions invitation candidates; %s eligible.',
        batch.scanned_count,
        len(batch.eligible),
    )
    for row in batch.evaluations:
        logger.debug(
            'Invitation evaluation %s (%s): W:%s L:%s recent:%s max:%s %s',
            row.name,
            row.discord_id,
            row.wins,
            row.losses,
            row.recent_games,
            row.elo_max_moonrise,
            row.reason,
        )
    if batch.truncated:
        logger.warning(
            'PolyChampions invitation scan reached the %s-member bound; the '
            'next task cycle will continue after database member %s.',
            workers.MAX_INVITATION_SCAN,
            batch.next_after_member_id,
        )

    delivered = already_recorded = missing = discord_failed = persistence_failed = 0
    for row in batch.eligible:
        guild_member = guild.get_member(row.discord_id)
        if guild_member is None:
            missing += 1
            logger.debug(
                'Could not load invitation candidate %s from guild %s.',
                row.discord_id,
                guild.id,
            )
            continue
        try:
            await guild_member.send(INVITATION_MESSAGE)
        except discord.DiscordException:
            discord_failed += 1
            logger.warning(
                'Discord rejected PolyChampions invitation for member %s.',
                row.discord_id,
                exc_info=True,
            )
            continue

        try:
            persisted = await workers.run_record_invitation_delivery(
                workers.LeagueInvitationDeliveryRequest(
                    member_id=row.member_id,
                    discord_id=row.discord_id,
                    sent_on=as_of.date(),
                )
            )
        except Exception:
            persistence_failed += 1
            logger.exception(
                'PolyChampions invitation was delivered to member %s but its '
                'sent date could not be persisted; reconcile before retry.',
                row.discord_id,
            )
            continue
        if persisted.recorded:
            delivered += 1
        else:
            already_recorded += 1

    return LeagueInvitationCycleResult(
        scanned_count=batch.scanned_count,
        eligible_count=len(batch.eligible),
        delivered_count=delivered,
        already_recorded_count=already_recorded,
        missing_member_count=missing,
        discord_failure_count=discord_failed,
        persistence_failure_count=persistence_failed,
        truncated=batch.truncated,
        next_after_member_id=batch.next_after_member_id,
    )
