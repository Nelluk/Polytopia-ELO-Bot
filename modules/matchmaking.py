import discord
from discord.ext import commands
from modules.models import Player, Match, MatchPlayer, db
from bot import logger, helper_roles, command_prefix, require_teams
from modules.elo_games import get_matching_roles as get_matching_roles
from modules.elo_games import get_teams_of_players as get_teams_of_players
from modules.elo_games import in_bot_channel as in_bot_channel
import peewee
import re
import datetime
import random


class Matchmaking_Cog():
    # Test matchmaking cog help
    """
    test help
    long string
    """

    def __init__(self, bot):
        self.bot = bot

    def poly_match(match_id):
        # Give game ID integer return matching Match or None. Can be used as a converter function for discord command input:
        # https://discordpy.readthedocs.io/en/rewrite/ext/commands/commands.html#basic-converters

        try:
            match_id = int(match_id)
        except ValueError:
            if match_id.upper()[0] == 'M':
                match_id = match_id[1:]
            else:
                logger.warn(f'Game with ID {match_id} cannot be found.')
                return
        with db:
            try:
                match = Match.get(id=match_id)
                logger.debug(f'Game with ID {match_id} found.')
                return match
            except peewee.DoesNotExist:
                logger.warn(f'Game with ID {match_id} cannot be found.')
                return None
            except ValueError:
                logger.error(f'Invalid game ID "{match_id}".')
                return None

    # @in_bot_channel()
    @commands.command()
    async def openmatch(self, ctx, *args):

        """
        Opens a matchmaking session for others to find

        Examples:
        openmatch 1v1

        openmatch 2v2 48h  (Expires in 48 hours)

        openmatch 2v2 Large map, no bardur
        """

        team_size = None
        expiration_hours = 24
        note_args = []

        match_host = Player.get_by_string(f'<@{ctx.author.id}>')
        if len(match_host) == 0:
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{command_prefix}setcode POLYCODE`')

        if len(Match.select().join(Player).where(Match.host.discord_id == ctx.author.id)) > 4:
            return await ctx.send(f'You have too many open matches already. Try using `{command_prefix}delmatch` on an existing one.')
        for arg in args:
            m = re.match(r"(\d+)v(\d+)", arg.lower())
            if m:
                # arg looks like '3v3'
                if int(m[1]) != int(m[2]):
                    return await ctx.send(f'Invalid match format {arg}. Sides must be equal.')
                if team_size is not None:
                    return await ctx.send(f'Multiple match formats included. Only include one, ie `{command_prefix}openmatch 3v3`')
                if not 0 < int(m[1]) < 7:
                    return await ctx.send(f'Invalid match size {arg}. Accepts 1v1 through 6v6')
                team_size = int(m[1])
                continue
            m = re.match(r"(\d+)h", arg.lower())
            if m:
                # arg looks like '12h'
                if not 0 < int(m[1]) < 97:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 96H (One hour through four days).')
                expiration_hours = int(m[1])
                continue
            note_args.append(arg)

        if team_size is None:
            return await ctx.send(f'Match size is required. Include argument like *2v2* to specify size')

        match_notes = ' '.join(note_args)[:75]
        notes_str = match_notes if match_notes else "\u200b"
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
        match = Match.create(host=match_host[0], team_size=team_size, expiration=expiration_timestamp, notes=match_notes)
        MatchPlayer.create(player=match_host[0], match=match)
        await ctx.send(f'Starting new open match ID M{match.id}. Size: {team_size}v{team_size}. Expiration: {expiration_hours} hours.\nNotes: *{notes_str}*')

    @commands.command(aliases=['leavematch'])
    async def leave_match(self, ctx, match: poly_match):
        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')
        if match.host.discord_id == ctx.author.id:
            return await ctx.send(f'You can\'t leave your own match. Use `{command_prefix}delmatch` instead.')

        try:
            matchplayer = MatchPlayer.select().join(Player).where((MatchPlayer.match == match) & (MatchPlayer.player.discord_id == ctx.author.id)).get()
        except peewee.DoesNotExist:
            return await ctx.send(f'You are not a member of match M{match.id}')

        with db:
            matchplayer.delete_instance()
            await ctx.send('Removing you from the match.')

    # @in_bot_channel()
    @commands.command(aliases=['joinmatch'])
    async def join_match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')
        if len(MatchPlayer.select().join(Player).where((MatchPlayer.player.discord_id == ctx.author.id) & (MatchPlayer.match == match))) > 0:
            return await ctx.send(f'You are already a member of match M{match.id}.')
        if len(match.matchplayer) >= (match.team_size * 2):
            return await ctx.send(f'Match M{match.id} cannot be joined. It is currently full and waiting for its host to `{command_prefix}startmatch M{match.id}`.')

        match_player = Player.get_by_string(f'<@{ctx.author.id}>')
        if len(match_player) == 0:
            return await ctx.send(f'You must be a registered player before joining a match. Try `{command_prefix}setcode POLYCODE`')

        if require_teams is True:
            _, player_teams = get_teams_of_players([ctx.message.author])
            if None in player_teams:
                return await ctx.send(f'You must be associated with one server Team.')

        with db:
            MatchPlayer.create(player=match_player[0], match=match)

            await ctx.send(f'You have joined match M{match.id}')
            if len(match.matchplayer) >= (match.team_size * 2):
                await ctx.send(f'Match M{match.id} is now full and the host <@{match.host.discord_id}> should start the game with `{command_prefix}startmatch M{match.id}`.')

            await ctx.send(embed=self.match_embed(match))

    # @in_bot_channel()
    @commands.command(aliases=['listmatches', 'matchlist', 'openmatches', 'listmatch', 'matches'])
    async def list_matches(self, ctx):
        Match.purge_expired_matches()

        embed = discord.Embed(title=f'Open matches - use `{command_prefix}joinmatch M#` to join one.')
        embed.add_field(name=f'`{"ID":<10}{"Host":<50} {"Capacity":<7} {"Exp":>4}`', value='\u200b', inline=False)
        for match in Match.select():
            notes_str = match.notes if match.notes else "\u200b"
            capacity_str = f' {len(match.matchplayer)} / {match.team_size * 2}'
            expiration = int((match.expiration - datetime.datetime.now()).total_seconds() / 3600.0)

            embed.add_field(name=f'`{"M"f"{match.id}":<10}{match.host.discord_name:<50} {capacity_str:<7} {expiration:>4}H`',
                value=f'{notes_str}')
        await ctx.send(embed=embed)

    # @in_bot_channel()
    @commands.command(aliases=['delmatch', 'deletematch'])
    async def delete_match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')

        if ctx.author.id == match.host.discord_id or len(get_matching_roles(ctx.author, helper_roles)) > 0:
            # User is deleting their own match, or user has a staff role
            await ctx.send(f'Deleting match M{match.id}')
            match.delete_instance()
            return
        else:
            return await ctx.send(f'You only have permission to delete your own matches.')

    # @in_bot_channel()
    @commands.command(aliases=['startmatch'])
    async def start_match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')

        if ctx.author.id != match.host.discord_id:
            return await ctx.send(f'Only the match host **{match.host.discord_name}** can do this.')

        if len(match.matchplayer) < (match.team_size * 2):
            return await ctx.send(f'This match is not yet full: {len(match.matchplayer)} / {match.team_size * 2} players.')

        team_home, team_away = match.return_suggested_teams()

        if match.team_size > 1:

            draft_order_str = 'Use draft order '
            if match.team_size == 2:
                draft_order_str += '**A B B A**\n'
            elif match.team_size == 3:
                draft_order_str += '**A B B A B A**\n'
            elif match.team_size == 4:
                draft_order_str += '**A B B A B A A B**\n'
            elif match.team_size == 5:
                draft_order_str += '**A B B A B A A B A B**\n'
            else:
                draft_order_str = ''

            await ctx.send(f'Suggested teams based on ELO:\n'
                f'{" / ".join(team_home)}\n**VS**\n{" / ".join(team_away)}\n\n'
                f'{draft_order_str}')

        await ctx.send(
            f'You\'ve got a match! Once you\'ve created the game in Polytopia, enter the following command to have it tracked for the ELO leaderboards:\n'
            f'`{command_prefix}reqgame "Name of Game" {" ".join(team_home)} vs {" ".join(team_away)}`')

        match.delete_instance()

    @in_bot_channel()
    @commands.command()
    async def match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')
        if len(match.matchplayer) >= (match.team_size * 2):
                await ctx.send(f'Match M{match.id} is now full and the host should start the game with `{command_prefix}startmatch M{match.id}`.')
        await ctx.send(embed=self.match_embed(match))

    @commands.command(aliases=['rtribes'])
    async def random_tribes(self, ctx, size='1v1'):

        m = re.match(r"(\d+)v(\d+)", size.lower())
        if m:
            # arg looks like '3v3'
            if int(m[1]) != int(m[2]):
                return await ctx.send(f'Invalid match format {size}. Sides must be equal.')
            if not 0 < int(m[1]) < 7:
                return await ctx.send(f'Invalid match size {size}. Accepts 1v1 through 6v6')
            team_size = int(m[1])

        tribes = [
            ('Bardur', 1),
            ('Kickoo', 1),
            ('Luxidoor', 1),
            ('Imperius', 1),
            ('Elyrion', 2),
            ('Zebasi', 2),
            ('Hoodrick', 2),
            ('Aquarion', 2),
            ('Oumaji', 3),
            ('Quetzali', 3),
            ('Vengir', 3),
            ('Ai-mo', 3),
            ('Xin-xi', 3)
        ]

        team_home, team_away = [], []

        tribe_groups = {}
        for tribe, group in tribes:
            tribe_groups.setdefault(group, set()).add(tribe)

        available_tribe_groups = list(tribe_groups.values())
        for _ in range(team_size):
            available_tribe_groups = [tg for tg in available_tribe_groups if len(tg) >= 2]

            this_tribe_group = random.choice(available_tribe_groups)

            new_home, new_away = random.sample(this_tribe_group, 2)
            this_tribe_group.remove(new_home)
            this_tribe_group.remove(new_away)

            team_home.append(new_home)
            team_away.append(new_away)

        await ctx.send(f'Home Team: {" / ".join(team_home)}\nAway Team: {" / ".join(team_away)}')

    def match_embed(ctx, match):
        embed = discord.Embed(title=f'Match **M{match.id}**\n{match.team_size}v{match.team_size} *hosted by* {match.host.discord_name}')
        notes_str = match.notes if match.notes else "\u200b"
        expiration = int((match.expiration - datetime.datetime.now()).total_seconds() / 3600.0)

        embed.add_field(name='Notes', value=notes_str, inline=False)
        embed.add_field(name='Capacity', value=f'{len(match.matchplayer)} / {match.team_size * 2}', inline=True)
        embed.add_field(name='Expires in', value=f'{expiration} hours', inline=True)
        embed.add_field(name='\u200b', value='\u200b', inline=False)

        for player in match.matchplayer:
            poly_str = player.player.polytopia_id if player.player.polytopia_id else '\u200b'
            embed.add_field(name=f'{player.player.discord_name} ({player.player.elo})', value=poly_str, inline=True)

        return embed


def setup(bot):
    bot.add_cog(Matchmaking_Cog(bot))
