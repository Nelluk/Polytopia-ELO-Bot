import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
import logging
import asyncio
import modules.exceptions as exceptions
import datetime
import random
import peewee
from modules import game_ping
from modules import game_ping_workers
from modules import role_leaderboard as role_leaderboard_service
from modules import role_leaderboard_workers
from modules import beta_feedback_views
from modules import beta_lab_sessions
from modules import beta_lab_workers
from modules import beta_readiness
from modules import beta_testing_guide
from modules import beta_testing_dashboard
from modules import staff_help
# import modules.imgen as imgen
# import modules.achievements as achievements

logger = logging.getLogger('polybot.' + __name__)


def role_lookup_server_check():
    def predicate(ctx):
        if ctx.guild.id == settings.server_ids['polychampions']:
            return True
        # elif ctx.guild.id == settings.server_ids['main'] and settings.is_staff(ctx.author):
        #     return True
        elif settings.is_staff(ctx.author):
            return True
        return False
    return commands.check(predicate)


class misc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_broadcast_newbie_message())
            self.bg_task3 = asyncio.create_task(self.task_broadcast_newbie_steam_message())

    @commands.command(hidden=True, aliases=['ts'])
    @commands.is_owner()
    async def test(self, ctx, *, args: str = None):

        nova_message = "- <:TIMEUP:707037861584699403> Don't just skip someone if they're timed out. We have rules for that. Read -https://discordapp.com/channels/447883341463814144/1129216509739270236/1129216680627814461"
        nova_message += "\n\n - :ring_buoy: Have a bad spawn? You get one bonus restart per game. Just be sure to ask before the end of your third turn"
        nova_message += "\n\n - ⌛ Don't have time to do your turn? Each side gets three 24 hour turn extensions. Ping to let your opponent know you are using it to protect yourself from getting skipped"
        nova_message += "\n\n - :help: Need more help with the bot? There's a YT tutorial :youtube_gif: in the pins in https://discord.com/channels/447883341463814144/448317497473630229 or you can do `$help` to see a full list of commands or `$tutorial` to see the basics"

        await ctx.send(nova_message)
    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def guide(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        bot_desc = ('This bot is designed to improve Polytopia multiplayer by filling in gaps in two areas: competitive leaderboards, and matchmaking.\n'
                    # 'Its primary home is [PolyChampions](https://discord.gg/YcvBheSv), a server focused on team play organized into a league.\n'
                    f'To register as a player with the bot use __`{ctx.prefix}setname Mobile User Name`__ or  __`{ctx.prefix}steamname Steam User Name`__')

        embed = discord.Embed(title='PolyELO Bot Donation Link', url='https://www.buymeacoffee.com/nelluk', description=bot_desc)

        embed.add_field(name='Matchmaking',
            value=f'This helps players organize and find games.\nFor example, use __`{ctx.prefix}opengame 1v1`__ to create an open 1v1 game that others can join.\n'
                f'To see a list of open games you can join use __`{ctx.prefix}opengames`__. Once the game is full the host would use __`{ctx.prefix}startgame`__ to close it and track it for the leaderboards.\n'
                f'See __`{ctx.prefix}help matchmaking`__ for all commands.', inline=False)

        embed.add_field(name='ELO Leaderboards',
            value='Win your games and climb the leaderboards! Earn sweet ELO points!\n'
                'ELO points are gained or lost based on your game results. You will gain more points if you defeat an opponent with a higher ELO.\n'
                f'Use __`{ctx.prefix}lb`__ to view the individual leaderboards. There is also a __`{ctx.prefix}lbsquad`__ squad leaderboard. Form a squad by playing with the same person in multiple games!'
                f'\nSee __`{ctx.prefix}help`__ for all commands.', inline=False)

        embed.add_field(name='Finishing tracked games',
            value=f'Use the __`{ctx.prefix}win`__ command to tell the bot that a game has concluded.\n'
            f'For example if Nelluk wins game 10150, he would type __`{ctx.prefix}win 10150 nelluk`__. The losing player can confirm using the same command. '
            'Games are auto-confirmed after 24 hours, or sooner if the losing side manually confirms.', inline=False)

        embed.set_thumbnail(url=self.bot.user.display_avatar.replace(size=512, format='webp'))
        embed.set_footer(text='Developer: Nelluk')
        await ctx.send(embed=embed)

    @commands.command(usage='map mode', aliases=['tp'])
    async def tribepoints(self, ctx, map: str = None, mode: str = None):
        """ Display the tribe points list

         **Examples**
        `[p]tribepoints archi 2v2`
        """
        if not mode:
            return await ctx.send(f'Map or mode not provided. *Example:* `{ctx.prefix}{ctx.invoked_with} archi 2v2`')

        guild = self.bot.get_guild(settings.server_ids['polychampions'])
        if guild:
            mode = mode.lower()
            if utilities.get_map_type(mode) and not utilities.get_map_type(map.lower()):
                map, mode = mode, map.lower()
            map = utilities.get_map_type(map)
            if not map:
                return await ctx.send(f'Invalid map passed. *Example:* `{ctx.prefix}{ctx.invoked_with} archi 2v2`')

            aliases = {'Archipelago': 'Archi', 'Dryland': 'Dry'}  
            map = aliases.get(map, map)

            try:
                # TODO: save the points list in the database
                channel = guild.get_channel(1326593750973153370)  # tribe-points   
                if mode == '2v2':
                    points_message = await channel.fetch_message(1326600885937377444)
                elif mode == '3v3':
                    points_message = await channel.fetch_message(1326600895525290134)
                else:
                    return await ctx.send(f'Invalid mode passed. *Example:* `{ctx.prefix}{ctx.invoked_with} archi 2v2`')
            except discord.NotFound:
                logger.warning(f'NotFound in tribepoints')
                return await ctx.send(f'*Warning!* Could not find message/channel')
            except discord.DiscordException as e:
                logger.warning(f'Exception in tribepoints')
                return await ctx.send(f'Error loading message/channel: {e}')

            points_message = points_message.content.split(f'{map} {mode}')
            if len(points_message) == 1:
                return await ctx.send(f'There is no tribe points list for {map}.')

            points_list = f'{map} {mode} Tribe Points:\n'

            for line in points_message[1].splitlines():
                line = line.strip()
                if line.endswith('>'):
                    points_list += f'{line}\n'
                elif len(line) != 0:
                    break
                
            await ctx.send(points_list)

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def credits(self, ctx):
        """
        Display development credits
        """
        embed = discord.Embed(title='Support this project', url='https://www.buymeacoffee.com/nelluk')

        embed.add_field(name='Developer', value='Nelluk')
        embed.add_field(name='Source code', value='https://github.com/Nelluk/Polytopia-ELO-Bot')

        embed.add_field(name='Contributions', value='rickdaheals, koric, Gerenuk, alphaSeahorse, Octo, Artemis, Legorooj,  theoldlove', inline=False)

        embed.set_thumbnail(url=self.bot.user.display_avatar.replace(size=512, format='webp'))
        await ctx.send(embed=embed)

    @commands.command()
    @settings.in_bot_channel_strict()
    async def stats(self, ctx):
        """ Display statistics on games logged with this bot """

        embed = discord.Embed(title='PolyELO Statistics')
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))
        last_quarter = (datetime.datetime.now() + datetime.timedelta(days=-90))
        last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))

        games_played = models.Game.select().where(models.Game.is_completed == 1)
        games_played_90d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_quarter))
        games_played_30d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_month))
        games_played_7d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_week))

        incomplete_games = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.is_completed == 0))

        participants_90d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_quarter)
        ).group_by(models.Lineup.player.discord_member).distinct()

        participants_30d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_month)
        ).group_by(models.Lineup.player.discord_member).distinct()

        participants_7d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_week)
        ).group_by(models.Lineup.player.discord_member).distinct()

        embed.add_field(value='\u200b', name=f'`{"----------------------------------":<35}` Global (Local)', inline=False)

        stats_0 = (f'`{"Total games completed:":<35}\u200b` {games_played.count()} ({games_played.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Incomplete games:":<35}\u200b` {incomplete_games.count()} ({incomplete_games.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_0[:256], inline=False)
        stats_1 = (f'`{"Games created in last 90 days:":<35}\u200b`\u200b {games_played_90d.count()} ({games_played_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 30 days:":<35}\u200b`\u200b {games_played_30d.count()} ({games_played_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 7 days:":<35}\u200b`\u200b {games_played_7d.count()} ({games_played_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   )
        embed.add_field(value='\u200b', name=stats_1[:256], inline=False)

        stats_2 = (f'`{"Participants in last 90 days:":<35}\u200b` {participants_90d.count()} ({participants_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 30 days:":<35}\u200b` {participants_30d.count()} ({participants_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 7 days:":<35}\u200b` {participants_7d.count()} ({participants_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_2[:256], inline=False)
        await ctx.send(embed=embed)

    @commands.command(hidden=True, usage='message')
    @commands.cooldown(1, 30, commands.BucketType.user)
    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    async def pingall(self, ctx, *, message: str = None):
        """Ping all incomplete games through the shared notification worker."""

        try:
            return await game_ping.run_prefix_all(
                ctx,
                message,
                attachments=getattr(getattr(ctx, 'message', None), 'attachments', ()),
            )
        except (
            game_ping_workers.GamePingValidationError,
            game_ping_workers.GamePingLookupError,
            game_ping_workers.GamePingPermissionError,
            game_ping_workers.GamePingConflictError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.warning('Prefix pingall failed: %s', exc)
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Unexpected prefix pingall failure')
            return await ctx.send(
                'The game ping could not be completed. No retry was created '
                'for a committed notification; check the public reconciliation '
                'message if one was posted.'
            )

    @commands.command(usage='game_id message')
    @models.is_registered_member()
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def ping(self, ctx, *, args=''):
        """Ping one incomplete game through the shared notification worker."""

        if not str(args or '').strip() and not getattr(
            getattr(ctx, 'message', None),
            'attachments',
            (),
        ):
            reset = getattr(getattr(ctx, 'command', None), 'reset_cooldown', None)
            if callable(reset):
                reset(ctx)
        try:
            result = await game_ping.run_prefix_single(
                ctx,
                args,
                attachments=getattr(getattr(ctx, 'message', None), 'attachments', ()),
            )
            if settings.is_mod(ctx.author):
                reset = getattr(getattr(ctx, 'command', None), 'reset_cooldown', None)
                if callable(reset):
                    reset(ctx)
            return result
        except (
            game_ping_workers.GamePingValidationError,
            game_ping_workers.GamePingLookupError,
            game_ping_workers.GamePingPermissionError,
            game_ping_workers.GamePingConflictError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.warning('Prefix ping failed: %s', exc)
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Unexpected prefix ping failure')
            return await ctx.send(
                'The game ping could not be completed. Please try again later.'
            )

    @discord.app_commands.command(
        name='staffhelp',
        description='Submit a structured help, bug, or feature report.',
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.user.id,
    )
    async def staffhelp_slash(self, interaction: discord.Interaction):
        """Open the shared requester-bound staff-help form."""

        if interaction.guild_id is None or interaction.channel_id is None:
            return await interaction.response.send_message(
                'Staff help is available in a server channel only.',
                ephemeral=True,
            )
        availability_error = staff_help.availability_error(
            self.bot,
            interaction.guild_id,
            profile=settings.runtime_profile,
        )
        if availability_error is not None:
            return await interaction.response.send_message(
                availability_error,
                ephemeral=True,
            )
        await interaction.response.send_modal(
            beta_feedback_views.StaffHelpModal(
                self.bot,
                requester_id=interaction.user.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                profile=settings.runtime_profile,
            )
        )

    @discord.app_commands.command(
        name='whattotest',
        description='Open your private compact Beta Lab testing dashboard.',
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.user.id,
    )
    async def whattotest_slash(self, interaction: discord.Interaction):
        """Open a compact private Beta Lab dashboard."""

        if settings.runtime_profile.environment != 'development':
            return await interaction.response.send_message(
                'This temporary command is available only on the development bot.',
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            guide = beta_testing_guide.load_guide()
        except OSError:
            logger.exception('Could not load the beta what-to-test checklist.')
            return await interaction.followup.send(
                'The testing checklist is temporarily unavailable.',
                ephemeral=True,
            )
        try:
            status = await beta_lab_workers.run_status(int(interaction.guild_id))
        except Exception:
            logger.exception('Could not load Beta Lab status.')
            return await interaction.followup.send(
                'The Beta Lab status is temporarily unavailable. No testing '
                'state was changed.',
                ephemeral=True,
            )
        role_ids = tuple(
            int(role.id) for role in getattr(interaction.user, 'roles', ())
        )
        has_lane_role = (
            int(interaction.user.id) == int(settings.owner_id)
            or beta_readiness.BETA_PINNED_TESTER_ROLE_ID in role_ids
        )
        session = None
        lane_notice = None
        try:
            session = await beta_lab_sessions.run_requester_session(
                beta_lab_sessions.BetaLabSessionRequest(
                    guild_id=int(interaction.guild_id),
                    requester_id=int(interaction.user.id),
                    requester_name=str(interaction.user.display_name),
                    role_ids=role_ids,
                )
            )
        except beta_lab_sessions.BetaLabSessionError as exc:
            lane_notice = str(exc)
        except Exception:
            logger.exception('Could not load the requester Beta Lab lane.')
            lane_notice = (
                'Your game-lane state is temporarily unavailable; the '
                'read-only tests are still safe to use.'
            )
        lane_authorized = has_lane_role or session is not None
        view = beta_testing_dashboard.BetaTestingDashboard(
            bot=self.bot,
            requester_id=int(interaction.user.id),
            requester_name=str(interaction.user.display_name),
            guild_id=int(interaction.guild_id),
            channel_id=int(interaction.channel_id),
            role_ids=role_ids,
            lane_authorized=lane_authorized,
            session=session,
            status=status,
            guide=guide,
        )
        if lane_notice:
            view.notice = lane_notice
            view.rebuild()
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @commands.command(hidden=False, aliases=['random_tribes', 'rtribe'], usage='n_tribes [-banned_tribe ...]')
    @settings.in_bot_channel()
    async def rtribes(self, ctx, *, arg):
        """
        Selects a random set of n tribes. 
        As shown in the examples below, you may add options to ban tribes, fix the random seed, require selection of free tribes, or allow duplicate tribes to be selected.
        **Examples:**
        `[p]rtribes 4` - Selects 4 random tribes.
        `[p]rtribes 6 -ho -aq` - Selects 6 random tribes, excluding Hoodrick and Aquarion. Matches by first 2 letters.
        `[p]rtribes 7 seed=12345` - Providing a seed guarantees the same selections each time. For example, use your game ID as the seed.
        `[p]rtribes 7 force_free=2` - Forces selection of at least 2 free tribes.
        `[p]rtribes 7 allow_duplicates` - Allows multiples of the same tribe to be selected.
        """
            
        args = arg.split() if arg else []
    
        # set default params
        n: int = None
        allow_duplicates = False
        seed: int = None
        force_free: int = 0
        banned_tribes = []

        # Set a flag to track if the number of tribes has been set
        n_set = False
    
        # parse inputs
        for a in args:
            if a.isdigit():
                if n_set:
                    return await ctx.send(f'Error: number of tribes has been specified as both {n} and {a}. Please include only one value for the number of tribes to select.')
                else:
                    n = int(a)
                    n_set = True
            elif a.startswith('-'):
                banned_tribes.append(a[1:3].lower())
            elif a.startswith('seed'):
                parts = a.split('=')
                if len(parts) < 2 or not parts[1].isdigit():
                    await ctx.send(f'Warning: the seed provided must be an integer (e.g. `seed=12345`). Ignoring the seed parameter.')
                else:
                    seed = int(parts[1])
                    random.seed(seed)
            elif a.startswith('force_free'):
                parts = a.split('=')
                if len(parts) < 2 or not parts[1].isdigit():
                    return await ctx.send(f'Error: force_free must be set to an integer (e.g. `force_free=2`).')
                force_free = int(parts[1])
            elif a == 'allow_duplicates':
                allow_duplicates = True
            else:
                await ctx.send(f'Warning: unrecognized parameter \'{a}\'. Ignoring it.')
        
        if force_free < 0: return await ctx.send(f'Error: you can\'t force a negative number of free tribes to appear.')
        if not allow_duplicates and force_free > 4: return await ctx.send(f'Error: you can\'t force more than 4 free tribes without allowing duplicates.')
        if n_set is False: n=1
    
        if n > 16 or n < 1:
            return await ctx.send(f'Error: invalid number of tribes selected, {n}. Must be between 1 and 16')
    
        FREE_TRIBES = ['Xin-xi',
                       'Imperius',
                       'Bardur',
                       'Oumaji']
        PAID_TRIBES = ['Kickoo',
                       'Hoodrick',
                       'Luxidoor',
                       'Vengir',
                       'Zebasi',
                       'Ai-mo',
                       'Quetzali',
                       'Yadakk',
                       'Aquarion',
                       'Elyrion',
                       'Polaris',
                       'Cymanti']
    
        available_free_tribes = [
            tribe for tribe in FREE_TRIBES
            if not any(tribe.lower().startswith(prefix) for prefix in banned_tribes)
        ]
        available_paid_tribes = [
            tribe for tribe in PAID_TRIBES
            if not any(tribe.lower().startswith(prefix) for prefix in banned_tribes)
        ]
    
        # error checking force_free input
        if not allow_duplicates and len(available_free_tribes) < force_free:
            await ctx.send(f'Warning: too many free tribes banned to satisfy force_free={force_free}. Selecting all unbanned free tribes.')
            force_free=len(available_free_tribes)
    
        if allow_duplicates and force_free > 0 and not available_free_tribes:
            await ctx.send(f'Warning: all free tribes have been banned, but force_free was above zero. Ignoring force_free parameter.')
            force_free=0
    
        # Select the required number of free tribes
        if allow_duplicates:
            # With duplicates allowed, if we have at least one tribe, we can pick any number of times from it
            selected_tribes = random.choices(available_free_tribes, k=force_free) if available_free_tribes else []
        else:
            # Without duplicates, we ensure we have enough unique tribes to pick from
            if len(available_free_tribes) < force_free:
                return await ctx.send(f"Error: too many free tribes banned to satisfy force_free requirement.")
            selected_tribes = random.sample(available_free_tribes, k=force_free)
    
        # Calculate how many more tribes to select after these free ones have been selected
        remaining_slots = n - len(selected_tribes)
        
        if remaining_slots > 0: 
            # Set the list of available tribes for the remaining selections
            remaining_tribes = available_free_tribes + available_paid_tribes
            if not allow_duplicates:
                remaining_tribes = [tribe for tribe in remaining_tribes if tribe not in selected_tribes]
        
            # Check if there are enough tribes left to select the requested amount
            if not allow_duplicates and remaining_slots > len(remaining_tribes):
                return await ctx.send(f"Error: not enough unbanned tribes to select the requested {n} tribes.")
        
            # Select from the remaining tribes
            if allow_duplicates:
                selected_tribes += random.choices(remaining_tribes, k=remaining_slots)
            else:
                selected_tribes += random.sample(remaining_tribes, k=remaining_slots)

        await ctx.send(', '.join(selected_tribes))
        emojis = []
        for tribe_name in selected_tribes:
            tribe = models.Tribe.get_by_name(tribe_name)
            if tribe and tribe.emoji:
                emojis.append(tribe.emoji)
            else:
                emojis.append(tribe_name)
        return await ctx.send(''.join(emojis))

    @commands.command(name='freeagents', usage='[sort]')
    @role_lookup_server_check()
    @settings.in_bot_channel_strict()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def freeagents(self, ctx: commands.Context, *, arg=None):
        """List registered members with the configured Free Agent role."""

        try:
            request = role_leaderboard_service.request_for_prefix(ctx, arg)
            async with ctx.typing():
                result = await role_leaderboard_workers.run_role_leaderboard(
                    request,
                )
        except (
            role_leaderboard_workers.RoleLeaderboardValidationError,
            ValueError,
        ) as exc:
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Could not load Free Agent role leaderboard')
            return await ctx.send(
                'Could not load the Free Agent leaderboard. Please try again.'
            )
        await role_leaderboard_service.publish_prefix(ctx, result, request)


    async def task_broadcast_newbie_message(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            sleep_cycle = (60 * 60 * 3)
            await asyncio.sleep(10)

            for guild in self.bot.guilds:
                broadcast_channels = [guild.get_channel(chan) for chan in settings.guild_setting(guild.id, 'newbie_message_channels')]
                if not broadcast_channels:
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                # ranked_chan = settings.guild_setting(guild.id, 'ranked_game_channel')
                # unranked_chan = settings.guild_setting(guild.id, 'unranked_game_channel')
                bot_spam_chan = settings.guild_setting(guild.id, 'bot_channels_strict')[0]
                elo_guide_channel = 533391050014720040

                broadcast_message = (f'To register for ELO leaderboards and matchmaking use the command __`{prefix}setname Your Mobile Name`__')
                broadcast_message += f'\nTo get started with joining an open game, go to <#{bot_spam_chan}> and type __`{prefix}games`__'
                broadcast_message += f'\nFor full information go read <#{elo_guide_channel}>.'

                for broadcast_channel in broadcast_channels:
                    if broadcast_channel:
                        message = await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))
                        self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, broadcast_channel.id, message.id)]

            await asyncio.sleep(sleep_cycle)

    async def task_broadcast_newbie_steam_message(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            sleep_cycle = (60 * 60 * 6)
            await asyncio.sleep(10)

            for guild in self.bot.guilds:
                broadcast_channel = guild.get_channel(settings.guild_setting(guild.id, 'steam_game_channel'))
                if not broadcast_channel:
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                elo_guide_channel = 533391050014720040

                broadcast_message = (f'To register for ELO leaderboards and matchmaking use the command __`{prefix}steamname Your Steam Name`__')
                broadcast_message += f'\nTo get started with joining an open game, type __`{prefix}games`__ or open your own with __`{prefix}opensteam`__'
                broadcast_message += f'\nFor full information go read <#{elo_guide_channel}>.'

                message = await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))
                self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, broadcast_channel.id, message.id)]

            await asyncio.sleep(sleep_cycle)


async def setup(bot):
    await bot.add_cog(misc(bot))
