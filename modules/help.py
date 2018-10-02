"""Overrides the built-in help formatter.
All help messages will be embed and pretty.
Most of the code stolen from
discord.ext.commands.formatter.py and
converted into embeds instead of codeblocks.
Docstr on cog class becomes category.
Docstr on command definition becomes command
summary and usage.
Use [p] in command docstr for bot prefix.
See [p]help here for example.
await bot.formatter.format_help_for(ctx, command)
to send help page for command. Optionally pass a
string as third arg to add a more descriptive
message to help page.
e.g. format_help_for(ctx, ctx.command, "Missing required arguments")
discord.py 1.0.0a
Experimental: compatibility with 0.16.8
Copyrights to logic of code belong to Rapptz (Danny)
Everything else credit to SirThane#1780
Pagination added by appu1232"""

import discord
from discord.ext import commands
from discord.ext.commands import formatter
import asyncio
# import sys
import re
import inspect
import itertools
import logging

logger = logging.getLogger(__name__)
# import traceback
# from modules.elo_games import in_bot_channel as in_bot_channel


empty = u'\u200b'


_mentions_transforms = {
    '@everyone': '@\u200beveryone',
    '@here': '@\u200bhere'
}


_mention_pattern = re.compile('|'.join(_mentions_transforms.keys()))


orig_help = None


class Help(formatter.HelpFormatter):
    """Formats help for commands."""

    def __init__(self, bot, *args, **kwargs):
        self.bot = bot
        global orig_help
        orig_help = bot.get_command('help')
        self.bot.remove_command('help')
        self.bot.formatter = self
        self.bot.help_formatter = self
        super().__init__(*args, **kwargs)

    # Shortcuts that allow cog to run on 0.16.8 and 1.0.0a

    def pm_check(self, ctx):
        return isinstance(ctx.channel, discord.abc.PrivateChannel)

    @property
    def me(self):
        return self.context.me

    @property
    def bot_all_commands(self):
        return self.bot.all_commands

    @property
    def avatar(self):
        return self.bot.user.avatar_url_as(format='png')

    @property
    def color(self):
        if self.pm_check(self.context):
            return 0
        else:
            return self.me.color

    async def send(self, dest, content=None, embeds=None):
        if len(embeds) == 1:
            embed = embeds[0]
            embed.set_author(name='{0} Help Manual'.format(self.bot.user.name), icon_url=self.avatar)
            return await dest.send(content=content, embed=embed)

        # Replaced message based pagination with reaction-based pagination
        first_loop = True
        page_number = 0
        page_end = len(embeds) - 1

        while True:
            # Saw a weird issue where embed content was being duplicated, discord said it was a known issue with embed field names being duplicated
            if first_loop is True:
                help_msg = await dest.send(content=content, embed=embeds[page_number])
            else:
                try:
                    await help_msg.clear_reactions()
                except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                    logger.warn('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
                # await help_msg.edit(embed=embed)
                await help_msg.edit(content=content, embed=embeds[page_number])

            if page_number > 0:
                await help_msg.add_reaction('⏪')
            if page_number < page_end:
                await help_msg.add_reaction('⏩')

            def check(reaction, user):
                e = str(reaction.emoji)
                if page_number > 0 and page_number < page_end:
                    compare = e.startswith(('⏪', '⏩'))
                elif page_number >= page_end:
                    compare = e.startswith('⏪')
                elif page_number <= 0:
                    compare = e.startswith('⏩')
                else:
                    compare = False
                return ((user == self.context.message.author) and (reaction.message.id == help_msg.id) and compare)

            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=20.0, check=check)
            except asyncio.TimeoutError:
                try:
                    await help_msg.clear_reactions()
                except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                    logger.debug('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
                finally:
                    break
            else:
                if '⏪' in str(reaction.emoji):

                    page_number = page_number - 1

                elif '⏩' in str(reaction.emoji):

                    page_number = page_number + 1
                    if page_number > page_end:
                        page_number = page_end

                first_loop = False

    @property
    def author(self):
        # Get author dict with username if PM and display name in guild
        if self.pm_check(self.context):
            name = self.bot.user.name
        else:
            name = self.me.display_name if not '' else self.bot.user.name
        author = {
            'name': '{0} Help Manual'.format(name),
            'icon_url': self.avatar
        }
        return author

    @property
    def destination(self):
        return self.context.message.author if self.bot.pm_help else self.context.message.channel

    def _add_subcommands(self, cmds):
        list_entries = []
        entries = ''
        for name, command in cmds:
            if name in command.aliases:
                # skip aliases
                continue

            new_short_doc = command.short_doc.replace('[p]', self.clean_prefix)

            if self.is_cog() or self.is_bot():
                name = '{0}{1}'.format(self.clean_prefix, name)

            if len(entries + '**{0}**  -  {1}\n'.format(name, new_short_doc)) > 1000:
                list_entries.append(entries)
                entries = ''
            entries += '**{0}**  -  {1}\n'.format(name, new_short_doc)
        list_entries.append(entries)
        return list_entries

    def get_ending_note(self):
        # command_name = self.context.invoked_with
        return "Type {0}help <command> for more info on a command.\n" \
               "You can also type {0}help <category> for more info on a category.".format(self.clean_prefix)

    def get_command_signature(self):
        """Retrieves the signature portion of the help page."""
        result = []
        prefix = self.clean_prefix
        cmd = self.command
        parent = cmd.full_parent_name
        # if len(cmd.aliases) > 0:
        #     aliases = '|'.join(cmd.aliases)
        #     fmt = '{0}[{1.name}|{2}]'
        #     if parent:
        #         fmt = '{0}{3} [{1.name}|{2}]'
        #     result.append(fmt.format(prefix, cmd, aliases, parent))
        # else:
        #     name = prefix + cmd.name if not parent else prefix + parent + ' ' + cmd.name
        #     result.append(name)

        name = prefix + cmd.name if not parent else prefix + parent + ' ' + cmd.name
        result.append(name)

        if cmd.usage:
            result.append(cmd.usage)
        else:
            params = cmd.clean_params
            if len(params) > 0:
                for name, param in params.items():
                    if param.default is not param.empty:
                        # We don't want None or '' to trigger the [name=value] case and instead it should
                        # do [name] since [name=None] or [name=] are not exactly useful for the user.
                        should_print = param.default if isinstance(param.default, str) else param.default is not None
                        if should_print:
                            result.append('[{}={}]'.format(name, param.default))
                        else:
                            result.append('[{}]'.format(name))
                    elif param.kind == param.VAR_POSITIONAL:
                        result.append('[{}...]'.format(name))
                    else:
                        result.append('<{}>'.format(name))

        return ' '.join(result)

    async def format(self, ctx, command):
        """Formats command for output.
        Returns a dict used to build embed"""

        # All default values for embed dict
        self.command = command
        self.context = ctx
        emb = {
            'embed': {
                'title': '',
                'description': '',
            },
            'footer': {
                'text': self.get_ending_note()
            },
            'fields': []
        }

        description = command.description if not self.is_cog() else inspect.getdoc(command)
        if not description == '' and description is not None:
            description = '*{0}*'.format(description)

        if description:
            # <description> portion
            emb['embed']['description'] = description

        if isinstance(command, discord.ext.commands.core.Command):
            # <signature portion>
            # emb['embed']['title'] = emb['embed']['description']
            emb['embed']['description'] = '`Syntax: {0}`'.format(self.get_command_signature())

            # <long doc> section
            if command.help:
                name = '{0}'.format(command.help.split('\n\n')[0])
                name_length = len(name)
                name = name.replace('[p]', self.clean_prefix)
                value = command.help[name_length:].replace('[p]', self.clean_prefix)
                if value == '':
                    name = '{0}'.format(command.help.split('\n')[0])
                    name_length = len(name)
                    value = command.help[name_length:].replace('[p]', self.clean_prefix)
                if len(command.aliases) > 0:
                    aliases = ', '.join(command.aliases)
                    value = value + '\n\nAliases: ' + aliases
                if value == '':
                    value = empty
                if len(value) > 1024:
                    first = value[:1024].rsplit('\n', 1)[0]
                    list_values = [first, value[len(first):]]
                    while len(list_values[-1]) > 1024:
                        next_val = list_values[-1][:1024].rsplit('\n', 1)[0]
                        remaining = [next_val, list_values[-1][len(next_val):]]
                        list_values = list_values[:-1] + remaining
                    for new_val in list_values:
                        field = {
                            'name': name,
                            'value': new_val,
                            'inline': False
                        }
                        emb['fields'].append(field)
                else:
                    field = {
                        'name': name,
                        'value': value,
                        'inline': False
                    }
                    emb['fields'].append(field)

            # end it here if it's just a regular command
            if not self.has_subcommands():
                return emb

        def category(tup):
            # Turn get cog (Category) name from cog/list tuples
            cog = tup[1].cog_name
            return '**__{0}:__**'.format(cog) if cog is not None else '**__\u200bNo Category:__**'

        # Get subcommands for bot or category
        filtered = await self.filter_command_list()

        if self.is_bot():
            # Get list of non-hidden commands for bot.
            data = sorted(filtered, key=category)
            for category, cmds in itertools.groupby(data, key=category):
                # there simply is no prettier way of doing this.

                cmds = sorted(cmds)
                if len(cmds) > 0:
                    for count, subcommands in enumerate(self._add_subcommands(cmds)):
                        field = {
                            'inline': False
                        }
                        if count > 0:
                            field['name'] = category + ' pt. {}'.format(count + 1)
                        else:
                            field['name'] = category
                        field['value'] = subcommands  # May need paginated
                        emb['fields'].append(field)

        else:
            # Get list of commands for category
            filtered = sorted(filtered)
            if filtered:
                for subcommands in self._add_subcommands(filtered):
                    field = {
                        'name': '**__Commands:__**' if not self.is_bot() and self.is_cog() else '**__Subcommands:__**',
                        'value': subcommands,  # May need paginated
                        'inline': False
                    }

                    emb['fields'].append(field)

        return emb

    async def format_help_for(self, ctx, command_or_bot, reason: str=None):
        """Formats the help page and handles the actual heavy lifting of how  ### WTF HAPPENED?
        the help command looks like. To change the behaviour, override the
        :meth:`~.HelpFormatter.format` method.
        Parameters
        -----------
        ctx: :class:`.Context`
            The context of the invoked help command.
        command_or_bot: :class:`.Command` or :class:`.Bot`
            The bot or command that we are getting the help of.
        Returns
        --------
        list
            A paginated output of the help command.
        """
        self.context = ctx
        self.command = command_or_bot
        emb = await self.format(ctx, command_or_bot)

        if reason:
            # print(f'Reason:{reason}')
            emb['embed']['title'] = "{0}".format(reason)
        embeds = []
        embed = discord.Embed(color=self.color, **emb['embed'])
        embed.set_author(name='{0} Help Manual Page 1'.format(self.bot.user.name), icon_url=self.avatar)
        embed.set_footer(**emb['footer'])
        txt = ""
        for field in emb['fields']:
            txt += field["name"] + field["value"]
            if len(txt) > 5000:
                embeds.append(embed)
                txt = field["name"] + field["value"]
                del embed
                embed = discord.Embed(color=self.color, **emb['embed'])
                embed.set_author(name='{} Help Manual Page {}'.format(self.bot.user.name, len(embeds) + 1), icon_url=self.avatar)
                embed.set_footer(**emb['footer'])
            embed.add_field(**field)
        embeds.append(embed)

        embed.set_footer(**emb['footer'])
        await self.send(self.destination, embeds=embeds)

    def simple_embed(self, title=None, description=None, color=None, author=None):
        # Shortcut
        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_footer(text=self.bot.formatter.get_ending_note())
        if author:
            embed.set_author(**author)
        return embed

    def cmd_not_found(self, cmd, color=0):
        # Shortcut for a shortcut. Sue me
        embed = self.simple_embed(title=self.bot.command_not_found.format(cmd),
                                  description='Commands are case sensitive. Please check your spelling and try again',
                                  color=color, author=self.author)
        return embed

    # @in_bot_channel()
    @commands.command(name='help', pass_context=True, hidden=True)
    async def help(self, ctx, *cmds: str):
        if not ctx.message.author.permissions_in(ctx.channel).embed_links:
            return await ctx.send(content="You don't have permissions to send embeds here. Find a different server/channel where you can embed links and try the help command there.")

        """Shows help documentation.
        [p]**help**: Shows the help manual.
        [p]**help** command: Show help for a command
        [p]**help** Category: Show commands and description for a category"""
        self.context = ctx

        def repl(obj):
            return _mentions_transforms.get(obj.group(0), '')

        # help by itself just lists our own commands.
        if len(cmds) == 0:
            await self.bot.formatter.format_help_for(ctx, self.bot)
            return

        elif len(cmds) == 1:
            # try to see if it is a cog name
            name = _mention_pattern.sub(repl, cmds[0])
            command = None
            if name in self.bot.cogs:
                command = self.bot.cogs[name]
            else:
                command = self.bot_all_commands.get(name)
                if command is None:
                    await self.send(self.destination, embeds=[self.cmd_not_found(name, self.color)])
                    return

            await self.bot.formatter.format_help_for(ctx, command)
        else:
            name = _mention_pattern.sub(repl, cmds[0])
            command = self.bot_all_commands.get(name)
            if command is None:
                await self.send(self.destination, embeds=[self.cmd_not_found(name, self.color)])
                return

            for key in cmds[1:]:
                try:
                    key = _mention_pattern.sub(repl, key)
                    command = command.all_commands.get(key)
                    if command is None:
                        await self.send(self.destination, embeds=[self.cmd_not_found(key, self.color)])
                        return
                except AttributeError:
                    await self.send(self.destination,
                                    embeds=[self.simple_embed(title='Command "{0.name}" has no subcommands.'.format(command),
                                                            color=self.color,
                                                            author=self.author)])
                    return

            await self.bot.formatter.format_help_for(ctx, command)

    def __unload(self):
        self.bot.formatter = formatter.HelpFormatter()
        self.bot.add_command(orig_help)


def setup(bot):
    bot.add_cog(Help(bot))
