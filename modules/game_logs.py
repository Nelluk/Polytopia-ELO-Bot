"""Discord adapters and parsing for permission-aware game audit logs."""

from __future__ import annotations

import re

import settings
from modules import exceptions, game_log_workers, utilities


MAX_SEARCH_LENGTH = 400


def _permission(member, checker) -> bool:
    try:
        return bool(checker(member))
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return False


def _setting(guild_id: int, name: str, default=None):
    try:
        return settings.guild_setting(int(guild_id), name)
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return default


def native_access_error(member, guild_id: int, channel_id: int | None) -> str | None:
    """Mirror the retained non-strict ``in_bot_channel`` policy."""

    if _permission(member, settings.is_mod):
        return None
    bot_channels = _setting(guild_id, 'bot_channels', None)
    if bot_channels is None:
        return None
    private_channels = _setting(guild_id, 'bot_channels_private', ()) or ()
    allowed = {int(value) for value in (*bot_channels, *private_channels)}
    if channel_id is not None and int(channel_id) in allowed:
        return None
    tags = ' '.join(f'<#{int(value)}>' for value in bot_channels)
    return (
        'This command can only be used in a designated ELO bot channel. '
        f'Try: {tags}'
    )


def parse_search_terms(value: str | None) -> tuple[tuple[str, ...], str]:
    """Parse legacy required terms and the first ``-excluded`` term."""

    text = re.sub(r'<@[!&]?([0-9]{17,21})>', r'\1', str(value or '')).strip()
    if len(text) > MAX_SEARCH_LENGTH:
        raise game_log_workers.GameLogReadError(
            f'Log search text must be at most {MAX_SEARCH_LENGTH} characters.'
        )
    include = []
    exclude = ''
    for token in text.split():
        if token.startswith('-') and len(token) > 1 and not exclude:
            exclude = token[1:]
        else:
            include.append(token)
    return tuple(include), exclude


def build_request(
    *,
    member,
    guild_id: int,
    key: game_log_workers.GameLogKey,
) -> game_log_workers.GameLogRequest:
    member_id = int(member.id)
    return game_log_workers.GameLogRequest(
        guild_id=int(guild_id),
        requester_id=member_id,
        requester_is_staff=_permission(member, settings.is_staff),
        requester_is_owner=(member_id == int(settings.owner_id)),
        key=key,
    )


def initial_key(*, member, game_id: int | None) -> game_log_workers.GameLogKey:
    if game_id is not None:
        return game_log_workers.GameLogKey(scope='game', game_id=int(game_id))
    member_is_owner = int(member.id) == int(settings.owner_id)
    if not _permission(member, settings.is_staff) and not member_is_owner:
        raise game_log_workers.GameLogPermissionError(
            'Non-staff users must provide a game ID for a game they participated in.'
        )
    return game_log_workers.GameLogKey(scope='guild')


def legacy_key(
    *,
    member,
    search_term: str | None,
    invoked_with: str,
) -> game_log_workers.GameLogKey:
    value = str(search_term or '').strip()
    if invoked_with == 'global_logs':
        include, exclude = parse_search_terms(value)
        return game_log_workers.GameLogKey(
            scope='global',
            include_terms=include,
            exclude_term=exclude,
        )
    if value.isnumeric():
        return game_log_workers.GameLogKey(scope='game', game_id=int(value))
    value = re.sub(r'\b(\d{4,6})\b', r'__\1__', value, count=1)
    include, exclude = parse_search_terms(value)
    member_is_owner = int(member.id) == int(settings.owner_id)
    if not _permission(member, settings.is_staff) and not member_is_owner:
        raise game_log_workers.GameLogPermissionError(
            'You do not have permission to view these logs.'
        )
    return game_log_workers.GameLogKey(
        scope='guild',
        include_terms=include,
        exclude_term=exclude,
    )


def filter_summary(key: game_log_workers.GameLogKey) -> str:
    include = ', '.join(key.include_terms) or 'none'
    exclude = key.exclude_term or 'none'
    return f'**Required terms:** {include}\n**Excluded term:** {exclude}'


async def run_prefix(ctx, search_term: str | None):
    """Retained prefix adapter over the shared bounded worker."""

    key = legacy_key(
        member=ctx.author,
        search_term=search_term,
        invoked_with=ctx.invoked_with or 'logs',
    )
    request = build_request(member=ctx.author, guild_id=ctx.guild.id, key=key)
    snapshot = await game_log_workers.run_game_log_read(request)
    title = snapshot.title
    if key.include_terms:
        title += f' containing {" ".join(key.include_terms).replace("__", "")}'
    if key.exclude_term:
        title += f' excluding {key.exclude_term}'
    rows = [
        (
            f'`{row.timestamp}`'
            + (f' · guild `{row.guild_id}`' if key.scope == 'global' else ''),
            row.message[:500],
        )
        for row in snapshot.rows
    ]
    if snapshot.truncated:
        title += f' · first {game_log_workers.MAX_LOG_ROWS} shown'
    return await utilities.paginate(
        ctx.bot,
        ctx,
        title=title,
        message_list=rows,
        page_start=0,
        page_end=10,
        page_size=10,
    )


def safe_log_text(value: str) -> str:
    return utilities.escape_role_mentions(value)
