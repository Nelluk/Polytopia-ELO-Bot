"""Discord-bound validation, autocomplete, and publication for badges."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

import discord

from modules import league_badges_workers as workers, league_user_commands
import settings


UNICODE_EMOJI_CHOICES = ('🏆', '🥇', '🥈', '🥉', '🎖️', '🏅')
CUSTOM_EMOJI_BADGE = re.compile(
    r'^(?P<emoji><a?:[A-Za-z0-9_]{2,32}:\d+>)(?: (?P<label>.*))?$'
)
logger = logging.getLogger('polybot.' + __name__)


class BadgePublicationError(RuntimeError):
    """A committed mutation could not be published publicly."""


@dataclass(frozen=True)
class BadgeDraft:
    operation: str
    badge: str
    label: str
    emoji: str


def access_error(member, guild_id: int) -> str | None:
    if not league_user_commands.league_scope(int(guild_id)):
        return 'Player badges are available only in the configured league server.'
    try:
        is_mod = bool(settings.is_mod(member))
    except Exception:
        is_mod = False
    if not is_mod:
        return 'Managing player badges requires Mod access.'
    return None


def _has_forbidden_character(value: str, *, allow_tab: bool = False) -> bool:
    return any(
        character in '\r\n'
        or (
            unicodedata.category(character).startswith('C')
            and not (allow_tab and character == '\t')
        )
        or unicodedata.category(character) in {'Zl', 'Zp'}
        for character in value
    )


def _has_newline(value: str) -> bool:
    return any(character in '\r\n' for character in value)


def normalize_add(label: str, emoji: str | None) -> BadgeDraft:
    raw_label = str(label or '').strip()
    if _has_forbidden_character(raw_label, allow_tab=True):
        raise workers.BadgeValidationError(
            'Badge labels cannot contain newlines or control characters.'
        )
    normalized_label = re.sub(r'\s+', ' ', raw_label)
    if not normalized_label:
        raise workers.BadgeValidationError('A badge label is required.')
    if len(normalized_label) > 100:
        raise workers.BadgeValidationError(
            'Badge labels must be 100 Unicode code points or fewer.'
        )
    normalized_emoji = str(emoji or '').strip()
    if _has_newline(normalized_emoji) or len(normalized_emoji) > 100:
        raise workers.BadgeValidationError(
            'Badge emoji must be one line and 100 code points or fewer.'
        )
    badge = (
        f'{normalized_emoji} {normalized_label}'
        if normalized_emoji else normalized_label
    )
    if len(badge) > 200:
        raise workers.BadgeValidationError(
            'The combined badge must be 200 code points or fewer.'
        )
    return BadgeDraft('add', badge, normalized_label, normalized_emoji)


def normalize_remove(badge: str) -> BadgeDraft:
    value = str(badge or '').strip()
    if not value or len(value) > 200 or _has_forbidden_character(value):
        raise workers.BadgeValidationError(
            'Choose or enter one valid stored badge on a single line.'
        )
    return BadgeDraft('remove', value, value, '')


def safe_text(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or '')),
    )


def safe_badge(value: object) -> str:
    """Escape badge text while preserving one valid leading custom emoji."""

    raw = str(value or '')
    match = CUSTOM_EMOJI_BADGE.fullmatch(raw)
    if match is None:
        return safe_text(raw)
    label = match.group('label')
    return match.group('emoji') + (f' {safe_text(label)}' if label else '')


def actor_label(member) -> str:
    discord_id = int(member.id)
    name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    return safe_text(name)[:160]


def build_request(*, draft: BadgeDraft, guild_id: int, actor, recipient_ids):
    ids = tuple(int(value) for value in recipient_ids)
    return workers.BadgeMutationRequest(
        operation=draft.operation,
        guild_id=int(guild_id),
        actor_discord_id=int(actor.id),
        actor_display_label=actor_label(actor),
        recipient_discord_ids=ids,
        badge=draft.badge,
    )


def emoji_autocomplete(guild, current: str) -> tuple[tuple[str, str], ...]:
    needle = str(current or '').casefold()
    values = [(emoji, emoji) for emoji in UNICODE_EMOJI_CHOICES]
    for emoji in getattr(guild, 'emojis', ()) if guild is not None else ():
        name = str(getattr(emoji, 'name', ''))
        if needle not in name.casefold():
            continue
        rendered = str(emoji)
        values.append((f':{name}:', rendered))
        if len(values) == 25:
            break
    return tuple(values[:25])


async def removal_autocomplete(interaction, current: str) -> tuple[str, ...]:
    guild = interaction.guild
    if guild is None or access_error(interaction.user, guild.id):
        return ()
    return await workers.run_badge_autocomplete(int(guild.id), current)


def public_message(result: workers.BadgeMutationResult) -> str:
    verb = 'added' if result.operation == 'add' else 'removed'
    preposition = 'to' if result.operation == 'add' else 'from'
    recipients = ' '.join(
        f'<@{value.discord_id}>' for value in result.recipients
    )
    message = (
        f'🏅 <@{result.actor_discord_id}> / **{result.actor_display_label}** '
        f'{verb} “{safe_badge(result.badge)}” {preposition} '
        f'{len(result.recipients)} selected player(s) '
        f'(changed: {result.changed_count}):\n{recipients}'
    )
    if result.unchanged_count:
        message += f'\nunchanged: {result.unchanged_count}'
    return message


async def publish_result(interaction, result: workers.BadgeMutationResult) -> None:
    try:
        await interaction.channel.send(
            public_message(result),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
                replied_user=False,
            ),
        )
    except Exception as exc:
        logger.exception(
            'Committed badge %s for guild %s could not publish',
            result.operation,
            result.guild_id,
        )
        raise BadgePublicationError(
            'The badge transaction committed, but its public result could '
            'not be published. An operator must reconcile the channel audit.'
        ) from exc
