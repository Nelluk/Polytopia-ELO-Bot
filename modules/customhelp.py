# import discord
from discord.ext import commands


class MyHelpCommand(commands.MinimalHelpCommand):
# class MyHelpCommand(commands.DefaultHelpCommand):
    def get_command_signature(self, command):
        return '{0.clean_prefix}{1.qualified_name} {1.signature}'.format(self, command)

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
