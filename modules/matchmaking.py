import discord
from discord.ext import commands
from modules.models import Game, Player, Match, MatchPlayer, db
from bot import config, logger, helper_roles, mod_roles, command_prefix
from modules.elo_games import get_matching_roles as get_matching_roles
import peewee
import re
import datetime


class Matchmaking_Cog():
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

    @commands.command(aliases=['openmatch'])
    async def open_match(self, ctx, *args):
        team_size = None
        expiration_hours = 24
        note_args = []

        match_host = Player.get_by_string(f'<@{ctx.author.id}>')
        if len(match_host) == 0:
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{command_prefix}setcode POLYCODE`')

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
                if not 0 < int(m[1]) < 96:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 96H (One hour through four days).')
                expiration_hours = int(m[1])
                continue
            note_args.append(arg)
        # TODO: Prevent one person from having more than 5(?) open matches
        if team_size is None:
            return await ctx.send(f'Match size is required. Include argument like *2v2* to specify size')

        match_notes = ' '.join(note_args)[:75]
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
        match = Match.create(host=match_host[0], team_size=team_size, expiration=expiration_timestamp, notes=match_notes)
        MatchPlayer.create(player=match_host[0], match=match)
        await ctx.send(f'Starting new open match ID M{match.id}. Size: {team_size}v{team_size}. Expiration: {expiration_hours} hours.\nNotes: *{match_notes}*')

    @commands.command(aliases=['joinmatch'])
    async def join_match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')
        # TODO: Check if you are already in match
        if len(match.matchplayer) >= (match.team_size * 2):
            return await ctx.send(f'Match M{match.id} cannot be joined. It is currently full and waiting for its host to start.')

        match_player = Player.get_by_string(f'<@{ctx.author.id}>')
        if len(match_player) == 0:
            return await ctx.send(f'You must be a registered player before joining a match. Try `{command_prefix}setcode POLYCODE`')

        with db:
            MatchPlayer.create(player=match_player[0], match=match)

            await ctx.send(f'You have joined match M{match.id}')
            if len(match.matchplayer) >= (match.team_size * 2):
                await ctx.send(f'Match M{match.id} is now full and the host <@{match.host.discord_id}> should start the game.')

            await ctx.send(embed=self.match_embed(match))

    @commands.command(aliases=['listmatches', 'matchlist'])
    async def list_matches(self, ctx):

        embed = discord.Embed(title='Open matches')
        embed.add_field(name=f'`{"ID":<10}{"Host":<50} {"Capacity":<7}`', value='\u200b', inline=False)
        for match in Match.select():
            notes_str = match.notes if match.notes else "\u200b"
            capacity_str = f' {len(match.matchplayer)} / {match.team_size * 2}'
            embed.add_field(name=f'`{"M"f"{match.id}":<10}{match.host.discord_name:<50} {capacity_str:<7}`',
                value=f'{notes_str}')
        await ctx.send(embed=embed)

    @commands.command(aliases=['delmatch', 'deletematch'])
    async def delete_match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')

        if ctx.author.id == match.host.id or len(get_matching_roles(ctx.author, helper_roles)) > 0:
            # User is deleting their own match, or user has a staff role
            await ctx.send(f'Deleting match M{match.id}')
            match.delete_instance()
            return
        else:
            return await ctx.send(f'You only have permission to delete your own matches.')

    @commands.command()
    async def match(self, ctx, match: poly_match):

        if match is None:
            return await ctx.send(f'No matching match was found. Use {command_prefix}listmatches to see available matches.')
        await ctx.send(embed=self.match_embed(match))

    def match_embed(ctx, match):
        embed = discord.Embed(title=f'**M{match.id}** {match.team_size}v{match.team_size} Match *hosted by* {match.host.discord_name}')
        notes_str = match.notes if match.notes else "\u200b"
        embed.add_field(name='Notes', value=notes_str, inline=False)
        embed.add_field(name='Capacity', value=f'{len(match.matchplayer)} / {match.team_size * 2}', inline=False)
        # TODO: add 'expires in'
        for player in match.matchplayer:
            poly_str = player.player.polytopia_id if player.player.polytopia_id else '\u200b'
            embed.add_field(name=f'{player.player.discord_name} ({player.player.elo})', value=poly_str, inline=True)

        return embed


def setup(bot):
    bot.add_cog(Matchmaking_Cog(bot))
