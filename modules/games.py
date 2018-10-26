import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, DiscordMember, Squad, SquadGame, Match
import logging
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
                await ctx.send(f'Game with ID {game_id} cannot be found on this Discord server.')
                raise commands.UserInputError()
            return game


class games():

    def __init__(self, bot):
        self.bot = bot

    async def on_member_update(self, before, after):
        # Updates display name in DB if user changes their discord name or guild nick
        if before.nick == after.nick and before.name == after.name:
            return

        try:
            player = Player.select(Player, DiscordMember).join(DiscordMember).where(
                (DiscordMember.discord_id == after.id) & (Player.guild_id == after.guild.id)
            ).get()
        except peewee.DoesNotExist:
            return

        player.discord_member.name = after.name
        player.discord_member.save()
        player.generate_display_name(player_name=after.name, player_nick=after.nick)

    @commands.command(aliases=['reqgame', 'helpstaff'])
    @commands.cooldown(2, 30, commands.BucketType.user)
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
        if settings.guild_setting(ctx.guild.id, 'game_request_channel') is None:
            return

        if not message:
            return await ctx.send(f'You must supply a help request, ie: `{ctx.prefix}staffhelp Game 51, restarted with name "Sweet New Game Name"`')
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_request_channel'))
        await channel.send(f'{ctx.message.author} submitted: {ctx.message.clean_content}')
        await ctx.send(f'Request has been logged\n**Reminder** Wins are now claimed using the `{ctx.prefix}win` command. See `{ctx.prefix}help win`')

    @commands.command(brief='Sends staff details on a League game', usage='Week 2 game vs Mallards started called "Oceans of Fire"')
    @settings.on_polychampions()
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def seasongame(self, ctx):
        """
        Teams should use this to notify staff of important events with their League games: names of started games, restarts, substitutions, winners.
        """
        # Ping AnarchoRex and send output to #season-drafts when team leaders send in game info
        channel = ctx.guild.get_channel(447902433964851210)
        helper_role = discord.utils.get(ctx.guild.roles, name='Season Helper')
        await channel.send(f'{ctx.message.author} submitted season game INFO <@&{helper_role.id}> <@451212023124983809>: {ctx.message.clean_content}')
        await ctx.send('Request has been logged')

    @commands.command(aliases=['namegame', 'rename'], usage='game_id "New Name"')
    @settings.is_staff_check()
    async def gamename(self, ctx, game: PolyGame, *args):
        """*Staff:* Renames an existing game
        **Example:**
        `[p]gamename 25 Mountains of Fire`
        """

        new_game_name = ' '.join(args)
        game.name = new_game_name.title()
        game.save()

        await game.update_squad_channels(ctx)
        await game.update_announcement(ctx)

        await ctx.send(f'Game ID {game.id} has been renamed to "{game.name}"')

    @settings.in_bot_channel()
    @commands.command()
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx):
        """ Display individual leaderboard"""

        leaderboard = []
        leaderboard_query = Player.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id)
        for counter, player in enumerate(leaderboard_query[:500]):
            wins, losses = player.get_record()
            emoji_str = player.team.emoji if player.team else ''
            leaderboard.append(
                (f'`{(counter + 1):>3}.` {emoji_str}`{player.name}`', f'`(ELO: {player.elo:4}) W {wins} / L {losses}`')
            )

        if ctx.guild.id != settings.server_ids['polychampions']:
            await ctx.send('Powered by PolyChampions. League server with a team focus and competitive player.\n'
                'Supporting up to 6-player team ELO games and automatic team channels. - <https://tinyurl.com/polychampions>')
            # link put behind url shortener to not show big invite embed
        await utilities.paginate(self.bot, ctx, title='**Individual Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel()
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

    @settings.in_bot_channel()
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
        recent_games = SquadGame.select(Game).join(Game).where(
            (SquadGame.squad == squad)
        ).order_by(-Game.date)

        embed.add_field(value='\u200b', name='Most recent games', inline=False)
        game_list = utilities.summarize_game_list(recent_games[:10])

        for game, result in game_list:
            embed.add_field(name=game, value=result)

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

        if len(args) == 0:
            # Player looking for info on themselves
            try:
                player = Player.get_or_except(player_string=str(ctx.author.id), guild_id=ctx.guild.id)
            except exceptions.NoSingleMatch as ex:
                return await ctx.send(f'{ex}\nTry setting your code with {ctx.prefix}setcode')
        else:
            # Otherwise look for a player matching whatever they entered
            player_mention = ' '.join(args)

            try:
                player = Player.get_or_except(player_string=player_mention, guild_id=ctx.guild.id)
            except exceptions.NoMatches:
                # No matching name in database. Warn if player is found in guild.
                matches = await utilities.get_guild_member(ctx, player_mention)
                if len(matches) > 0:
                    await ctx.send(f'"{player_mention}" was found in the server but is not registered with me. '
                        f'Players can be registered with `{ctx.prefix}setcode` or being in a new game\'s lineup.')

                return await ctx.send(f'Could not find \"{player_mention}\" by Discord name, Polytopia name, or Polytopia ID.')
            except exceptions.TooManyMatches:
                return await ctx.send(f'There is more than one player found with name "{player_mention}". Specify user with @Mention.')

        wins, losses = player.get_record()
        rank, lb_length = player.leaderboard_rank(settings.date_cutoff)

        if rank is None:
            rank_str = 'Unranked'
        else:
            rank_str = f'{rank} of {lb_length}'

        embed = discord.Embed(title=f'Player card for __{player.name}__')
        embed.add_field(name='Results', value=f'ELO: {player.elo}, W {wins} / L {losses}')
        embed.add_field(name='Ranking', value=rank_str)

        guild_member = ctx.guild.get_member(player.discord_member.discord_id)
        if guild_member is not None:
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

        favorite_tribes = player.favorite_tribes(limit=3)

        if favorite_tribes:
            tribes_str = ' '.join([f'{t["emoji"] if t["emoji"] else t["name"]} ' for t in favorite_tribes])
            embed.add_field(value=tribes_str, name='Most-logged Tribes', inline=True)

        recent_games = Game.search(player_filter=[player])
        if not recent_games:
            recent_games_str = 'No games played'
        else:
            recent_games_str = f'Most recent games ({len(recent_games)} total):'
        embed.add_field(value='\u200b', name=recent_games_str, inline=False)

        game_list = utilities.summarize_game_list(recent_games[:7])
        for game, result in game_list:
            embed.add_field(name=game, value=result)

        if ctx.guild.id != 447883341463814144:
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
                    member_stats.append((member.name, 0, '\u200b'))
                else:
                    wins, losses = p[0].get_record()
                    rank_str = f', #{p[0].leaderboard_rank(date_cutoff=settings.date_cutoff)[0]}' if p[0].leaderboard_rank(date_cutoff=settings.date_cutoff)[0] else ''
                    member_stats.append((f'{p[0].name}', p[0].elo, f'**({p[0].elo}{rank_str})**'))

            member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by ELO
            members_sorted = [f'{x[0]} {x[2]}' for x in member_stats]    # create list of strings like Nelluk(1000)
            members_str = " / ".join(members_sorted) if len(members_sorted) > 0 else '\u200b'
            embed.add_field(name=f'Members({len(member_stats)})', value=f'{members_str}')
        else:
            await ctx.send(f'Warning: No matching discord role "{team.name}" could be found. Player membership cannot be detected.')

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        embed.add_field(name='**Recent games**', value='\u200b', inline=False)

        recent_games = Game.search(team_filter=[team])

        game_list = utilities.summarize_game_list(recent_games[:10])

        for game, result in game_list:
            embed.add_field(name=game, value=result)

        await ctx.send(embed=embed)

    @commands.command(brief='Sets a Polytopia game code and registers user with the bot', usage='[user] polytopia_code')
    async def setcode(self, ctx, *args):
        """
        Sets your own Polytopia code, or allows a staff member to set a player's code. This also will register the player with the bot if not already.
        **Examples:**
        `[p]setcode somelongpolycode`
        `[p]setcode Nelluk somelongpolycode`
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
                return await ctx.send(f'Could not find any server member matching "{args[0]}". Try specifying with an @Mention')
            elif len(guild_matches) > 1:
                return await ctx.send(f'Found multiple server members matching "{args[0]}". Try specifying with an @Mention')
            target_discord_member = guild_matches[0]
            new_id = args[1]
        else:
            # Unexpected input
            await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setcode my_polytopia_code`')
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
            await ctx.send('Player {0.name} added to system with Polytopia code {0.discord_member.polytopia_id} and ELO {0.elo}'.format(player))
        else:
            await ctx.send('Player {0.name} updated in system with Polytopia code {0.discord_member.polytopia_id}.'.format(player))

    @commands.command(aliases=['code'], usage='player_name')
    async def getcode(self, ctx, player_string: str = None):
        """Get game code of a player
        Just returns the code and nothing else so it can easily be copied."""

        if not player_string:
            player_string = str(ctx.author.id)

        guild_matches = await utilities.get_guild_member(ctx, player_string)
        if len(guild_matches) == 0:
            return await ctx.send(f'Could not find any server member matching "{player_string}". Try specifying with an @Mention')
        elif len(guild_matches) > 1:
            return await ctx.send(f'Found multiple server members matching "{player_string}". Try specifying with an @Mention')
        target_discord_member = guild_matches[0]

        discord_member = DiscordMember.get_or_none(discord_id=target_discord_member.id)

        if discord_member and discord_member.polytopia_id:
            return await ctx.send(discord_member.polytopia_id)
        else:
            return await ctx.send(f'Member **{target_discord_member.name}** has no code on file.')

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
        await ctx.send(f'Player {player_target.name} updated in system with Polytopia name {new_name}.')

    @settings.in_bot_channel()
    @commands.command(aliases=['games'], usage='game_id')
    async def game(self, ctx, *args):

        """See a game's details, or list all games for various player/team combinations
        **Examples**:
        `[p]game 51` - See details on game # 51.
        `[p]games Jets`
        `[p]games Jets Ronin`
        `[p]games Nelluk`
        `[p]games Nelluk rickdaheals frodakcin Jets Ronin` - See games in which three players and two teams were all involved
        """

        # TODO: remove 'and/&' to remove confusion over game names like Ocean & Prophesy
        # TODO: would be nice if $games S3W4 Ronin worked - extract team/players first then use whatevers left as a title match?

        arg_list = [arg.upper() for arg in args]

        try:
            game_id = int(''.join(arg_list))
            game = Game.load_full_game(game_id=game_id)     # Argument is an int, so show game by ID
        except ValueError:
            pass
        except peewee.DoesNotExist:
            return await ctx.send(f'Game with ID {game_id} cannot be found.')
        else:
            if game.guild_id != ctx.guild.id:
                return await ctx.send(f'Game with ID {game_id} cannot be found on this server.')
            embed, content = game.embed(ctx)
            return await ctx.send(embed=embed, content=content)

        target_list = list(args)

        if len(args) == 1 and args[0].upper() == 'ALL':
            query = Game.search(status_filter=1, guild_id=ctx.guild.id)
            list_name = f'All games ({len(query)})'
            game_list = utilities.summarize_game_list(query[:500])
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

            print(player_matches, team_matches, remaining_args)
            query = Game.search(player_filter=player_matches, team_filter=team_matches, title_filter=remaining_args, guild_id=ctx.guild.id)
            print(f'len of player/team query: {len(query)}')
            game_list = utilities.summarize_game_list(query[:500])
            list_name = f'{len(query)} game{"s" if len(query) != 1 else ""}\n{results_str}'

        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help games` for usage examples. Searched for:\n{results_str}')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=15, page_size=15)

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

            query = Game.search(status_filter=1, player_filter=player_matches, team_filter=team_matches)

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

            query = Game.search(status_filter=2, player_filter=player_matches, team_filter=team_matches)

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

    @commands.command()
    async def wins(self, ctx, *args):
        """List incomplete games for you or other players
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

        query = Game.search(status_filter=3, player_filter=player_matches, team_filter=team_matches)

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

    @commands.command(aliases=['loss', 'lose'])
    async def losses(self, ctx, *args):
        """List incomplete games for you or other players
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

        query = Game.search(status_filter=4, player_filter=player_matches, team_filter=team_matches)

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

    @commands.command(aliases=['newgame'], usage='"Name of Game" player1 player2 vs player3 player4')
    async def startgame(self, ctx, game_name: str = None, *args):
        """Adds a new game to the bot for tracking

        **Examples:**
        `[p]startgame "Name of Game" nelluk vs koric` - Sets up a 1v1 game
        `[p]startgame "Name of Game" koric` - Sets up a 1v1 game versus yourself and koric (shortcut)
        `[p]startgame "Name of Game" nelluk frodakcin vs bakalol ben` - Sets up a 2v2 game
        """
        example_usage = (f'Example usage:\n`{ctx.prefix}startgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game\n'
                         f'`{ctx.prefix}startgame "Name of Game" player2` - Start a 1v1 with yourself and player2')

        waitlist = [f'M{m.id}' for m in Match.waiting_to_start(guild_id=ctx.guild.id, host_discord_id=ctx.author.id)]

        if waitlist and not settings.is_staff(ctx):
            return await ctx.send(f'You have matches waiting to be started as games: **{", ".join(waitlist)}**\n'
                f'Type `{ctx.prefix}match M#` for more details or if the game is created start it with `{ctx.prefix}startmatch M# Name of Game`.\n'
                f'The `{ctx.prefix}{ctx.invoked_with}` command is for creating a game that had no matchmaking session.')

        if not game_name:
            return await ctx.send(f'Invalid format. {example_usage}')
        if not args:
            return await ctx.send(f'Invalid format. {example_usage}')
        if game_name.upper()[:1] == 'M' and str.isdigit(game_name[1:]):
            return await ctx.send(f'It looks like you\'re trying to start a full matchmaking session. You probably want `{ctx.prefix}startmatch {game_name} "Name of Game"`')

        if len(game_name.split(' ')) < 2 and ctx.author.id != settings.owner_id:
            return await ctx.send(f'Invalid game name. Make sure to use "quotation marks" around the full game name.\n{example_usage}')

        if len(args) == 1:
            args_list = [str(ctx.author.id), 'vs', args[0]]
        else:
            args_list = list(args)

        player_groups = [list(group) for k, group in groupby(args_list, lambda x: x.lower() in ('vs', 'versus')) if not k]
        # split ['foo', 'bar', 'vs', 'baz', 'bat'] into [['foo', 'bar']['baz', 'bat']]

        biggest_team = max(len(group) for group in player_groups)
        total_players = sum(len(group) for group in player_groups)

        if len(player_groups) < 2:
            return await ctx.send(f'Invalid format. {example_usage}')
        if total_players > 2 and (not settings.is_power_user(ctx)) and ctx.guild.id != settings.server_ids['polychampions']:
            return await ctx.send('You only have permissions to create 1v1 games. More active server members can create larger games.')

        if total_players > 12:
            return await ctx.send(f'You cannot have more than twelve players.')
        if biggest_team > settings.guild_setting(ctx.guild.id, 'max_team_size'):
            return await ctx.send(f'This server has a maximum team size of {settings.guild_setting(ctx.guild.id, "max_team_size")}. For full functionality with support for up to 5-player team games and league play check out PolyChampions.')

        discord_groups, discord_players_flat = [], []
        author_found = False
        for group in player_groups:
            # Convert each arg into a Discord Guild Member and build a new list of lists. Or return if any arg can't be matched.
            discord_group = []
            for p in group:
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')

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

            await post_newgame_messaging(ctx, game=newgame)

    @commands.command(aliases=['endgame', 'wingame', 'winner'], usage='game_id winner_name')
    async def win(self, ctx, winning_game: PolyGame, winning_side_name: str):
        """
        Declare winner of an existing game

        The win must be confirmed by a member of the losing side (or staff) if the game has two sides.
        If the game has more than two sides, staff will need to confirm the win.
        Use player name for 1v1 games, otherwise use team names *(Home/Away/Owls/Sharks/etc)*

        To request staff help with confirmation use `[p]staffhelp`
        **Example:**
        `[p]win 5 Ronin` - Declare Ronin winner of game 5
        `[p]win 5 Nelluk` - Declare Nelluk winner of game 5
        """

        try:
            winning_obj, winning_side = winning_game.squadgame_by_name(ctx, name=winning_side_name)
            # winning_obj will be a Team or a Player depending on squad size
            # winning_side will be their SquadGame
        except exceptions.MyBaseException as ex:
            return await ctx.send(f'{ex}')

        if winning_game.is_completed is True:
            if winning_game.is_confirmed is True:
                return await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winner **{winning_game.winner.name()}**')
            elif winning_game.winner != winning_side:
                await ctx.send(f'Warning: Unconfirmed game with ID {winning_game.id} had previously been marked with winner **{winning_game.winner.name()}**')

        if settings.is_staff(ctx):
            confirm_win = True
        else:
            has_player, author_side = winning_game.has_player(discord_id=ctx.author.id)

            if not has_player:
                return await ctx.send(f'You were not a participant in this game.')

            if len(winning_game.squads) == 2:
                if winning_side == author_side:
                    # Author declaring their side won
                    await ctx.send(f'Game {winning_game.id} concluded pending confirmation of winner **{winning_obj.name}**\n'
                        f'To confirm, have a losing opponent use the same `{ctx.prefix}wingame` command, or ask server staff to confirm with `{ctx.prefix}staffhelp`')
                    confirm_win = False
                else:
                    # Author declaring their side lost
                    await ctx.send(f'Detected confirmation from losing side. Good game!')
                    confirm_win = True
            else:
                # Game with more than two teams - staff confirmation required. Possibly improve later so that every team can unanimously confirm
                await ctx.send(f'Since this is a {len(winning_game.squads)}-team game, staff confirmation is required.')
                confirm_win = False

                # Automatically inform staff of needed confirmation if game_request_channel is enabled
                if settings.guild_setting(ctx.guild.id, 'game_request_channel'):
                    channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_request_channel'))
                    try:
                        await channel.send(f'{ctx.message.author} submitted game winner: Game {winning_game.id} - Winner: **{winning_obj.name}**'
                            f'\nUse `{ctx.prefix}confirm {winning_game.id}` to confirm win.'
                            f'\nUse `{ctx.prefix}confirm` to list all games awaiting confirmation.')
                    except discord.errors.DiscordException:
                        logger.warn(f'Could not send message to game_request_channel: {settings.guild_setting(ctx.guild.id, "game_request_channel")}')
                        await ctx.send(f'Use `{ctx.prefix}staffhelp` to request staff confirm the win.')
                    else:
                        await ctx.send(f'Staff has automatically been informed of this win and confirming is pending.')

        winning_game.declare_winner(winning_side=winning_side, confirm=confirm_win)

        if confirm_win:
            # Cleanup game channels and announce winners
            await post_win_messaging(ctx, winning_game)


async def post_win_messaging(ctx, winning_game):

    await winning_game.delete_squad_channels(ctx=ctx)
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

    if settings.guild_setting(ctx.guild.id, 'game_channel_category') is not None:
            await game.create_squad_channels(ctx)

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
            return
    await ctx.send(f'New game ID {game.id} started! Roster: {" ".join(mentions_list)}')
    await ctx.send(embed=embed, content=content)


def parse_players_and_teams(input_list, guild_id: int):
    # Given a [List, of, string, args], try to match each one against a Team or a Player, and return lists of those matches
    # return any args that matched nothing back in edited input_list

    player_matches, team_matches = [], []
    for arg in list(input_list):  # Copy of list
        teams = Team.get_by_name(arg, guild_id)
        if len(teams) == 1:
            team_matches.append(teams[0])
            input_list.remove(arg)
        else:
            players = Player.string_matches(arg, guild_id)
            if len(players) == 1:
                player_matches.append(players[0])
                input_list.remove(arg)

    print(input_list)
    return player_matches, team_matches, input_list


def setup(bot):
    bot.add_cog(games(bot))
