import discord
import datetime
import configparser
import argparse
import traceback
from discord.ext import commands
from models import db
import logging

logger = logging.getLogger('discord')
logger.setLevel(logging.WARNING)
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(handler)

config = configparser.ConfigParser()
config.read('config.ini')

try:
    discord_key = config['DEFAULT']['discord_key']
    helper_roles = list(map(str.strip, (config['DEFAULT']['helper_roles']).split(',')))     # list(map(str.strip, foo))  clears extra trailing/leading whitespace
    mod_roles = list(map(str.strip, (config['DEFAULT']['mod_roles']).split(',') + helper_roles))
except KeyError:
    print('Error finding required settings in config.ini file - discord_key / helper_roles / mod_roles')
    exit(0)


bot_channels = config['DEFAULT'].get('bot_channels', None)
game_request_channel = config['DEFAULT'].get('game_request_channel', None)
game_announce_channel = config['DEFAULT'].get('game_announce_channel', None)
command_prefix = config['DEFAULT'].get('command_prefix', '$')
require_teams = True if config['DEFAULT'].get('require_teams') == 'True' else False
date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards

parser = argparse.ArgumentParser()
parser.add_argument('--add_default_data', action='store_true')
parser.add_argument('--add_example_games', action='store_true')
args = parser.parse_args()

initial_extensions = ['elo_games', 'game_import_export']

if __name__ == '__main__':

    bot = commands.Bot(command_prefix=commands.when_mentioned_or(command_prefix))
    bot.remove_command('help')

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

        exception_str = "".join(traceback.format_tb(exc.__traceback__))
        logger.critical(f'Ignoring exception in command {ctx.command}: {exc} {exception_str}', exc_info=True)
        print(f'Exception raised. {exc}\r{exception_str}')
        await ctx.send(f'Unhandled error: {exc}')

    @bot.after_invoke
    async def post_invoke_cleanup(ctx):
        db.close()

    # Here we load our extensions(cogs) listed above in [initial_extensions].

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

        print(f'\n\nLogged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print(f'Successfully logged in and booted...!')

    bot.run(discord_key, bot=True, reconnect=True)
