"""Owner-only application service for reviewed manual channel cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import logging

import discord

import settings
from modules import channels
from modules import operator_channel_purge_workers as workers


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class ManualPurgeOutcome:
    state: str
    preview: workers.ManualPurgePreview
    selected_keys: tuple[str, ...]
    deleted_count: int
    reconciled_count: int
    skipped_count: int
    failed_count: int
    reconciliation_count: int
    private_message: str


def _channel_snapshot(channel, guild) -> workers.ChannelSnapshot:
    category = getattr(channel, 'category', None)
    category_name = getattr(category, 'name', None)
    name = str(getattr(channel, 'name', f'channel-{channel.id}'))
    last_message_id = getattr(channel, 'last_message_id', None)
    last_activity_at = (
        discord.utils.snowflake_time(int(last_message_id))
        if last_message_id else None
    )
    try:
        manageable = bool(channel.permissions_for(guild.me).manage_channels)
    except Exception:
        manageable = False
    archive_protected = (
        'ARCHIVE' in name.upper()
        or bool(category_name and 'ARCHIVE' in str(category_name).upper())
    )
    return workers.ChannelSnapshot(
        channel_id=int(channel.id),
        name=name,
        category_id=(
            int(channel.category_id)
            if getattr(channel, 'category_id', None) else None
        ),
        category_name=str(category_name) if category_name else None,
        last_message_id=int(last_message_id) if last_message_id else None,
        last_activity_at=last_activity_at,
        manageable=manageable,
        archive_protected=archive_protected,
    )


def preview_request(interaction, mode: str) -> workers.ManualPurgePreviewRequest:
    guild = interaction.guild
    if guild is None:
        raise workers.ManualChannelPurgeError(
            'This command can only be used in a server.'
        )
    category_ids = tuple(dict.fromkeys(
        int(value)
        for value in (
            settings.guild_setting(guild.id, 'game_channel_categories') or ()
        )
    ))
    if not category_ids:
        raise workers.ManualChannelPurgeError(
            'This guild has no configured game-channel categories.'
        )
    return workers.ManualPurgePreviewRequest(
        guild_id=int(guild.id),
        requester_id=int(interaction.user.id),
        mode=str(mode),
        as_of=discord.utils.utcnow(),
        guild_channel_count=len(tuple(guild.channels)),
        configured_category_ids=category_ids,
        channels=tuple(
            _channel_snapshot(channel, guild)
            for channel in tuple(guild.text_channels)
        ),
    )


async def load_preview(interaction, mode: str):
    return await workers.run_load_manual_purge_preview(
        preview_request(interaction, mode)
    )


def _description(user) -> str:
    name = str(
        getattr(user, 'display_name', None)
        or getattr(user, 'name', None)
        or f'user-{user.id}'
    )
    return f'{name} (`{int(user.id)}`)'


async def _fetch_exact_channel(bot, guild_id: int, channel_id: int):
    try:
        channel = await bot.fetch_channel(int(channel_id))
    except discord.NotFound:
        return None
    if int(getattr(channel.guild, 'id', 0)) != int(guild_id):
        raise workers.ManualChannelPurgeError(
            f'Channel `{channel_id}` resolved outside the requested guild.'
        )
    return channel


def _matches_fresh_channel(candidate, channel, guild) -> bool:
    if channel is None:
        return bool(candidate.missing)
    if candidate.missing:
        return False
    snapshot = _channel_snapshot(channel, guild)
    return (
        snapshot.category_id == candidate.category_id
        and snapshot.last_message_id == candidate.last_message_id
        and snapshot.manageable
        and not snapshot.archive_protected
    )


async def _send_capacity_notices(bot, candidate) -> int:
    failed = 0
    for guild_id, channel_id in candidate.notice_targets:
        if int(channel_id) == int(candidate.channel_id):
            continue
        target_guild = bot.get_guild(int(guild_id))
        if target_guild is None:
            failed += 1
            continue
        try:
            await channels.send_message_to_channel(
                target_guild,
                channel_id=int(channel_id),
                message=(
                    'The central game channel for this game was manually '
                    'purged to free capacity on the server.'
                ),
                suppress_errors=False,
            )
        except Exception:
            failed += 1
            logger.exception(
                'Manual capacity-purge notice failed for game %s channel %s',
                candidate.game_id,
                channel_id,
            )
    return failed


async def _process_candidate(interaction, candidate):
    guild = interaction.guild
    bot = interaction.client
    authorized = await workers.run_authorize_manual_purge_candidate(
        workers.ManualPurgeAuthorizationRequest(
            guild_id=int(guild.id),
            requester_id=int(interaction.user.id),
            candidate=candidate,
            as_of=discord.utils.utcnow(),
        )
    )
    if not authorized:
        return 'skipped', 'Database ownership or protection state changed.'
    channel = await _fetch_exact_channel(
        bot, int(guild.id), int(candidate.channel_id)
    )
    if not _matches_fresh_channel(candidate, channel, guild):
        return 'skipped', 'Discord channel state changed after refresh.'

    if channel is not None:
        try:
            await channel.delete(
                reason=(
                    f'Owner manual game-channel purge: {candidate.mode}; '
                    f'game={candidate.game_id or "orphan"}'
                )
            )
        except discord.DiscordException as exc:
            logger.warning(
                'Manual channel purge could not delete %s: %s',
                candidate.channel_id,
                exc,
            )
            return 'failed', 'Discord deletion failed.'

    if candidate.kind == workers.ORPHAN_TARGET:
        logger.warning(
            'Owner %s manually purged orphan channel %s in guild %s',
            interaction.user.id,
            candidate.channel_id,
            guild.id,
        )
        return 'deleted', 'Orphan channel deleted.'

    try:
        result = await workers.run_reconcile_manual_purge(
            workers.ManualPurgeReconcileRequest(
                guild_id=int(guild.id),
                requester_id=int(interaction.user.id),
                requester_description=_description(interaction.user),
                candidate=candidate,
            )
        )
    except Exception:
        logger.exception(
            'Manual channel %s was deleted/absent but reconciliation failed',
            candidate.channel_id,
        )
        return 'reconciliation', 'Database reconciliation failed.'
    if result.status == workers.TARGET_CHANGED:
        return 'reconciliation', 'Database reference changed after deletion.'
    if result.status not in {workers.RECONCILED, workers.ALREADY_RECONCILED}:
        return 'reconciliation', f'Unexpected reconciliation state {result.status}.'

    notice_failures = 0
    if candidate.mode == workers.CAPACITY:
        notice_failures = await _send_capacity_notices(bot, candidate)
    if notice_failures:
        return 'reconciliation', (
            f'Reference cleared, but {notice_failures} capacity notice(s) failed.'
        )
    return 'reconciled', 'Channel deleted/absent and reference reconciled.'


async def _drain_target(task):
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break
    try:
        result = task.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation
    return result


class ManualPurgeCoordinator:
    """Reject overlapping owner purges in the same guild."""

    def __init__(self):
        self.active_guilds: set[int] = set()

    async def run(self, guild_id: int, operation):
        guild_id = int(guild_id)
        if guild_id in self.active_guilds:
            raise workers.ManualChannelPurgeError(
                'Another manual channel purge is already active in this guild.'
            )
        self.active_guilds.add(guild_id)
        try:
            return await operation()
        finally:
            self.active_guilds.discard(guild_id)


manual_purge_coordinator = ManualPurgeCoordinator()


async def confirm_purge(
    interaction,
    accepted_preview,
    selected_keys,
    confirmation_text,
):
    if int(interaction.user.id) != int(settings.owner_id):
        raise workers.ManualChannelPurgeError(
            'Only the configured bot owner can confirm channel deletion.'
        )
    selected_keys = tuple(dict.fromkeys(str(value) for value in selected_keys))
    if not selected_keys or len(selected_keys) > workers.MAX_SELECTED_CHANNELS:
        raise workers.ManualChannelPurgeError(
            f'Select between 1 and {workers.MAX_SELECTED_CHANNELS} channels.'
        )
    expected = f'PURGE {len(selected_keys)}'
    if confirmation_text != expected:
        raise workers.ManualChannelPurgeError(
            f'Type exactly `{expected}`. No channel was deleted.'
        )

    async def execute():
        fresh = await load_preview(interaction, accepted_preview.mode)
        fresh_by_key = {row.key: row for row in fresh.candidates}
        accepted_by_key = {row.key: row for row in accepted_preview.candidates}
        changed = [
            key for key in selected_keys
            if key not in fresh_by_key
            or key not in accepted_by_key
            or fresh_by_key[key].eligibility_token
            != accepted_by_key[key].eligibility_token
        ]
        if changed:
            retained = tuple(key for key in selected_keys if key in fresh_by_key)
            return ManualPurgeOutcome(
                state='refreshed',
                preview=fresh,
                selected_keys=retained,
                deleted_count=0,
                reconciled_count=0,
                skipped_count=len(changed),
                failed_count=0,
                reconciliation_count=0,
                private_message=(
                    'The selected candidate set changed during refresh. '
                    'Review the new preview; nothing was deleted.'
                ),
            )

        counts = {
            'deleted': 0,
            'reconciled': 0,
            'skipped': 0,
            'failed': 0,
            'reconciliation': 0,
        }
        details = []
        for key in selected_keys:
            candidate = fresh_by_key[key]
            try:
                state, detail = await _drain_target(asyncio.create_task(
                    _process_candidate(interaction, candidate)
                ))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    'Manual channel-purge target %s failed unexpectedly',
                    candidate.channel_id,
                )
                state, detail = 'failed', 'Unexpected target failure; inspect logs.'
            counts[state] += 1
            details.append(f'- `{candidate.channel_id}`: {detail}')
        terminal_state = (
            'reconciliation'
            if counts['reconciliation'] else
            ('partial' if counts['failed'] or counts['skipped'] else 'complete')
        )
        summary = (
            f'Deleted orphan: {counts["deleted"]}; reconciled tracked: '
            f'{counts["reconciled"]}; skipped: {counts["skipped"]}; failed: '
            f'{counts["failed"]}; reconciliation required: '
            f'{counts["reconciliation"]}.'
        )
        return ManualPurgeOutcome(
            state=terminal_state,
            preview=fresh,
            selected_keys=(),
            deleted_count=counts['deleted'],
            reconciled_count=counts['reconciled'],
            skipped_count=counts['skipped'],
            failed_count=counts['failed'],
            reconciliation_count=counts['reconciliation'],
            private_message='\n'.join([summary, *details])[:1900],
        )

    return await manual_purge_coordinator.run(
        int(interaction.guild_id), execute
    )
