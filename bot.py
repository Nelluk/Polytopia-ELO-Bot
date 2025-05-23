import argparse
import asyncio
import logging
import sys
import traceback
from timeit import default_timer as timer
from typing import List

import discord
from discord.ext import commands

import logging_config
import modules.exceptions as exceptions
import settings
from modules import initialize_data, models, utilities

logger = logging.getLogger('polybot.' + __name__)
# https://discord.com/channels/336642139381301249/1042604006226280468/1042645381143613532

def main(args: List[str] = None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--add_default_data', action='store_true')
    parser.add_argument('--recalc_elo', action='store_true')
    parser.add_argument('--game_export', action='store_true')
    parser.add_argument('--skip_tasks', action='store_true')
    # Ignore extra args from uvicorn.
    args, unkown = parser.parse_known_args(args)
    if args.add_default_data:
        initialize_data.initialize_data()
        exit(0)
    if args.recalc_elo:
        print('Recalculating all ELO')
        start = timer()
        models.Game.recalculate_all_elo()
        end = timer()
        print(f'Recalculation complete - took {end - start} seconds.')
        exit(0)
    if args.game_export:
        print('Exporting game data to file')
        start = timer()
        utilities.export_game_data()
        print(f'Recalculation complete - took {timer() - start} seconds.')
        exit(0)
    if args.skip_tasks:
        settings.run_tasks = False

    logger.info('Resetting Discord ID ban list')
    with models.db:
        models.DiscordMember.update(is_banned=False).execute()
        if settings.discord_id_ban_list:
            query = models.DiscordMember.update(is_banned=True).where(
                (models.DiscordMember.discord_id.in_(settings.discord_id_ban_list))
            )
            logger.info(f'{query.execute()} discord IDs are banned')

        if settings.poly_id_ban_list:
            query = models.DiscordMember.update(is_banned=True).where(
                (models.DiscordMember.polytopia_id.in_(settings.poly_id_ban_list))
            )
            logger.info(f'{query.execute()} polytopia IDs are banned')

class MyBot(commands.Bot):
    intents = discord.Intents().all()
    intents.typing = False
    def __init__(self):
        super().__init__(command_prefix=get_prefix,
                         owner_id=settings.owner_id,
                         allowed_mentions=discord.AllowedMentions(everyone=False),
                         intents=self.intents,
                         activity=discord.Activity(name='$guide', type=discord.ActivityType.playing))
        settings.bot = self
        self.purgable_messages = []  # auto-deleting messages to get cleaned up by Administraton.quit  (guild, channel, message) tuple list
        self.locked_game_records = set()  # Games which cannot be written to since another command is working on them right now. Ugly hack to do what should be done at the DB level

    async def setup_hook(self):
        initial_extensions = [
            'modules.games', 'modules.customhelp', 'modules.matchmaking',
            'modules.administration', 'modules.misc', 'modules.league',
            'modules.api_cog', 'modules.bullet'
        ]
        for extension in initial_extensions:
            await self.load_extension(extension)

def get_prefix(bot, message):
    # Guild-specific command prefixes
    if message.guild and message.guild.id in settings.config:
        # Current guild is allowed
        set_prefix = settings.guild_setting(message.guild.id, "command_prefix")
        if not set_prefix:
            logger.error(f'No prefix found in settings! Guild: {message.guild.id} {message.guild.name}')
            return 'fakeprefix'

        # temp debug log to try to fix NoneType errors related to prefixes
        # logger.debug(f'Found prefix setting {settings.guild_setting(message.guild.id, "command_prefix")} for guild {message.guild.id}')
        return commands.when_mentioned_or(settings.guild_setting(message.guild.id, 'command_prefix'))(bot, message)
    else:
        if message.guild:
            logger.error(f'Message received not from allowed guild. ID {message.guild.id }')
        # probably a PM
        logger.warning(f'returning None prefix for received PM. Author: {message.author.name}')
        return 'fakeprefix'


def init_bot(loop: asyncio.AbstractEventLoop = None, args: List[str] = None):
    main(args)
    utilities.connect()
    bot = MyBot()

    cooldown = commands.CooldownMapping.from_cooldown(6, 30.0, commands.BucketType.user)

    @bot.check
    async def globally_block_dms(ctx):
        # Should prevent bot from being able to be controlled via DM
        return ctx.guild is not None

    @bot.check
    async def restrict_banned_users(ctx):
        if ctx.author.id in settings.discord_id_ban_list or discord.utils.get(ctx.author.roles, name='ELO Banned'):
            await ctx.send('You are banned from using this bot. :kissing_heart:')
            return False
        return True

    @bot.check
    async def cooldown_check(ctx):
        if ctx.invoked_with == 'help' and ctx.command.name != 'help':
            # otherwise check will run once for every command in the bot when someone invokes $help
            return True
        if ctx.author.id == settings.owner_id:
            return True
        bucket = cooldown.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await ctx.send('You\'re on cooldown. Slow down those commands!')
            logger.warning(f'Cooldown limit reached for user {ctx.author.id}')
            return False

        # not on cooldown
        return True

    @bot.event
    async def on_command_error(ctx, exc):
        # This prevents any commands with local handlers being handled here in on_command_error.
        if hasattr(ctx.command, 'on_error'):
            return
        print(type(exc))
        error = getattr(exc, "original", exc)
        print(error, type(error))
        ignored = (commands.CommandNotFound, commands.UserInputError, commands.CheckFailure)

        if isinstance(exc, commands.CommandNotFound) and ctx.invoked_with[:4] == 'join':
            await ctx.send(f'Cannot understand command. Make sure to include a space and a numeric game ID.\n*Example:* `{ctx.prefix}join 11234`')

        # Anything in ignored will return and prevent anything happening.
        if isinstance(exc, ignored):
            logger.warning(f'Exception on ignored list raised in {ctx.command}. {exc}')
            return
        if isinstance(exc, commands.CommandOnCooldown):
            logger.info(f'Cooldown triggered: {exc}')
            await ctx.send(f'This command is on a cooldown period. Try again in {exc.retry_after:.0f} seconds.')
        elif isinstance(exc, exceptions.RecordLocked):
            return await ctx.send(f':warning: {exc}')
        else:
            exception_str = ''.join(traceback.format_exception(etype=type(exc), value=exc, tb=exc.__traceback__))
            logger.critical(f'Ignoring exception in command {ctx.command}: {exc} {exception_str}', exc_info=True)
            await ctx.send(f'Unhandled error (notifying <@{settings.owner_id}> and <@608290258978865174>): {exc}')  #added legorooj to notification

    @bot.before_invoke
    async def pre_invoke_setup(ctx):
        utilities.connect()
        logger.debug(f'Command invoked: {ctx.message.clean_content}. By {ctx.message.author.name} in {ctx.channel.id} {ctx.channel.name} on {ctx.guild.name}')

    @bot.event
    async def on_message(message):
        if settings.maintenance_mode:
            if message.content and message.content.startswith(tuple(get_prefix(bot, message))):
                logger.debug('Ignoring messages while settings.maintenance_mode is set to True')
        else:
            # it is possible to modify the content of a message here before processing, ie replace curly quotes in message.content with straight quotes
            await bot.process_commands(message)

    @bot.event
    async def on_ready():
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        print(f'\n\nv2 Logged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print('Successfully logged in and booted...!')

        for g in bot.guilds:
            if g.id in settings.config:
                logger.debug(f'Loaded in guild {g.id} {g.name}')
            else:
                logger.error(f'Unauthorized guild {g.id} {g.name} not found in settings.py configuration - Leaving...')
                await g.leave()

        await bot.tree.sync(guild=discord.Object(settings.server_ids['polychampions']))

    if loop:
        loop.create_task(bot.start(settings.discord_key))
    else:
        bot.run(settings.discord_key)


if __name__ == '__main__':
    init_bot()
