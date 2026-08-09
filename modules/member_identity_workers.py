"""Worker-owned persistence for Discord identity and ELO-ban events."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import models


class MemberIdentityError(RuntimeError):
    """A member identity or moderation update could not be persisted."""


@dataclass(frozen=True)
class UsernameUpdateRequest:
    discord_id: int
    before_name: str
    after_name: str
    stored_name: str
    member_description: str


@dataclass(frozen=True)
class UsernameUpdateResult:
    discord_id: int
    registered: bool
    discord_member_id: int | None
    updated_player_ids: tuple[int, ...]


@dataclass(frozen=True)
class NicknameUpdateRequest:
    guild_id: int
    member_id: int
    before_nick: str | None
    after_name: str
    after_nick: str | None
    member_description: str


@dataclass(frozen=True)
class NicknameUpdateResult:
    guild_id: int
    member_id: int
    registered: bool
    player_id: int | None
    display_name: str | None


@dataclass(frozen=True)
class EloBanUpdateRequest:
    guild_id: int
    member_id: int
    is_banned: bool
    member_description: str


@dataclass(frozen=True)
class EloBanUpdateResult:
    guild_id: int
    member_id: int
    registered: bool
    player_id: int | None
    is_banned: bool


_member_identity_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='member-identity-update',
)


def _validate_member(*, member_id: int, member_description: str) -> None:
    if int(member_id) <= 0:
        raise MemberIdentityError('The member ID must be valid.')
    if not str(member_description).strip():
        raise MemberIdentityError('The member description is required.')


def _player_for_member(*, guild_id: int, member_id: int):
    return (
        models.Player
        .select(models.Player)
        .join(models.DiscordMember)
        .where(
            (models.DiscordMember.discord_id == int(member_id))
            & (models.Player.guild_id == int(guild_id))
        )
        .get()
    )


def _player_ids_for_discord_member(discord_member_id: int) -> tuple[int, ...]:
    return tuple(
        int(player_id)
        for (player_id,) in (
            models.Player
            .select(models.Player.id)
            .where(models.Player.discord_member == int(discord_member_id))
            .order_by(models.Player.id)
            .tuples()
        )
    )


def update_username(request: UsernameUpdateRequest) -> UsernameUpdateResult:
    """Persist an account-wide username and its global audit atomically."""

    _validate_member(
        member_id=request.discord_id,
        member_description=request.member_description,
    )
    if not str(request.before_name).strip() or not str(request.after_name).strip():
        raise MemberIdentityError('Both username values are required.')
    if not str(request.stored_name).strip():
        raise MemberIdentityError('The stored username is required.')

    with models.db.connection_context():
        with models.db.atomic():
            try:
                discord_member = models.DiscordMember.get(
                    models.DiscordMember.discord_id == int(request.discord_id)
                )
            except peewee.DoesNotExist:
                return UsernameUpdateResult(
                    discord_id=int(request.discord_id),
                    registered=False,
                    discord_member_id=None,
                    updated_player_ids=(),
                )

            player_ids = _player_ids_for_discord_member(discord_member.id)
            discord_member.update_name(new_name=str(request.stored_name))
            models.GameLog.write(
                game_id=0,
                guild_id=0,
                message=(
                    f'{request.member_description} changed username from '
                    f'"{request.before_name}"" to "{request.after_name}"'
                ),
            )

    return UsernameUpdateResult(
        discord_id=int(request.discord_id),
        registered=True,
        discord_member_id=int(discord_member.id),
        updated_player_ids=player_ids,
    )


def update_nickname(request: NicknameUpdateRequest) -> NicknameUpdateResult:
    """Persist one guild nickname/display name and its audit atomically."""

    _validate_member(
        member_id=request.member_id,
        member_description=request.member_description,
    )
    if int(request.guild_id) <= 0:
        raise MemberIdentityError('The guild ID must be valid.')
    if not str(request.after_name).strip():
        raise MemberIdentityError('The current username is required.')

    with models.db.connection_context():
        with models.db.atomic():
            try:
                player = _player_for_member(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                )
            except peewee.DoesNotExist:
                return NicknameUpdateResult(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                    registered=False,
                    player_id=None,
                    display_name=None,
                )

            display_name = player.generate_display_name(
                player_name=str(request.after_name),
                player_nick=request.after_nick,
            )
            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{request.member_description} had changed nickname from '
                    f'"{request.before_nick}" to "{request.after_nick}"'
                ),
            )

    return NicknameUpdateResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        registered=True,
        player_id=int(player.id),
        display_name=str(display_name),
    )


def update_elo_ban(request: EloBanUpdateRequest) -> EloBanUpdateResult:
    """Persist one guild Player ELO-ban state and its audit atomically."""

    _validate_member(
        member_id=request.member_id,
        member_description=request.member_description,
    )
    if int(request.guild_id) <= 0:
        raise MemberIdentityError('The guild ID must be valid.')

    with models.db.connection_context():
        with models.db.atomic():
            try:
                player = _player_for_member(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                )
            except peewee.DoesNotExist:
                return EloBanUpdateResult(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                    registered=False,
                    player_id=None,
                    is_banned=bool(request.is_banned),
                )

            player.is_banned = bool(request.is_banned)
            player.save()
            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{request.member_description} had *ELO Banned* role '
                    f'{"applied" if request.is_banned else "removed"}.'
                ),
            )

    return EloBanUpdateResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        registered=True,
        player_id=int(player.id),
        is_banned=bool(request.is_banned),
    )


async def _drain_future(future: Future):
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException:
            pass
        raise
    return future.result()


async def run_username_update(
    request: UsernameUpdateRequest,
) -> UsernameUpdateResult:
    return await _drain_future(
        _member_identity_executor.submit(update_username, request)
    )


async def run_nickname_update(
    request: NicknameUpdateRequest,
) -> NicknameUpdateResult:
    return await _drain_future(
        _member_identity_executor.submit(update_nickname, request)
    )


async def run_elo_ban_update(
    request: EloBanUpdateRequest,
) -> EloBanUpdateResult:
    return await _drain_future(
        _member_identity_executor.submit(update_elo_ban, request)
    )
