import discord
import argparse
import traceback
from discord.ext import commands
from modules import models
from modules import initialize_data
from modules import utilities
import settings
import logging
import sys
from logging.handlers import RotatingFileHandler
from timeit import default_timer as timer
# import peewee


# Logger config is a bit of a mess and probably could be simplified a lot, but works. debug and above sent to file / error above sent to stderr
handler = RotatingFileHandler(filename='logs/full_bot.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=10)
partial_handler = RotatingFileHandler(filename='logs/discord.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=10)  # without peewee logging
elo_handler = RotatingFileHandler(filename='logs/elo.log', encoding='utf-8', maxBytes=1024 * 1024 * 2, backupCount=5)

handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
partial_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
elo_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))

my_logger = logging.getLogger('polybot')
my_logger.setLevel(logging.DEBUG)
my_logger.addHandler(handler)  # root handler for app. module-specific loggers will inherit this
my_logger.addHandler(partial_handler)

elo_logger = logging.getLogger('polybot.elo')
elo_logger.setLevel(logging.DEBUG)
elo_logger.addHandler(elo_handler)

err = logging.StreamHandler(sys.stderr)
err.setLevel(logging.ERROR)
err.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
my_logger.addHandler(err)


discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.INFO)

if (discord_logger.hasHandlers()):
    discord_logger.handlers.clear()

discord_logger.addHandler(handler)
discord_logger.addHandler(partial_handler)

logger_peewee = logging.getLogger('peewee')
logger_peewee.setLevel(logging.DEBUG)

if (logger_peewee.hasHandlers()):
    logger_peewee.handlers.clear()

logger_peewee.addHandler(handler)

logger = logging.getLogger('polybot.' + __name__)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('--add_default_data', action='store_true')
    parser.add_argument('--recalc_elo', action='store_true')
    parser.add_argument('--game_export', action='store_true')
    parser.add_argument('--skip_tasks', action='store_true')
    args = parser.parse_args()
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


if __name__ == '__main__':

    main()
    utilities.connect()
    am = discord.AllowedMentions(everyone=False)
    bot = commands.Bot(command_prefix=get_prefix,
                       owner_id=settings.owner_id,
                       allowed_mentions=am,
                       activity=discord.Activity(name='$guide', type=discord.ActivityType.playing))
    settings.bot = bot
    bot.purgable_messages = []  # auto-deleting messages to get cleaned up by Administraton.quit  (guild, channel, message) tuple list

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
        else:
            exception_str = ''.join(traceback.format_exception(etype=type(exc), value=exc, tb=exc.__traceback__))
            logger.critical(f'Ignoring exception in command {ctx.command}: {exc} {exception_str}', exc_info=True)
            await ctx.send(f'Unhandled error (notifying <@{settings.owner_id}>): {exc}')

    @bot.before_invoke
    async def pre_invoke_setup(ctx):
        utilities.connect()
        logger.debug(f'Command invoked: {ctx.message.clean_content}. By {ctx.message.author.name} in {ctx.channel.id} {ctx.channel.name} on {ctx.guild.name}')

    initial_extensions = ['modules.games', 'modules.customhelp', 'modules.matchmaking', 'modules.administration', 'modules.misc', 'modules.league']
    for extension in initial_extensions:
        bot.load_extension(extension)

    @bot.event
    async def on_ready():
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        print(f'\n\nv2 Logged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print(f'Successfully logged in and booted...!')

        for g in bot.guilds:
            if g.id in settings.config:
                logger.debug(f'Loaded in guild {g.id} {g.name}')
            else:
                logger.error(f'Unauthorized guild {g.id} {g.name} not found in settings.py configuration - Leaving...')
                await g.leave()

    bot.run(settings.discord_key, bot=True, reconnect=True)
