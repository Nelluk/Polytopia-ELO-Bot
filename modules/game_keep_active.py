"""Discord-facing keep-active adapters and post-commit publication."""

from __future__ import annotations

import datetime
import logging

from modules import game_keep_active_workers as workers

logger = logging.getLogger('polybot.' + __name__)


def actor_description(user) -> str:
    return f'<@{int(user.id)}>'


def request(
    *, game_id: int, user, guild_id: int | None, channel_id: int | None,
    is_staff: bool, button: bool = False, as_of: datetime.date | None = None,
    protected_through: datetime.date | None = None,
):
    return workers.KeepActiveRequest(
        game_id=int(game_id),
        actor_id=int(user.id),
        actor_description=actor_description(user),
        invocation_guild_id=(int(guild_id) if guild_id is not None else None),
        invocation_channel_id=(
            int(channel_id) if channel_id is not None else None
        ),
        require_warning_target=button,
        actor_is_staff=bool(is_staff),
        as_of=as_of,
        warning_protected_through=protected_through,
    )


def success_message(result: workers.KeepActiveResult) -> str:
    return (
        f'<@{result.actor_id}> kept game **{result.game_id}** active. '
        f'Cleanup is deferred until **{result.new_protected_through.isoformat()}**.'
    )


async def run(request_value):
    return await workers.run_keep_active(request_value)


async def respond(interaction, result=None, error: Exception | None = None):
    if error is not None:
        message = str(error)
        ephemeral = True
    else:
        message = success_message(result)
        ephemeral = False
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(message, ephemeral=ephemeral)
    except Exception:
        if error is None:
            logger.exception(
                'Committed keep-active game %s could not publish; do not retry '
                'the database mutation.',
                result.game_id,
            )
        raise


def _sendable_channel(interaction):
    channel = getattr(interaction, 'channel', None)
    if channel is None or not callable(getattr(channel, 'send', None)):
        raise workers.KeepActiveError(
            'The invocation channel cannot receive the public keep-active notice.'
        )
    return channel


async def _publish_success(interaction, result):
    channel = _sendable_channel(interaction)
    try:
        await channel.send(success_message(result))
    except Exception:
        logger.exception(
            'Committed keep-active game %s could not publish its public notice; '
            'do not retry the database mutation.',
            result.game_id,
        )
        terminal = (
            f'Keep-active for game {result.game_id} committed through '
            f'{result.new_protected_through.isoformat()}, but its public notice '
            'failed. Reconcile the notice manually; do not retry the mutation.'
        )
        try:
            await interaction.followup.send(terminal, ephemeral=True)
        except Exception:
            logger.exception(
                'Could not publish keep-active terminal reconciliation for game %s',
                result.game_id,
            )
        return False
    await interaction.followup.send(
        'Keep-active committed and posted publicly.', ephemeral=True,
    )
    return True


async def run_button(
    interaction,
    *,
    game_id: int,
    protected_through: datetime.date | None = None,
):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    try:
        _sendable_channel(interaction)
    except workers.KeepActiveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return None
    user = interaction.user
    # The cog-level permission helper is intentionally not used here: button
    # requests must resolve the game globally before applying the owning-guild
    # staff rule, and the worker performs that authoritative check.
    import settings
    try:
        value = await run(request(
            game_id=game_id,
            user=user,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            is_staff=settings.is_staff(user),
            button=True,
            protected_through=protected_through,
        ))
    except workers.KeepActiveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return None
    except Exception:
        logger.exception('Keep-active button failed for game %s', game_id)
        await interaction.followup.send(
            'The game could not be kept active. No database change was committed.',
            ephemeral=True,
        )
        return None
    await _publish_success(interaction, value)
    return value


async def run_slash(interaction, *, game_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        _sendable_channel(interaction)
    except workers.KeepActiveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return None
    user = interaction.user
    import settings
    try:
        value = await run(request(
            game_id=game_id,
            user=user,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            is_staff=settings.is_staff(user),
        ))
    except workers.KeepActiveError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return None
    except Exception:
        logger.exception('Slash keep-active failed for game %s', game_id)
        await interaction.followup.send(
            'The game could not be kept active. No database change was committed.',
            ephemeral=True,
        )
        return None
    await _publish_success(interaction, value)
    return value
