"""Shared application service for the focused game-notes attribute."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import re

import discord

import settings
from modules import exceptions, game_workers, image_storage, models, utilities


logger = logging.getLogger('polybot.' + __name__)

MENTION_WARNING = (
    '**Warning**: Updated notes included role/user mentions. This will not '
    'impact who is allowed to join the game and will only change the content '
    'of the notes.'
)


@dataclass(frozen=True)
class GameNotesActor:
    """Safe, event-loop-captured identity for public native notes output."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        """Keep a mention and a readable fallback visible together."""

        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> GameNotesActor:
    """Capture only stable identity text before submitting notes work."""

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
    mention = str(mention or f'<@{discord_id}>')
    return GameNotesActor(
        discord_id=discord_id,
        mention=mention,
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


def contains_note_mentions(value: str | None) -> bool:
    """Recognize user and role mention syntax in native modal text."""

    return bool(re.search(r'<@!?\d{1,21}>|<@&\d{1,21}>', str(value or '')))


def build_read_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int | None,
    allow_related_channel: bool = False,
    legacy_tokens: tuple[str, ...] = (),
) -> game_workers.GameNotesReadRequest:
    """Capture a read request as primitive values only."""

    return game_workers.GameNotesReadRequest(
        game_id=(int(game_id) if game_id is not None else None),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        allow_related_channel=bool(allow_related_channel),
        legacy_tokens=tuple(str(value) for value in legacy_tokens),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int | None = None,
    notes: str | None = None,
    clear: bool = False,
    expected_notes: str | None = None,
    check_expected_notes: bool = False,
    legacy_tokens: tuple[str, ...] = (),
    allow_related_channel: bool = False,
    invoked_with: str = 'gamenotes',
    prefix: str = '$',
    truncate: bool = False,
    legacy_none: bool = False,
    mention_warning: bool = False,
) -> game_workers.GameNotesMutationRequest:
    """Capture Discord/member values into an immutable worker request."""

    return game_workers.GameNotesMutationRequest(
        game_id=(int(game_id) if game_id is not None else None),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        requester_is_staff=_requester_is_staff(member),
        requester_description=models.GameLog.member_string(member),
        notes=(str(notes) if notes is not None else None),
        clear=bool(clear),
        expected_notes=(
            str(expected_notes) if expected_notes is not None else None
        ),
        check_expected_notes=bool(check_expected_notes),
        legacy_tokens=tuple(str(value) for value in legacy_tokens),
        allow_related_channel=bool(allow_related_channel),
        invoked_with=str(invoked_with),
        prefix=str(prefix or '$'),
        truncate=bool(truncate),
        legacy_none=bool(legacy_none),
        mention_warning=bool(mention_warning),
    )


async def run_notes_mutation(
    request: game_workers.GameNotesMutationRequest,
    *,
    after_commit=None,
) -> game_workers.GameNotesMutationResult:
    """Run a notes mutation under the existing keyed game claim."""

    if request.game_id is None:
        target = await game_workers.run_prepare_legacy_game_notes(request)
        request = replace(
            request,
            game_id=target.game_id,
            legacy_tokens=(),
        )

    game_id = int(request.game_id)
    locked = False
    try:
        utilities.lock_game(game_id)
        locked = True
        result = await game_workers.run_game_notes_mutation(request)
    finally:
        if locked:
            # Release immediately after the synchronous transaction, before
            # any public output, card refresh, or warning can await Discord.
            utilities.unlock_game(game_id)
    if after_commit is not None:
        await after_commit(result)
    return result


async def run_notes_read(
    request: game_workers.GameNotesReadRequest,
) -> game_workers.GameNotesReadResult:
    """Run the separately bounded current-value read."""

    return await game_workers.run_game_notes_read(request)


def read_message(
    result: game_workers.GameNotesReadResult,
    *,
    actor: GameNotesActor | None = None,
) -> str:
    value = result.notes or 'None'
    message = f'Current notes for game {result.game_id}: {value}'
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def workspace_message(
    game_id: int,
    notes: str | None,
    *,
    actor: GameNotesActor | None = None,
) -> str:
    value = notes or 'None'
    message = f'Current notes for game {int(game_id)}: {value}'
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def mutation_message(result: game_workers.GameNotesMutationResult) -> str:
    """Preserve the established prefix success wording."""

    return f'Updated notes for game {result.game_id} to: {result.notes}'


def native_mutation_message(
    result: game_workers.GameNotesMutationResult,
    *,
    actor: GameNotesActor,
) -> str:
    """Describe a committed native edit or clear with its actor."""

    if result.cleared or result.notes is None:
        return f'{actor.label} cleared notes for game {result.game_id}.'
    return (
        f'{actor.label} edited notes for game {result.game_id} to: '
        f'{result.notes}'
    )


async def refresh_game_card(
    result: game_workers.GameNotesMutationResult,
    *,
    destination,
    guild,
    prefix: str,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Send the established dense game card after a committed notes write."""

    if load_game is None:
        load_game = models.Game.load_full_game
    if send_game_embed is None:
        send_game_embed = image_storage.send_game_embed
    game = load_game(game_id=result.game_id)
    embed, content = game.embed(guild=guild, prefix=prefix)
    await send_game_embed(
        destination,
        game,
        embed=embed,
        content=content,
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
                        'Could not clear the private deferred game-notes '
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
            'Committed game-notes mutation %s reconciliation warning failed',
            game_id,
        )


async def publish_mutation_result(
    result: game_workers.GameNotesMutationResult,
    *,
    send,
    refresh_card,
    actor: GameNotesActor | None = None,
) -> None:
    """Publish committed notes output, card refresh, and mention warning.

    Each Discord effect is post-commit and independently observable. A later
    failure never turns a committed notes/audit transaction into a rollback.
    """

    try:
        success_message = (
            native_mutation_message(result, actor=actor)
            if actor is not None
            else mutation_message(result)
        )
        await send(success_message)
    except Exception:
        logger.exception(
            'Committed game-notes mutation %s could not publish success',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} notes were saved, but the public '
            'success message could not be sent. An operator must reconcile '
            'the game card and audit trail.',
            result.game_id,
        )

    try:
        refreshed = await refresh_card(result)
        if refreshed is False:
            raise RuntimeError('the game-card refresh reported failure')
    except Exception:
        logger.exception(
            'Committed game-notes mutation %s game-card refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} notes were saved, but the '
            'public game-card refresh failed. An operator must reconcile '
            'the game card.',
            result.game_id,
        )

    if result.mention_warning:
        try:
            await send(MENTION_WARNING)
        except Exception:
            logger.exception(
                'Committed game-notes mutation %s mention warning failed',
                result.game_id,
            )
