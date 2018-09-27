from discord.ext import commands
from modules import models as models
# import modules.models as models
# from bot import logger


class CheckFailedError(Exception):
    """ Custom exception for when an input check fails """
    pass


async def get_guild_member(ctx, input):

        # Find matching Guild member by @Mention or Name. Fall back to case-insensitive search

        guild_matches, substring_matches = [], []
        try:
            guild_matches.append(await commands.MemberConverter().convert(ctx, input))
        except commands.errors.BadArgument:
            pass
            # No matches in standard MemberConverter. Move on to a case-insensitive search.
            for p in ctx.guild.members:
                name_str = p.nick.upper() + p.name.upper() if p.nick else p.name.upper()
                if p.name.upper() == input.upper():
                    guild_matches.append(p)
                if input.upper() in name_str:
                    substring_matches.append(p)

            if len(guild_matches) > 0:
                return guild_matches
            if len(input) > 2:
                return substring_matches

        return guild_matches


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)


def get_teams_of_players(guild_id, list_of_players):
    # given [List, Of, discord.Member, Objects] - return a, b
    # a = binary flag if all members are on the same Poly team. b = [list] of the Team objects from table the players are on
    # input: [Nelluk, Frodakcin]
    # output: True, [<Ronin>, <Ronin>]

    with models.db:
        query = models.Team.select().where(models.Team.guild_id == guild_id)
        list_of_teams = [team.name for team in query]               # ['The Ronin', 'The Jets', ...]
        list_of_matching_teams = []
        for player in list_of_players:
            matching_roles = get_matching_roles(player, list_of_teams)
            if len(matching_roles) == 1:
                name = next(iter(matching_roles))
                list_of_matching_teams.append(models.Team.get(models.Team.name == name))
            else:
                list_of_matching_teams.append(None)
                # Would be here if no player Roles match any known teams, -or- if they have more than one match

        same_team_flag = True if all(x == list_of_matching_teams[0] for x in list_of_matching_teams) else False
        return same_team_flag, list_of_matching_teams
