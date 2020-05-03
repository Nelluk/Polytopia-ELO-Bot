import discord
from discord.ext import commands

# notes:
# look into the discord.py code for $help category (ie $help matchmaking) output which by default is not too far from what i want for the overall $help output
# probably best to override DefaultHelpCommand directly:
# https://github.com/Rapptz/discord.py/blob/master/discord/ext/commands/help.py


class MyHelpCommand(commands.MinimalHelpCommand):
    # class MyHelpCommand(commands.DefaultHelpCommand):
    # DefaultHelpCommand advantage is it shows short help text at the /help level - but all output is in codeblocks and ignores formatting
    # MinimalHelpCommand looks cleaner on mobile and the `/help command` usage will show formatting

    def __init__(self, **options):
        self.width = options.pop('width', 80)
        self.indent = options.pop('indent', 2)
        self.sort_commands = options.pop('sort_commands', True)
        self.dm_help = options.pop('dm_help', False)
        self.dm_help_threshold = options.pop('dm_help_threshold', 1000)
        self.commands_heading = options.pop('commands_heading', "Commands:")
        self.no_category = options.pop('no_category', 'No Category')
        self.paginator = options.pop('paginator', None)

        if self.paginator is None:
            self.paginator = commands.Paginator(prefix=None)

        super().__init__(**options)

    def get_command_signature(self, command):
        # top line of '$help <command>' output
        return '{0.clean_prefix}{1.qualified_name} {1.signature}'.format(self, command)

    def add_indented_commands(self, commands, *, heading, max_size=None):
        """Indents a list of commands after the specified heading.
        The formatting is added to the :attr:`paginator`.
        The default implementation is the command name indented by
        :attr:`indent` spaces, padded to ``max_size`` followed by
        the command's :attr:`Command.short_doc` and then shortened
        to fit into the :attr:`width`.
        Parameters
        -----------
        commands: Sequence[:class:`Command`]
            A list of commands to indent for output.
        heading: :class:`str`
            The heading to add to the output. This is only added
            if the list of commands is greater than 0.
        max_size: Optional[:class:`int`]
            The max size to use for the gap between indents.
            If unspecified, calls :meth:`get_max_size` on the
            commands parameter.
        """

        if not commands:
            return

        self.paginator.add_line(heading)
        max_size = max_size or self.get_max_size(commands)

        get_width = discord.utils._string_width
        for command in commands:
            name = command.name
            width = max_size - (get_width(name) - len(name))
            entry = '{0}{1:<{width}} {2}'.format(self.indent * ' ', name, command.short_doc.replace('[p]', self.clean_prefix), width=width)
            self.paginator.add_line(self.shorten_text(entry))

    def add_subcommand_formatting(self, command):
        """Adds formatting information on a subcommand.
        The formatting should be added to the :attr:`paginator`.
        The default implementation is the prefix and the :attr:`Command.qualified_name`
        optionally followed by an En dash and the command's :attr:`Command.short_doc`.
        Parameters
        -----------
        command: :class:`Command`
            The command to show information of.
        """
        fmt = '{0}{1} \N{EN DASH} {2}' if command.short_doc else '{0}{1}'
        self.paginator.add_line(fmt.format(self.clean_prefix, command.qualified_name, command.short_doc.replace('[p]', self.clean_prefix)))

    def add_command_formatting(self, command):
        """A utility function to format the non-indented block of commands and groups.
        Parameters
        ------------
        command: :class:`Command`
            The command to format.
        """

        if command.description:
            self.paginator.add_line(command.description, empty=True)
        else:
            pass

        signature = self.get_command_signature(command)
        # print(signature)
        self.paginator.add_line(signature, empty=True)

        if command.help:
            try:
                self.paginator.add_line(command.help.replace('[p]', self.clean_prefix), empty=True)
            except RuntimeError:
                for line in command.help.replace('[p]', self.clean_prefix).splitlines():
                    self.paginator.add_line(line)
                self.paginator.add_line()

    def get_opening_note(self):
        """Returns help command's opening note. This is mainly useful to override for i18n purposes.
        The default implementation returns ::
            Use `{prefix}{command_name} [command]` for more info on a command.
            You can also use `{prefix}{command_name} [category]` for more info on a category.
        """
        command_name = self.invoked_with
        return "Use `{0}{1} [command]` for more info on a command.\n" \
               "You can also use `{0}{1} [category]` for short descriptions of each command".format(self.clean_prefix, command_name)
        # return "Use `{0}{1} [command]` for more info on a command.\n".format(self.clean_prefix, command_name)


class CustomHelp(commands.Cog):
    def __init__(self, bot):
        self._original_help_command = bot.help_command
        bot.help_command = MyHelpCommand(command_attrs={"hidden": True})
        bot.help_command.cog = self
        # self.bot.help_command.command_attrs("hidden": True)

    def cog_unload(self):
        self.bot.help_command = self._original_help_command


def setup(bot):
    bot.add_cog(CustomHelp(bot))
