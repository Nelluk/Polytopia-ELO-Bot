"""Bounded immutable reads for retained ``getname``/``getnames`` commands."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging

import peewee

from modules import exceptions, models, player_timezone_values


logger = logging.getLogger('polybot.' + __name__)
_legacy_name_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-legacy-name-read',
)


class GameNamesLookupError(ValueError):
    """A retained game-name lookup could not resolve one usable game."""

    def __init__(self, message: str, *, code: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AccountNameSnapshot:
    display_name: str
    account_name: str | None


@dataclass(frozen=True)
class RegisteredNameMatch:
    match_count: int
    player: AccountNameSnapshot | None


@dataclass(frozen=True)
class GameNameRow:
    player_name: str
    account_name: str | None
    timezone: str | None


@dataclass(frozen=True)
class GameNamesSnapshot:
    game_id: int
    rows: tuple[GameNameRow, ...]


def load_account_name(discord_id: int) -> AccountNameSnapshot | None:
    """Freeze one account-wide name on a worker-owned connection."""

    with models.db.connection_context():
        member = models.DiscordMember.get_or_none(discord_id=int(discord_id))
        if member is None:
            return None
        return AccountNameSnapshot(
            display_name=str(member.name),
            account_name=(
                str(member.polytopia_name or member.name_steam)
                if member.polytopia_name or member.name_steam
                else None
            ),
        )


def load_registered_name_match(
    player_string: str,
    guild_id: int,
) -> RegisteredNameMatch:
    """Freeze the legacy registered-player disambiguation result."""

    with models.db.connection_context():
        matches = models.Player.string_matches(
            player_string=str(player_string),
            guild_id=int(guild_id),
        )
        match_count = len(matches)
        if match_count != 1:
            return RegisteredNameMatch(match_count=match_count, player=None)
        player = matches[0]
        member = player.discord_member
        account_name = member.polytopia_name or member.name_steam
        return RegisteredNameMatch(
            match_count=1,
            player=AccountNameSnapshot(
                display_name=str(player.name),
                account_name=str(account_name) if account_name else None,
            ),
        )


def load_game_names(
    *,
    game_id: int | None,
    channel_id: int,
) -> GameNamesSnapshot:
    """Freeze one game's draft-ordered account names and timezones."""

    with models.db.connection_context():
        if game_id is None:
            try:
                game = models.Game.by_channel_id(chan_id=int(channel_id))
            except exceptions.NoSingleMatch as exc:
                raise GameNamesLookupError(
                    'Game ID not provided and cannot detect a game channel.',
                    code='channel_lookup',
                ) from exc
        else:
            try:
                game = models.Game.get_by_id(int(game_id))
            except peewee.DataError as exc:
                raise GameNamesLookupError(
                    f'Invalid game ID "{game_id}".',
                    code='invalid_id',
                ) from exc
            except peewee.DoesNotExist as exc:
                raise GameNamesLookupError(
                    f'Game with ID {game_id} cannot be found.',
                    code='not_found',
                ) from exc

        try:
            draft_order = game.draft_order()
        except exceptions.MyBaseException as exc:
            raise GameNamesLookupError(str(exc), code='draft_order') from exc

        rows = []
        for pick in draft_order:
            player = pick['player']
            member = player.discord_member
            account_name = member.polytopia_name or member.name_steam
            rows.append(GameNameRow(
                player_name=str(player.name),
                account_name=str(account_name) if account_name else None,
                timezone=player_timezone_values.effective_timezone_offset(
                    member
                ),
            ))
        return GameNamesSnapshot(game_id=int(game.id), rows=tuple(rows))


async def _run_bounded(call):
    concurrent_future = _legacy_name_executor.submit(call)
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException:
            logger.exception(
                'Cancelled retained-name worker completed with an error'
            )
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_account_name(
    discord_id: int,
) -> AccountNameSnapshot | None:
    return await _run_bounded(
        functools.partial(load_account_name, discord_id)
    )


async def run_registered_name_match(
    player_string: str,
    guild_id: int,
) -> RegisteredNameMatch:
    return await _run_bounded(
        functools.partial(
            load_registered_name_match,
            player_string,
            guild_id,
        )
    )


async def run_game_names(
    *,
    game_id: int | None,
    channel_id: int,
) -> GameNamesSnapshot:
    return await _run_bounded(
        functools.partial(
            load_game_names,
            game_id=game_id,
            channel_id=channel_id,
        )
    )
