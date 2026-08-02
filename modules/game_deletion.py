"""Shared application service for every game-deletion entry point.

Prefix commands, ``/game delete``, and the pending-game card all build the
same frozen request, call :func:`delete_game`, and publish the returned
immutable effect plan through :func:`publish_result`.  Database workers never
receive Discord objects and never perform Discord I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import logging
from typing import Awaitable, Callable

import settings
from modules import channels, elo_workers, game_deletion_workers, game_detail_views
from modules import models, utilities
from modules.elo_jobs import EloJobConflict


logger = logging.getLogger('polybot.' + __name__)


GameDeletionValidationError = game_deletion_workers.GameDeletionValidationError
PendingGameDeletionValidationError = (
    game_deletion_workers.PendingGameDeletionValidationError
)


@dataclass(frozen=True)
class DeletionResult:
    """Uniform application result for pending and ELO-backed deletion."""

    game_id: int
    state: str
    recalculated: bool
    effect_plan: game_deletion_workers.DeletionEffectPlan

    @property
    def plan(self) -> game_deletion_workers.DeletionEffectPlan:
        """Short alias useful to presentation adapters."""

        return self.effect_plan


def build_request(
    *,
    game_id: int,
    member,
    guild_id: int | None = None,
    prefix: str | None = None,
    invoked_with: str = 'delete',
) -> game_deletion_workers.DeletionRequest:
    """Capture Discord-only values before entering the worker boundary."""

    member_guild = getattr(member, 'guild', None)
    resolved_guild_id = int(guild_id or member_guild.id)
    resolved_prefix = prefix
    if resolved_prefix is None:
        resolved_prefix = settings.guild_setting(
            resolved_guild_id,
            'command_prefix',
        )
    try:
        requester_is_staff = bool(settings.is_staff(member))
    except AttributeError:
        requester_is_staff = False
    try:
        requester_is_mod = bool(settings.is_mod(member))
    except AttributeError:
        requester_is_mod = False
    return game_deletion_workers.DeletionRequest(
        game_id=int(game_id),
        guild_id=resolved_guild_id,
        requester_id=int(member.id),
        requester_name=str(getattr(member, 'display_name', '') or member.name),
        requester_description=models.GameLog.member_string(member),
        requester_is_staff=requester_is_staff,
        requester_is_mod=requester_is_mod,
        prefix=str(resolved_prefix),
        invoked_with=str(invoked_with),
    )


def _authorize(
    request: game_deletion_workers.DeletionRequest,
    classification: game_deletion_workers.DeletionClassification,
) -> None:
    """Apply the one permission policy shared by all adapters."""

    if not classification.registered:
        raise GameDeletionValidationError(
            'This command requires bot registration first. Type '
            f'__`{request.prefix}setname Your Mobile Name`__ or  '
            f'__`{request.prefix}steamname Your Steam Username`__ '
            'to get started.'
        )

    if classification.state == game_deletion_workers.PENDING:
        if (
            classification.host_id != request.requester_id
            and not request.requester_is_staff
        ):
            host_name = (
                f' **{classification.host_name}**'
                if classification.host_name else ''
            )
            raise GameDeletionValidationError(
                f'Only the game host{host_name} or server staff can do this.'
            )
        return

    if not request.requester_is_mod:
        raise GameDeletionValidationError(
            'Only server mods can delete completed or in-progress games.'
        )


async def authorize_delete(
    request: game_deletion_workers.DeletionRequest,
) -> game_deletion_workers.DeletionClassification:
    """Revalidate state and permission without mutating the game."""

    classification = await game_deletion_workers.run_classify_game_deletion(
        request,
    )
    _authorize(request, classification)
    return classification


def _fallback_plan(
    request: game_deletion_workers.DeletionRequest,
    classification: game_deletion_workers.DeletionClassification,
) -> game_deletion_workers.DeletionEffectPlan:
    if classification.state == game_deletion_workers.PENDING:
        message = (
            f'Deleting open game {request.game_id}\n'
            'Notifying players: '
        )
    else:
        message = (
            f'Game with ID {request.game_id} has been deleted and team/player '
            'ELO changes have been reverted, if applicable.\n'
            'Notifying players: '
        )
    return game_deletion_workers.DeletionEffectPlan(
        game_id=request.game_id,
        guild_id=request.guild_id,
        state=classification.state,
        mentions=(),
        public_message=message,
    )


async def _run_elo_deletion(
    request: game_deletion_workers.DeletionRequest,
    classification: game_deletion_workers.DeletionClassification,
) -> DeletionResult:
    coordinator = settings.elo_job_coordinator
    lock_acquired = False

    def lock_game() -> None:
        nonlocal lock_acquired
        utilities.lock_game(request.game_id)
        lock_acquired = True

    def unlock_game() -> None:
        if lock_acquired:
            utilities.unlock_game(request.game_id)

    result = await coordinator.run(
        operation='delete_game',
        game_id=request.game_id,
        requester_id=request.requester_id,
        requester_name=request.requester_name,
        worker=elo_workers.delete_game,
        worker_args=(
            request.game_id,
            request.guild_id,
            request.requester_description,
        ),
        before_submit=lock_game,
        after_complete=unlock_game,
    )
    effect_plan = getattr(result, 'effect_plan', None) or _fallback_plan(
        request,
        classification,
    )
    return DeletionResult(
        game_id=int(result.game_id),
        state=effect_plan.state,
        recalculated=bool(result.recalculated),
        effect_plan=effect_plan,
    )


async def delete_game(
    request: game_deletion_workers.DeletionRequest,
) -> DeletionResult:
    """Classify, authorize, dispatch, and return one committed deletion."""

    classification = await authorize_delete(request)
    if classification.state == game_deletion_workers.PENDING:
        try:
            result = await game_deletion_workers.run_pending_game_deletion(
                request,
            )
        except game_deletion_workers.PendingGameDeletionStateChanged:
            # A start/unstart race can change state after classification but
            # before the pending coordinator gets the worker. Reclassify and
            # use the ELO path only when the new state and permission policy
            # authorize it; never delete through the wrong transaction.
            classification = await authorize_delete(request)
            if classification.state == game_deletion_workers.PENDING:
                raise
            return await _run_elo_deletion(request, classification)
        effect_plan = result.effect_plan
        return DeletionResult(
            game_id=int(result.game_id),
            state=effect_plan.state,
            recalculated=bool(result.recalculated),
            effect_plan=effect_plan,
        )
    try:
        return await _run_elo_deletion(request, classification)
    except elo_workers.DeleteValidationError:
        # The serialized ELO worker reloads state inside its transaction.  If
        # a concurrent start/unstart changed the row to pending after the
        # initial classification, its safe rejection is the handoff point to
        # the pending coordinator.  Reclassify and reauthorize before using
        # that different transaction boundary.
        classification = await authorize_delete(request)
        if classification.state != game_deletion_workers.PENDING:
            raise
        result = await game_deletion_workers.run_pending_game_deletion(request)
        effect_plan = result.effect_plan
        return DeletionResult(
            game_id=int(result.game_id),
            state=effect_plan.state,
            recalculated=bool(result.recalculated),
            effect_plan=effect_plan,
        )


async def _send_post_commit_message(
    send: Callable[[str], Awaitable],
    content: str,
    *,
    game_id: int,
    effect: str,
) -> None:
    """Publish committed text and attempt an operator-visible warning."""

    try:
        await send(content)
    except Exception:
        logger.exception(
            'Committed game %s public %s failed',
            game_id,
            effect,
        )
        warning = (
            f':warning: Game {game_id} was deleted successfully, but the '
            f'{effect} could not be published. An operator must reconcile '
            'the public Discord state.'
        )
        try:
            await send(warning)
        except Exception:
            logger.exception(
                'Committed game %s reconciliation warning failed after %s '
                'send failure',
                game_id,
                effect,
            )


async def _publish_broadcasts(
    plan: game_deletion_workers.DeletionEffectPlan,
    *,
    bot,
    send: Callable[[str], Awaitable],
) -> None:
    for target in plan.broadcast_targets:
        try:
            channel = bot.get_channel(target.channel_id)
            if channel is None and hasattr(bot, 'fetch_channel'):
                channel = await bot.fetch_channel(target.channel_id)
            message = (
                await channel.fetch_message(target.message_id)
                if channel is not None else None
            )
            if message is None:
                raise LookupError('broadcast message was not found')
            await message.edit(
                content=(
                    f'~~{message.content}~~\n'
                    '(This game has been deleted and can no longer be joined.)'
                )
            )
            await message.clear_reactions()
        except Exception:
            logger.exception(
                'Could not update external broadcast for deleted game %s '
                '(%s/%s)',
                plan.game_id,
                target.channel_id,
                target.message_id,
            )
            await _send_post_commit_message(
                send,
                f':warning: Game {plan.game_id} was deleted successfully, '
                'but an external game announcement could not be updated. '
                'An operator must reconcile the announcement.',
                game_id=plan.game_id,
                effect='external broadcast update',
            )


async def _publish_announcement(
    plan: game_deletion_workers.DeletionEffectPlan,
    *,
    guild,
    bot,
    prefix: str,
    send: Callable[[str], Awaitable],
) -> None:
    target = plan.announcement
    if target is None:
        return
    try:
        target_guild = guild
        if target.guild_id != getattr(guild, 'id', None):
            target_guild = bot.get_guild(target.guild_id)
        channel = (
            target_guild.get_channel(target.channel_id)
            if target_guild is not None else None
        )
        if channel is None:
            raise LookupError('announcement channel was not found')
        message = await channel.fetch_message(target.message_id)
        if message is None or target.snapshot is None:
            raise LookupError('announcement message or snapshot was not found')
        deleted_snapshot = replace(
            target.snapshot,
            name=f'~~{target.snapshot.name}~~ GAME DELETED',
        )
        display = game_detail_views.resolve_display(
            deleted_snapshot,
            guild=target_guild,
            bot=bot,
            prefix=prefix,
        )
        rendered = game_detail_views.render_classic_game_detail(display)
        await message.edit(**game_detail_views.classic_edit_kwargs(
            message,
            rendered,
            view=None,
        ))
    except Exception:
        logger.exception(
            'Could not update announcement for deleted game %s',
            plan.game_id,
        )
        await _send_post_commit_message(
            send,
            f':warning: Game {plan.game_id} was deleted successfully, but its '
            'announcement could not be updated. An operator must reconcile '
            'the announcement.',
            game_id=plan.game_id,
            effect='announcement update',
        )


async def _publish_channels(
    plan: game_deletion_workers.DeletionEffectPlan,
    *,
    guild,
    bot,
    send: Callable[[str], Awaitable],
) -> None:
    for target in plan.channel_targets:
        target_guild = (
            guild
            if target.guild_id == getattr(guild, 'id', None)
            else bot.get_guild(target.guild_id)
        )
        if target_guild is None:
            logger.warning(
                'Could not load guild %s for deleted game channel %s',
                target.guild_id,
                target.channel_id,
            )
            continue
        try:
            deleted = await channels.delete_game_channel(
                target_guild,
                channel_id=target.channel_id,
            )
            if deleted is False:
                raise RuntimeError('channel deletion was not completed')
        except Exception:
            logger.exception(
                'Could not delete channel %s for deleted game %s',
                target.channel_id,
                plan.game_id,
            )
            await _send_post_commit_message(
                send,
                f':warning: Game {plan.game_id} was deleted successfully, '
                f'but channel `{target.channel_id}` could not be deleted. '
                'An operator must reconcile the channel.',
                game_id=plan.game_id,
                effect='game-channel deletion',
            )


async def publish_result(
    result: DeletionResult,
    *,
    send: Callable[[str], Awaitable],
    guild,
    bot,
    prefix: str,
) -> None:
    """Apply the immutable post-commit effect plan in legacy order."""

    plan = result.effect_plan
    if plan.state == game_deletion_workers.PENDING:
        await _publish_broadcasts(plan, bot=bot, send=send)
        await _send_post_commit_message(
            send,
            plan.public_message,
            game_id=result.game_id,
            effect='deletion output',
        )
        return

    await _publish_announcement(
        plan,
        guild=guild,
        bot=bot,
        prefix=prefix,
        send=send,
    )
    await _send_post_commit_message(
        send,
        plan.public_message,
        game_id=result.game_id,
        effect='deletion output',
    )
    await _publish_channels(
        plan,
        guild=guild,
        bot=bot,
        send=send,
    )
