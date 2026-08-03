"""Shared application service for game tribe reads and bulk edits."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

import discord

import settings
from modules import exceptions, game_workers, image_storage, models, utilities


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class GameTribeActor:
    """Safe, event-loop-captured identity for public native output."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> GameTribeActor:
    """Capture stable identity text before submitting database work."""

    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name),
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return GameTribeActor(
        discord_id=discord_id,
        mention=str(mention or f'<@{discord_id}>'),
        identity=f'**{safe_name}** (`{discord_id}`)',
    )


def _requester_level(member) -> int:
    try:
        level = int(settings.get_user_level(member))
    except (AttributeError, TypeError, ValueError, exceptions.CheckFailedError):
        level = 0
    try:
        if settings.is_staff(member):
            level = max(level, 5)
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        pass
    return level


def _requester_is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def build_read_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int,
    allow_related_channel: bool = False,
) -> game_workers.GameTribeReadRequest:
    """Capture a native read as primitive values only."""

    return game_workers.GameTribeReadRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        allow_related_channel=bool(allow_related_channel),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int | None = None,
    assignments: tuple[game_workers.GameTribeAssignmentInput, ...] = (),
    expected_snapshots: tuple[game_workers.GameTribeExpectedSnapshot, ...] = (),
    check_expected_snapshots: bool = False,
    raw_bulk: str | None = None,
    legacy_tokens: tuple[str, ...] = (),
    allow_related_channel: bool = False,
    native: bool = True,
    legacy_partial: bool = False,
    require_elevated: bool = False,
    invoked_with: str = 'settribe',
) -> game_workers.GameTribeMutationRequest:
    """Capture Discord/member values into an immutable worker request."""

    return game_workers.GameTribeMutationRequest(
        game_id=(int(game_id) if game_id is not None else None),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        requester_is_staff=_requester_is_staff(member),
        requester_description=models.GameLog.member_string(member),
        assignments=tuple(assignments),
        expected_snapshots=tuple(expected_snapshots),
        check_expected_snapshots=bool(check_expected_snapshots),
        raw_bulk=(str(raw_bulk) if raw_bulk is not None else None),
        legacy_tokens=tuple(str(value) for value in legacy_tokens),
        allow_related_channel=bool(allow_related_channel),
        native=bool(native),
        legacy_partial=bool(legacy_partial),
        require_elevated=bool(require_elevated),
        invoked_with=str(invoked_with),
    )


def expected_snapshots(
    result: game_workers.GameTribeReadResult,
) -> tuple[game_workers.GameTribeExpectedSnapshot, ...]:
    """Return the immutable lineup snapshot used by workspace mutations."""

    return tuple(result.expected_snapshots)


async def run_tribe_mutation(
    request: game_workers.GameTribeMutationRequest,
    *,
    after_commit=None,
) -> game_workers.GameTribeMutationResult:
    """Run one tribe batch under the keyed per-game claim."""

    if request.game_id is None:
        target = await game_workers.run_prepare_legacy_game_tribe(request)
        request = replace(
            request,
            game_id=target.game_id,
            legacy_tokens=target.assignment_tokens,
            allow_related_channel=target.inferred_from_channel,
        )

    game_id = int(request.game_id)
    locked = False
    try:
        utilities.lock_game(game_id)
        locked = True
        result = await game_workers.run_game_tribe_mutation(request)
    finally:
        if locked:
            # The worker wrapper drains a canceled synchronous transaction
            # before returning, so the claim cannot release early.
            utilities.unlock_game(game_id)
    if after_commit is not None:
        await after_commit(result)
    return result


async def run_tribe_read(
    request: game_workers.GameTribeReadRequest,
) -> game_workers.GameTribeReadResult:
    return await game_workers.run_game_tribe_read(request)


async def run_tribe_preview(
    request: game_workers.GameTribeMutationRequest,
) -> game_workers.GameTribeBatchPreview:
    return await game_workers.run_game_tribe_preview(request)


def _display(value: str | None) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _tribe_display(name: str | None, emoji: str = '') -> str:
    if not name:
        return 'None'
    return f'{_display(name)} {emoji}'.rstrip()


def read_message(
    result: game_workers.GameTribeReadResult,
    *,
    actor: GameTribeActor | None = None,
) -> str:
    lines = [f'Current player-to-tribe mapping for game {result.game_id}:']
    for row in result.players:
        lines.append(
            f'• **{_display(row.player_name)}** — '
            f'{_tribe_display(row.tribe_name, row.tribe_emoji)}'
        )
    if not result.players:
        lines.append('• No players are currently recorded in this game.')
    if actor is not None:
        lines.append(f'Requested by {actor.label}.')
    return '\n'.join(lines)


def workspace_message(
    result: game_workers.GameTribeReadResult,
    *,
    actor: GameTribeActor | None = None,
) -> str:
    return read_message(result, actor=actor)


def preview_message(
    preview: game_workers.GameTribeBatchPreview,
) -> str:
    lines = [
        f'Bulk tribe preview for game {preview.game_id}:',
    ]
    for assignment in preview.resolved_assignments:
        lines.append(
            f'• **{_display(assignment.player_name)}** → '
            f'{_tribe_display(assignment.tribe_name, assignment.tribe_emoji)}'
        )
    lines.append('No changes have been applied. Confirm or cancel this batch.')
    return '\n'.join(lines)


def native_mutation_message(
    result: game_workers.GameTribeMutationResult,
    *,
    actor: GameTribeActor,
) -> str:
    if not result.changes:
        return (
            f'{actor.label} submitted tribe changes for game {result.game_id}, '
            'but no player tribe values changed.'
        )
    lines = [
        f'{actor.label} updated tribes for game {result.game_id}:',
    ]
    for change in result.changes:
        lines.append(
            f'• **{_display(change.player_name)}** → '
            f'{_tribe_display(change.tribe_name, change.tribe_emoji)}'
        )
    return '\n'.join(lines)


def legacy_pair_message(
    outcome: game_workers.GameTribePairOutcome,
    *,
    game_id: int,
    permission_suffix: str,
) -> str:
    """Preserve the established prefix per-pair output vocabulary."""

    if outcome.valid:
        return (
            f'Player **{_display(outcome.player_name)}** assigned to tribe '
            f'*{_display(outcome.tribe_name or "None")}* in game {game_id} '
            f'{outcome.tribe_emoji}'
        )
    if outcome.error_kind == 'permission':
        return (
            f'Matching player not found in game {game_id} matching '
            f'"{utilities.escape_role_mentions(outcome.player_token)}". '
            f'Check spelling or be more specific.{permission_suffix}'
        )
    if outcome.error_kind == 'tribe':
        if outcome.error_detail == 'ambiguous':
            return (
                f'Matching Tribe is ambiguous for '
                f'"{discord.utils.escape_mentions(outcome.tribe_token)}". '
                f'Matches: {", ".join(_display(item) for item in outcome.matches)}. '
                f'Check spelling or be more specific.{permission_suffix}'
            )
        return (
            f'Matching Tribe not found matching '
            f'"{discord.utils.escape_mentions(outcome.tribe_token)}". '
            f'Check spelling or be more specific.{permission_suffix}'
        )
    if outcome.error_detail == 'ambiguous':
        return (
            f'Matching player is ambiguous in game {game_id} for '
            f'"{utilities.escape_role_mentions(outcome.player_token)}". '
            f'Matches: {", ".join(_display(item) for item in outcome.matches)}. '
            f'Check spelling or be more specific.{permission_suffix}'
        )
    return (
        f'Matching player not found in game {game_id} matching '
        f'"{utilities.escape_role_mentions(outcome.player_token)}". '
        f'Check spelling or be more specific.{permission_suffix}'
    )


def mutation_message(result: game_workers.GameTribeMutationResult) -> str:
    """Compatibility formatter for callers that only need a short summary."""

    return native_mutation_message(
        result,
        actor=GameTribeActor(0, '', '**unknown** (`0`)'),
    )


def public_interaction_sender(interaction):
    """Return a public sender that clears one private deferred response."""

    cleared = False

    async def send(content, **kwargs):
        nonlocal cleared
        if not cleared:
            cleared = True
            delete_original = getattr(
                interaction,
                'delete_original_response',
                None,
            )
            if delete_original is not None:
                try:
                    await delete_original()
                except Exception:
                    logger.exception(
                        'Could not clear the private deferred game-tribe '
                        'response before public output'
                    )
        channel = getattr(interaction, 'channel', None)
        channel_send = getattr(channel, 'send', None)
        if channel_send is None:
            raise RuntimeError('The interaction has no public channel sender.')
        return await channel_send(content, **kwargs)

    return send


async def _send_reconciliation_warning(send, content: str, game_id: int) -> None:
    try:
        await send(content)
    except Exception:
        logger.exception(
            'Committed game-tribe mutation %s reconciliation warning failed',
            game_id,
        )


async def reconcile_game_presentation(
    result: game_workers.GameTribeMutationResult,
    *,
    send,
    destination,
    guild,
    prefix: str,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Refresh the established announcement and dense game card post-commit."""

    if load_game is None:
        load_game = models.Game.load_full_game
    if send_game_embed is None:
        send_game_embed = image_storage.send_game_embed

    try:
        game = load_game(game_id=result.game_id)
    except Exception:
        logger.exception(
            'Committed game-tribe mutation %s could not reload its game '
            'presentation',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} tribe data was saved, but the '
            'post-commit game presentation could not be reloaded.',
            result.game_id,
        )
        return

    try:
        refreshed = await game.update_announcement(
            guild=guild,
            prefix=prefix,
        )
        if (
            refreshed is False
            and result.announcement_channel_id is not None
            and result.announcement_message_id is not None
        ):
            raise RuntimeError('the announcement refresh reported failure')
    except Exception:
        logger.exception(
            'Committed game-tribe mutation %s announcement refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} tribe data was saved, but the '
            'announcement refresh failed. An operator must reconcile the '
            'announcement.',
            result.game_id,
        )

    try:
        embed, content = game.embed(guild=guild, prefix=prefix)
        await send_game_embed(
            destination,
            game,
            embed=embed,
            content=content,
        )
    except Exception:
        logger.exception(
            'Committed game-tribe mutation %s dense game-card refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} tribe data was saved, but the '
            'dense game-card refresh failed. An operator must reconcile the '
            'game card.',
            result.game_id,
        )


async def publish_mutation_result(
    result: game_workers.GameTribeMutationResult,
    *,
    send,
    destination,
    guild,
    prefix: str,
    actor: GameTribeActor,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Publish a native committed summary, then reconcile all presentations."""

    try:
        await send(native_mutation_message(result, actor=actor))
    except Exception:
        logger.exception(
            'Committed game-tribe mutation %s could not publish success',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} tribe data was saved, but the '
            'public success message could not be sent. An operator must '
            'reconcile the game card and audit trail.',
            result.game_id,
        )
    await reconcile_game_presentation(
        result,
        send=send,
        destination=destination,
        guild=guild,
        prefix=prefix,
        load_game=load_game,
        send_game_embed=send_game_embed,
    )


async def publish_legacy_mutation_result(
    result: game_workers.GameTribeMutationResult,
    *,
    send,
    destination,
    guild,
    prefix: str,
    requester_level: int,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Publish legacy per-pair feedback only after its transaction commits."""

    permission_suffix = ''
    if requester_level < 4:
        permission_suffix = (
            ' You only have permissions to set your own tribe. '
            f'**Example usage:** `{prefix}settribe 1234 bardur`'
        )
    for outcome in result.outcomes:
        try:
            await send(
                legacy_pair_message(
                    outcome,
                    game_id=result.game_id,
                    permission_suffix=permission_suffix,
                )
            )
        except Exception:
            logger.exception(
                'Committed game-tribe mutation %s pair feedback failed',
                result.game_id,
            )
            await _send_reconciliation_warning(
                send,
                f':warning: Game {result.game_id} tribe data was saved, but '
                'some per-player feedback could not be sent.',
                result.game_id,
            )
    await reconcile_game_presentation(
        result,
        send=send,
        destination=destination,
        guild=guild,
        prefix=prefix,
        load_game=load_game,
        send_game_embed=send_game_embed,
    )
