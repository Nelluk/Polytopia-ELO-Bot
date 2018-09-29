import discord
import datetime
import configparser
import argparse
import traceback
from discord.ext import commands
from modules import models
from modules import initialize_data
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
if (logger.hasHandlers()):
    logger.handlers.clear()
handler = RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=500 * 1024, backupCount=1)
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

config = configparser.ConfigParser(allow_no_value=True)
config.read('config.ini')

try:
    discord_key = config['DEFAULT']['discord_key']
except KeyError:
    print('Error finding required setting discord_key in config.ini file - it should be in the DEFAULT section')
    exit(0)

date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards


def get_prefix(bot, message):
    # Guild-specific command prefixes
    if message.guild and str(message.guild.id) in config.sections():
        # Current guild is allowed
        return commands.when_mentioned_or(config[str(message.guild.id)]['command_prefix'])(bot, message)
    else:
        logger.error(f'Message received not from allowed guild')
        return commands.when_mentioned_or(config['DEFAULT']['command_prefix'])(bot, message)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--add_default_data', action='store_true')
    args = parser.parse_args()
    if args.add_default_data:
        initialize_data.initialize_data()
        exit(0)

    bot = commands.Bot(command_prefix=get_prefix)
    # bot.remove_command('help')

    @bot.check
    async def globally_block_dms(ctx):
        # Should prevent bot from being able to be controlled via DM
        return ctx.guild is not None

    @bot.event
    async def on_command_error(ctx, exc):

        # This prevents any commands with local handlers being handled here in on_command_error.
        if hasattr(ctx.command, 'on_error'):
            return

        ignored = (commands.CommandNotFound, commands.UserInputError, commands.CheckFailure)

        # Anything in ignored will return and prevent anything happening.
        if isinstance(exc, ignored):
            logger.warn(f'Exception on ignored list raised in {ctx.command}. {exc}')
            return

        exception_str = ''.join(traceback.format_exception(etype=type(exc), value=exc, tb=exc.__traceback__))
        logger.critical(f'Ignoring exception in command {ctx.command}: {exc} {exception_str}', exc_info=True)
        print(f'Exception raised. {exc}\n{exception_str}')
        await ctx.send(f'Unhandled error: {exc}')

    @bot.after_invoke
    async def post_invoke_cleanup(ctx):
        models.db.close()

    # Here we load our extensions(cogs) listed above in [initial_extensions].

    initial_extensions = ['modules.games', 'modules.help']
    for extension in initial_extensions:
        bot.load_extension(extension)
        try:
            bot.load_extension(extension)
        except Exception as e:
            print(f'Failed to load extension {extension}: {e}')
            pass

    @bot.event
    async def on_ready():
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        print(f'\n\nv2 Logged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print(f'Successfully logged in and booted...!')

    bot.run(discord_key, bot=True, reconnect=True)
