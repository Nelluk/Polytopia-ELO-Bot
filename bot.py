import discord
# import asyncio
# import websockets
import datetime
import configparser
import argparse
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
    helper_roles = (config['DEFAULT']['helper_roles']).split(',')
    mod_roles = (config['DEFAULT']['mod_roles']).split(',') + helper_roles
except KeyError:
    print('Error finding required settings in config.ini file - discord_key / helper_roles / mod_roles')
    exit(0)

bot_channels = config['DEFAULT'].get('bot_channels', None)
command_prefix = config['DEFAULT'].get('command_prefix', '$')
require_teams = True if config['DEFAULT'].get('require_teams') == 'True' else False
date_cutoff = datetime.datetime.today() - datetime.timedelta(days=90)  # Players who haven't played since cutoff are not included in leaderboards

parser = argparse.ArgumentParser()
parser.add_argument('--add_default_data', action='store_true')
parser.add_argument('--add_example_games', action='store_true')
args = parser.parse_args()

initial_extensions = ['elo_games']

if __name__ == '__main__':

    bot = commands.Bot(command_prefix=commands.when_mentioned_or(command_prefix))
    bot.remove_command('help')

    @bot.check
    async def globally_block_dms(ctx):
        # Should prevent bot from being able to be controlled via DM
        return ctx.guild is not None

    @bot.after_invoke
    async def post_invoke_cleanup(ctx):
        db.close()

    # Here we load our extensions(cogs) listed above in [initial_extensions].

    for extension in initial_extensions:
        bot.load_extension(extension)
        # try:
        #     bot.load_extension(extension)
        #     print('loaded!')
        # except Exception as e:
        #     print(f'Failed to load extension {extension}.')
        #     pass

    @bot.event
    async def on_ready():
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        print(f'\n\nLogged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print(f'Successfully logged in and booted...!')

    bot.run(discord_key, bot=True, reconnect=True)

else:
    print('name != main!')
