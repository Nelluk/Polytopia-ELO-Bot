"""Model-free Discord publishers for ordinary win and unwin results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

import settings
from modules import (
    confirmation_publication,
    confirmation_publication_workers,
    nova_graduation_workers,
)
from modules.game_result_publication_workers import GameResultPublicationSnapshot


Send = Callable[[str], Awaitable]
ConfirmedPublisher = Callable[[object, str, object, object], Awaitable]


def capture_publication_context(
    guild_id: int,
    *,
    bot=None,
) -> confirmation_publication_workers.ConfirmationPublicationContext:
    """Capture only primitive Discord cache state before worker dispatch."""

    bot = bot or settings.bot
    nova_guild_ids = tuple(dict.fromkeys(
        int(settings.server_ids[key])
        for key in ('polychampions', 'test')
        if settings.server_ids.get(key)
    ))
    nova_candidates = ()
    get_guild = getattr(bot, 'get_guild', None)
    runtime_guild = get_guild(int(guild_id)) if callable(get_guild) else None
    if runtime_guild is not None and int(guild_id) in nova_guild_ids:
        nova_role = discord.utils.get(runtime_guild.roles, name='The Novas')
        grad_role = discord.utils.get(runtime_guild.roles, name='Nova Grad')
        if nova_role is not None and grad_role is not None:
            nova_candidates = tuple(
                nova_graduation_workers.NovaParticipantSnapshot(
                    discord_id=int(member.id),
                    member_name=str(member.name),
                    mention=str(member.mention),
                    has_nova_role=True,
                    has_grad_role=False,
                )
                for member in tuple(getattr(nova_role, 'members', ()) or ())
                if grad_role not in tuple(getattr(member, 'roles', ()) or ())
            )
    return confirmation_publication_workers.ConfirmationPublicationContext(
        bot_guild_ids=tuple(
            int(candidate.id) for candidate in getattr(bot, 'guilds', ())
        ),
        nova_guild_ids=nova_guild_ids,
        nova_candidates=nova_candidates,
    )


async def publish_win_result(
    *,
    request,
    result,
    snapshot: GameResultPublicationSnapshot,
    guild,
    current_channel,
    send_public: Send,
    confirmed_publisher: ConfirmedPublisher,
    bot=None,
) -> None:
    """Publish one committed win using frozen data only."""

    bot = bot or settings.bot
    if result.previous_winner_name is not None:
        await send_public(
            f':warning: Unconfirmed game with ID {request.game_id} had '
            'previously been marked with winner '
            f'**{result.previous_winner_name}**.\n'
            f'{result.previous_confirmed_count} of '
            f'{result.previous_side_count} sides had confirmed.'
        )

    await confirmation_publication.publish_game_channels(
        snapshot,
        bot=bot,
        message=(
            'A win claim has been placed by '
            f'**{request.requester_name}** for winner '
            f'**{result.winner_name}**'
        ),
    )

    if result.confirmed:
        if result.all_sides_confirmed:
            await send_public('All sides have confirmed this victory. Good game!')
        if snapshot.confirmed_publication is None:
            raise RuntimeError('Committed win has no confirmation publication snapshot.')
        await confirmed_publisher(
            guild,
            request.prefix,
            current_channel,
            snapshot.confirmed_publication,
        )
        return

    printed_side_name = (
        result.winner_name
        if request.winning_side_id is not None or '@' in request.winner_text
        else request.winner_text
    )
    if result.first_claim:
        await send_public(
            f'**Game {request.game_id}** *{snapshot.game.name}* concluded '
            f'pending confirmation of winner **{result.winner_name}**\n'
            'To confirm, have opponents use the command '
            f'__`{request.prefix}win {request.game_id} '
            f'{printed_side_name}`__\n'
            'If opponents do not dispute the win then the game will be '
            'confirmed automatically after a period of time.\n'
            'If this win was claimed falsely please use the '
            '`/staffhelp` to contest, or you can '
            'cancel your claim with the command '
            f'`{request.prefix}unwin {request.game_id}`.\n'
            f'*Game lineup*: {" ".join(snapshot.roster_mentions)}'
        )
        return

    conf_str = 'Your confirmation has been logged. ' if result.new_confirmation else ''
    await send_public(
        f'{conf_str}**Game {request.game_id}** *{snapshot.game.name}* '
        'is pending confirmation: '
        f'{result.confirmed_count} of {result.side_count} sides have '
        'confirmed.\n'
        'Participants in the game should use the command '
        f'__`{request.prefix}win {request.game_id} '
        f'{printed_side_name}`__ to confirm the victory.\n'
        'Please post a screenshot of your victory in case there is '
        'a dispute. If this win was claimed in error please use the '
        '`/staffhelp`, or you can cancel your '
        'claim with the command '
        f'`{request.prefix}unwin {request.game_id}`'
    )


async def publish_unwin_result(
    *,
    snapshot: GameResultPublicationSnapshot,
    current_channel,
    previously_confirmed: bool,
    bot=None,
) -> None:
    """Publish one committed reset using frozen data only."""

    bot = bot or settings.bot
    await confirmation_publication.publish_game_channels(
        snapshot,
        bot=bot,
        message='The game has reset to *Incomplete* status.',
    )
    if previously_confirmed:
        for effect in snapshot.experience_roles:
            await confirmation_publication.publish_experience_role(effect, bot)
        if snapshot.champion_roles is not None:
            await confirmation_publication.publish_champion_roles(
                snapshot.champion_roles,
                bot,
            )
    await current_channel.send(
        'Game reset to *Incomplete*. Previously claimed win has been '
        'canceled.  Notifying game roster: '
        f'{" ".join(snapshot.roster_mentions)}'
    )
