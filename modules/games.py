import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, DiscordMember, Squad, GameSide, TribeFlair, Lineup
import logging
import datetime
import asyncio
from itertools import groupby

logger = logging.getLogger('polybot.' + __name__)


class PolyGame(commands.Converter):
    async def convert(self, ctx, game_id):

        try:
            game = Game.get(id=int(game_id))
        except ValueError:
            await ctx.send(f'Invalid game ID "{game_id}".')
            raise commands.UserInputError()
        except peewee.DoesNotExist:
            await ctx.send(f'Game with ID {game_id} cannot be found.')
            raise commands.UserInputError()
        else:
            logger.debug(f'Game with ID {game_id} found.')
            if game.guild_id != ctx.guild.id:
                logger.warn('Game does not belong to same guild')
                await ctx.send(f'Game with ID {game_id} is associated with a different Discord server.')
                raise commands.UserInputError()
            return game


class elo_games():

    def __init__(self, bot):
        self.bot = bot
        self.bg_task = bot.loop.create_task(self.task_purge_game_channels())

    async def on_member_update(self, before, after):
        player_query = Player.select().join(DiscordMember).where(
            (DiscordMember.discord_id == after.id) & (Player.guild_id == after.guild.id)
        )

        banned_role = discord.utils.get(before.guild.roles, name='ELO Banned')
        if banned_role not in before.roles and banned_role in after.roles:
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.is_banned = True
            player.save()
            logger.info(f'ELO Ban added for player {player.id} {player.name}')

        if banned_role in before.roles and banned_role not in after.roles:
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.is_banned = False
            player.save()
            logger.info(f'ELO Ban removed for player {player.id} {player.name}')

        # Updates display name in DB if user changes their discord name or guild nick
        if before.nick == after.nick and before.name == after.name:
            return

        if before.nick != after.nick:
            # update nick in guild's Player record
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.generate_display_name(player_name=after.name, player_nick=after.nick)

        if before.name != after.name:
            # update Discord Member Name, and update display name for each Guild/Player they share with the bot
            try:
                discord_member = DiscordMember.select().where(DiscordMember.discord_id == after.id).get()
            except peewee.DoesNotExist:
                return
            discord_member.update_name(new_name=after.name)

    @commands.command(aliases=['reqgame', 'helpstaff'])
    @commands.cooldown(2, 30, commands.BucketType.user)
    @settings.on_polychampions()
    async def staffhelp(self, ctx, *, message: str = None):
        """
        Send staff updates/fixes for an ELO game
        Teams should use this to notify staff of important events with their standard ELO games:
        restarts, substitutions, tribe choices

        Use `[p]seasongame` if the game is a League/Season game.
        **Example:**
        `[p]staffhelp Game 250 renamed to Fields of Fire`
        `[p]staffhelp Game 250 tribe choices: nelluk ai-mo, koric bardur.`
        """
        # Used so that users can submit game information to staff - bot will relay the text in the command to a specific channel.
        # Staff would then take action and create games. Also use this to notify staff of winners or name changes
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_request_channel'))
        if not channel:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'This server has not been configured for `{ctx.prefix}staffhelp` requests. You will need to ping a staff member.')

        if not message:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You must supply a help request, ie: `{ctx.prefix}staffhelp Game 51, restarted with name "Sweet New Game Name"`')

        await channel.send(f'{ctx.message.author} submitted: {ctx.message.clean_content}')
        await ctx.send(f'Request has been logged\n**Reminder** Wins are now claimed using the `{ctx.prefix}win` command. See `{ctx.prefix}help win`')

    @commands.command(brief='Sends staff details on a League game', usage='Week 2 game vs Mallards started called "Oceans of Fire"')
    @settings.on_polychampions()
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def seasongame(self, ctx, *, message: str = None):
        """
        Teams should use this to notify staff of important events with their League games: names of started games, restarts, substitutions, winners.
        """
        if not message:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You must supply a help request, ie: `{ctx.prefix}seasongame Week 2 game Ronin vs Jets started "Fields of Fire"`')

        # Ping AnarchoRex and send output to #season-drafts when team leaders send in game info
        channel = ctx.guild.get_channel(447902433964851210)
        helper_role = discord.utils.get(ctx.guild.roles, name='Season Helper')
        await channel.send(f'{ctx.message.author} submitted season game INFO <@&{helper_role.id}> <@451212023124983809>: {ctx.message.clean_content}')
        await ctx.send('Request has been logged')

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['leaderboard', 'leaderboards', 'lbglobal', 'lbg'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx, *, filters: str = ''):
        """ Display individual leaderboard

        Filters available:
        **global**
        Takes into account games played regardless of what server they were logged on.
        A player's global ELO is independent of their local server ELO.
        **max**
        Ranks leaderboard by a player's maximum ELO ever achieved

        Examples:
        `[p]lb` - Default local leaderboard
        `[p]lb global` - Global leaderboard
        `[p]lb max` - Local leaderboard for maximum historic ELO
        `[p]lb global max` - Leaderboard of maximum historic *global* ELO
        """

        leaderboard = []
        max_flag, global_flag = False, False
        target_model = Player
        lb_title = 'Individual Leaderboard'

        if ctx.invoked_with == 'lbglobal' or ctx.invoked_with == 'lbg':
            filters = filters + 'GLOBAL'

        if 'GLOBAL' in filters.upper():
            global_flag = True
            lb_title = 'Global Leaderboard'
            target_model = DiscordMember

        if 'MAX' in filters.upper():
            max_flag = True  # leaderboard ranked by player.max_elo
            lb_title += ' - Maximum ELO Achieved'

        leaderboard_query = target_model.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id, max_flag=max_flag)

        for counter, player in enumerate(leaderboard_query[:500]):
            wins, losses = player.get_record()
            emoji_str = player.team.emoji if not global_flag and player.team else ''
            leaderboard.append(
                (f'{(counter + 1):>3}. {emoji_str}{player.name}', f'`ELO {player.elo_max if max_flag else player.elo}\u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}`')
            )

        if ctx.guild.id != settings.server_ids['polychampions']:
            await ctx.send('Powered by PolyChampions. League server with a team focus and competitive players.\n'
                'Supporting up to 6-player team ELO games and automatic team channels. - <https://tinyurl.com/polychampions>')
            # link put behind url shortener to not show big invite embed
        await utilities.paginate(self.bot, ctx, title=f'**{lb_title}**\n{leaderboard_query.count()} ranked players', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['recent', 'active'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbrecent(self, ctx):
        """ Display most active recent players"""
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))

        leaderboard = []

        query = Player.select(Player, peewee.fn.COUNT(Lineup.id).alias('count')).join(Lineup).join(Game).where(
            (Lineup.player == Player.id) & (Game.is_pending == 0) & (Game.date > last_month) & (Game.guild_id == ctx.guild.id)
        ).group_by(Player.id).order_by(-peewee.SQL('count'))

        for counter, player in enumerate(query[:500]):
            wins, losses = player.get_record()
            emoji_str = player.team.emoji if player.team else ''
            leaderboard.append(
                (f'{(counter + 1):>3}. {emoji_str}{player.name}', f'`ELO {player.elo}\u00A0\u00A0\u00A0\u00A0Recent Games {player.count}`')
            )

        if ctx.guild.id != settings.server_ids['polychampions']:
            await ctx.send('Powered by PolyChampions. League server with a team focus and competitive players.\n'
                'Supporting up to 6-player team ELO games and automatic team channels. - <https://tinyurl.com/polychampions>')
            # link put behind url shortener to not show big invite embed
        await utilities.paginate(self.bot, ctx, title=f'**Most Active Recent Players**\n{query.count()} players in past 30 days', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @settings.teams_allowed()
    @commands.command(aliases=['teamlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbteam(self, ctx):
        """display team leaderboard"""
        # TODO: Only show number of members who have an ELO ranking?
        embed = discord.Embed(title='**Team Leaderboard**')

        query = Team.select().where(
            (Team.is_hidden == 0) & (Team.guild_id == ctx.guild.id)
        ).order_by(-Team.elo)
        for counter, team in enumerate(query):
            team_role = discord.utils.get(ctx.guild.roles, name=team.name)
            if not team_role:
                logger.error(f'Could not find matching role for team {team.name}')
                continue
            team_name_str = f'**{team.name}**   ({len(team_role.members)})'  # Show team name with number of members
            wins, losses = team.get_record()

            embed.add_field(name=f'{team.emoji} {(counter + 1):>3}. {team_name_str}\n`ELO: {team.elo:<5} W {wins} / L {losses}`', value='\u200b', inline=False)

        await ctx.send(embed=embed)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['squadlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbsquad(self, ctx):
        """Display squad leaderboard"""

        leaderboard = []
        squads = Squad.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id)
        for counter, sq in enumerate(squads[:200]):
            wins, losses = sq.get_record()
            squad_members = sq.get_members()
            emoji_list = [p.team.emoji for p in squad_members if p.team is not None]
            emoji_string = ' '.join(emoji_list)
            squad_names = ' / '.join(sq.get_names())
            leaderboard.append(
                (f'`{(counter + 1):>3}.` {emoji_string}`{squad_names}`', f'`(ELO: {sq.elo:4}) W {wins} / L {losses}`')
            )
        await utilities.paginate(self.bot, ctx, title='**Squad Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel()
    @commands.command(brief='Find squads or see details on a squad', usage='player1 [player2] [player3]', aliases=['squads'])
    async def squad(self, ctx, *args):
        """Find squads with specific players, or see details on a squad
        **Examples:**
        `[p]squad 5` - details on squad 5
        `[p]squad Nelluk` - squads containing Nelluk
        `[p]squad Nelluk frodakcin` - squad containing both players
        """
        try:
            # Argument is an int, so show squad by ID
            squad_id = int(''.join(args))
            squad = Squad.get(id=squad_id)
        except ValueError:
            squad_id = None
            # Args is not an int, which means search by game name
        except peewee.DoesNotExist:
            await ctx.send('Squad with ID {} cannot be found.'.format(squad_id))
            return

        if squad_id is None:
            # Search by player names
            squad_players = []
            for p_name in args:

                try:
                    squad_players.append(Player.get_or_except(p_name, guild_id=ctx.guild.id))
                except exceptions.NoSingleMatch as e:
                    return await ctx.send(e)

            squad_list = Squad.get_all_matching_squads(squad_players)
            if len(squad_list) == 0:
                return await ctx.send(f'Found no squads containing players: {" / ".join([p.name for p in squad_players])}')
            if len(squad_list) > 1:
                # More than one matching name found, so display a short list
                embed = discord.Embed(title=f'Found {len(squad_list)} matches. Try `{ctx.prefix}squad IDNUM`:')
                for squad in squad_list[:10]:
                    wins, losses = squad.get_record()
                    embed.add_field(
                        name=f'`ID {squad.id:>3} - {" / ".join(squad.get_names()):40}`',
                        value=f'`(ELO: {squad.elo}) W {wins} / L {losses}`',
                        inline=False
                    )
                return await ctx.send(embed=embed)

            # Exact matching squad found by player name
            squad = squad_list[0]

        wins, losses = squad.get_record()
        rank, lb_length = squad.leaderboard_rank(settings.date_cutoff)

        if rank is None:
            rank_str = 'Unranked'
        else:
            rank_str = f'{rank} of {lb_length}'

        names_with_emoji = [f'{p.team.emoji} {p.name}' if p.team is not None else f'{p.name}' for p in squad.get_members()]

        embed = discord.Embed(title=f'Squad card for Squad {squad.id}\n{"  /  ".join(names_with_emoji)}', value='\u200b')
        embed.add_field(name='Results', value=f'ELO: {squad.elo},  W {wins} / L {losses}', inline=True)
        embed.add_field(name='Ranking', value=rank_str, inline=True)
        recent_games = GameSide.select(Game).join(Game).where(
            (GameSide.squad == squad)
        ).order_by(-Game.date)

        embed.add_field(value='\u200b', name='Most recent games', inline=False)
        game_list = utilities.summarize_game_list(recent_games[:10])

        for game, result in game_list:
            embed.add_field(name=game, value=result, inline=False)

        await ctx.send(embed=embed)

    @settings.in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo'])
    async def player(self, ctx, *args):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.
        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """

        args_list = list(args)
        if len(args_list) == 0:
            # Player looking for info on themselves
            args_list.append(f'<@{ctx.author.id}>')

        # Otherwise look for a player matching whatever they entered
        player_mention = ' '.join(args_list)

        try:
            player = Player.get_or_except(player_string=player_mention, guild_id=ctx.guild.id)
        except exceptions.TooManyMatches:
            return await ctx.send(f'There is more than one player found with name *{player_mention}*. Specify user with @Mention.')
        except exceptions.NoMatches:
            # No Player matches - check for guild membership
            guild_matches = await utilities.get_guild_member(ctx, player_mention)
            if len(guild_matches) > 1:
                return await ctx.send(f'There is more than one member found with name *{player_mention}*. Specify user with @Mention.')
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not find *{player_mention}* by Discord name, Polytopia name, or Polytopia ID.')

            player, _ = Player.get_by_discord_id(discord_id=guild_matches[0].id, discord_name=guild_matches[0].name, discord_nick=guild_matches[0].nick, guild_id=ctx.guild.id)
            if not player:
                # Matching guild member but no Player or DiscordMember
                return await ctx.send(f'*{player_mention}* was found in the server but is not registered with me. '
                    f'Players can be register themselves with  `{ctx.prefix}setcode YOUR_POLYCODE`.')

        wins, losses = player.get_record()
        rank, lb_length = player.leaderboard_rank(settings.date_cutoff)

        wins_g, losses_g = player.discord_member.get_record()
        rank_g, lb_length_g = player.discord_member.leaderboard_rank(settings.date_cutoff)

        if rank is None:
            rank_str = 'Unranked'
        else:
            rank_str = f'{rank} of {lb_length}'

        results_str = f'ELO: {player.elo}\u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}'

        if rank_g:
            rank_str = f'{rank_str}\n{rank_g} of {lb_length_g} *Global*'
        if wins_g > wins or losses_g > losses:
            results_str = f'{results_str}\n**Global**\nELO: {player.discord_member.elo}\u00A0\u00A0\u00A0\u00A0W {wins_g} / L {losses_g}'

        embed = discord.Embed(title=f'Player card for __{player.name}__')
        embed.add_field(name='Results', value=results_str)
        embed.add_field(name='Ranking', value=rank_str)

        guild_member = ctx.guild.get_member(player.discord_member.discord_id)
        if guild_member:
            embed.set_thumbnail(url=guild_member.avatar_url_as(size=512))

        if player.team:
            team_str = f'{player.team.name} {player.team.emoji}' if player.team.emoji else player.team.name
            embed.add_field(name='Last-known Team', value=team_str)
        if player.discord_member.polytopia_name:
            embed.add_field(name='Polytopia Game Name', value=player.discord_member.polytopia_name)
        if player.discord_member.polytopia_id:
            embed.add_field(name='Polytopia ID', value=player.discord_member.polytopia_id)
            content_str = player.discord_member.polytopia_id
            # Used as a single message before player card so users can easily copy/paste Poly ID
        else:
            content_str = ''

        favorite_tribes = player.discord_member.favorite_tribes(limit=3)
        if favorite_tribes:
            favorite_tribe_objs = [TribeFlair.get_by_name(name=t['name'], guild_id=ctx.guild.id) for t in favorite_tribes]
            tribes_str = ' '.join([f'{t.emoji if t.emoji else t.tribe.name}' for t in favorite_tribe_objs])
            embed.add_field(value=tribes_str, name='Most-logged Tribes', inline=True)

        games_list = Game.search(player_filter=[player])
        if not games_list:
            recent_games_str = 'No games played'
        else:
            recent_games_count = player.games_played(in_days=30).count()
            recent_games_str = f'Most recent games ({games_list.count()} total, {recent_games_count} recently):'
        embed.add_field(value='\u200b', name=recent_games_str, inline=False)

        game_list = utilities.summarize_game_list(games_list[:5])
        for game, result in game_list:
            embed.add_field(name=game, value=result, inline=False)

        if ctx.guild.id != settings.server_ids['polychampions']:
            embed.add_field(value='Powered by **PolyChampions** - https://discord.gg/cX7Ptnv', name='\u200b', inline=False)

        await ctx.send(content=content_str, embed=embed)

    @settings.in_bot_channel()
    @settings.teams_allowed()
    @commands.command(usage='team_name')
    async def team(self, ctx, team_string: str):
        """See details on a team
        **Example:**
        [p]team Ronin
        """

        try:
            team = Team.get_or_except(team_string, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')

        embed = discord.Embed(title=f'Team card for **{team.name}** {team.emoji}')
        team_role = discord.utils.get(ctx.guild.roles, name=team.name)
        member_stats = []

        wins, losses = team.get_record()
        embed.add_field(name='Results', value=f'ELO: {team.elo}   Wins {wins} / Losses {losses}')

        if team_role:
            for member in team_role.members:
                # Create a list of members - pull ELO score from database if they are registered, or with 0 ELO if they are not
                p = Player.string_matches(player_string=str(member.id), guild_id=ctx.guild.id)
                if len(p) == 0:
                    member_stats.append((member.name, 0, f'`{member.name[:23]:.<25}{" - ":.<8}{" - ":.<5}{" - ":.<4}`'))
                else:
                    wins, losses = p[0].get_record()
                    lb_rank = p[0].leaderboard_rank(date_cutoff=settings.date_cutoff)[0]
                    games_played = p[0].games_played(in_days=30).count()
                    rank_str = f'#{lb_rank}' if lb_rank else ' - '
                    member_stats.append(({p[0].discord_member.name}, p[0].elo, f'`{p[0].discord_member.name[:23]:.<25}{p[0].elo:.<8}{rank_str:.<5}{games_played:.<4}`'))

            member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by ELO
            members_sorted = [str(x[2].replace(".", "\u200b ")) for x in member_stats[:28]]    # create list of strings like 'Nelluk  1277 #3  21'.
            # replacing '.' with "\u200b " (alternated zero width space with a normal space) so discord wont strip spaces

            members_str = "\n".join(members_sorted) if len(members_sorted) > 0 else '\u200b'
            embed.description = f'**Members({len(member_stats)})**\n__Player - ELO - Ranking - Recent Games__\n{members_str}'
        else:
            await ctx.send(f'Warning: No matching discord role "{team.name}" could be found. Player membership cannot be detected.')

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        embed.add_field(name='**Recent games**', value='\u200b', inline=False)

        recent_games = Game.search(team_filter=[team])

        game_list = utilities.summarize_game_list(recent_games[:5])

        for game, result in game_list:
            embed.add_field(name=game, value=result)

        await ctx.send(embed=embed)

    @commands.command(brief='Sets a Polytopia game code and registers user with the bot', usage='[user] polytopia_code')
    async def setcode(self, ctx, *args):
        """
        Sets your own Polytopia code, or allows a staff member to set a player's code. This also will register the player with the bot if not already.
        **Examples:**
        `[p]setcode YOUR_POLY_GAME_CODE`
        `[p]setcode Nelluk YOUR_POLY_GAME_CODE`
        """

        if len(args) == 1:      # User setting code for themselves. No special permissions required.
            target_discord_member = ctx.message.author
            new_id = args[0]

        elif len(args) == 2:    # User changing another user's code. Helper permissions required.

            if settings.is_staff(ctx) is False:
                return await ctx.send(f'You only have permission to set your own code. To do that use `{ctx.prefix}setcode YOURCODEHERE`')

            # Try to find matching guild/server member
            guild_matches = await utilities.get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not find any server member matching *{args[0]}*. Try specifying with an @Mention')
            elif len(guild_matches) > 1:
                return await ctx.send(f'Found {len(guild_matches)} server members matching *{args[0]}*. Try specifying with an @Mention')
            target_discord_member = guild_matches[0]
            new_id = args[1]
        else:
            # Unexpected input
            await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setcode YOURCODEHERE`')
            return

        if len(new_id) != 16 or new_id.isalnum() is False:
            # Very basic polytopia code sanity checking. Making sure it is 16-character alphanumeric.
            return await ctx.send(f'Polytopia code "{new_id}" does not appear to be a valid code.')

        _, team_list = Player.get_teams_of_players(guild_id=ctx.guild.id, list_of_players=[target_discord_member])

        player, created = Player.upsert(discord_id=target_discord_member.id,
                                        discord_name=target_discord_member.name,
                                        discord_nick=target_discord_member.nick,
                                        guild_id=ctx.guild.id,
                                        team=team_list[0])
        player.discord_member.polytopia_id = new_id
        player.discord_member.save()

        if created:
            await ctx.send('Player **{0.name}** added to system with Polytopia code {0.discord_member.polytopia_id} and ELO {0.elo}'.format(player))
        else:
            await ctx.send('Player **{0.name}** updated in system with Polytopia code {0.discord_member.polytopia_id}.'.format(player))

    @commands.command(aliases=['code'], usage='player_name')
    async def getcode(self, ctx, *, player_string: str = None):
        """Get game code of a player
        Just returns the code and nothing else so it can easily be copied."""

        if not player_string:
            player_string = str(ctx.author.id)

        guild_matches = await utilities.get_guild_member(ctx, player_string)

        if len(guild_matches) == 0:
            return await ctx.send(f'Could not find any server member matching *{player_string}*. Try specifying with an @Mention')
        elif len(guild_matches) > 1:
            player_matches = Player.string_matches(player_string=player_string, guild_id=ctx.guild.id)
            if len(player_matches) == 1:
                await ctx.send(f'Found {len(guild_matches)} server members matching *{player_string}*, but only **{player_matches[0].name}** is registered.')
                return await ctx.send(player_matches[0].discord_member.polytopia_id)

            return await ctx.send(f'Found {len(guild_matches)} server members matching *{player_string}*. Try specifying with an @Mention')
        target_discord_member = guild_matches[0]

        discord_member = DiscordMember.get_or_none(discord_id=target_discord_member.id)

        if discord_member and discord_member.polytopia_id:
            return await ctx.send(discord_member.polytopia_id)
        else:
            return await ctx.send(f'Member **{target_discord_member.name}** has no code on file.\n'
                f'Register your own code with `{ctx.prefix}setcode YOURCODEHERE`')

    @commands.command(aliases=['codes'], usage='game_id')
    async def getcodes(self, ctx, *, game: PolyGame = None):
        """Print all player codes associated with a game ID
        The codes will be printed on separate line for ease of copying, and in the order that players should be added to the game.
        **Examples:**
        `[p]getcodes 1250` - Get all player codes for players in game 1250
        """

        if not game:
            return await ctx.send(f'Game ID not provided. Usage: __`{ctx.prefix}getcodes GAME_ID`__')

        try:
            ordered_player_list = game.draft_order()
        except exceptions.MyBaseException as e:
            return await ctx.send(f'**Error:** {e}')

        warn_str = '\n*(List may take a few seconds to print due to discord anti-spam measures.)*' if len(ordered_player_list) > 2 else ''
        header_str = f'Polytopia codes for **game {game.id}**, in draft order:{warn_str}'

        first_loop = True
        async with ctx.typing():
            for p in ordered_player_list:
                if first_loop:
                    # header_str combined with first player's name in order to reduce number of ctx.send() that are done.
                    # More than 3-4 and they will drip out due to API rate limits
                    await ctx.send(f'{header_str}\n**{p["player"].name}** -- *Creates the game and invites everyone else*')
                    first_loop = False
                else:
                    await ctx.send(f'**{p["player"].name}**')
                poly_id = p['player'].discord_member.polytopia_id
                await ctx.send(poly_id if poly_id else '*No code registered*')

    @commands.command(brief='Set in-game name', usage='new_name')
    async def setname(self, ctx, *args):
        """Sets your own in-game name, or lets staff set a player's in-game name
        When this is set, people can find you by the in-game name with the `[p]player` command.
        **Examples:**
        `[p]setname PolyChamp` - Set your own in-game name to *PolyChamp*
        `[p]setname Nelluk PolyChamp` - Lets staff set in-game name of Nelluk to *PolyChamp*
        """

        if len(args) == 1:
            # User setting code for themselves. No special permissions required.
            target_string = f'<@{ctx.author.id}>'
            new_name = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            if settings.is_staff(ctx) is False:
                return await ctx.send('You do not have permission to trigger this command.')
            target_string = args[0]
            new_name = args[1]
        else:
            # Unexpected input
            return await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setname my_polytopia_name`. Use "quotation marks" if the name is more than one word.')

        try:
            player_target = Player.get_or_except(target_string, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nCorrect usage: `{ctx.prefix}getcode @Player`')

        player_target.discord_member.polytopia_name = new_name
        player_target.discord_member.save()
        await ctx.send(f'Player **{player_target.name}** updated in system with Polytopia name **{new_name}**.')

    @commands.command(aliases=['match'], usage='game_id')
    async def game(self, ctx, *, game_search: str = None):
        # async def game(self, ctx, game: PolyGame = None):

        """See details on a specific game ID
        **Examples**:
        `[p]game 51` - See details on game # 51.
        """
        if not game_search:
            return await ctx.send(f'Game ID number must be supplied, example: __`{ctx.prefix}game 1250`__')
        try:
            int(game_search)
        except ValueError:
            # User passed in non-numeric, probably searching by game title
            return await ctx.invoke(self.bot.get_command('games'), args=game_search)

        # Converting manually here to handle case of user passing a game name so info can be redirected to games() command
        game_converter = PolyGame()
        game = await game_converter.convert(ctx, game_search)

        embed, content = game.embed(ctx)
        return await ctx.send(embed=embed, content=content)

    @settings.in_bot_channel_strict()
    @commands.command(usage='player1 player2 ... ')
    async def games(self, ctx, *, args=None):

        """Search for games by participants or game name
        **Examples**:
        `[p]games Nelluk`
        `[p]games Nelluk oceans` - See games that included player Nelluk and the word *oceans* in the game name
        `[p]games Jets` - See games between those two teams
        `[p]games Jets Ronin`
        `[p]games Nelluk rickdaheals frodakcin Jets Ronin` - See games in which three players and two teams were all involved
        """

        # TODO: remove 'and/&' to remove confusion over game names like Ocean & Prophesy

        target_list = args.split() if args else []

        if len(target_list) == 1 and target_list[0].upper() == 'ALL':
            query = Game.search(status_filter=0, guild_id=ctx.guild.id)
            list_name = f'All games ({len(query)})'
            game_list = utilities.summarize_game_list(query[:500])
            results_str = 'All games'
        else:
            if not target_list:
                # Target is person issuing command
                target_list.append(str(ctx.author.id))

            results_title = []

            player_matches, team_matches, remaining_args = parse_players_and_teams(target_list, ctx.guild.id)
            p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

            if p_names:
                results_title.append(f'Including players: *{"* & *".join(p_names)}*')
            if t_names:
                results_title.append(f'Including teams: *{"* & *".join(t_names)}*')
            if remaining_args:
                results_title.append(f'Included in name: *{"* *".join(remaining_args)}*')

            results_str = '\n'.join(results_title)
            if not results_title:
                results_str = 'No filters applied'

            query = Game.search(player_filter=player_matches, team_filter=team_matches, title_filter=remaining_args, guild_id=ctx.guild.id)
            game_list = utilities.summarize_game_list(query[:500])
            list_name = f'{len(query)} game{"s" if len(query) != 1 else ""}\n{results_str}'

        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help games` for usage examples. Searched for:\n{results_str}')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=15, page_size=15)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['completed'])
    async def complete(self, ctx, *args):
        """List complete games for you or other players
        **Example:**
        `[p]complete` - Lists complete games you are playing in
        `[p]complete all` - Lists all complete games
        `[p]complete Nelluk` - Lists all complete games for player Nelluk
        `[p]complete Nelluk anarchoRex` - Lists all complete games with both players
        `[p]complete Nelluk Jets` - Lists all complete games for Nelluk that include team Jets
        `[p]complete Ronin Jets` - Lists all complete games that include teams Ronin and Jets
        """
        target_list = list(args)

        if len(args) == 1 and args[0].upper() == 'ALL':
            query = Game.search(status_filter=1, guild_id=ctx.guild.id)
            list_name = f'All completed games ({len(query)})'
        else:
            if not target_list:
                # Target is person issuing command
                target_list.append(str(ctx.author.id))

            player_matches, team_matches, _ = parse_players_and_teams(target_list, ctx.guild.id)
            p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

            query = Game.search(status_filter=1, player_filter=player_matches, team_filter=team_matches, guild_id=ctx.guild.id)

            list_name = f'{len(query)} completed game{"s" if len(query) != 1 else ""} '
            if len(p_names) > 0:
                list_name += f'that include *{"* & *".join(p_names)}*'
                if len(t_names) > 0:
                    list_name += f'\nIncluding teams: *{"* & *".join(t_names)}*'
            elif len(t_names) > 0:
                list_name += f'that include Team *{"* & *".join(t_names)}*'

        game_list = utilities.summarize_game_list(query[:500])
        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help complete` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @commands.command()
    async def incomplete(self, ctx, *args):
        """List incomplete games for you or other players
        **Example:**
        `[p]incomplete` - Lists incomplete games you are playing in
        `[p]incomplete all` - Lists all incomplete games
        `[p]incomplete Nelluk` - Lists all incomplete games for player Nelluk
        `[p]incomplete Nelluk anarchoRex` - Lists all incomplete games with both players
        `[p]incomplete Nelluk Jets` - Lists all incomplete games for Nelluk that include team Jets
        `[p]incomplete Ronin Jets` - Lists all incomplete games that include teams Ronin and Jets
        """
        target_list = list(args)

        if len(args) == 1 and args[0].upper() == 'ALL':
            query = Game.search(status_filter=2, guild_id=ctx.guild.id).order_by(Game.date)
            list_name = f'All incomplete games ({len(query)})'
        else:
            if not target_list:
                # Target is person issuing command
                target_list.append(str(ctx.author.id))

            player_matches, team_matches, _ = parse_players_and_teams(target_list, ctx.guild.id)
            p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

            query = Game.search(status_filter=2, player_filter=player_matches, team_filter=team_matches, guild_id=ctx.guild.id)

            list_name = f'{len(query)} incomplete game{"s" if len(query) != 1 else ""} '
            if len(p_names) > 0:
                list_name += f'that include *{"* & *".join(p_names)}*'
                if len(t_names) > 0:
                    list_name += f'\nIncluding teams: *{"* & *".join(t_names)}*'
            elif len(t_names) > 0:
                list_name += f'that include Team *{"* & *".join(t_names)}*'

        game_list = utilities.summarize_game_list(query[:500])
        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help incomplete` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @commands.command()
    async def wins(self, ctx, *args):
        """List games that you or others have won
        If any players names are listed, the first played is who the win is checked against. If no players listed, then the first team listed is checked for the win.
        **Example:**
        `[p]wins` - Lists all games you have won
        `[p]wins Nelluk` - Lists all wins for player Nelluk
        `[p]wins Nelluk anarchoRex` - Lists all games for both players, in which the first player is the winner
        `[p]wins Nelluk frodakcin Jets` - Lists all wins for Nelluk in which player frodakcin and team Jets participated
        `[p]wins Ronin Jets` - Lists all wins for team Ronin in which team Jets participated
        """
        target_list = list(args)

        if not target_list:
            # Target is person issuing command
            target_list.append(str(ctx.author.id))

        player_matches, team_matches, _ = parse_players_and_teams(target_list, ctx.guild.id)
        p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

        query = Game.search(status_filter=3, player_filter=player_matches, team_filter=team_matches, guild_id=ctx.guild.id)

        list_name = f'{len(query)} winning game{"s" if len(query) != 1 else ""} '
        if len(p_names) > 0:
            list_name += f'for **{p_names[0]}** '
            if len(p_names) > 1:
                list_name += f'that include *{"* & *".join(p_names[1:])}*'
            if len(t_names) > 0:
                list_name += f'\nIncluding teams: *{"* & *".join(t_names)}*'
        elif len(t_names) > 0:
            list_name += f'for Team **{t_names[0]}** '
            if len(t_names) > 1:
                list_name += f'that include Team *{"* & *".join(t_names[1:])}*'

        game_list = utilities.summarize_game_list(query[:500])
        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help wins` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['loss', 'lose'])
    async def losses(self, ctx, *args):
        """List games that you have lost, or others
        If any players names are listed, the first played is who the loss is checked against. If no players listed, then the first team listed is checked for the loss.
        **Examples:**
        `[p]losses` - Lists all games you have lost
        `[p]losses anarchoRex` - Lists all losses for player anarchoRex
        `[p]losses anarchoRex Nelluk` - Lists all games for both players, in which the first player is the loser
        `[p]losses rickdaheals Nelluk Ronin` - Lists all losses for rickdaheals in which player Nelluk and team Ronin participated
        `[p]losses Jets Ronin` - Lists all losses for team Jets in which team Ronin participated
        """
        target_list = list(args)

        if not target_list:
            # Target is person issuing command
            target_list.append(str(ctx.author.id))

        player_matches, team_matches, _ = parse_players_and_teams(target_list, ctx.guild.id)
        p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

        query = Game.search(status_filter=4, player_filter=player_matches, team_filter=team_matches, guild_id=ctx.guild.id)

        list_name = f'{len(query)} losing game{"s" if len(query) != 1 else ""} '
        if len(p_names) > 0:
            list_name += f'for **{p_names[0]}** '
            if len(p_names) > 1:
                list_name += f'that include *{"* & *".join(p_names[1:])}*'
            if len(t_names) > 0:
                list_name += f'\nIncluding teams: *{"* & *".join(t_names)}*'
        elif len(t_names) > 0:
            list_name += f'for Team **{t_names[0]}** '
            if len(t_names) > 1:
                list_name += f'that include Team *{"* & *".join(t_names[1:])}*'

        game_list = utilities.summarize_game_list(query[:500])
        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help losses` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel()
    @commands.command(usage='"Name of Game" player1 player2 vs player3 player4')
    @settings.is_user_check()
    async def newgame(self, ctx, game_name: str = None, *args):
        """Adds an existing game to the bot for tracking

        **Examples:**
        `[p]newgame "Name of Game" nelluk vs koric` - Sets up a 1v1 game
        `[p]newgame "Name of Game" koric` - Sets up a 1v1 game versus yourself and koric (shortcut)
        `[p]newgame "Name of Game" nelluk frodakcin vs bakalol ben` - Sets up a 2v2 game
        """
        example_usage = (f'Example usage:\n`{ctx.prefix}newgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game\n'
                         f'`{ctx.prefix}newgame "Name of Game" player2` - Start a 1v1 with yourself and player2')

        if not game_name:
            return await ctx.send(f'Invalid format. {example_usage}')
        if not args:
            return await ctx.send(f'Invalid format. {example_usage}')

        if len(game_name.split(' ')) < 2 and ctx.author.id != settings.owner_id:
            return await ctx.send(f'Invalid game name. Make sure to use "quotation marks" around the full game name.\n{example_usage}')

        if not utilities.is_valid_poly_gamename(input=game_name):
            return await ctx.send('That name looks made up. :thinking: You need to manually create the game __in Polytopia__, come back and input the name of the new game you made.\n'
                f'You can use `{ctx.prefix}code NAME` to get the code of each player in this game.')

        if len(args) == 1:
            args_list = [str(ctx.author.id), 'vs', args[0]]
        else:
            args_list = list(args)

        player_groups = [list(group) for k, group in groupby(args_list, lambda x: x.lower() in ('vs', 'versus')) if not k]
        # split ['foo', 'bar', 'vs', 'baz', 'bat'] into [['foo', 'bar']['baz', 'bat']]

        biggest_team = max(len(group) for group in player_groups)
        smallest_team = min(len(group) for group in player_groups)
        total_players = sum(len(group) for group in player_groups)

        if len(player_groups) < 2:
            return await ctx.send(f'Invalid format. {example_usage}')
        if total_players > 4 and (not settings.is_power_user(ctx)) and ctx.guild.id != settings.server_ids['polychampions']:
            return await ctx.send('You only have permissions to create games of up to 4 players. More active server members can create larger games.')

        if total_players > 12:
            return await ctx.send(f'You cannot have more than twelve players.')
        if biggest_team > settings.guild_setting(ctx.guild.id, 'max_team_size'):
            if settings.is_mod(ctx):
                await ctx.send('Moderator over-riding server size limits')
            elif settings.guild_setting(ctx.guild.id, 'allow_uneven_teams') and smallest_team <= settings.guild_setting(ctx.guild.id, 'max_team_size'):
                await ctx.send('Warning: Team sizes are uneven.')
            else:
                return await ctx.send(f'This server has a maximum team size of {settings.guild_setting(ctx.guild.id, "max_team_size")}. For full functionality with support for up to 5-player team games and league play check out PolyChampions.')

        discord_groups, discord_players_flat = [], []
        author_found = False
        for group in player_groups:
            # Convert each arg into a Discord Guild Member and build a new list of lists. Or return if any arg can't be matched.
            discord_group = []
            for p in group:
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "**{p}**" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "**{p}**". Try being more specific or using an @Mention.')

                if guild_matches[0].id in settings.ban_list or discord.utils.get(guild_matches[0].roles, name='ELO Banned'):
                    if settings.is_mod(ctx):
                        await ctx.send(f'**{guild_matches[0].name}** has been **ELO Banned** -- *moderator over-ride* :thinking:')
                    else:
                        return await ctx.send(f'**{guild_matches[0].name}** has been **ELO Banned** and cannot join any new games. :cry:')

                if guild_matches[0] in discord_players_flat:
                    return await ctx.send('Duplicate players detected. Game not created.')
                else:
                    discord_players_flat.append(guild_matches[0])

                if guild_matches[0] == ctx.author:
                    author_found = True

                discord_group.append(guild_matches[0])

            discord_groups.append(discord_group)

        n = len(discord_groups[0])
        if not all(len(g) == n for g in discord_groups):
            if settings.guild_setting(ctx.guild.id, 'allow_uneven_teams'):
                await ctx.send('**Warning:** Teams are not the same size. This is allowed but may not be what you want.')
            else:
                return await ctx.send('Teams are not the same size. This is not allowed on this server. Game not created.')

        if not author_found and not settings.is_staff(ctx):
            # TODO: possibly allow this in PolyChampions (rickdaheals likes to do this)
            return await ctx.send('You can\'t create a game that you are not a participant in.')

        logger.info(f'All input checks passed. Creating new game records with args: {args}')

        with db.atomic():
            newgame = Game.create_game(discord_groups, name=game_name, guild_id=ctx.guild.id, require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))
            host_player, _ = Player.get_by_discord_id(discord_id=ctx.author.id, guild_id=ctx.guild.id)
            if host_player:
                newgame.host = host_player
                newgame.save()
            else:
                logger.error('Could not add host for newgame')

            await post_newgame_messaging(ctx, game=newgame)

    # @settings.in_bot_channel()
    @commands.command(aliases=['endgame', 'wingame', 'winner'], usage='game_id winner_name')
    async def win(self, ctx, winning_game: PolyGame = None, winning_side_name: str = None):
        """
        Declare winner of an existing game

        The win must be confirmed by a member of the losing side (or staff) if the game has two sides.
        If the game has more than two sides, staff will need to confirm the win.
        Use player name for 1v1 games, otherwise use team names *(Home/Away/Owls/Sharks/etc)*

        **Example:**
        `[p]win 5 Ronin` - Declare Ronin winner of game 5
        `[p]win 5 Nelluk` - Declare Nelluk winner of game 5
        """

        usage = ('Include both game ID and the name of the winning side. Example usage:\n'
                f'`{ctx.prefix}win 422 Nelluk`\n`{ctx.prefix}win 425 Owls` *For a team game*')
        if not winning_game or not winning_side_name:
            return await ctx.send(usage)

        try:
            winning_obj, winning_side = winning_game.gameside_by_name(ctx, name=winning_side_name)
            # winning_obj will be a Team or a Player depending on squad size
            # winning_side will be their GameSide
        except exceptions.MyBaseException as ex:
            return await ctx.send(f'{ex}')

        if winning_game.is_completed is True:
            if winning_game.is_confirmed is True:
                return await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winner **{winning_game.winner.name()}**')
            elif winning_game.winner != winning_side:
                await ctx.send(f'Warning: Unconfirmed game with ID {winning_game.id} had previously been marked with winner **{winning_game.winner.name()}**')

        if winning_game.is_pending:
            return await ctx.send(f'This game has not started yet.')

        if settings.is_staff(ctx):
            confirm_win = True
        else:
            has_player, author_side = winning_game.has_player(discord_id=ctx.author.id)

            if not has_player:
                return await ctx.send(f'You were not a participant in this game.')

            if len(winning_game.gamesides) == 2:
                if winning_side == author_side:
                    # Author declaring their side won
                    for side in winning_game.gamesides:
                        if side != winning_side:
                            if len(side.lineup) == 1:
                                confirm_str = f'losing opponent <@{side.lineup[0].player.discord_member.discord_id}>'
                            else:
                                confirm_str = f'a member of losing side **{side.name()}**'
                            break

                    await ctx.send(f'Game {winning_game.id} concluded pending confirmation of winner **{winning_obj.name}**\n'
                        f'To confirm, have {confirm_str} use the command __`{ctx.prefix}win {winning_game.id} {winning_side_name}`__ or ask an **@ELO Helper** to confirm your win with screenshot evidence.')
                    confirm_win = False
                else:
                    # Author declaring their side lost
                    await ctx.send(f'Detected confirmation from losing side. Good game!')
                    confirm_win = True
            else:
                # Game with more than two teams - staff confirmation required. Possibly improve later so that every team can unanimously confirm
                await ctx.send(f'Since this is a {len(winning_game.gamesides)}-team game, staff confirmation is required. Ping **@ELO Helper** with a screenshot of your victory. ')
                confirm_win = False

                # # Automatically inform staff of needed confirmation if game_request_channel is enabled
                # if settings.guild_setting(ctx.guild.id, 'game_request_channel'):
                #     channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_request_channel'))
                #     try:
                #         await channel.send(f'{ctx.message.author} submitted game winner: Game {winning_game.id} - Winner: **{winning_obj.name}**'
                #             f'\nUse `{ctx.prefix}confirm {winning_game.id}` to confirm win.'
                #             f'\nUse `{ctx.prefix}confirm` to list all games awaiting confirmation.')
                #     except discord.errors.DiscordException:
                #         logger.warn(f'Could not send message to game_request_channel: {settings.guild_setting(ctx.guild.id, "game_request_channel")}')
                #         await ctx.send(f'Use `{ctx.prefix}staffhelp` to request staff confirm the win.')
                #     else:
                #         await ctx.send(f'Staff has automatically been informed of this win and confirmation is pending.')

        winning_game.declare_winner(winning_side=winning_side, confirm=confirm_win)

        if confirm_win:

            if winning_game.is_ranked and any(l.elo_change_player == 0 for l in winning_game.lineup):
                # try to catch a phantom bug where rarely a completed game will not save the ELO changes correctly to the DB.
                # not reproducible at all so simply re-running the calc if it happens and logging

                lineup_details = [f'\nLineup ID: {l.id}, player: {l.player.name}, elo: {l.player.elo}, elo_change_player: {l.elo_change_player}' for l in winning_game.lineup]
                # Game.recalculate_elo_since(timestamp=winning_game.completed_ts)  # Temporarily removing since ELO totals after this runs arent right, winner getting double ELO for example
                logger.critical(f'Possible ELO bug in result from {winning_game.id}\n{" ".join(lineup_details)}')
                owner = ctx.guild.get_member(settings.owner_id)
                if owner:
                    try:
                        await owner.send(f'Possible ELO bug in result from {winning_game.id} - Check debug logs for more info on ELO calcs\n{" ".join(lineup_details)}')
                    except discord.DiscordException as e:
                        logger.warn(f'Error DMing bot owner: {e}')

            # Cleanup game channels and announce winners
            await post_win_messaging(ctx, winning_game)

    @settings.in_bot_channel()
    @commands.command(usage='game_id', aliases=['delete_game', 'delgame', 'delmatch', 'delete'])
    async def deletegame(self, ctx, game: PolyGame):
        """Deletes a game

        You can delete a game if you are the host and is has not started yet.
        Mods can delete completed games which will reverse any ELO changes they caused.
        **Example:**
        `[p]deletegame 25`
        """

        if game.is_pending:
            is_hosted_by, host = game.is_hosted_by(ctx.author.id)
            if not is_hosted_by and not settings.is_staff(ctx):
                host_name = f' **{host.name}**' if host else ''
                return await ctx.send(f'Only the game host{host_name} or server staff can do this.')
            game.delete_game()
            return await ctx.send(f'Deleting open game {game.id}')

        if not settings.is_mod(ctx):
            return await ctx.send('Only server mods can delete completed or in-progress games.')

        if game.winner and game.is_confirmed:
            await ctx.send(f'Deleting game with ID {game.id} and re-calculating ELO for all subsequent games. This will take a few seconds.')

        if game.announcement_message:
            game.name = f'~~{game.name}~~ GAME DELETED'
            await game.update_announcement(ctx)

        await game.delete_squad_channels(ctx.guild)

        async with ctx.typing():
            gid = game.id
            await self.bot.loop.run_in_executor(None, game.delete_game)
            # Allows bot to remain responsive while this large operation is running.
            # Can result in funky behavior especially if another operation tries to close DB connection, but seems to still get this operation done reliably
            await ctx.send(f'Game with ID {gid} has been deleted and team/player ELO changes have been reverted, if applicable.')

    @settings.in_bot_channel()
    @commands.command(aliases=['namegame', 'gamename'], usage='game_id "New Name"')
    async def rename(self, ctx, game: PolyGame = None, *args):
        """Renames an existing game (due to restarts)

        You can rename a game for which you are the host
        **Example:**
        `[p]rename 25 Mountains of Fire`
        """

        if not game:
            return await ctx.send(f'No game ID supplied')
        if game.is_pending:
            return await ctx.send(f'This game has not started yet.')

        is_hosted_by, host = game.is_hosted_by(ctx.author.id)
        if not is_hosted_by and not settings.is_staff(ctx) and not game.is_created_by(discord_id=ctx.author.id):
            # host_name = f' **{host.name}**' if host else ''
            return await ctx.send(f'Only the game creator **{game.creating_player().name}** or server staff can do this.')

        new_game_name = ' '.join(args)
        old_game_name = game.name
        game.name = new_game_name

        game.save()

        await game.update_squad_channels(ctx)
        await game.update_announcement(ctx)

        await ctx.send(f'Game ID {game.id} has been renamed to "**{game.name}**" from "**{old_game_name}**"')

    async def task_purge_game_channels(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            # purge game channels from games that were concluded at least 24 hours ago

            await asyncio.sleep(60)
            yesterday = (datetime.datetime.now() + datetime.timedelta(hours=-24))

            old_games = Game.select().join(GameSide, on=(GameSide.game == Game.id)).where(
                (Game.is_confirmed == 1) & (Game.completed_ts < yesterday) & (GameSide.team_chan.is_null(False))
            )

            logger.info(f'running task_purge_game_channels on {len(old_games)} games')
            for game in old_games:
                guild = discord.utils.get(self.bot.guilds, id=game.guild_id)
                if guild:
                    await game.delete_squad_channels(guild=guild)

            await asyncio.sleep(60 * 60 * 2)


async def post_win_messaging(ctx, winning_game):

    # await winning_game.delete_squad_channels(guild=ctx.guild)
    await winning_game.update_squad_channels(ctx=ctx, message=f'The game is over with **{winning_game.winner.name()}** victorious. *This channel will be purged in ~24 hours.*')
    player_mentions = [f'<@{l.player.discord_member.discord_id}>' for l in winning_game.lineup]
    embed, content = winning_game.embed(ctx)

    if settings.guild_setting(ctx.guild.id, 'game_announce_channel') is not None:
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
        if channel is not None:
            await channel.send(f'Game concluded! Congrats **{winning_game.winner.name()}**. Roster: {" ".join(player_mentions)}')
            await channel.send(embed=embed)
            return await ctx.send(f'Game concluded! See {channel.mention} for full details.')

    await ctx.send(f'Game concluded! Congrats **{winning_game.winner.name()}**. Roster: {" ".join(player_mentions)}')
    await ctx.send(embed=embed, content=content)


async def post_newgame_messaging(ctx, game):

    mentions_list = [f'<@{l.player.discord_member.discord_id}>' for l in game.lineup]

    embed, content = game.embed(ctx)

    if settings.guild_setting(ctx.guild.id, 'game_announce_channel') is not None:
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
        if channel is not None:
            await channel.send(f'New game ID {game.id} started! Roster: {" ".join(mentions_list)}')
            announcement = await channel.send(embed=embed, content=content)
            await ctx.send(f'New game ID {game.id} started! See {channel.mention} for full details.')
            game.announcement_message = announcement.id
            game.announcement_channel = announcement.channel.id
            game.save()
    else:
        await ctx.send(f'New game ID {game.id} started! Roster: {" ".join(mentions_list)}')
        await ctx.send(embed=embed, content=content)

    if settings.guild_setting(ctx.guild.id, 'game_channel_categories'):
        await game.create_squad_channels(ctx)


def parse_players_and_teams(input_list, guild_id: int):
    # Given a [List, of, string, args], try to match each one against a Team or a Player, and return lists of those matches
    # return any args that matched nothing back in edited input_list

    player_matches, team_matches = [], []
    for arg in list(input_list):  # Copy of list
        if arg.upper() in ['THE', 'OF', 'AND', '&']:
            input_list.remove(arg)
            continue
        teams = Team.get_by_name(arg, guild_id)
        if len(teams) == 1:
            team_matches.append(teams[0])
            input_list.remove(arg)
        else:
            players = Player.string_matches(player_string=arg, guild_id=guild_id, include_poly_info=False)
            if len(players) > 0:
                player_matches.append(players[0])
                input_list.remove(arg)

    return player_matches, team_matches, input_list


def setup(bot):
    bot.add_cog(elo_games(bot))
