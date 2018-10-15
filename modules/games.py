import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import peewee
from modules.models import Game, db, Player, Team, DiscordMember, Squad, TribeFlair, Lineup, SquadGame
import logging

logger = logging.getLogger('polybot.' + __name__)


class games():

    def __init__(self, bot):
        self.bot = bot

    def poly_game(game_id):
        # Give game ID integer return matching game or None. Can be used as a converter function for discord command input:
        # https://discordpy.readthedocs.io/en/rewrite/ext/commands/commands.html#basic-converters
        # all-related records are prefetched

        # This pre-fetches all related game records such as lineups and tribe choices. Works great if you are only reading or updating objects it loads
        # but this means if you want to, for example, udpate a specific Lineup associated with this game, you need to iterate through them rather than doing
        # another DB query. The Game object thiat this returns would not be updated to reflect the latter unless it was refreshed from the DB.

        try:
            game = Game.load_full_game(game_id=int(game_id))
            logger.debug(f'Game with ID {game_id} found.')
            return game
        except ValueError:
            logger.warn(f'Invalid game ID "{game_id}".')
            return None
        except peewee.DoesNotExist:
            logger.warn(f'Game with ID {game_id} cannot be found.')
            return None

    def poly_game_mini(game_id):
        # similar to poly_game except no related records are prefetched. Works better for functions like settribe where related records are updated and then displayed

        try:
            game = Game.get(id=int(game_id))
            logger.debug(f'Game with ID {game_id} found.')
            return game
        except ValueError:
            logger.warn(f'Invalid game ID "{game_id}".')
            return None
        except peewee.DoesNotExist:
            logger.warn(f'Game with ID {game_id} cannot be found.')
            return None

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

    @commands.command(aliases=['namegame'], usage='game_id "New Name"')
    # @commands.has_any_role(*helper_roles)
    async def gamename(self, ctx, game: poly_game, *args):
        """*Staff:* Renames an existing game
        **Example:**
        `[p]gamename 25 Mountains of Fire`
        """

        if game is None:
            return await ctx.send('No matching game was found.')

        new_game_name = ' '.join(args)
        with db:
            game.name = new_game_name.title()
            game.save()

        await game.update_squad_channels(ctx)
        await game.update_announcement(ctx)

        await ctx.send(f'Game ID {game.id} has been renamed to "{game.name}"')

    # @in_bot_channel()
    @commands.command()
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx):
        """ Display individual leaderboard"""

        leaderboard = []
        with db:
            leaderboard_query = Player.leaderboard(date_cutoff=settings.date_cutoff, guild_id=ctx.guild.id)
            for counter, player in enumerate(leaderboard_query[:500]):
                wins, losses = player.get_record()
                emoji_str = player.team.emoji if player.team else ''
                leaderboard.append(
                    (f'`{(counter + 1):>3}.` {emoji_str}`{player.name}`', f'`(ELO: {player.elo:4}) W {wins} / L {losses}`')
                )

        await utilities.paginate(self.bot, ctx, title='**Individual Leaderboards**', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    # @in_bot_channel()
    @commands.command(aliases=['teamlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbteam(self, ctx):
        """display team leaderboard"""
        # TODO: Only show number of members who have an ELO ranking?
        embed = discord.Embed(title='**Team Leaderboard**')
        with db:
            query = Team.select().where(
                ((Team.name != 'Home') & (Team.name != 'Away') & (Team.guild_id == ctx.guild.id))
            ).order_by(-Team.elo)
            for counter, team in enumerate(query):
                team_role = discord.utils.get(ctx.guild.roles, name=team.name)
                team_name_str = f'{team.name}({len(team_role.members)})'  # Show team name with number of members
                wins, losses = team.get_record()

                embed.add_field(name=f'`{(counter + 1):>3}. {team_name_str:30}  (ELO: {team.elo:4})  W {wins} / L {losses}` {team.emoji}', value='\u200b', inline=False)
        await ctx.send(embed=embed)

    # @in_bot_channel()
    @commands.command(aliases=['squadlb'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbsquad(self, ctx):
        """Display squad leaderboard"""

        leaderboard = []
        with db:
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

    # @in_bot_channel()
    @commands.command(brief='Find squads or see details on a squad', usage='player1 [player2] [player3]', aliases=['squads'])
    async def squad(self, ctx, *args):
        """Find squads with specific players, or see details on a squad
        **Examples:**
        `[p]squad 5` - details on squad 5
        `[p]squad Nelluk` - squads containing Nelluk
        `[p]squad Nelluk frodakcin` - squad containing both players
        """
        with db:
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
                    p_matches = Player.get_by_string(p_name, guild_id=ctx.guild.id)
                    if len(p_matches) == 1:
                        squad_players.append(p_matches[0])
                    elif len(p_matches) > 1:
                        return await ctx.send(f'Found multiple matches for player "{p_name}". Try being more specific or quoting players "Full Name".')
                    else:
                        return await ctx.send(f'Found no matches for player "{p_name}".')

                squad_list = Squad.get_all_matching_squads(squad_players)
                if len(squad_list) == 0:
                    return await ctx.send(f'Found no squads containing players: {" / ".join(args)}')
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

        with db:
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
            recent_games = SquadGame.select().join(Game).where(
                (SquadGame.squad == squad) & (Game.is_pending == 0)
            ).order_by(-Game.date)[:5]
            embed.add_field(value='\u200b', name='Most recent games', inline=False)

            for squadgame in recent_games:
                game = Game.load_full_game(game_id=squadgame.game)  # preloads game data to reduce DB queries.
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    status = '**WIN**' if squadgame.id == Game.winner else '***Loss***'

                embed.add_field(name=f'{game.get_headline()}',
                            value=f'{status} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

            await ctx.send(embed=embed)

    # @in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo'])
    async def player(self, ctx, *args):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.
        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """

        with db:
            if len(args) == 0:
                # Player looking for info on themselves
                player = Player.get_by_string(player_string=f'<@{ctx.author.id}>', guild_id=ctx.guild.id)
                if len(player) != 1:
                    return await ctx.send(f'Could not find you in the database. Try setting your code with {ctx.prefix}setcode')
                player = player[0]
            else:
                # Otherwise look for a player matching whatever they entered
                player_mention = ' '.join(args)
                matching_players = Player.get_by_string(player_string=player_mention, guild_id=ctx.guild.id)
                if len(matching_players) == 1:
                    player = matching_players[0]
                elif len(matching_players) == 0:
                    # No matching name in database. Fall back to searching on polytopia_id or polytopia_name. Warn if player is found in guild.
                    matches = await utilities.get_guild_member(ctx, player_mention)
                    if len(matches) > 0:
                        await ctx.send(f'"{player_mention}" was found in the server but is not registered with me. '
                            f'Players can be registered with `{ctx.prefix}setcode` or being in a new game\'s lineup.')

                    return await ctx.send(f'Could not find \"{player_mention}\" by Discord name, Polytopia name, or Polytopia ID.')

                else:
                    return await ctx.send('There is more than one player found with that name. Specify user with @Mention.'.format(player_mention))

        with db:
            wins, losses = player.get_record()
            rank, lb_length = player.leaderboard_rank(settings.date_cutoff)

            if rank is None:
                rank_str = 'Unranked'
            else:
                rank_str = f'{rank} of {lb_length}'

            embed = discord.Embed(title=f'Player card for {player.name}')
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

            embed.add_field(value='\u200b', name='Most recent games', inline=False)

            recent_games = SquadGame.select(SquadGame, Game).join(Game).join_from(SquadGame, Lineup).where(
                (Lineup.player == player) & (Game.is_pending == 0)
            ).order_by(-Game.date)[:7]

            for squadgame in recent_games:
                game = Game.load_full_game(game_id=squadgame.game)  # preloads game data to reduce DB queries.
                if game.is_completed == 0:
                    status = 'Incomplete'
                else:
                    status = '**WIN**' if squadgame.id == Game.winner else '***Loss***'

                embed.add_field(name=f'{game.get_headline()}',
                            value=f'{status} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

            await ctx.send(content=content_str, embed=embed)

    # @in_bot_channel()
    @commands.command(usage='team_name')
    async def team(self, ctx, team_string: str):
        """See details on a team
        **Example:**
        [p]team Ronin
        """

        matching_teams = Team.get_by_name(team_string, ctx.guild.id)
        if len(matching_teams) > 1:
            return await ctx.send('More than one matching team found. Be more specific or trying using a quoted \"Team Name\"')
        if len(matching_teams) == 0:
            return await ctx.send(f'Cannot find a team with name "{team_string}". Be sure to use the full name, surrounded by quotes if it is more than one word.')
        team = matching_teams[0]

        embed = discord.Embed(title=f'Team card for **{team.name}** {team.emoji}')
        team_role = discord.utils.get(ctx.guild.roles, name=team.name)
        member_stats = []

        wins, losses = team.get_record()
        embed.add_field(name='Results', value=f'ELO: {team.elo}   Wins {wins} / Losses {losses}')

        if team_role:
            for member in team_role.members:
                # Create a list of members - pull ELO score from database if they are registered, or with 0 ELO if they are not
                p = Player.get_by_string(player_string=str(member.id), guild_id=ctx.guild.id)
                if len(p) == 0:
                    member_stats.append((member.name, 0, '\u200b'))
                else:
                    wins, losses = p[0].get_record()
                    member_stats.append((f'{p[0].name}', p[0].elo, f'*({p[0].elo}, #{p[0].leaderboard_rank(date_cutoff=settings.date_cutoff)[0]})*'))

            member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by ELO
            members_sorted = [f'{x[0]} {x[2]}' for x in member_stats]    # create list of strings like Nelluk(1000)
            members_str = " / ".join(members_sorted) if len(members_sorted) > 0 else '\u200b'
            embed.add_field(name=f'Members({len(member_stats)})', value=f'{members_str}')
        else:
            await ctx.send(f'Warning: No matching discord role "{team.name}" could be found. Player membership cannot be detected.')

        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

        embed.add_field(value='*Recent games*', name='\u200b', inline=False)

        recent_games = SquadGame.select(SquadGame, Game).join(Game).where(
            (Game.id.in_(Team.team_games_subq())) & (SquadGame.team == team)
        ).order_by(-Game.date)[:7]

        for squadgame in recent_games:
            game = Game.load_full_game(game_id=squadgame.game)
            if game.is_completed == 1:
                result = '**WIN**' if squadgame.id == Game.winner else 'LOSS'
            else:
                result = 'Incomplete'

            embed.add_field(name=f'{game.get_headline()}',
                value=f'{result} - {str(game.date)} - {game.team_size()}v{game.team_size()}')

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

            if settings.is_staff(ctx, ctx.author) is False:
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
        print(team_list)

        with db:
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
    async def getcode(self, ctx, player_string: str):
        """Get game code of a player
        Just returns the code and nothing else so it can easily be copied."""

        # TODO: If no player argument display own code?
        with db:
            player_matches = Player.get_by_string(player_string, ctx.guild.id)
            if len(player_matches) == 0:
                return await ctx.send('Cannot find player with that name. Correct usage: `{}getcode @Player`'.format(ctx.prefix))
            if len(player_matches) > 1:
                return await ctx.send('More than one matching player found. Use @player to specify. Correct usage: `{}getcode @Player`'.format(ctx.prefix))
            player_target = player_matches[0]
            if player_target.discord_member.polytopia_id:
                await ctx.send(player_target.discord_member.polytopia_id)
            else:
                await ctx.send('User was found but does not have a Polytopia ID on file.')

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
            if settings.is_staff(ctx, ctx.author) is False:
                return await ctx.send('You do not have permission to trigger this command.')
            target_string = args[0]
            new_name = args[1]
        else:
            # Unexpected input
            return await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}setname my_polytopia_name`. Use "quotation marks" if the name is more than one word.')

        target_player = Player.get_by_string(target_string, ctx.guild.id)
        if len(target_player) == 0:
            return await ctx.send(f'Could not match any players to query "{target_string}". Try registering with {ctx.prefix}setcode first.')
        elif len(target_player) > 1:
            return await ctx.send(f'Multiple players found with query "{target_string}". Be more specfic or use an @Mention.')

        with db:
            target_player[0].discord_member.polytopia_name = new_name
            target_player[0].discord_member.save()
            await ctx.send(f'Player {target_player[0].name} updated in system with Polytopia name {new_name}.')

    # @in_bot_channel()
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

        arg_list = [arg.upper() for arg in args]

        try:
            game_id = int(''.join(arg_list))
            game = Game.load_full_game(game_id=game_id)     # Argument is an int, so show game by ID
            embed = game.embed(ctx)
            return await ctx.send(embed=embed)
        except ValueError:
            pass
        except peewee.DoesNotExist:
            return await ctx.send('Game with ID {} cannot be found.'.format(game_id))

        target_list = list(args)

        if len(args) == 1 and args[0].upper() == 'ALL':
            query = Game.search(status_filter=1, guild_id=ctx.guild.id)
            list_name = f'All games ({len(query)})'
        else:
            if not target_list:
                # Target is person issuing command
                target_list.append(str(ctx.author.id))

        query = Game.select().where(Game.name.contains('%'.join(arg_list)))

        if len(query) > 0:
            if len(query) == 1:
                # if only one matching game is found, display its embed
                return await ctx.send(embed=query[0].embed(ctx))
            else:
                list_name = f'Games with titles matching *"{" ".join(arg_list)}"*'
        else:
            player_matches, team_matches = parse_players_and_teams(target_list, ctx.guild.id)
            p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

            query = Game.search(player_filter=player_matches, team_filter=team_matches)

            list_name = f'{len(query)} game{"s" if len(query) != 1 else ""} '
            if len(p_names) > 0:
                list_name += f'that include *{"* & *".join(p_names)}*'
                if len(t_names) > 0:
                    list_name += f'\nIncluding teams: *{"* & *".join(t_names)}*'
            elif len(t_names) > 0:
                list_name += f'that include Team *{"* & *".join(t_names)}*'

        game_list = utilities.summarize_game_list(query[:500])
        if len(game_list) == 0:
            await ctx.send(f'No results. See `{ctx.prefix}help games` for usage examples.')
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

            player_matches, team_matches = parse_players_and_teams(target_list, ctx.guild.id)
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
            await ctx.send(f'No results. See `{ctx.prefix}help complete` for usage examples.')
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

            player_matches, team_matches = parse_players_and_teams(target_list, ctx.guild.id)
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
            await ctx.send(f'No results. See `{ctx.prefix}help incomplete` for usage examples.')
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

        player_matches, team_matches = parse_players_and_teams(target_list, ctx.guild.id)
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
            await ctx.send(f'No results. See `{ctx.prefix}help wins` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @commands.command(aliases=['loss', 'lose'])
    async def losses(self, ctx, *args):
        """List incomplete games for you or other players
        If any players names are listed, the first played is who the loss is checked against. If no players listed, then the first team listed is checked for the loss.
        **Example:**
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

        player_matches, team_matches = parse_players_and_teams(target_list, ctx.guild.id)
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
            await ctx.send(f'No results. See `{ctx.prefix}help losses` for usage examples.')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=10, page_size=10)

    @commands.command(aliases=['newgame'], brief='Helpers: Sets up a new game to be tracked', usage='"Name of Game" player1 player2 vs player3 player4')
    # @commands.has_any_role(*helper_roles)
    # TODO: command should require 'Rider' role on main server. 2v2 should require above that
    async def startgame(self, ctx, game_name: str, *args):
        side_home, side_away = [], []
        example_usage = (f'Example usage:\n`{ctx.prefix}startgame "Name of Game" player2`- Starts a 1v1 game between yourself and player2'
            f'\n`{ctx.prefix}startgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game')

        if len(args) == 1:
            # Shortcut version for 1v1s:
            # $startgame "Name of Game" opponent_name
            guild_matches = await utilities.get_guild_member(ctx, args[0])
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not match "{args[0]}" to a server member. Try using an @Mention.')
            if len(guild_matches) > 1:
                return await ctx.send(f'More than one server matches found for "{args[0]}". Try being more specific or using an @Mention.')
            if guild_matches[0] == ctx.author:
                return await ctx.send(f'Stop playing with yourself!')
            side_away.append(guild_matches[0])
            side_home.append(ctx.author)

        elif len(args) > 1:
            # $startgame "Name of Game" p1 p2 vs p3 p4
            if settings.guild_setting(ctx.guild.id, 'allow_teams') is False:
                return await ctx.send('Only 1v1 games are enabled on this server. For team ELO games with squad leaderboards check out PolyChampions.')
            if len(args) not in [3, 5, 7, 9, 11] or args[int(len(args) / 2)].upper() != 'VS':
                return await ctx.send(f'Invalid format. {example_usage}')

            for p in args[:int(len(args) / 2)]:         # Args in first half before 'VS', converted to Discord Members
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                side_home.append(guild_matches[0])

            for p in args[int(len(args) / 2) + 1:]:     # Args in second half after 'VS'
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(f'Could not match "{p}" to a server member. Try using an @Mention.')
                if len(guild_matches) > 1:
                    return await ctx.send(f'More than one server matches found for "{p}". Try being more specific or using an @Mention.')
                side_away.append(guild_matches[0])

            if len(side_home) > settings.guild_setting(ctx.guild.id, 'max_team_size') or len(side_home) > settings.guild_setting(ctx.guild.id, 'max_team_size'):
                return await ctx.send('Maximium {0}v{0} games are enabled on this server. For full functionality with support for up to 5v5 games and league play check out PolyChampions.'.format(settings.guild_setting(ctx.guild.id, 'max_team_size')))

        else:
            return await ctx.send(f'Invalid format. {example_usage}')

        if len(side_home + side_away) > len(set(side_home + side_away)):
            if settings.guild_setting(ctx.guild.id, 'allow_uneven_teams'):
                await ctx.send('Duplicate players detected. Are you sure this is what you want? (That means the two sides are uneven.)')
            else:
                return await ctx.send('Duplicate players detected. Game not created.')

        if ctx.author not in (side_home + side_away) and settings.is_staff(ctx, ctx.author) is False:
            return await ctx.send('You can\'t create a game that you are not a participant in.')

        logger.debug(f'All input checks passed. Creating new game records with args: {args}')

        newgame = Game.create_game([side_home, side_away],
            name=game_name, guild_id=ctx.guild.id,
            require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))

        await newgame.create_squad_channels(ctx)

        mentions = [p.mention for p in side_home + side_away]
        embed = newgame.embed(ctx)

        if settings.guild_setting(ctx.guild.id, 'game_announce_channel') is not None:
            channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
            if channel is not None:
                await channel.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')
                announcement = await channel.send(embed=embed)
                await ctx.send(f'New game ID {newgame.id} started! See {channel.mention} for full details.')
                newgame.announcement_message = announcement.id
                newgame.announcement_channel = announcement.channel.id
                newgame.save()
                return
        await ctx.send(f'New game ID {newgame.id} started! Roster: {" ".join(mentions)}')
        await ctx.send(embed=embed)

    @commands.command(aliases=['endgame', 'win'], usage='game_id winner_name')
    # @commands.has_any_role(*helper_roles)
    # TODO: output/announcements
    async def wingame(self, ctx, winning_game: poly_game, winning_side_name: str):
        if winning_game is None:
            return await ctx.send(f'No matching game was found.')

        if winning_game.is_completed is True:
            logger.debug('here is_completed')
            if winning_game.is_confirmed is True:
                logger.debug('here is_confirmed')
                return await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winner **{winning_game.get_winner().name}**')
            else:
                await ctx.send(f'Warning: Unconfirmed game with ID {winning_game.id} had previously been marked with winner **{winning_game.get_winner().name}**')

        if settings.is_staff(ctx, ctx.author):
            is_staff = True
        else:
            is_staff = False

            try:
                _, _ = winning_game.return_participant(ctx, player=ctx.author.id)
            except exceptions.CheckFailedError:
                return await ctx.send(f'You were not a participant in game {winning_game.id}, and do not have staff privileges.')

        try:
            if winning_game.team_size() == 1:
                winning_obj, winning_side = winning_game.return_participant(ctx, player=winning_side_name)

            elif winning_game.team_size() > 1:
                winning_obj, winning_side = winning_game.return_participant(ctx, team=winning_side_name)
            else:
                return logger.error('Invalid team_size. Aborting wingame command.')
        except exceptions.CheckFailedError as ex:
            return await ctx.send(f'{ex}')

        winning_game.declare_winner(winning_side=winning_side, confirm=is_staff)
        if is_staff:
            # Only delete channels if win confirmation is final/confirmed
            await winning_game.delete_squad_channels(ctx=ctx)
            player_mentions = [f'<@{p.discord_member.discord_id}>' for p, _, _ in (winning_game.squads[0].roster() + winning_game.squads[1].roster())]
            embed = winning_game.embed(ctx)

            if settings.guild_setting(ctx.guild.id, 'game_announce_channel') is not None:
                channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
                if channel is not None:
                    await channel.send(f'Game concluded! Congrats **{winning_game.get_winner().name}**. Roster: {" ".join(player_mentions)}')
                    await channel.send(embed=embed)
                    return await ctx.send(f'Game concluded! See {channel.mention} for full details.')

            await ctx.send(f'Game concluded! Congrats **{winning_game.get_winner().name}**. Roster: {" ".join(player_mentions)}')
            await ctx.send(embed=embed)

    @commands.command(usage='game_id')
    # @commands.has_any_role(*mod_roles)
    async def deletegame(self, ctx, game: poly_game):
        """Mod: Deletes a game and reverts ELO changes"""

        if game is None:
            return await ctx.send('No matching game was found.')

        if game.announcement_message:
            game.name = f'~~{game.name}~~ GAME DELETED'
            await game.update_announcement(ctx)

        await game.delete_squad_channels(ctx)

        with db:
            gid = game.id
            game.delete_game()
            await ctx.send(f'Game with ID {gid} has been deleted and team/player ELO changes have been reverted, if applicable.')

    @commands.command(usage='game_id player_name tribe_name [player2 tribe2 ... ]')
    # @commands.has_any_role(*helper_roles)
    async def settribe(self, ctx, game: poly_game_mini, *args):
        """*Staff:* Set tribe of a player for a game
        **Examples**
        `[p]settribe 5 nelluk bardur` - Sets Nelluk to Bardur for game 5
        `[p]settribe 5 nelluk bardur rickdaheals kickoo` - Sets both player tribes in one command
        """

        if game is None:
            return await ctx.send(f'No matching game was found.')

        if len(args) % 2 != 0:
            return await ctx.send(f'Wrong number of arguments. See `{ctx.prefix}help settribe` for usage examples.')

        lineups = Lineup.select(Lineup, Player).join(Player).where(Lineup.game == game)

        with db:
            for i in range(0, len(args), 2):
                # iterate over args two at a time
                player_name = args[i]
                tribe_name = args[i + 1]

                tribeflair = TribeFlair.get_by_name(name=tribe_name, guild_id=ctx.guild.id)
                if not tribeflair:
                    await ctx.send(f'Matching Tribe not found matching "{tribe_name}". Check spelling or be more specific.')
                    continue

                lineup_match = None
                for lineup in lineups:
                    if player_name.upper() in lineup.player.name.upper():
                        lineup_match = lineup
                        break

                if not lineup_match:
                    await ctx.send(f'Matching player not found in game {game.id} matching "{player_name}". Check spelling or be more specific. @Mentions are not supported here.')
                    continue

                with db.atomic():
                    lineup_match.tribe = tribeflair
                    lineup_match.save()
                    await ctx.send(f'Player {lineup_match.player.name} assigned to tribe {tribeflair.tribe.name} in game {game.id} {tribeflair.emoji}')

        game = game.load_full_game()
        await game.update_announcement(ctx)

    @commands.command(usage='tribe_name new_emoji')
    # @commands.has_any_role(*mod_roles)
    async def tribe_emoji(self, ctx, tribe_name: str, emoji):
        """Mod: Assign an emoji to a tribe
        **Example:**
        `[p]tribe_emoji Bardur :new_bardur_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}tribe_emoji Tribename :my_custom_emoji:`'.format(ctx.prefix))

        try:
            tribeflair = TribeFlair.upsert(name=tribe_name, guild_id=ctx.guild.id, emoji=emoji)
        except exceptions.CheckFailedError as e:
            return await ctx.send(e)

        await ctx.send('Tribe {0.tribe.name} updated with new emoji: {0.emoji}'.format(tribeflair))

    @commands.command(aliases=['addteam'], usage='new_team_name')
    # @commands.has_any_role(*mod_roles)
    async def team_add(self, ctx, *args):
        """Mod: Create new server Team
        The team should have a Role with an identical name.
        **Example:**
        `[p]team_add The Amazeballs`
        """

        name = ' '.join(args)
        try:
            with db.atomic():
                team = Team.create(name=name, guild_id=ctx.guild.id)
        except peewee.IntegrityError:
            return await ctx.send('That team already exists!')

        await ctx.send(f'Team {name} created! Starting ELO: {team.elo}. Players with a Discord Role exactly matching \"{name}\" will be considered team members. '
                f'You can now set the team flair with `{ctx.prefix}`team_emoji and `{ctx.prefix}team_image`.')

    @commands.command(usage='team_name new_emoji')
    # @commands.has_any_role(*mod_roles)
    async def team_emoji(self, ctx, team_name: str, emoji):
        """Mod: Assign an emoji to a team
        **Example:**
        `[p]team_emoji Amazeballs :my_fancy_emoji:`
        """

        if len(emoji) != 1 and ('<:' not in emoji):
            return await ctx.send('Valid emoji not detected. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

        with db:
            matching_teams = Team.get_by_name(team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

            team = matching_teams[0]
            team.emoji = emoji
            team.save()

            await ctx.send('Team {0.name} updated with new emoji: {0.emoji}'.format(team))

    @commands.command(usage='team_name image_url')
    # @commands.has_any_role(*mod_roles)
    async def team_image(self, ctx, team_name: str, image_url):
        """Mod: Set a team's logo image

        **Example:**
        `[p]team_image Amazeballs http://www.path.to/image.png`
        """

        if 'http' not in image_url:
            return await ctx.send(f'Valid image url not detected. Example usage: `{ctx.prefix}team_image name http://url_to_image.png`')
            # This is a very dumb check to make sure user is passing a URL and not a random string. Assumes mod can figure it out from there.

        with db:
            matching_teams = Team.get_by_name(team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_emoji name :my_custom_emoji:`'.format(ctx.prefix))

            team = matching_teams[0]
            team.image_url = image_url
            team.save()

            await ctx.send(f'Team {team.name} updated with new image_url (image should appear below)')
            await ctx.send(team.image_url)

    @commands.command(usage='old_name new_name')
    # @commands.has_any_role(*mod_roles)
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):
        """Mod: Change a team's name
        The team should have a Role with an identical name.
        Old name doesn't need to be precise, but new name does. Include quotes if it's more than one word.
        **Example:**
        `[p]team_name Amazeballs "The Wowbaggers"`
        """

        with db:
            matching_teams = Team.get_by_name(old_team_name, ctx.guild.id)
            if len(matching_teams) != 1:
                return await ctx.send('Can\'t find matching team or too many matches. Example: `{}team_name \"Current name\" \"New Team Name\"`'.format(ctx.prefix))

            team = matching_teams[0]
            team.name = new_team_name
            team.save()

            await ctx.send('Team **{}** has been renamed to **{}**.'.format(old_team_name, new_team_name))

    @commands.command()
    # @commands.has_any_role(*helper_roles)
    async def ts(self, ctx, game: int, *args):

        player = Player.get_or_except('nelluk', ctx.guild.id)
        q = Lineup.select(Lineup.tribe).where(
            (Lineup.player == player)
        ).group_by(Lineup.tribe)

        # team_a = Team.get(id=1)
        # player_list = []
        # players = Player.get_or_('nelluk', ctx.guild.id)
        # for p in players:
        #     player_list.append(p)
        # teams = [team_a]

        # games = Game.search(team_filter=[teams], player_filter=player_list, status_filter=6)
        # print(games.count())

        for g in q.dicts():
            print(g)


def parse_players_and_teams(input_list, guild_id: int):
    player_matches, team_matches = [], []
    for arg in input_list:
        teams = Team.get_by_name(arg, guild_id)
        if len(teams) == 1:
            team_matches.append(teams[0])
        else:
            players = Player.get_by_string(arg, guild_id)
            if len(players) == 1:
                player_matches.append(players[0])

    return player_matches, team_matches


def setup(bot):
    bot.add_cog(games(bot))
