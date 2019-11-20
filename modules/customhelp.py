import discord
from discord.ext import commands

# notes:
# look into the discord.py code for $help category (ie $help matchmaking) output which by default is not too far from what i want for the overall $help output

class MyHelpCommand(commands.MinimalHelpCommand):
# class MyHelpCommand(commands.DefaultHelpCommand):
    def get_command_signature(self, command):
        # top line of '$help <command>' output
        return '{0.clean_prefix}{1.qualified_name} {1.signature}'.format(self, command)

    # # below copied from https://github.com/mpsparrow/applesauce/blob/master/cogs/required/help.py
    # async def send_command_help(self, command):
    #     embed = discord.Embed(title=f'{command.name}', description=f'**Description:**  {command.description}\n**Usage:**  `{command.usage}`\n**Aliases:**  {command.aliases}', color=0xc1c100)
    #     await self.context.send(embed=embed)

    # async def send_bot_help(self, mapping):

    #     embed = discord.Embed(title='Help', description=f'All commands. Use `help command` for more info.', color=0xc1c100)

    #     # get list of commands
    #     cmds = []
    #     for cog, cog_commands in mapping.items():
    #         cmds = cmds + cog_commands

    #     # put commands in alphabetical order
    #     newCmds = []
    #     for item in cmds:
    #         newCmds.append(str(item))
    #     newCmds = sorted(newCmds)

    #     # combine commands into string for output
    #     commandStr = ''
    #     for cmd in newCmds:
    #         commandStr += '``' + str(cmd) + '`` '

    #     # add all commands to embed and message it
    #     embed.add_field(name='Commands', value=f'{commandStr}', inline=False)
    #     await self.context.send(embed=embed)

# new_short_doc = command.short_doc.replace('[p]', self.clean_prefix)


class CustomHelp(commands.Cog):
    def __init__(self, bot):
        self._original_help_command = bot.help_command
        bot.help_command = MyHelpCommand()
        bot.help_command.cog = self

    def cog_unload(self):
        self.bot.help_command = self._original_help_command


def setup(bot):
    bot.add_cog(CustomHelp(bot))
