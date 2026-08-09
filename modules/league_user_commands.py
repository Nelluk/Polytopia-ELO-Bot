"""Shared service layer for small PolyChampions user workflows."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import house_show, interaction_lifecycle, league_user_workers


logger = logging.getLogger('polybot.' + __name__)

LEADER_ROLE_NAMES = ('House Leader', 'House Co-Leader', 'Mod')
NOVAS_ROLE_NAME = 'The Novas'
NEWBIE_ROLE_NAME = 'Newbie'


@dataclass(frozen=True)
class ActorIdentity:
    discord_id: int
    mention: str
    safe_name: str

    @property
    def label(self) -> str:
        return f'{self.mention} / **{self.safe_name}** (`{self.discord_id}`)'


def capture_actor(member) -> ActorIdentity:
    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name)
    )
    mention = str(getattr(member, 'mention', None) or f'<@{discord_id}>')
    return ActorIdentity(discord_id, mention, safe_name)


def league_scope(guild_id: int) -> bool:
    return bool(house_show._league_scope(int(guild_id)))


def guide_message() -> str:
    """Return the canonical modern league quick-start guide."""

    return (
        '# PolyChampions quick start\n'
        '1. **Register:** use `/player register` to set your Polytopia name.\n'
        '2. **Join the starter group:** use `/league join-novas`.\n'
        '3. **Find or open a game:** use `/game search` and choose **Open**, '
        'or use `/game open`.\n'
        '4. **Start your game:** use `/game start` with its game ID and '
        'Polytopia game name.\n'
        '5. **Review a game:** use `/game show`.\n\n'
        'Video tutorial: https://youtu.be/_KsDd0LT54M'
    )


def _role_names(member) -> frozenset[str]:
    return frozenset(str(role.name) for role in getattr(member, 'roles', ()))


def can_target_mark_active(actor, target) -> bool:
    return int(actor.id) == int(target.id) or bool(
        _role_names(actor).intersection(LEADER_ROLE_NAMES)
    )


def inactive_role(guild):
    role_name = settings.guild_setting(int(guild.id), 'inactive_role')
    return discord.utils.get(guild.roles, name=role_name)


def build_join_request(member, guild) -> league_user_workers.LeagueJoinRequest:
    return league_user_workers.LeagueJoinRequest(
        guild_id=int(guild.id),
        requester_id=int(member.id),
        requester_name=str(getattr(member, 'name', '') or ''),
        requester_nick=str(getattr(member, 'nick', '') or ''),
        league_scope=league_scope(guild.id),
    )


async def run_join_check(member, guild):
    return await league_user_workers.run_join_eligibility(
        build_join_request(member, guild)
    )


def matching_team(result, member):
    roles = _role_names(member)
    return next((team for team in result.team_roles if team.name in roles), None)


def public_sender(interaction):
    return interaction_lifecycle.public_interaction_sender(interaction)


def mark_active_success(*, actor, target, role_name: str, native: bool) -> str:
    if not native:
        return f'Removed *{role_name}* from {target.mention}.'
    actor_identity = capture_actor(actor)
    target_identity = capture_actor(target)
    return (
        f'{actor_identity.label} marked {target_identity.label} active by '
        f'removing *{discord.utils.escape_markdown(role_name)}*.'
    )


def join_success(*, member, native: bool, prefix: str = '$') -> str:
    if not native:
        return (
            'Congrats, you are now a member of the **The Novas**! To join '
            f'the fight go to a bot channel and type `{prefix}novagames`'
        )
    actor = capture_actor(member)
    return (
        f'{actor.label} joined **The Novas**. Use `/game search` and choose '
        '**Open** to find a game.'
    )
