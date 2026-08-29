"""Shared game-ping application service and post-commit Discord delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import unicodedata
from urllib.parse import urlparse

import discord

import settings
from modules import exceptions, models, utilities
from modules import game_ping_workers as workers


logger = logging.getLogger('polybot.' + __name__)

MAX_GAMES = workers.MAX_GAMES
MAX_GAME_CHOICES = workers.MAX_GAME_CHOICES
MAX_PARTICIPANTS_PER_GAME = workers.MAX_PARTICIPANTS_PER_GAME
MAX_DESTINATIONS = workers.MAX_DESTINATIONS
MAX_TEXT_SECTION_LENGTH = workers.MAX_TEXT_SECTION_LENGTH
MAX_TEXT_SECTIONS = workers.MAX_TEXT_SECTIONS
MAX_TEXT_LENGTH = workers.MAX_TEXT_LENGTH
MAX_FORMATTED_TEXT_LENGTH = workers.MAX_FORMATTED_TEXT_LENGTH
MAX_ATTACHMENTS = workers.MAX_ATTACHMENTS
MAX_ATTACHMENT_BYTES = workers.MAX_ATTACHMENT_BYTES
MAX_TOTAL_ATTACHMENT_BYTES = workers.MAX_TOTAL_ATTACHMENT_BYTES
DISCORD_MESSAGE_LIMIT = 2_000
MAX_PREVIEW_MESSAGE_LENGTH = 6_000
MAX_PREVIEW_RECIPIENTS = 25
MAX_PREVIEW_DESTINATIONS = 30


@dataclass(frozen=True, slots=True)
class GamePingDraft:
    """The exact private draft state shown in the preview."""

    sections: tuple[str, ...]
    text: str
    attachments: tuple[workers.AttachmentMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class NativePingInput:
    """Parsed positional-style input for the native game-ping command."""

    compose: bool
    explicit_game_id: int | None
    message: str


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    game_id: int | None
    guild_id: int
    channel_id: int
    detail: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    committed: workers.GamePingCommitResult
    delivered_destinations: tuple[workers.GamePingDestination, ...]
    failures: tuple[DeliveryFailure, ...]


def _safe_name(value, *, fallback: str, limit: int = 160) -> str:
    text = unicodedata.normalize('NFKC', str(value or fallback))
    text = ''.join(
        character
        for character in text
        if character in '\n\r\t'
        or not unicodedata.category(character).startswith('C')
    ).strip()
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return text[:limit] or fallback


def _description(member) -> str:
    member_id = int(getattr(member, 'id'))
    display_name = _safe_name(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None),
        fallback=f'user-{member_id}',
    )
    return f'**{display_name}** (`{member_id}`)'


def _permission_value(member, name: str, default=False) -> bool:
    try:
        value = getattr(member, name)
        return bool(value() if callable(value) else value)
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return bool(default)


def _settings_permission(member, checker) -> bool:
    try:
        return bool(checker(member))
    except (AttributeError, TypeError, KeyError, exceptions.CheckFailedError):
        return False


def _member_level(member) -> int:
    try:
        return int(settings.get_user_level(member))
    except (AttributeError, TypeError, ValueError, exceptions.CheckFailedError):
        return 0


def capture_member(member, guild_id: int) -> workers.MemberSnapshot:
    """Capture only immutable member/role facts for worker submission."""

    roles = tuple(getattr(member, 'roles', ()) or ())
    role_ids = []
    role_names = []
    for role in roles:
        role_id = getattr(role, 'id', None)
        try:
            role_ids.append(int(role_id))
        except (TypeError, ValueError):
            continue
        role_names.append(str(getattr(role, 'name', '')))
    member_id = int(getattr(member, 'id'))
    return workers.MemberSnapshot(
        guild_id=int(guild_id),
        discord_id=member_id,
        display_name=_safe_name(
            getattr(member, 'display_name', None),
            fallback=f'user-{member_id}',
        ),
        name=_safe_name(
            getattr(member, 'name', None),
            fallback=f'user-{member_id}',
        ),
        role_ids=tuple(role_ids),
        role_names=tuple(role_names),
        level=_member_level(member),
        is_staff=_permission_value(
            member,
            'is_staff',
            _settings_permission(member, settings.is_staff),
        ),
        is_mod=_permission_value(
            member,
            'is_mod',
            _settings_permission(member, settings.is_mod),
        ),
        description=_description(member),
    )


def target_snapshot(member, guild_id: int) -> workers.MemberSnapshot:
    """Alias used by the requester-bound native UserSelect."""

    return capture_member(member, guild_id)


def target_id_snapshot(discord_id: int, guild_id: int) -> workers.MemberSnapshot:
    """Create a bounded audit label when the target is not cached in Discord."""

    member_id = int(discord_id)
    return workers.MemberSnapshot(
        guild_id=int(guild_id),
        discord_id=member_id,
        display_name=f'user-{member_id}',
        name=f'user-{member_id}',
        role_ids=(),
        role_names=(),
        level=0,
        is_staff=False,
        is_mod=False,
        description=f'<@{member_id}> (`{member_id}`)',
    )


def _setting_channel_ids(guild_id: int, name: str) -> tuple[int, ...]:
    try:
        values = settings.guild_setting(int(guild_id), name) or ()
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    else:
        try:
            iter(values)
        except TypeError:
            values = (values,)
    result = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(result))


def _can_read(channel, member) -> bool:
    if member is None or channel is None:
        return False
    permissions_for = getattr(channel, 'permissions_for', None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(member)
    except Exception:
        return False
    value = getattr(permissions, 'read_messages', None)
    if value is None:
        value = getattr(permissions, 'view_channel', False)
    return bool(value)


def capture_channel_facts(
    interaction_or_context,
    result: workers.GamePingLoadResult,
) -> workers.ChannelFacts:
    """Capture current-channel readability without passing Discord objects."""

    guild = getattr(interaction_or_context, 'guild', None)
    guild_id = int(
        getattr(guild, 'id', None)
        or getattr(interaction_or_context, 'guild_id', 0)
        or 0
    )
    channel = getattr(interaction_or_context, 'channel', None)
    channel_id = int(
        getattr(interaction_or_context, 'channel_id', None)
        or getattr(channel, 'id', 0)
        or 0
    )
    permissions = []
    seen = set()
    for game in result.games[:MAX_GAMES]:
        for participant in game.participants:
            key = (game.game_id, participant.discord_id)
            if key in seen:
                continue
            seen.add(key)
            member = None
            get_member = getattr(guild, 'get_member', None)
            if callable(get_member):
                member = get_member(participant.discord_id)
            permissions.append(workers.ParticipantPermission(
                game_id=game.game_id,
                discord_id=participant.discord_id,
                can_read=_can_read(channel, member),
            ))
    return workers.ChannelFacts(
        guild_id=guild_id,
        channel_id=channel_id,
        bot_channel_ids=_setting_channel_ids(guild_id, 'bot_channels'),
        private_bot_channel_ids=_setting_channel_ids(
            guild_id,
            'bot_channels_private',
        ),
        participant_permissions=tuple(permissions),
    )


def _safe_attachment_url(url: str) -> bool:
    parsed = urlparse(str(url or ''))
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or '').lower()
    return (
        host == 'cdn.discordapp.com'
        or host == 'media.discordapp.net'
        or host.endswith('.discordapp.com')
        or host.endswith('.discordapp.net')
    )


def _safe_filename(filename, fallback: str) -> str:
    value = unicodedata.normalize('NFKC', str(filename or fallback))
    value = value.replace('/', '_').replace('\\', '_')
    value = ''.join(
        character
        for character in value
        if character in ' .-_'
        or character.isalnum()
    ).strip(' .')
    return value[:255] or fallback


def capture_attachments(values) -> tuple[workers.AttachmentMetadata, ...]:
    """Freeze FileUpload values without downloading arbitrary bodies."""

    values = tuple(values or ())
    if len(values) > MAX_ATTACHMENTS:
        raise workers.GamePingValidationError(
            f'You may attach at most {MAX_ATTACHMENTS} files.'
        )
    captured = []
    total_size = 0
    for index, attachment in enumerate(values, start=1):
        url = str(getattr(attachment, 'url', '') or '')
        if not _safe_attachment_url(url):
            raise workers.GamePingValidationError(
                'Attachments must use safe HTTPS Discord URLs.'
            )
        try:
            size = int(getattr(attachment, 'size', 0) or 0)
        except (TypeError, ValueError) as exc:
            raise workers.GamePingValidationError(
                'An attachment size could not be validated.'
            ) from exc
        if size < 0 or size > MAX_ATTACHMENT_BYTES:
            raise workers.GamePingValidationError(
                f'Each attachment must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
            )
        total_size += size
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise workers.GamePingValidationError(
                f'Attachments may total at most {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
            )
        captured.append(workers.AttachmentMetadata(
            filename=_safe_filename(
                getattr(attachment, 'filename', None),
                f'attachment-{index}',
            ),
            url=url,
            content_type=str(
                getattr(attachment, 'content_type', None)
                or 'application/octet-stream'
            ).split(';', 1)[0].strip().lower(),
            size=size,
        ))
    return tuple(captured)


def combine_sections(sections) -> str:
    """Combine up to three modal sections while omitting blank sections."""

    values = tuple(str(value or '') for value in sections)
    if len(values) > MAX_TEXT_SECTIONS:
        raise workers.GamePingValidationError(
            f'Use at most {MAX_TEXT_SECTIONS} text sections.'
        )
    if any(len(value) > MAX_TEXT_SECTION_LENGTH for value in values):
        raise workers.GamePingValidationError(
            f'Each text section must be {MAX_TEXT_SECTION_LENGTH:,} characters or fewer.'
        )
    if sum(len(value) for value in values) > MAX_TEXT_LENGTH:
        raise workers.GamePingValidationError(
            f'Text sections may total at most {MAX_TEXT_LENGTH:,} characters.'
        )
    return '\n\n'.join(value for value in values if value.strip())


def build_draft(
    sections,
    attachments=(),
) -> GamePingDraft:
    values = tuple(str(value or '') for value in sections)
    text = utilities.escape_role_mentions(combine_sections(values))
    frozen_attachments = tuple(attachments)
    if any(
        not isinstance(attachment, workers.AttachmentMetadata)
        for attachment in frozen_attachments
    ):
        raise workers.GamePingValidationError(
            'Attachments must be captured as validated Discord metadata first.'
        )
    if not text.strip() and not frozen_attachments:
        raise workers.GamePingValidationError(
            'Add text or at least one attachment before confirming.'
        )
    if len(frozen_attachments) > MAX_ATTACHMENTS:
        raise workers.GamePingValidationError(
            f'You may attach at most {MAX_ATTACHMENTS} files.'
        )
    return GamePingDraft(
        sections=values,
        text=text,
        attachments=frozen_attachments,
    )


def parse_native_input(value: str) -> NativePingInput:
    """Parse ``compose [game-id]`` or ``[game-id] message`` slash input."""

    raw = str(value or '').strip()
    if not raw:
        raise workers.GamePingValidationError(
            'Enter a message, or enter `compose` to open the composer.'
        )
    parts = raw.split(maxsplit=1)
    first = parts[0]
    remainder = parts[1] if len(parts) == 2 else ''
    if first.casefold() == 'compose':
        compose_tail = remainder.strip()
        if not compose_tail:
            return NativePingInput(True, None, '')
        if len(compose_tail.split()) != 1:
            raise workers.GamePingValidationError(
                'Use `compose` or `compose GAME_ID` to open the composer.'
            )
        try:
            game_id = int(compose_tail)
        except ValueError as exc:
            raise workers.GamePingValidationError(
                'The value after `compose` must be a game ID.'
            ) from exc
        if game_id <= 0:
            raise workers.GamePingValidationError('Game IDs must be positive integers.')
        return NativePingInput(True, game_id, '')

    try:
        game_id = int(first)
    except ValueError:
        return NativePingInput(False, None, raw)
    if game_id <= 0:
        raise workers.GamePingValidationError('Game IDs must be positive integers.')
    return NativePingInput(False, game_id, remainder.strip())


def _game_ids_for_scope(
    result: workers.GamePingLoadResult,
    *,
    scope: str,
    selected_game_id: int | None,
) -> tuple[int, ...]:
    if scope == 'single':
        if selected_game_id is None:
            raise workers.GamePingValidationError(
                'Choose one game before composing the notification.'
            )
        if int(selected_game_id) not in {game.game_id for game in result.games}:
            raise workers.GamePingValidationError(
                'That game is no longer in the loaded private draft.'
            )
        return (int(selected_game_id),)
    if scope != 'all':
        raise workers.GamePingValidationError('Choose a valid notification scope.')
    if not result.all_scope_allowed:
        raise workers.GamePingPermissionError(
            'You do not have permission to ping all of your incomplete games.'
        )
    ids = tuple(
        game.game_id
        for game in result.games
        if game.guild_id == result.guild_id
        and not game.is_confirmed
        and result.target_id in {
            participant.discord_id for participant in game.participants
        }
    )
    if not ids:
        raise workers.GamePingValidationError(
            'No incomplete games are available for this target.'
        )
    return ids[:MAX_GAMES]


def build_commit_request(
    *,
    result: workers.GamePingLoadResult,
    requester: workers.MemberSnapshot,
    target: workers.MemberSnapshot,
    scope: str,
    selected_game_id: int | None,
    channel_facts: workers.ChannelFacts,
    draft: GamePingDraft,
    invoked_with: str,
) -> workers.GamePingCommitRequest:
    return workers.GamePingCommitRequest(
        guild_id=int(result.guild_id),
        requester=requester,
        target_id=int(target.discord_id),
        target_description=target.description,
        scope=str(scope),
        game_ids=_game_ids_for_scope(
            result,
            scope=scope,
            selected_game_id=selected_game_id,
        ),
        channel_facts=channel_facts,
        text=draft.text,
        attachments=tuple(draft.attachments),
        truncated=bool(result.truncated),
        invoked_with=str(invoked_with),
    )


def _destination_label(
    destination: workers.GamePingDestination,
    *,
    invoking_guild_id: int,
) -> str:
    if destination.kind.startswith('blocked:'):
        return destination.kind.removeprefix('blocked:').strip()
    label = f'<#{destination.channel_id}>'
    if int(destination.guild_id) != int(invoking_guild_id):
        label += f' (server `{destination.guild_id}`)'
    return label


def _preview_games(
    result: workers.GamePingLoadResult,
    *,
    scope: str,
    selected_game_id: int | None,
) -> tuple[workers.GamePingGame, ...]:
    if scope == 'single':
        return tuple(
            game for game in result.games if game.game_id == selected_game_id
        )
    return tuple(
        game for game in result.games
        if game.guild_id == result.guild_id
        and not game.is_confirmed
        and result.target_id in {
            participant.discord_id for participant in game.participants
        }
    )


def preview_destinations(
    result: workers.GamePingLoadResult,
    *,
    requester: workers.MemberSnapshot,
    target: workers.MemberSnapshot,
    scope: str,
    selected_game_id: int | None,
    draft: GamePingDraft | None,
    channel_facts: workers.ChannelFacts,
) -> tuple[tuple[workers.GamePingDestination, ...], str | None]:
    """Resolve the current private preview or explain why it cannot send."""

    games = _preview_games(
        result,
        scope=scope,
        selected_game_id=selected_game_id,
    )
    if not games:
        return (), (
            'Choose a game before sending.'
            if scope == 'single'
            else 'No incomplete games are available for this player.'
        )
    destinations = []
    for game in games:
        try:
            destinations.extend(
                workers._destinations_for_game(
                    game,
                    workers.GamePingCommitRequest(
                        guild_id=result.guild_id,
                        requester=requester,
                        target_id=result.target_id,
                        target_description=target.description,
                        scope=scope,
                        game_ids=(game.game_id,),
                        channel_facts=channel_facts,
                        text=(draft.text if draft else 'preview'),
                        attachments=(draft.attachments if draft else ()),
                    ),
                )
            )
        except workers.GamePingValidationError as exc:
            return (), str(exc)
    resolved = workers._dedupe_destinations(tuple(destinations))
    if not resolved:
        return (), 'No destination channels are available for this ping.'
    return resolved, None


def preview_message(
    result: workers.GamePingLoadResult,
    *,
    requester: workers.MemberSnapshot,
    target: workers.MemberSnapshot,
    scope: str,
    selected_game_id: int | None,
    draft: GamePingDraft | None,
    channel_facts: workers.ChannelFacts,
) -> str:
    """Render a compact private review focused on the pending notification."""

    games = _preview_games(
        result,
        scope=scope,
        selected_game_id=selected_game_id,
    )
    recipient_map = {}
    for game in games:
        for participant in game.participants:
            recipient_map.setdefault(participant.discord_id, participant.display_name)
    recipient_items = [
        _safe_name(name, fallback=f'user-{discord_id}')
        for discord_id, name in list(recipient_map.items())[:MAX_PREVIEW_RECIPIENTS]
    ]
    if len(recipient_map) > MAX_PREVIEW_RECIPIENTS:
        recipient_items.append(
            f'… and {len(recipient_map) - MAX_PREVIEW_RECIPIENTS} more'
        )
    recipients = ', '.join(recipient_items) or 'none'

    lines = ['**Review game ping — private**']
    if requester.discord_id != target.discord_id:
        lines.append(
            'On behalf of: '
            + _safe_name(
                target.display_name,
                fallback=f'user-{target.discord_id}',
            )
        )
    if scope == 'single':
        if games:
            game = games[0]
            game_label = f'`{game.game_id}`'
            if game.name:
                game_label += f' · {_safe_name(game.name, fallback="game")}'
            lines.append(f'Game: {game_label}')
        else:
            lines.append('Game: **choose one below**')
    else:
        ids = ', '.join(str(game.game_id) for game in games)
        lines.append(f'Games: all {len(games)} loaded incomplete games (`{ids}`)')
        if result.truncated:
            lines.append(
                f'⚠️ More than {MAX_GAMES} games are available; this draft '
                f'is limited to the newest {MAX_GAMES}.'
            )
    lines.append(f'To: {recipients}')
    if draft is None:
        lines.append('Message: *(not written yet)*')
    else:
        if draft.text:
            lines.append('Message:')
            preview_text = utilities.escape_role_mentions(draft.text)
            if len(preview_text) > MAX_PREVIEW_MESSAGE_LENGTH:
                preview_text = (
                    preview_text[:MAX_PREVIEW_MESSAGE_LENGTH]
                    + f'\n… ({len(preview_text) - MAX_PREVIEW_MESSAGE_LENGTH:,} '
                    'characters omitted from this preview; delivery preserves '
                    'the full draft)'
                )
            lines.append(preview_text)
        else:
            lines.append('Message: *(files only)*')
        if draft.attachments:
            lines.append(
                'Files: '
                + ', '.join(
                    attachment.filename
                    for attachment in draft.attachments
                )
            )

    destination_rows, delivery_error = preview_destinations(
        result,
        requester=requester,
        target=target,
        scope=scope,
        selected_game_id=selected_game_id,
        draft=draft,
        channel_facts=channel_facts,
    )
    if delivery_error is not None:
        lines.append(f'⚠️ Cannot send: {delivery_error}')
    else:
        visible_destinations = destination_rows[:MAX_PREVIEW_DESTINATIONS]
        destination_labels = tuple(dict.fromkeys(
            _destination_label(
                destination,
                invoking_guild_id=result.guild_id,
            )
            for destination in visible_destinations
        ))
        destinations = ', '.join(destination_labels)
        if len(destination_rows) > MAX_PREVIEW_DESTINATIONS:
            destinations += (
                f', and {len(destination_rows) - MAX_PREVIEW_DESTINATIONS} more'
            )
        lines.append(f'Destination{("s" if len(destination_labels) != 1 else "")}: {destinations}')
    return '\n'.join(lines)


def split_message_chunks(
    content: str,
    *,
    max_length: int = DISCORD_MESSAGE_LIMIT,
) -> tuple[str, ...]:
    """Split without dropping characters or line breaks."""

    if max_length <= 0:
        raise ValueError('max_length must be positive')
    value = str(content)
    if not value:
        return ()
    chunks = []
    remaining = value
    while len(remaining) > max_length:
        boundary = remaining.rfind('\n', 0, max_length)
        if boundary <= 0:
            boundary = max_length
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:]
        else:
            chunks.append(remaining[:boundary + 1])
            remaining = remaining[boundary + 1:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def allowed_mentions_for(ids: tuple[int, ...]):
    users = [discord.Object(id=int(discord_id)) for discord_id in ids]
    return discord.AllowedMentions(
        users=users,
        roles=False,
        everyone=False,
        replied_user=False,
    )


def delivery_content(
    result: workers.GamePingCommitResult,
    destination: workers.GamePingDestination,
) -> str:
    if destination.game_id is None:
        title = 'Game ping for all selected incomplete games'
    else:
        title = f'Game ping for game {destination.game_id}'
    requester_label, target_label = _attribution_labels(result)
    sections = [title, f'Actor: {requester_label}']
    if target_label is not None:
        sections.append(f'On behalf of: {target_label}')
    if result.text:
        sections.append(utilities.escape_role_mentions(result.text))
    if result.attachments:
        sections.append('\n'.join(
            f'{attachment.filename}: {attachment.url}'
            for attachment in result.attachments
        ))
    if destination.mention_ids:
        sections.append(' '.join(
            f'<@{int(discord_id)}>' for discord_id in destination.mention_ids
        ))
    return '\n'.join(sections)


def _find_guild(guilds, guild_id: int):
    for guild in tuple(guilds or ()):
        if int(getattr(guild, 'id', 0)) == int(guild_id):
            return guild
    return None


def _safe_delivery_detail(exc: BaseException) -> str:
    return _safe_name(str(exc), fallback=type(exc).__name__, limit=240)


def _attribution_labels(
    result: workers.GamePingCommitResult,
    *,
    requester_description: str | None = None,
) -> tuple[str, str | None]:
    actor = _attribution_label(
        requester_description or result.requester_description,
        discord_id=result.requester_id,
        fallback='Actor',
    )
    target = None
    if result.target_id != result.requester_id:
        target = _attribution_label(
            result.target_description,
            discord_id=result.target_id,
            fallback='Target',
        )
    return actor, target


_ATTRIBUTION_DESCRIPTION = re.compile(
    r'^\*\*(?P<name>.+)\*\* \(`(?P<discord_id>[1-9][0-9]*)`\)$'
)


def _attribution_label(
    description: str | None,
    *,
    discord_id: int,
    fallback: str,
) -> str:
    """Render only the exact trusted member-description Markdown shape."""

    match = _ATTRIBUTION_DESCRIPTION.fullmatch(str(description or ''))
    if (
        match is not None
        and int(match.group('discord_id')) == int(discord_id)
    ):
        return (
            f'**{match.group("name")}** '
            f'(`{int(discord_id)}`)'
        )
    return f'{fallback} (`{int(discord_id)}`)'


def _completion_message(
    result: workers.GamePingCommitResult,
    failures: tuple[DeliveryFailure, ...],
    *,
    requester_description: str | None,
    delivered_count: int,
) -> str:
    game_ids = ', '.join(str(game_id) for game_id in result.game_ids)
    actor, target = _attribution_labels(
        result,
        requester_description=requester_description,
    )
    attribution = f'Actor: {actor}'
    if target is not None:
        attribution += f' on behalf of: {target}'
    if failures:
        failed = '; '.join(
            f'game {failure.game_id if failure.game_id is not None else "all"} '
            f'guild {failure.guild_id} channel {failure.channel_id}'
            for failure in failures
        )
        return (
            f':warning: {attribution} committed a game ping for '
            f'game IDs `{game_ids}`, but delivery completed for only '
            f'{delivered_count} destination(s). Failed destinations: {failed}. '
            'Already-delivered notifications were not retried; do not submit '
            'this draft again.'
        )
    return (
        f'{attribution} completed a {result.scope} game ping for '
        f'game IDs `{game_ids}` to {len(result.recipient_ids)} resolved '
        f'participant(s) across {delivered_count} destination(s). '
        'The committed notification is terminal; do not retry it.'
    )


async def deliver_committed(
    result: workers.GamePingCommitResult,
    *,
    guilds,
    completion_destination=None,
    requester_description: str | None = None,
    completion_on_success: bool = True,
) -> DeliveryResult:
    """Deliver a committed plan once and publish reconciliation publicly."""

    delivered = []
    failures = []
    for destination in result.destinations:
        guild = _find_guild(guilds, destination.guild_id)
        channel = (
            guild.get_channel(destination.channel_id)
            if guild is not None and callable(getattr(guild, 'get_channel', None))
            else None
        )
        if channel is None:
            failure = DeliveryFailure(
                game_id=destination.game_id,
                guild_id=destination.guild_id,
                channel_id=destination.channel_id,
                detail='channel not found',
            )
            failures.append(failure)
            logger.warning(
                'Committed game ping delivery destination unavailable: '
                'game_id=%s guild_id=%s channel_id=%s',
                failure.game_id,
                failure.guild_id,
                failure.channel_id,
            )
            continue
        chunks = split_message_chunks(delivery_content(result, destination))
        allowed_mentions = allowed_mentions_for(destination.mention_ids)
        try:
            for chunk in chunks:
                await channel.send(
                    chunk,
                    allowed_mentions=allowed_mentions,
                )
        except Exception as exc:
            failure = DeliveryFailure(
                game_id=destination.game_id,
                guild_id=destination.guild_id,
                channel_id=destination.channel_id,
                detail=_safe_delivery_detail(exc),
            )
            failures.append(failure)
            logger.exception(
                'Committed game ping delivery failed: game_id=%s '
                'guild_id=%s channel_id=%s; already-delivered destinations '
                'will not be retried',
                failure.game_id,
                failure.guild_id,
                failure.channel_id,
            )
            continue
        delivered.append(destination)

    failure_tuple = tuple(failures)
    if completion_destination is not None and (
        failure_tuple or completion_on_success
    ):
        completion = _completion_message(
            result,
            failure_tuple,
            requester_description=requester_description,
            delivered_count=len(delivered),
        )
        try:
            await completion_destination.send(
                completion,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            failure = DeliveryFailure(
                game_id=None,
                guild_id=int(getattr(getattr(completion_destination, 'guild', None), 'id', result.guild_id)),
                channel_id=int(getattr(completion_destination, 'id', 0)),
                detail=_safe_delivery_detail(exc),
            )
            failures.append(failure)
            logger.exception(
                'Committed game ping completion reconciliation failed: '
                'guild_id=%s channel_id=%s',
                failure.guild_id,
                failure.channel_id,
            )

    return DeliveryResult(
        committed=result,
        delivered_destinations=tuple(delivered),
        failures=tuple(failures),
    )


async def confirm_and_deliver(
    request: workers.GamePingCommitRequest,
    *,
    guilds,
    completion_destination=None,
    completion_on_success: bool = True,
) -> DeliveryResult:
    """Confirm once; pre-commit errors remain retryable, commit is terminal."""

    try:
        committed = await workers.run_ping_commit(request)
    except workers.GamePingCancelled as cancellation:
        if not cancellation.committed:
            error = cancellation.error
            if isinstance(error, BaseException):
                raise error
            raise workers.GamePingValidationError(
                'The notification was canceled before commit.'
            )
        committed = cancellation.result
    return await deliver_committed(
        committed,
        guilds=guilds,
        completion_destination=completion_destination,
        requester_description=request.requester.description,
        completion_on_success=completion_on_success,
    )


def _prefix_error(ctx, message: str):
    return ctx.send(message)


async def run_prefix_single(ctx, args: str, *, attachments=()):
    """Immediate legacy ``$ping`` adapter over the shared service."""

    requester = capture_member(ctx.author, ctx.guild.id)
    raw = str(args or '')
    frozen_attachments = capture_attachments(attachments)
    if not raw.strip() and not frozen_attachments:
        usage = (
            f'**Example usage:** `{ctx.prefix}ping 100 Here\'s a note for '
            'everyone in game 100.`\nYou can omit the game ID in an '
            'unambiguous game channel, or attach a file.'
        )
        return await _prefix_error(ctx, usage)
    tokens = raw.split()
    explicit_game_id = None
    if tokens:
        try:
            explicit_game_id = int(tokens[0])
            tokens = tokens[1:]
        except ValueError:
            pass
    text = ' '.join(tokens)
    load = await workers.run_ping_candidates(workers.GamePingLoadRequest(
        guild_id=int(ctx.guild.id),
        requester=requester,
        target_id=requester.discord_id,
        explicit_game_id=explicit_game_id,
        channel_id=int(ctx.channel.id),
        discover_all=False,
    ))
    if load.inferred_game_id is not None and explicit_game_id is not None:
        text = ' '.join(raw.split())
    if not text.strip() and not frozen_attachments:
        usage = (
            f'**Example usage:** `{ctx.prefix}ping 100 Here\'s a note for '
            'everyone in game 100.`\nYou can omit the game ID in an '
            'unambiguous game channel, or attach a file.'
        )
        return await _prefix_error(ctx, usage)
    draft = build_draft((text,), frozen_attachments)
    selected_game_id = (
        load.inferred_game_id
        if load.inferred_game_id is not None
        else explicit_game_id
    )
    facts = capture_channel_facts(ctx, load)
    request = build_commit_request(
        result=load,
        requester=requester,
        target=requester,
        scope='single',
        selected_game_id=selected_game_id,
        channel_facts=facts,
        draft=draft,
        invoked_with=getattr(ctx, 'invoked_with', 'ping') or 'ping',
    )
    result = await confirm_and_deliver(
        request,
        guilds=getattr(getattr(ctx, 'bot', None), 'guilds', ())
        or getattr(settings.bot, 'guilds', ()),
        completion_destination=ctx.channel,
        completion_on_success=False,
    )
    return result


async def run_native_single(
    interaction,
    *,
    message: str | None,
    game_id: int | None,
    attachment=None,
    guilds=(),
) -> DeliveryResult:
    """Immediate ``/game ping`` path for the ordinary one-game case."""

    requester = capture_member(interaction.user, interaction.guild.id)
    frozen_attachments = capture_attachments(
        (attachment,) if attachment is not None else (),
    )
    draft = build_draft((str(message or ''),), frozen_attachments)
    load = await asyncio.wait_for(
        workers.run_ping_candidates(workers.GamePingLoadRequest(
            guild_id=int(interaction.guild.id),
            requester=requester,
            target_id=requester.discord_id,
            explicit_game_id=game_id,
            channel_id=int(interaction.channel_id or 0),
            discover_all=False,
            prefer_explicit_game_id=True,
        )),
        timeout=20.0,
    )
    selected_game_id = (
        game_id
        if game_id is not None
        else load.inferred_game_id
    )
    facts = capture_channel_facts(interaction, load)
    request = build_commit_request(
        result=load,
        requester=requester,
        target=requester,
        scope='single',
        selected_game_id=selected_game_id,
        channel_facts=facts,
        draft=draft,
        invoked_with='/game ping',
    )
    return await confirm_and_deliver(
        request,
        guilds=guilds,
        completion_destination=interaction.channel,
        completion_on_success=False,
    )


async def run_prefix_all(ctx, message: str | None, *, attachments=()):
    """Immediate legacy ``$pingall`` adapter with no platform filter."""

    requester = capture_member(ctx.author, ctx.guild.id)
    raw = str(message or '')
    tokens = raw.split()
    target_id = requester.discord_id
    target_member = requester
    if tokens:
        parsed = utilities.string_to_user_id(tokens[0])
        if parsed:
            target_id = int(parsed)
            tokens = tokens[1:]
            member = getattr(ctx.guild, 'get_member', lambda _id: None)(target_id)
            if member is not None:
                target_member = capture_member(member, ctx.guild.id)
            else:
                target_member = target_id_snapshot(target_id, ctx.guild.id)
    text = ' '.join(tokens)
    frozen_attachments = capture_attachments(attachments)
    if not text.strip() and not frozen_attachments:
        return await _prefix_error(ctx, 'Message or an attachment is required.')
    if target_id == requester.discord_id and requester.level <= 2:
        return await _prefix_error(
            ctx,
            'You do not have permission to use this command. Ask a server '
            'staff member to use it on your games for you.',
        )
    if target_id != requester.discord_id and requester.level <= 3:
        return await _prefix_error(
            ctx,
            "You do not have permission to use this command on another player's games.",
        )
    draft = build_draft((text,), frozen_attachments)
    load = await workers.run_ping_candidates(workers.GamePingLoadRequest(
        guild_id=int(ctx.guild.id),
        requester=requester,
        target_id=target_id,
        channel_id=int(ctx.channel.id),
        discover_all=True,
    ))
    if not load.games:
        return await _prefix_error(
            ctx,
            f'No incomplete games found for <@{target_id}>.',
        )
    facts = capture_channel_facts(ctx, load)
    request = build_commit_request(
        result=load,
        requester=requester,
        target=target_member,
        scope='all',
        selected_game_id=None,
        channel_facts=facts,
        draft=draft,
        invoked_with='pingall',
    )
    return await confirm_and_deliver(
        request,
        guilds=getattr(getattr(ctx, 'bot', None), 'guilds', ())
        or getattr(settings.bot, 'guilds', ()),
        completion_destination=ctx.channel,
    )
