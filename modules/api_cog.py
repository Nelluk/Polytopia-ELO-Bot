"""Discord.py extension for managing API applications."""
import discord
import logging
from discord.ext import commands

from . import models

api_logger = logging.getLogger('polybot.api')


class Api(commands.Cog, name='api'):
    """Commands for viewing and managing your API apps."""

    def __init__(self, bot: commands.Bot):
        """Store a reference to the bot."""
        self.bot = bot

    @commands.is_owner()
    @commands.command(name='add-application', aliases=['add-app'], hidden=True)
    async def add_application(
            self, ctx: commands.Context, owner: discord.User, *, name: str):
        """Authorise a new application to use the API.

        `owner` is the person who will be able to manage the application
        (ie. get and reset its token).

        Example: `[p]add-app @Legorooj Awesome Bot 3000`
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author}')
        owner_member = models.DiscordMember.get_or_none(
            models.DiscordMember.discord_id == owner.id
        )
        if not owner_member:
            owner_member = models.DiscordMember.create(
                discord_id=owner.id, name=owner.name
            )
        app = models.ApiApplication.create(owner=owner_member, name=name)
        app.generate_new_token()

        api_logger.debug(f'App "{name}" created for {owner.display_name}')
        await ctx.send(
            f'Created app **{name}** for **{owner.display_name}**.'
        )

    @commands.is_owner()
    @commands.command(name='all-applications', aliases=['all-apps'], hidden=True)
    async def all_applications(self, ctx: commands.Context):
        """Get a list of every app, its scopes, ID, name and owner.

        Example: `[p]all-apps`
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author}')
        embed = discord.Embed(title='API Apps')
        for app in models.ApiApplication.select().join(models.DiscordMember):
            embed.add_field(
                name=app.name,
                value=(
                    f'`{app.id:>2}` owned by '
                    f'<@{app.owner.discord_id}> ({app.owner.name})\n\n'
                    f'`{app.scopes}`'
                ),
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.is_owner()
    @commands.command(name='delete-application', aliases=['del-app'], hidden=True)
    async def delete_application(
            self, ctx: commands.Context, app: models.ApiApplication):
        """Delete an application by ID.

        Example: `[p]del-app 5`
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author} for app {app.name}')
        name = app.name
        app.delete_instance()
        await ctx.send(f'Deleted app **{name}**.')

    @commands.is_owner()
    @commands.command(name='authorise-application', aliases=['auth-app'], hidden=True)
    async def authorise_application(
            self, ctx: commands.Context,
            app: models.ApiApplication, *, scopes: str):
        """Authorise an application to use certain scopes.

        Example: `[p]auth-app 2 users:read games:read`

        Note that this also removes any scopes the app previously had access
        to - you must specify them all in the same command.
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author} for scopes {scopes} on app {app.name}')
        app.scopes = scopes
        app.save()
        await ctx.send(f'Updated scopes for **{app.name}**.')

    @commands.command(brief='View your apps.', hidden=True)
    async def apps(self, ctx: commands.Context):
        """View all API apps you own.

        Example: `[p]apps`
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author}')
        member = models.DiscordMember.get_or_none(
            ctx.author.id == models.DiscordMember.discord_id
        )
        if not member:
            await ctx.send('You are not registered with the bot.')
            return
        lines = ['You have the following apps:']
        for app in member.applications:
            lines.append(f'`{app.id:>2}` **{app.name}**\n`{app.scopes}`')
        await ctx.send('\n\n'.join(lines))

    @commands.command(brief='Get your app\'s token.', name='app-token', hidden=True)
    async def app_token(
            self, ctx: commands.Context, *, app: models.ApiApplication):
        """Get the token of an app you own (it will be DMed to you).

        You must specify the app by its ID.

        Example: `[p]app-token 3`
        """

        api_logger.debug(f'{ctx.invoked_with} invoked by {ctx.author} for app {app.id}')
        if app.owner.discord_id != ctx.author.id:
            await ctx.send('You don\'t own that app!')
            return
        await ctx.author.send(
            f'Username: `{app.id}`\n'
            f'Password: `{app.token}`\n'
            f'Authorization: `Basic {app.user_pass}`\n'
            f'Scopes: `{app.scopes}`'
        )
        await ctx.send('DMed you.')

    @commands.command(brief='Reset your app\'s token.', name='reset-token', hidden=True)
    async def reset_token(
            self, ctx: commands.Context, *, app: models.ApiApplication):
        """Reset the token of an app you own.

        You must specify the app by its ID.

        Example: `[p]app-token 5`
        """
        if app.owner.discord_id != ctx.author.id:
            await ctx.send('You don\'t own that app!')
            return
        app.generate_new_token()
        await ctx.send(f'Reset token for **{app.name}**!')


async def setup(bot: commands.Bot):
    """Add the cog to a bot."""
    await bot.add_cog(Api(bot))
