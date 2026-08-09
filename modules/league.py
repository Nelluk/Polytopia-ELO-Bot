import asyncio

# import re
import datetime
import time
import logging
import typing
from collections import defaultdict

import discord
import peewee
from discord.ext import commands, tasks
from discord.ui import Button, Select, View
from PIL import UnidentifiedImageError

import modules.exceptions as exceptions

# import random
import modules.imgen as imgen
import modules.image_storage as image_storage
import modules.house_attributes as house_attributes
import modules.house_attributes_workers as house_attributes_workers
import modules.house_show as house_show
import modules.house_show_workers as house_show_workers
import modules.league_tokens as league_tokens
import modules.league_tokens_views as league_tokens_views
import modules.league_tokens_workers as league_tokens_workers
import modules.league_season as league_season
import modules.league_season_views as league_season_views
import modules.league_season_workers as league_season_workers
import modules.league_free_agents as league_free_agents
import modules.league_free_agent_reactions as league_free_agent_reactions
import modules.league_free_agents_views as league_free_agents_views
import modules.league_free_agents_workers as league_free_agents_workers
import modules.league_user_commands as league_user_commands
import modules.league_user_workers as league_user_workers
import modules.league_roster_cards as league_roster_cards
import modules.league_roster_cards_workers as league_roster_cards_workers
import modules.models as models
import modules.utilities as utilities
import settings
from modules import team_attributes as team_attributes_service
from modules import team_attributes_workers

logger = logging.getLogger('polybot.' + __name__)


grad_role_name = 'Nova Grad'           # met graduation requirements and is eligible to sign up for draft
free_agent_role_name = 'Free Agent'    # signed up for a prior draft but did not get drafted
novas_role_name = 'The Novas'          # Umbrella newbie role that all of above should also have
league_role_name = 'League Member'     # Umbrella role for all Pro+Junior members
pc_emoji = '<:PolyChampions:1327340966448730163>'
leader_role_name = 'House Leader'
coleader_role_name = 'House Co-Leader'
recruiter_role_name = 'House Recruiter'
captain_role_name = 'Team Captain'
mod_role_name = 'Mod'
league_helper_role_name = 'League Helper'

league_team_channels = []

ONE_WEEK = 60*60*24*7

def get_team_roles(guild=None):
    if not guild:
        guild = settings.bot.get_guild(settings.server_ids['polychampions']) or settings.bot.get_guild(settings.server_ids['test'])

    teams = models.Team.select(models.Team.name).where(
                (models.Team.guild_id == guild.id) & (models.Team.is_hidden == 0) & (models.Team.is_archived == 0)
            )
    
    team_names = [house.name for house in teams]
    team_roles = [discord.utils.get(guild.roles, name=r) for r in team_names]
    if None in team_roles:
        logger.warning(f'Problem loading at least one role in get_house_roles: {team_roles} / {team_names}')
    
    logger.debug(f'get_team_roles: {team_roles}')
    return team_roles

def get_tier_roles(guild=None):
    if not guild:
        guild = settings.bot.get_guild(settings.server_ids['polychampions']) or settings.bot.get_guild(settings.server_ids['test'])

    tier_names = [tier[1] for tier in settings.league_tiers]
    tier_roles = [discord.utils.get(guild.roles, name=f'{r} Player') for r in tier_names]
    
    if None in tier_roles:
        logger.warning(f'Problem loading at least one role in get_tier_roles: {tier_roles} / {tier_names}')
    
    logger.debug(f'get_tier_roles: {tier_roles}')
    return tier_roles

def get_house_roles(guild=None):
    houses = models.House.select(models.House.name)
    if not guild:
        guild = settings.bot.get_guild(settings.server_ids['polychampions']) or settings.bot.get_guild(settings.server_ids['test'])

    house_names = [house.name for house in houses]
    house_roles = [discord.utils.get(guild.roles, name=r) for r in house_names]
    if None in house_roles:
        logger.warning(f'Problem loading at least one role in get_house_roles: {house_roles} / {house_names}')
    
    logger.debug(f'get_house_roles: {house_roles}')
    return house_roles

def get_team_leadership(team):
    leaders, coleaders, recruiters, captains = [], [], [], []
    guild = settings.bot.get_guild(team.guild_id)

    house_role = utilities.guild_role_by_name(guild, name=team.house.name, allow_partial=False) if team.house else None
    team_role = utilities.guild_role_by_name(guild, name=team.name, allow_partial=False)
    leader_role = utilities.guild_role_by_name(guild, name=leader_role_name, allow_partial=False)
    coleader_role = utilities.guild_role_by_name(guild, name=coleader_role_name, allow_partial=False)
    recruiter_role = utilities.guild_role_by_name(guild, name=recruiter_role_name, allow_partial=False)
    captain_role = utilities.guild_role_by_name(guild, name=captain_role_name, allow_partial=False)
    # logger.debug(f'get_team_leadership: {leader_role} {coleader_role} {recruiter_role} {captain_role}')
    
    if house_role:
        for member in house_role.members:
            if leader_role in member.roles:
                leaders.append(member)
            if coleader_role in member.roles:
                coleaders.append(member)
            if recruiter_role in member.roles:
                recruiters.append(member)
    
    for member in team_role.members:
        if captain_role in member.roles:
            captains.append(member)

    # logger.debug(f'get_team_leadership: leaders {leaders} coleaders {coleaders} recruiters {recruiters} captains {captains}')
    return leaders, coleaders, recruiters, captains

async def update_member_league_roles(member):
    # TODO: This is not completed - partially completed in order to fix problem of league roles needing refreshing when a team
    # changes tier or house 
    # Update member's managed league roles (tier and house roles). This is triggered from on_member_update
    # if a member's -team- roles are changed, or triggered if the team they are in changes houses/tiers

    logger.debug(f'update_member_league_roles for member {member.name}')
    team_roles = get_team_roles(member.guild)
    league_role = discord.utils.get(member.guild.roles, name=league_role_name)
    player, team = None, None

    member_team_roles = [x for x in member.roles if x in team_roles]

    tier_roles = get_tier_roles(member.guild)
    house_roles = get_house_roles(member.guild)

    roles_to_remove = tier_roles + house_roles + [league_role]
    # Remove all managed league roles, then later will add back those needed 
    logger.debug(f'update_member_league_roles roles_to_remove: {roles_to_remove}')

    if member_team_roles:
        if len(member_team_roles) > 1:
            logger.warning(f'League.update_member_league_roles - more than one team role. Updated based on the first one found')
        try:
            player = models.Player.get_or_except(player_string=member.id, guild_id=member.guild.id)
            team = models.Team.get_or_except(team_name=member_team_roles[0].name, guild_id=member.guild.id)
            player.team = team
            player.save()
            models.PlayerHousePreference.clear_preferences(player.id)
            house_name = team.house.name if team.house else None
            team_tier = team.league_tier
            house_role = discord.utils.get(member.guild.roles, name=house_name) if house_name else None
            tier_role = tier_roles[team_tier - 1]
        except exceptions.NoSingleMatch as e:
            logger.warning(f'League.update_member_league_roles: could not load Player or Team for changing league member {member.display_name}: {e}')
            house_name, team_tier, house_role, tier_role = None, None, None, None

        roles_to_add = [house_role, tier_role, league_role]
        roles_to_remove = roles_to_remove + [r for r in member.roles if r.name.startswith('Prefers ')]
        logger.debug(f'roles_to_add: {roles_to_add}')
    else:
        roles_to_add = []  # No team role
        logger.debug(f'no roles_to_add due to no member_team_roles')

    member_roles = member.roles.copy()
    member_roles = [r for r in member_roles if r not in roles_to_remove]

    roles_to_add = [r for r in roles_to_add if r]  # remove any Nones

    if roles_to_add:
        member_roles = member_roles + roles_to_add

    logger.debug(f'Attempting to update member {member.display_name} role set to {member_roles} from old roles {member.roles}')
    # using member.edit() sets all the roles in one API call, much faster than using add_roles and remove_roles which uses one API call per role change, or two calls total if atomic=False
    await member.edit(roles=member_roles, reason='Refreshing member\'s league roles')


class league(commands.Cog):
    """
    Commands specific to the PolyChampions league, such as drafting-related commands
    """

    emoji_draft_signup = '🔆'
    emoji_draft_close = '⏯'
    emoji_draft_conclude = '❎'
    emoji_draft_list = [emoji_draft_signup, emoji_draft_close, emoji_draft_conclude]

    season_standings_cache = {}
    last_team_elos = defaultdict(lambda: [])

    house_group = discord.app_commands.Group(
        name='house',
        description='View PolyChampions Houses and their teams.',
        guild_only=True,
    )
    league_group = discord.app_commands.Group(
        name='league',
        description='View and manage PolyChampions league information.',
        guild_only=True,
    )
    league_free_agents_group = discord.app_commands.Group(
        name='free-agents',
        description='Manage Free Agent signup announcements.',
        parent=league_group,
        guild_only=True,
    )
    league_roster_group = discord.app_commands.Group(
        name='roster',
        description='Create league roster announcement cards.',
        parent=league_group,
        guild_only=True,
    )

    draft_open_format_str = f'The league is now open for Free Agent signups! {{0}}s can react with a {emoji_draft_signup} below to sign up. {{1}} who have not graduated have until the end of the signup period to meet requirements and sign up. If Free Agents have favorite teams, they may react to the team emojis in <#1489844936202260710> to note those preferences.\n\n{{3}}'
    draft_closed_message = f'The league is closed to new Free Agent signups. Mods can use the {emoji_draft_conclude} reaction to clean up and delete this message.'

    def __init__(self, bot):

        self.bot = bot
        self.announcement_message = None  # Will be populated from db if exists
        # self.auction_task.start()
        if settings.run_tasks:
            self.task_send_polychamps_invite.start()
            self.task_draft_reminders.start()

    async def cog_check(self, ctx):
        return ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']

    
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # if a a team role ('The Ronin') is added or removed, set or remove related roles on member (League Member, Pro Player, Ronin, etc)
        # this update will never touch a specific junior or pro team role ('The Ronin'), partially because that would trigger further on_member_updates

        # logger.debug(f'before roles: {before.roles} / after roles: {after.roles}')
        if before.roles == after.roles:
            return

        if after.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
            return

        # Check to see if Team roles changed
        team_roles = get_team_roles(after.guild)
        before_member_team_roles = [x for x in before.roles if x in team_roles]
        member_team_roles = [x for x in after.roles if x in team_roles]

        if before_member_team_roles == member_team_roles:
            return

        if len(member_team_roles) > 1:
            # If member has two team roles, usually they are in the process of having their roles edited in the UI
            return logger.debug(f'Member has more than one team role. Abandoning League.on_member_update. {member_team_roles}')

        await update_member_league_roles(after)
        # Edit after.roles with Tier/House roles that reflect current Team

        if member_team_roles:
            log_message = f'{models.GameLog.member_string(after)} had team role **{member_team_roles[0].name}** added.'
        else:
            log_message = f'{models.GameLog.member_string(after)} had team role **{before_member_team_roles[0].name}** removed and is teamless.'

        await utilities.send_to_log_channel(after.guild, log_message)
        models.GameLog.write(guild_id=after.guild.id, message=log_message)

    @commands.Cog.listener()
    async def on_ready(self):
        utilities.connect()
        guild_id = settings.server_ids['polychampions']
        if self.bot.user.id == 479029527553638401:
            guild_id = settings.server_ids['test']
        draft_state = await league_free_agents_workers.run_load_draft_state(guild_id)
        self.announcement_message = draft_state.announcement_message_id

        populate_league_team_channels()

        # global league_guild
        # league_guild = self.bot.get_guild(settings.server_ids['polychampions']) or self.bot.get_guild(settings.server_ids['test'])
        # print(league_guild)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        # Monitors all reactions being added to all messages, looking for reactions added to relevant league announcement messages
        if payload.message_id != self.announcement_message:
            return

        if payload.user_id == self.bot.user.id:
            return

        member = payload.member
        if member is None:
            return logger.warning(
                'Free Agent reaction add had no guild member for user %s',
                payload.user_id,
            )
        channel = member.guild.get_channel(payload.channel_id)
        if channel is None:
            return logger.warning(
                'Free Agent reaction channel %s is unavailable', payload.channel_id
            )
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException:
            return logger.warning(
                'Free Agent reaction message %s could not be loaded',
                payload.message_id,
                exc_info=True,
            )

        if payload.emoji.name not in self.emoji_draft_list:
            # Irrelevant reaction was added to relevant message. Clear it off.
            removal_emoji = self.bot.get_emoji(payload.emoji.id) if payload.emoji.id else payload.emoji.name

            try:
                await message.remove_reaction(removal_emoji, member)
                logger.debug(f'Removing irrelevant {payload.emoji.name} reaction placed by {member.name} on message {payload.message_id}')
            except discord.DiscordException as e:
                logger.debug(f'Unable to remove irrelevant reaction in on_raw_reaction_add(): {e}')
            return

        if payload.emoji.name == self.emoji_draft_signup:
            await self.signup_emoji_clicked(member, channel, message, reaction_added=True)
        elif payload.emoji.name == self.emoji_draft_close:
            await self.close_draft_emoji_added(member, channel, message)
        elif payload.emoji.name == self.emoji_draft_conclude:
            await self.conclude_draft_emoji_added(member, channel, message)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        # Monitors all reactions being removed from all messages, looking for reactions added to relevant league announcement messages

        if payload.message_id != self.announcement_message:
            return

        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.DiscordException:
                return logger.warning(
                    'Free Agent reaction-remove member %s could not be loaded',
                    payload.user_id,
                    exc_info=True,
                )
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.DiscordException:
            return logger.warning(
                'Free Agent reaction-remove message %s could not be loaded',
                payload.message_id,
                exc_info=True,
            )

        if payload.emoji.name not in self.emoji_draft_list:
            # Irrelevant reaction was removed
            pass
        if payload.emoji.name == self.emoji_draft_signup:
            await self.signup_emoji_clicked(member, channel, message, reaction_added=False)
        elif payload.emoji.name == self.emoji_draft_close:
            pass
        elif payload.emoji.name == self.emoji_draft_conclude:
            pass

    async def conclude_draft_emoji_added(self, member, channel, message):
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Conclude close reaction added by {member.name} to draft announcement {announce_message_link}')

        try:
            await message.remove_reaction(self.emoji_draft_conclude, member)
            logger.debug(f'Removing {self.emoji_draft_conclude} reaction placed by {member.name} on message {message.id}')
        except discord.DiscordException as e:
            logger.warning(f'Unable to remove reaction in conclude_draft_emoji_added(): {e}')

        if not settings.is_mod(member):
            return

        free_agent_role = discord.utils.get(member.guild.roles, name=free_agent_role_name)
        if free_agent_role is None:
            try:
                await member.send(
                    'The Free Agent role is missing, so the signup cannot be concluded.'
                )
            except discord.DiscordException:
                logger.warning(
                    'Could not notify moderator %s about missing Free Agent role',
                    member.id,
                )
            return

        confirm_message = await channel.send(f'{member.mention}, react below to confirm the conclusion of the current Free Agent signup. '
            f'{len(free_agent_role.members)} members currently have the Free Agent role. No role changes will result from closing the signup.\n'
            '*If you do not react within 30 seconds the signup will remain open.*', delete_after=35)
        await confirm_message.add_reaction('✅')

        logger.debug('waiting for reaction confirmation')

        def check(reaction, user):
            e = str(reaction.emoji)
            return ((user == member) and (reaction.message.id == confirm_message.id) and e == '✅')

        try:
            reaction, user = await self.bot.wait_for('reaction_add', check=check, timeout=33)

        except asyncio.TimeoutError:
            logger.debug('No reaction to confirmation message.')
            return

        await league_free_agent_reactions.conclude_signup(
            cog=self,
            member=member,
            channel=channel,
            message=message,
            free_agent_count=len(free_agent_role.members),
        )

    async def close_draft_emoji_added(self, member, channel, message):
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft close reaction added by {member.name} to draft announcement {announce_message_link}')
        await league_free_agent_reactions.toggle_signup_state(
            cog=self,
            member=member,
            channel=channel,
            message=message,
            close_emoji=self.emoji_draft_close,
            closed_message=self.draft_closed_message,
            open_format=self.draft_open_format_str,
            grad_role_name=grad_role_name,
            novas_role_name=novas_role_name,
            free_agent_role_name=free_agent_role_name,
        )

    async def signup_emoji_clicked(self, member, channel, message, reaction_added=True):
        await league_free_agent_reactions.handle_signup_reaction(
            member=member,
            channel=channel,
            message=message,
            reaction_added=bool(reaction_added),
            signup_emoji=self.emoji_draft_signup,
            grad_role_name=grad_role_name,
            free_agent_role_name=free_agent_role_name,
        )

    @commands.command(usage=None)
    # @settings.in_bot_channel_strict()
    async def tutorial(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        await ctx.send(league_user_commands.guide_message())

    @commands.command(usage=None)
    @settings.is_mod_check()
    async def newfreeagent(self, ctx, channel_override: typing.Optional[discord.TextChannel], *, added_message: str = ''):

        """
        *Mod:* Post a new Free Agent signup announcement

        Will post a default Free Agent signup announcement into a default announcement channel.

        Three emoji reactions are used to interact with the draft.
        The first can be used by any member who has the Nova Grad role, and they will receive the Free Agent role when they react. They can also unreact to lose the role.

        The play/pause reaction is mod-only and can be used to close or re-open the signup to new Nova Grads.
        A Free Agent member can remove themselves from the list while it is closed, but any new signups will be rejected.

        The ❎ reaction should be used by a mod after the draft has been performed and members have been put onto their new teams.
        Any current Free Agents will be remain Free Agents.

        Hitting this reaction will tell you exactly how many members will be affected by role changes and ask for a confirmation.

        You can optionally direct the announcement to a non-default channel, and add an optional message to the end of the announcement message.

        **Examples**
        `[p]newfreeagent` Normal usage with a generic message
        `[p]newfreeagent #special-channel` Direct message to a non-standard channel
        `[p]newfreeagent Signups will be closing on Sunday and the draft will occur the following Sunday` Add an extra message to the announcement.

        """

        announcement_channel = (
            channel_override or league_free_agents.default_channel(ctx.guild)
        )
        if announcement_channel is None:
            return await ctx.send(
                'The default Free Agent announcement channel is unavailable. '
                'Supply a channel explicitly.'
            )
        try:
            result = await league_free_agents.post_announcement(
                cog=self,
                guild=ctx.guild,
                actor=ctx.author,
                channel=announcement_channel,
                added_message=added_message,
            )
        except league_free_agents_workers.FreeAgentPostError as exc:
            return await ctx.send(str(exc))
        await ctx.send(
            f'Free Agent signup announcement posted and activated: '
            f'{result.message_link}'
        )

    @league_free_agents_group.command(
        name='post',
        description='Preview and post a new Free Agent signup announcement.',
    )
    @discord.app_commands.describe(
        channel='Destination channel; omit to use the configured default.',
    )
    async def league_free_agents_post_slash(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'Free Agent announcements require a server.', ephemeral=True
            )
        error = league_free_agents.access_error(interaction.user, guild.id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        announcement_channel = channel or league_free_agents.default_channel(guild)
        if announcement_channel is None:
            return await interaction.response.send_message(
                'The default Free Agent announcement channel is unavailable. '
                'Choose a destination channel explicitly.',
                ephemeral=True,
            )
        try:
            roles = league_free_agents.capture_roles(guild)
        except league_free_agents_workers.FreeAgentPostError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        async def confirm(_component_interaction, view):
            return await league_free_agents.post_announcement(
                cog=self,
                guild=guild,
                actor=interaction.user,
                channel=announcement_channel,
                added_message=view.added_message,
            )

        view = league_free_agents_views.FreeAgentPostView(
            requester_id=interaction.user.id,
            actor_mention=interaction.user.mention,
            channel=announcement_channel,
            roles=roles,
            confirmer=confirm,
        )
        try:
            await league_free_agents_views.open_initial_modal(interaction, view)
        except Exception:
            logger.exception('Could not open /league free-agents post modal')
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    'The announcement editor could not be opened. Try again.',
                    ephemeral=True,
                )

    @league_group.command(
        name='tokens',
        description='Browse House token balances/history or update one balance.',
    )
    @discord.app_commands.autocomplete(
        house=team_attributes_service.autocomplete_houses,
    )
    @discord.app_commands.describe(
        house='House to inspect or update; omit for all balances.',
        amount='New token balance; staff level 5+ only.',
        note='Optional audit note for a balance update.',
    )
    async def league_tokens_slash(
        self,
        interaction: discord.Interaction,
        house: str | None = None,
        amount: int | None = None,
        note: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'League token commands require a server.', ephemeral=True
            )
        error = league_tokens.native_access_error(interaction.user, guild.id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if amount is not None and house is None:
            return await interaction.response.send_message(
                'Choose a House when supplying a new token balance.', ephemeral=True
            )
        if note is not None and amount is None:
            return await interaction.response.send_message(
                'A token note is valid only when supplying a new balance.', ephemeral=True
            )

        actor = league_tokens.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        mutation = None
        try:
            current = await league_tokens.run_read(
                league_tokens.build_read_request(
                    member=interaction.user,
                    guild_id=guild.id,
                    house_lookup=house,
                )
            )
            banner = None
            if amount is not None:
                mutation = await league_tokens.run_mutation(
                    league_tokens.build_mutation_request(
                        member=interaction.user,
                        current=current,
                        new_balance=amount,
                        note=note,
                    )
                )
                current = league_tokens.apply_mutation(current, mutation)
                banner = league_tokens.mutation_banner(mutation, actor=actor)
            view = league_tokens_views.LeagueTokensWorkspace(
                result=current,
                requester_id=interaction.user.id,
                banner=banner,
            )
            await league_tokens_views.publish(interaction, view)
            return current
        except league_tokens_workers.LeagueTokensPublicationError as exc:
            if mutation is None:
                return await interaction.followup.send(str(exc), ephemeral=True)
            logger.exception(
                'Committed /league tokens update for House %s could not publish',
                mutation.house_id,
            )
            return await interaction.followup.send(
                'The token balance and audit entry were committed, but the public '
                'workspace could not be published. Do not retry the update; an '
                'operator should reconcile the public output.',
                ephemeral=True,
            )
        except league_tokens_workers.LeagueTokensError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /league tokens')
            await interaction.followup.send(
                'League token operation failed and rolled back.', ephemeral=True
            )
        except Exception:
            logger.exception('Unexpected failure in /league tokens')
            if mutation is not None:
                return await interaction.followup.send(
                    'The token update committed, but its public output failed. '
                    'Do not retry the update; an operator should reconcile it.',
                    ephemeral=True,
                )
            await interaction.followup.send(
                'League token output could not be completed.', ephemeral=True
            )

    @league_group.command(
        name='guide',
        description='Show the PolyChampions quick-start guide.',
    )
    async def league_guide_slash(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not league_user_commands.league_scope(guild.id):
            return await interaction.response.send_message(
                'This guide is available only in the configured league server.',
                ephemeral=True,
            )
        await interaction.response.send_message(
            league_user_commands.guide_message()
        )

    @league_group.command(
        name='mark-active',
        description='Remove the Inactive role from yourself or an allowed member.',
    )
    @discord.app_commands.describe(
        member='Member to mark active; omit to target yourself.',
    )
    async def league_mark_active_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        guild = interaction.guild
        if guild is None or not league_user_commands.league_scope(guild.id):
            return await interaction.response.send_message(
                'This command is available only in the configured league server.',
                ephemeral=True,
            )
        target = member or interaction.user
        if not league_user_commands.can_target_mark_active(
            interaction.user, target
        ):
            return await interaction.response.send_message(
                'You must be a House Leader, Co-Leader, or Mod to use this '
                'on another player.',
                ephemeral=True,
            )
        role = league_user_commands.inactive_role(guild)
        if role is None:
            logger.warning('Could not load configured Inactive role in guild %s', guild.id)
            return await interaction.response.send_message(
                'Error loading the configured Inactive role.', ephemeral=True
            )
        if role not in target.roles:
            return await interaction.response.send_message(
                f'{target.mention} does not have the *{role.name}* role.',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await interaction.response.defer(ephemeral=True)
        try:
            await target.remove_roles(
                role,
                reason=f'Marked active by {interaction.user} ({interaction.user.id})',
            )
        except discord.DiscordException:
            logger.exception('Discord role failure in /league mark-active')
            return await interaction.followup.send(
                'The Inactive role could not be removed.', ephemeral=True
            )
        try:
            await league_user_commands.public_sender(interaction)(
                league_user_commands.mark_active_success(
                    actor=interaction.user,
                    target=target,
                    role_name=role.name,
                    native=True,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception(
                'Committed /league mark-active role change could not publish'
            )
            await interaction.followup.send(
                'The Inactive role was removed, but the public confirmation '
                'could not be posted. Do not retry; staff may need to '
                'reconcile the announcement.',
                ephemeral=True,
            )

    @league_group.command(
        name='join-novas',
        description='Join the PolyChampions starter group.',
    )
    async def league_join_novas_slash(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not league_user_commands.league_scope(guild.id):
            return await interaction.response.send_message(
                'This command is available only in the configured league server.',
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            eligibility = await league_user_commands.run_join_check(
                interaction.user, guild
            )
            if not eligibility.registered:
                return await interaction.followup.send(
                    'You are not registered with the bot. Use `/player register` '
                    'first.',
                    ephemeral=True,
                )
            if eligibility.team_roles_truncated:
                return await interaction.followup.send(
                    'The configured team list is too large to validate safely. '
                    'Ask staff to review the league configuration.',
                    ephemeral=True,
                )
            team = league_user_commands.matching_team(
                eligibility, interaction.user
            )
            if team is not None:
                return await interaction.followup.send(
                    f'You are already a member of team *{team.name}* '
                    f'{team.emoji}. Server staff is required to remove you '
                    'from a team.',
                    ephemeral=True,
                )
            novas_role = discord.utils.get(
                guild.roles, name=league_user_commands.NOVAS_ROLE_NAME
            )
            if novas_role is None:
                logger.warning('Could not load The Novas role in guild %s', guild.id)
                return await interaction.followup.send(
                    'Error finding the **The Novas** role.', ephemeral=True
                )
            newbie_role = discord.utils.get(
                guild.roles, name=league_user_commands.NEWBIE_ROLE_NAME
            )
            await interaction.user.add_roles(
                novas_role,
                reason=f'Joined via /league join-novas by {interaction.user.id}',
            )
            warning = None
            if newbie_role is not None and newbie_role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(
                        newbie_role,
                        reason='Joining Novas',
                    )
                except discord.DiscordException:
                    logger.exception(
                        'Joined Novas but could not remove Newbie from member %s',
                        interaction.user.id,
                    )
                    warning = (
                        '\nThe Novas role was added, but the Newbie role could '
                        'not be removed; staff may need to reconcile it.'
                    )
        except league_user_workers.LeagueUserError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /league join-novas')
            return await interaction.followup.send(
                'Your league eligibility could not be checked.', ephemeral=True
            )
        except discord.DiscordException:
            logger.exception('Discord role failure in /league join-novas')
            return await interaction.followup.send(
                'The Novas role could not be added.', ephemeral=True
            )
        except Exception:
            logger.exception('Unexpected failure in /league join-novas')
            return await interaction.followup.send(
                'The Novas join could not be completed.', ephemeral=True
            )
        try:
            await league_user_commands.public_sender(interaction)(
                league_user_commands.join_success(
                    member=interaction.user,
                    native=True,
                ) + (warning or ''),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception(
                'Committed /league join-novas role change could not publish'
            )
            await interaction.followup.send(
                'The Novas role was added, but the public confirmation could '
                'not be posted. Do not retry; staff may need to reconcile the '
                'announcement.',
                ephemeral=True,
            )

    @league_group.command(
        name='season',
        description='Show team records for one league season or all seasons.',
    )
    @discord.app_commands.describe(
        season='Season number; omit for all recorded seasons.',
    )
    async def league_season_slash(
        self,
        interaction: discord.Interaction,
        season: int | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'League season records require a server.', ephemeral=True
            )
        error = league_season.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        request = league_season.build_request(
            member=interaction.user,
            guild_id=guild.id,
            channel_id=interaction.channel_id,
            season=season,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await league_season_workers.run_league_season(request)
            view = league_season_views.LeagueSeasonWorkspace(
                result=result,
                requester_id=interaction.user.id,
            )
            await league_season_views.publish(interaction, view)
        except league_season_views.LeagueSeasonPublicationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except league_season_workers.LeagueSeasonError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /league season')
            await interaction.followup.send(
                'League season records could not be loaded.', ephemeral=True
            )
        except Exception:
            logger.exception('Unexpected failure in /league season')
            await interaction.followup.send(
                'League season output could not be completed.', ephemeral=True
            )

    @commands.command()
    @settings.on_polychampions()
    async def imalive(self, ctx, *, member: discord.Member = None):
        """Remove your own Inactive role. Leaders/Co-Leaders/Mods can target another player."""
        target = member or ctx.author
        if not league_user_commands.can_target_mark_active(ctx.author, target):
            return await ctx.send('You must be a House Leader, Co-Leader, or Mod to use this on another player.')

        inactive_role = league_user_commands.inactive_role(ctx.guild)
        if not inactive_role:
            logger.warning(f'Could not load Inactive role by name {settings.guild_setting(ctx.guild.id, "inactive_role")}')
            return await ctx.send('Error loading Inactive role.')

        if inactive_role not in target.roles:
            return await ctx.send(
                f'{target.mention} does not have the *{inactive_role.name}* role.',
                allowed_mentions=discord.AllowedMentions.none()
            )

        await target.remove_roles(inactive_role, reason=f'Removed via imalive by {ctx.author.name}')
        await ctx.send(
            league_user_commands.mark_active_success(
                actor=ctx.author,
                target=target,
                role_name=inactive_role.name,
                native=False,
            ),
            allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(usage='House Name')
    @settings.in_bot_channel()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def house(self, ctx, *, arg=None):
        """
        Details on a House structure
        See also `[p]houses`
        **Examples**
        `[p]house ronin`
        """

        if not arg:
            return await ctx.send(
                f'House name not provided. *Example:* '
                f'`{ctx.prefix}{ctx.invoked_with} ronin`'
            )
        request = house_show.build_request(
            member=ctx.author,
            guild=ctx.guild,
            house_lookup=arg,
            require_selection=True,
            channel_id=ctx.channel.id,
        )
        try:
            async with ctx.typing():
                result = await house_show_workers.run_house_show(request)
        except house_show_workers.HouseShowError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure reading House %s', arg)
            return await ctx.send('House details could not be loaded.')
        await utilities.buffered_send(
            destination=ctx,
            content=house_show.render_prefix_house(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @house_group.command(
        name='show',
        description='Show one House, its leadership, teams, ELO, and roster.',
    )
    @discord.app_commands.describe(
        house='House to show; omit to infer your exact House role.',
    )
    @discord.app_commands.autocomplete(
        house=team_attributes_service.autocomplete_houses,
    )
    async def house_show_slash(
        self,
        interaction: discord.Interaction,
        house: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'House commands require a server.', ephemeral=True
            )
        error = house_show.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        request = house_show.build_request(
            member=interaction.user,
            guild=guild,
            house_lookup=house,
            require_selection=True,
            channel_id=interaction.channel_id,
        )
        try:
            result = await house_show_workers.run_house_show(request)
            await house_show.publish_native(
                interaction,
                result,
                detail_house_id=result.selected_house_id,
            )
        except house_show_workers.HouseShowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /house show')
            await interaction.followup.send(
                'House details could not be loaded.', ephemeral=True
            )

    @commands.command(usage='', aliases=['balance'])
    @settings.in_bot_channel()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def houses(self, ctx, *, arg=None):
        """
        Summarize League structure
        See also `[p]house house_name`
        **Examples**
        `[p]houses`
        """
        
        request = house_show.build_request(
            member=ctx.author,
            guild=ctx.guild,
            house_lookup=None,
            require_selection=False,
            channel_id=ctx.channel.id,
        )
        try:
            async with ctx.typing():
                result = await house_show_workers.run_house_show(request)
        except house_show_workers.HouseShowError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure reading House directory')
            return await ctx.send('The House directory could not be loaded.')
        await utilities.buffered_send(
            destination=ctx,
            content=house_show.render_prefix_list(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @house_group.command(
        name='list',
        description='Browse all configured Houses and their active teams.',
    )
    async def house_list_slash(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'House commands require a server.', ephemeral=True
            )
        error = house_show.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        request = house_show.build_request(
            member=interaction.user,
            guild=guild,
            house_lookup=None,
            require_selection=False,
            channel_id=interaction.channel_id,
        )
        try:
            result = await house_show_workers.run_house_show(request)
            await house_show.publish_native(
                interaction,
                result,
                detail_house_id=None,
            )
        except house_show_workers.HouseShowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /house list')
            await interaction.followup.send(
                'The House directory could not be loaded.', ephemeral=True
            )

    @house_group.command(
        name='create',
        description='Create a new league House.',
    )
    @discord.app_commands.describe(
        name='Required unique House name; create its exact Discord role separately.',
    )
    async def house_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'House commands require a server.', ephemeral=True
            )
        error = house_attributes.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
            mutation=True,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        actor = house_attributes.capture_actor(interaction.user)
        request = house_attributes.build_creation_request(
            member=interaction.user,
            guild_id=guild.id,
            channel_id=interaction.channel_id,
            name=name,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            result = await house_attributes.run_creation(request)
        except house_attributes_workers.HouseAttributeError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /house create')
            return await interaction.followup.send(
                'House creation failed and rolled back.', ephemeral=True
            )
        except Exception:
            logger.exception('Unexpected pre-commit failure in /house create')
            return await interaction.followup.send(
                'House creation failed before completion.', ephemeral=True
            )
        try:
            await house_attributes.publish_creation(
                result,
                send=house_attributes.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except Exception:
            logger.exception(
                'Committed House creation %s could not publish',
                result.house_id,
            )
            return await interaction.followup.send(
                'The House may have been created, but its public confirmation '
                'could not be published. An operator must reconcile it before '
                'retrying.',
                ephemeral=True,
            )

    @house_group.command(
        name='name',
        description='View or update a House name.',
    )
    @discord.app_commands.autocomplete(
        house=team_attributes_service.autocomplete_houses,
    )
    @discord.app_commands.describe(
        house='House to inspect; omit to infer your exact House role.',
        name='New required House name; omit to view the current name.',
    )
    async def house_name_slash(
        self,
        interaction: discord.Interaction,
        house: str | None = None,
        name: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'House commands require a server.', ephemeral=True
            )
        mutation = name is not None
        error = house_attributes.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
            mutation=mutation,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        actor = house_attributes.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            current = await house_attributes.run_read(
                house_attributes.build_read_request(
                    member=interaction.user,
                    guild_id=guild.id,
                    channel_id=interaction.channel_id,
                    house_lookup=house,
                    attribute=house_attributes_workers.HOUSE_ATTRIBUTE_NAME,
                )
            )
            send = house_attributes.public_interaction_sender(interaction)
            if not mutation:
                await house_attributes.publish_read(current, send=send, actor=actor)
                return current
            result = await house_attributes.run_mutation(
                house_attributes.build_mutation_request(
                    member=interaction.user,
                    current=current,
                    attribute=house_attributes_workers.HOUSE_ATTRIBUTE_NAME,
                    value=name,
                )
            )
            await house_attributes.publish_mutation(result, send=send, actor=actor)
            return result
        except house_attributes_workers.HouseAttributeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /house name')
            await interaction.followup.send(
                'House name operation failed and rolled back.', ephemeral=True
            )

    @house_group.command(
        name='image',
        description='View, replace, or clear a House image.',
    )
    @discord.app_commands.autocomplete(
        house=team_attributes_service.autocomplete_houses,
    )
    @discord.app_commands.describe(
        house='House to inspect; omit to infer your exact House role.',
        image='PNG, JPEG, or WebP attachment; omit to view the current image.',
        clear='Explicitly clear the House image.',
    )
    async def house_image_slash(
        self,
        interaction: discord.Interaction,
        house: str | None = None,
        image: discord.Attachment | None = None,
        clear: bool = False,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                'House commands require a server.', ephemeral=True
            )
        mutation = image is not None or clear
        error = house_attributes.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
            mutation=mutation,
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        if image is not None and clear:
            return await interaction.response.send_message(
                'Choose either an image replacement or `clear`, not both.',
                ephemeral=True,
            )
        actor = house_attributes.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            current = await house_attributes.run_read(
                house_attributes.build_read_request(
                    member=interaction.user,
                    guild_id=guild.id,
                    channel_id=interaction.channel_id,
                    house_lookup=house,
                    attribute=house_attributes_workers.HOUSE_ATTRIBUTE_IMAGE,
                )
            )
            send = house_attributes.public_interaction_sender(interaction)
            if not mutation:
                await house_attributes.publish_read(current, send=send, actor=actor)
                return current
            staged = None
            operation = house_attributes_workers.HOUSE_IMAGE_CLEAR
            if image is not None:
                staged = await house_attributes.stage_attachment(
                    image,
                    house_id=current.house_id,
                )
                operation = house_attributes_workers.HOUSE_IMAGE_LOCAL
            result = await house_attributes.run_mutation(
                house_attributes.build_mutation_request(
                    member=interaction.user,
                    current=current,
                    attribute=house_attributes_workers.HOUSE_ATTRIBUTE_IMAGE,
                    image_operation=operation,
                    staged_path=(staged.path if staged is not None else None),
                ),
                staged=staged,
            )
            await house_attributes.publish_mutation(result, send=send, actor=actor)
            return result
        except house_attributes.HouseImagePublicationError as exc:
            logger.exception(
                'Committed House image %s requires reconciliation',
                exc.result.house_id,
            )
            warning = house_attributes.publication_failure_message(exc, actor=actor)
            try:
                await house_attributes.public_interaction_sender(interaction)(warning)
            except Exception:
                logger.exception('House image reconciliation warning failed')
                await interaction.followup.send(warning, ephemeral=True)
        except (
            house_attributes_workers.HouseAttributeError,
            house_attributes.HouseImageDownloadError,
            image_storage.ImageStorageError,
        ) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in /house image')
            await interaction.followup.send(
                'House image operation failed and rolled back.', ephemeral=True
            )
    
    @commands.command(hidden=True)
    @settings.is_mod_check()
    async def gtest(self, ctx, *, arg=None):
        args = arg.split() if arg else []
        game = models.Game.get(135855)
        logger.debug(f'calling gtest on game {game.id}')
        await auto_grad_novas(guild=ctx.guild, game=game, output_channel=ctx)

        return

        # total_games = (models.GameSide
        #            .select()
        #            .join(models.Game)
        #            .where((models.GameSide.team_id == team_id) &
        #                   (models.Game.league_season == league_season))
        #            )
        
        # for g in total_games:
        #     print(g.id, g.game.id, g.game.name)
        # await ctx.send(len(total_games))

        team = models.Team.get(team_id)
        # records = team.get_season_record(season=league_season)

        records = team.get_tier_season_records(guild_id=447883341463814144, league_tier=2, league_season=league_season)
        # records = models.Team.get_tier_season_records(guild_id=447883341463814144, league_tier=2, league_season=league_season)
        print(records)
        print(len(records))
        for record in records:
            print(record.name, record.id, record.emoji, record.regular_season_wins, record.regular_season_losses, record.regular_season_incomplete, record.post_season_wins, record.post_season_losses, record.post_season_incomplete)
    
    @commands.command(aliases=['team_tier'], usage='team_name arguments')
    @settings.is_mod_check()
    async def team_edit(self, ctx, *, arg=None):
        """*Mod*: Edit a team's league tier or archive it
        **Example:**
        `/team house team:ronin house:Ninjas` - Put team Ronin into a House
        `/team house team:ronin clear:true` - Remove team Ronin from its House affiliation
        `[p]team_edit ronin ARCHIVE` - Mark a defunct team as archived. This cannot be undone via the bot. Team must first have no house affiliation and no incomplete games.
        `[p]team_tier ronin gold` - Change league tier of team. Does not impact current or past games from this team.
        
        See also: `/team house`, `team_add`, `team_name`, `team_server`, `team_image`, `team_emoji`, `/house create`, `/house name`
        """
        args = arg.split() if arg else []
        if not args or len(args) != 2:
            return await ctx.send(f'See `{ctx.prefix}help {ctx.invoked_with}` for usage examples. Teams and Houses must be each identified by a single word.')

        if ctx.invoked_with == 'team_tier':
            try:
                preflight = await team_attributes_service.run_tier_preflight(
                    member=ctx.author,
                    guild=ctx.guild,
                    team_lookup=args[0],
                    invoked_with=ctx.invoked_with,
                )
                request = team_attributes_service.build_mutation_request(
                    member=ctx.author,
                    guild_id=ctx.guild.id,
                    attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
                    team_lookup=args[0],
                    tier=args[1],
                    expected_team_id=preflight.current.team_id,
                    expected_value=preflight.current.value,
                    expected_value_present=True,
                    team_role_id=preflight.team_role_id,
                    team_role_name=preflight.team_role_name,
                    team_member_ids=getattr(preflight, 'member_ids', ()),
                    native=False,
                    invoked_with=ctx.invoked_with,
                    prefix=ctx.prefix,
                )
                result = await team_attributes_service.run_mutation(request)
            except team_attributes_workers.TeamAttributeValidationError as ex:
                return await ctx.send(str(ex))
            except peewee.PeeweeException:
                logger.exception(
                    'Database failure updating team tier for guild %s',
                    ctx.guild.id,
                )
                return await ctx.send('Team tier operation failed and rolled back.')
            except Exception:
                logger.exception(
                    'Unexpected team tier failure for guild %s',
                    ctx.guild.id,
                )
                return await ctx.send('Team tier operation failed and rolled back.')

            try:
                reconciliation = await team_attributes_service.reconcile_tier_roles(
                    ctx.guild,
                    result,
                )
            except Exception:
                logger.exception(
                    'Committed team tier %s could not reconcile roles',
                    result.team_id,
                )
                reconciliation = team_attributes_service.TierRoleReconciliation(
                    team_id=result.team_id,
                    attempted=0,
                    updated=0,
                    team_role_missing=True,
                )
            await team_attributes_service.publish_mutation_result(
                result,
                send=ctx.send,
                reconciliation=reconciliation,
            )
            return result

        if ctx.invoked_with == 'team_edit' and args[1] != 'ARCHIVE':
            return await ctx.send(
                f'House changes now use `/team house`. Use '
                f'`/team house team:{args[0]} house:{args[1]}` to assign a '
                'House or `clear:true` to remove the affiliation.'
            )

        try:
            team = models.Team.get_or_except(team_name = args[0], guild_id=ctx.guild.id)
        except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
            return await ctx.send(e)

        logger.debug(f'Loaded team {team.name} for editing')
        team_role = utilities.guild_role_by_name(ctx.guild, name=team.name, allow_partial=False)
        if not team_role:
            return await ctx.send(f':warning: No role matching **{team.name}**. It must have a role to edit team properties. ')

        if team.is_archived:
            logger.warn('Team is_archive is True')
            return await ctx.send(f'Team **{team.name}** is **archived**. If it *really* needs to be unarchived, ask the bot owner.')

        if ctx.invoked_with == 'team_edit' and args[1] == 'ARCHIVE':
            logger.debug(f'Attempting to archive team {team.name}')
            if team.house:
               logger.warn(f'Cannot archive due to house affiliation')
               return await ctx.send(
                   f'Remove the House affiliation of team **{team.name}** '
                   f'first with `/team house team:{args[0]} clear:true`. '
                   f'Currently in {team.house.name}.'
               )
            incomplete_game_count = models.Game.search(team_filter=[team], status_filter=2).count()
            if incomplete_game_count > 0:
                logger.warn(f'Cannot archive due to {incomplete_game_count} incomplete games')
                return await ctx.send(f'Team **{team.name}** has {incomplete_game_count} incomplete games. Cannot archive unless there are zero incomplete games.')
            
            team.is_archived = True
            team.save()
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} archived Team {team.name} ID {team.id}')
            return await ctx.send(f':warning: Team **{team.name}** has been successfully **archived**. May it be long remembered, but never again used.')
        
        return await ctx.send(f'See `{ctx.prefix}help {ctx.invoked_with}` for usage examples. Teams and Houses must be each identified by a single word.')
    

    @commands.command(aliases=['jrseason', 'ps', 'js', 'seasonjr'], usage='[season #]')
    @settings.in_bot_channel()
    async def season(self, ctx, *, season: str = None):
        """
        Display team records for one or all seasons. All active tiers that participated in that season will be shown.

        **Examples**
        `[p]season` Records for all seasons
        `[p]season 14` Records for a specific season
        """

        # TODO: Could add option for `$season teamname` to show season record history for a team
        if season:
            try:
                season = int(season)
            except ValueError:
                return await ctx.send(f'Invalid argument. Leave blank for all seasons or use an integer like `{ctx.prefix}{ctx.invoked_with} 13`')

        request = league_season.build_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            season=season,
        )
        async with ctx.typing():
            try:
                result = await league_season_workers.run_league_season(request)
            except league_season_workers.LeagueSeasonError as exc:
                return await ctx.send(str(exc))
            except peewee.PeeweeException:
                logger.exception('Database failure in prefix season command')
                return await ctx.send('League season records could not be loaded.')
            await utilities.buffered_send(
                destination=ctx,
                content=league_season.legacy_text(result),
            )


    @commands.command(aliases=['joinnovas'])
    async def novas(self, ctx, *, arg=None):
        """ Join yourself to the Novas team
        """

        try:
            eligibility = await league_user_commands.run_join_check(
                ctx.author, ctx.guild
            )
        except (league_user_workers.LeagueUserError, peewee.PeeweeException):
            logger.exception('Eligibility failure in prefix novas command')
            return await ctx.send('Your league eligibility could not be checked.')
        if not eligibility.registered:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'*{ctx.author.name}* was found in the server but is not registered with me. '
                f'Players can register themselves with `{ctx.prefix}setname Your Mobile Name`.')

        if eligibility.team_roles_truncated:
            return await ctx.send(
                'The configured team list is too large to validate safely. '
                'Ask staff to review the league configuration.'
            )

        player_team = league_user_commands.matching_team(eligibility, ctx.author)
        if player_team is not None:
            return await ctx.send(f'You are already a member of team *{player_team.name}* {player_team.emoji}. Server staff is required to remove you from a team.')

        novas_role = discord.utils.get(
            ctx.guild.roles, name=league_user_commands.NOVAS_ROLE_NAME
        )
        newbie_role = discord.utils.get(
            ctx.guild.roles, name=league_user_commands.NEWBIE_ROLE_NAME
        )

        if not novas_role:
            return await ctx.send('Error finding Novas role. Searched *The Novas*.')


        await ctx.author.add_roles(novas_role, reason='Joining Novas')
        await ctx.send(
            league_user_commands.join_success(
                member=ctx.author,
                native=False,
                prefix=ctx.prefix,
            )
        )

        if newbie_role:
            await ctx.author.remove_roles(newbie_role, reason='Joining Novas')

    async def _publish_roster_card(
        self,
        interaction: discord.Interaction,
        request: league_roster_cards_workers.RosterCardRequest,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await league_roster_cards.run_roster_card(request)
            await league_roster_cards.public_interaction_sender(interaction)(
                league_roster_cards.public_caption(
                    interaction.user, request.mode
                ),
                file=league_roster_cards.discord_file(result),
            )
            return result
        except league_roster_cards_workers.RosterCardError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except (imgen.ImageFetchError, UnidentifiedImageError):
            logger.exception('Could not fetch or decode a roster-card image')
            await interaction.followup.send(
                'One of the images could not be retrieved or decoded. Use a '
                'direct HTTP(S) image URL or choose another source.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected roster-card rendering failure')
            await interaction.followup.send(
                'The roster card could not be generated. Try again later.',
                ephemeral=True,
            )

    @league_roster_group.command(
        name='promote',
        description='Create a player promotion announcement card.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        player='Player being promoted.',
        team='Destination team whose stored image is used.',
        top_text='Optional headline (default: PROMOTION).',
        bottom_text='Optional footer (default: destination team).',
        player_image_url='Advanced: direct HTTP(S) override for the player image.',
        team_image_url='Advanced: direct HTTP(S) override for the team image.',
    )
    async def league_roster_promote_slash(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        team: str,
        top_text: str | None = None,
        bottom_text: str | None = None,
        player_image_url: str | None = None,
        team_image_url: str | None = None,
    ):
        guild = interaction.guild
        channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
        if guild is None:
            return await interaction.response.send_message(
                'Roster cards require a server.', ephemeral=True
            )
        error = league_roster_cards.access_error(
            interaction.user, guild.id, channel_id
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        roster_request = league_roster_cards.request(
            guild=guild,
            mode='promote',
            top_text=(top_text if top_text is not None else 'PROMOTION'),
            bottom_text=(bottom_text if bottom_text is not None else team),
            left=league_roster_cards.raw_or_avatar(player_image_url, player),
            right=league_roster_cards.raw_or_team(team_image_url, team),
        )
        return await self._publish_roster_card(interaction, roster_request)

    @league_roster_group.command(
        name='trade',
        description='Create a two-player trade announcement card.',
    )
    @discord.app_commands.describe(
        left_player='Player displayed on the left.',
        right_player='Player displayed on the right.',
        top_text='Optional headline (default: TRADE).',
        bottom_text='Optional footer (default: ROSTER UPDATE).',
        left_image_url='Advanced: direct HTTP(S) override for the left image.',
        right_image_url='Advanced: direct HTTP(S) override for the right image.',
    )
    async def league_roster_trade_slash(
        self,
        interaction: discord.Interaction,
        left_player: discord.Member,
        right_player: discord.Member,
        top_text: str | None = None,
        bottom_text: str | None = None,
        left_image_url: str | None = None,
        right_image_url: str | None = None,
    ):
        guild = interaction.guild
        channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
        if guild is None:
            return await interaction.response.send_message(
                'Roster cards require a server.', ephemeral=True
            )
        error = league_roster_cards.access_error(
            interaction.user, guild.id, channel_id
        )
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        roster_request = league_roster_cards.request(
            guild=guild,
            mode='trade',
            top_text=(top_text if top_text is not None else 'TRADE'),
            bottom_text=(
                bottom_text if bottom_text is not None else 'ROSTER UPDATE'
            ),
            left=league_roster_cards.raw_or_avatar(left_image_url, left_player),
            right=league_roster_cards.raw_or_avatar(right_image_url, right_player),
        )
        return await self._publish_roster_card(interaction, roster_request)

    @commands.command(usage='', aliases=['trade'])
    @settings.is_staff_check()
    @settings.in_bot_channel_strict()
    async def promote(self, ctx, *, args=None):
        """
        *Helper:* Generate a trade or promotion image

        Requires four arguments:
        - Top text (Use "quotation marks" if more than one word. Use 'none' to leave blank.)
        - Bottom text (same)
        - Left box image
        - Right box image

        A box can be any one of the following:
        - An image URL
        - A member mention, which will use the member's avatar
        - A team name, which will use the team image

        **Examples**
        `[p]promote Promotion "to Ronin" @nelluk Ronin`
        `[p]trade "Bombers Trade" "With Crawfish" @jd @luna`
        """

        import shlex
        args = args.replace("'", "\\'").replace("“", "\"").replace("”", "\"") if args else ''  # Escape single quotation marks for shlex.split() parsing
        try:
            args = shlex.split(args)
        except ValueError as e:
            return await ctx.send(f'Error parsing arguments: {e}')

        if len(args) != 4:
            return await ctx.send(f'Usage error (expected 4 arguments and found {len(args)})\n**Example**: `{ctx.prefix}{ctx.invoked_with} "Top Text" "Bottom Text" @PromotedPlayer Ronin`')

        top_string = '' if args[0].upper() == 'NONE' else args[0]
        bottom_string = '' if args[1].upper() == 'NONE' else args[1]

        try:
            left = await league_roster_cards.prefix_lookup_source(ctx, args[2])
            right = await league_roster_cards.prefix_lookup_source(ctx, args[3])
            mode = 'trade' if ctx.invoked_with.casefold() == 'trade' else 'promote'
            result = await league_roster_cards.run_roster_card(
                league_roster_cards.request(
                    guild=ctx.guild,
                    mode=mode,
                    top_text=top_string,
                    bottom_text=bottom_string,
                    left=left,
                    right=right,
                )
            )
        except league_roster_cards_workers.RosterCardError as exc:
            return await ctx.send(str(exc))
        except imgen.ImageFetchError as exc:
            logger.warning('Unable to create promotion card: %s', exc)
            return await ctx.send(
                'Unable to retrieve one of the images. Please try again later '
                'or use another image URL.'
            )
        except UnidentifiedImageError as exc:
            logger.warning('UnidentifiedImageError: %s', exc)
            return await ctx.send(
                'Image is formatted incorrectly. Use an image URL that links '
                f'directly to a file. {exc}'
            )
        await ctx.send(file=league_roster_cards.discord_file(result))

    @commands.command(usage='@Draftee TeamName')
    @settings.draft_check()
    # @settings.in_bot_channel_strict()
    async def draft(self, ctx, *, args=None):
        """
        *Mod:* Generate a draft announcement image
        Currently will not alter any roles or do anything other than display an image.

        **Examples**
        `[p]draft` @Nelluk Ronin
        """
        args = args.split() if args else []
        usage = (f'**Example usage:** `{ctx.prefix}draft @Nelluk Ronin`')

        if len(args) < 2:
            return await ctx.send(f'Insufficient arguments.\n{usage}')
        draftee = ctx.guild.get_member(utilities.string_to_user_id(args[0]))
        if not draftee:
            return await ctx.send(f'Could not find server member from **{args[0]}**. Make sure to use a @Mention.\n{usage}')

        try:
            team = models.Team.get_or_except(team_name=' '.join(args[1:]), guild_id=ctx.guild.id)
        except exceptions.NoSingleMatch as e:
            return await ctx.send(f'Error looking up team: {e}\n{usage}')

        if not image_storage.resolve_image('team', team):
            return await ctx.send(f'Team **{team.name}** does not have an image set. Use `{ctx.prefix}team_image` first.')
        draft_team_role = utilities.guild_role_by_name(ctx.guild, name=team.name, allow_partial=False)
        if not draft_team_role:
            return await ctx.send(f'Found matching team but no matching role with name *{team.name}*!')

        if team.house:
            house_roles = [hr for hr in get_house_roles() if hr and hr.name == team.house.name]
            house_role = house_roles[0] if house_roles else None
        else:
            house_role = None

        selecting_string = house_role.name if house_role else draft_team_role.name
        try:
            fs = await asyncio.to_thread(
                imgen.player_draft_card,
                member=draftee,
                team_role=draft_team_role,
                selecting_string=selecting_string,
            )
        except imgen.ImageFetchError as exc:
            logger.warning('Unable to create draft card: %s', exc)
            return await ctx.send('Unable to retrieve one of the draft card images. Please try again later.')
        except UnidentifiedImageError as exc:
            logger.warning(f'UnidentifiedImageError: {exc}')
            return await ctx.send(f'An image is formatted incorrectly: {exc}')

        await ctx.send(file=fs)

    @commands.command(aliases=['playerprice'], hidden=True)
    async def tradeprice(self, ctx, season: typing.Optional[int], *, player_name: str):
        """Calculate a player's trade price

        **Examples:**
        `[p]tradeprice Nelluk`
        """
        guild_matches = await utilities.get_guild_member(ctx, player_name)
        if len(guild_matches) > 1:
            return await ctx.send(f'There is more than one player found with name "{player_name}". Try specifying with a @Mention.')
        elif len(guild_matches) == 0:
            return await ctx.send(f'Could not find "{player_name}" on this server.')
        else:
            member = guild_matches[0]

        player, _ = models.Player.get_by_discord_id(discord_id=member.id, discord_name=member.name, discord_nick=member.nick, guild_id=ctx.guild.id)
        if not player:
            # Mention user without pinging him
            return await ctx.send(f'*{member.mention}* is not registered in the bot.', allowed_mentions=discord.AllowedMentions.none())

        if not season:
            current_season = models.Game.select(peewee.fn.MAX(models.Game.league_season)).scalar()
            incomplete_games = models.Game.search(player_filter=[player], status_filter=2, season_filter=current_season).count()
            logger.debug(f'Incomplete games for player {player}: {incomplete_games}')
            if incomplete_games > 0:
                season = current_season - 1
                logger.debug(f'Inferring season of {season} due to incomplete games in current season')
            else:
                season = current_season
                logger.debug(f'Inferring season of {season} (current)')

        is_leader = len(utilities.get_matching_roles(member, [leader_role_name, coleader_role_name])) > 0
        record = []
        for i in range(season-2, season+1):
            season_tier = player.polychamps_season_tier(i)
            if season_tier:
                season_record = player.polychamps_season_record(i)
                if sum(season_record):
                    record.append((season_tier, sum(season_record), season_record[0]))  # tier, total games, wins
                else:
                    # No games played
                    record.append((None, 0, 0))
            else:
                record.append((None, 0, 0))

        if record.count((None, 0, 0)) == 3:
            return await ctx.send(f'{member.display_name} has not played in the past 3 seasons.')

        price = utilities.trade_price_formula(record, is_leader)
        await ctx.send(f"Trade price for {member.display_name} is **{price}**.")

    @commands.command()
    @settings.is_staff_check()
    @commands.cooldown(1, 120, commands.BucketType.channel)
    async def league_export(self, ctx, *, arg=None):
        """
        *Staff:* Export all league games to a compressed CSV file

        Specifically includes all ranked 2v2 or 3v3 games. This takes several minutes to run. You will be pinged upon completion.

        **Examples:**
        `[p]league_export`
        `[p]league_export logs` Include game logs in the export
        """

        import io

        export_logs = arg and arg.lower() == 'logs'
        # TODO: one query instead of if/else queries
        if export_logs:
            query = (models.Game
                .select(models.Game, peewee.fn.ARRAY_AGG(models.GameLog.message).alias('gamelogs'))
                .join(models.GameLog, peewee.JOIN.LEFT_OUTER, on=(models.GameLog.message ** peewee.fn.CONCAT('__', models.Game.id, '__%')))
                .where(
                    (models.Game.is_confirmed == 1) & (models.Game.guild_id == settings.server_ids['polychampions']) & (models.Game.is_ranked == 1) &
                    ((models.Game.size == [2, 2]) | (models.Game.size == [3, 3]))
                )
                .group_by(models.Game.id)
                .order_by(models.Game.date)
            )
        else:
            query = (models.Game
                .select()
                .where(
                    (models.Game.is_confirmed == 1) & (models.Game.guild_id == settings.server_ids['polychampions']) & (models.Game.is_ranked == 1) &
                    ((models.Game.size == [2, 2]) | (models.Game.size == [3, 3]))
                )
                .order_by(models.Game.date)
            )

        def async_call_export_func():

            filename = utilities.export_game_data_brief(query=query, export_logs=export_logs)
            return filename

        if query:
            await ctx.send(f'Exporting {len(query)} game records. This might take over an hour to run. I will ping you once the file is ready.')
        else:
            return await ctx.send('No matching games found.')

        async with ctx.typing():
            filename = await asyncio.get_running_loop().run_in_executor(None, async_call_export_func)
            with open(filename, 'rb') as f:
                file = io.BytesIO(f.read())
            file = discord.File(file, filename=filename)
            await ctx.send(f'{ctx.author.mention}, your export is complete. Wrote to `{filename}`', file=file)

    # @discord.app_commands.command(name="bid", description="Bid on a free agent")
    # @discord.app_commands.describe(amount="Amount of FAT to bid", player="The free agent you are bidding on")
    # @discord.app_commands.guilds(discord.Object(settings.server_ids['polychampions']))
    # async def bid(self, interaction: discord.Interaction, amount: discord.app_commands.Range[int, 1, None], player: discord.Member):
    #     is_leader = len(utilities.get_matching_roles(interaction.user, [leader_role_name, coleader_role_name])) > 0
    #     if not is_leader:
    #         await interaction.response.send_message(f'You must be a house leader or co-leader to bid.', ephemeral=True)
    #         return

    #     is_freeagent = len(utilities.get_matching_roles(player, [free_agent_role_name])) > 0
    #     if not is_freeagent:
    #         await interaction.response.send_message(f'{player.display_name} is not a free agent.', ephemeral=True)
    #         return

    #     current_auction = models.Auction.select().where(models.Auction.ongoing == True).first()
    #     if not current_auction:
    #         await interaction.response.send_message(f'There is no ongoing auction.', ephemeral=True)
    #         return

    #     bidder, _ = models.Player.get_by_discord_id(interaction.user.id, interaction.guild.id)
    #     p, _ = models.Player.get_by_discord_id(player.id, interaction.guild.id)

    #     in_preferred_houses = models.PlayerHousePreference.player_prefers_house(p.id, bidder.team.house.id)
    #     if not in_preferred_houses:
    #         await interaction.response.send_message(
    #             f'Your house is not in {player.display_name}\'s preferred houses.',
    #             ephemeral=True
    #         )
    #         return

    #     previous_bids = models.Bid.select().where(
    #         (models.Bid.auction == current_auction) &
    #         (models.Bid.player == p) &
    #         (models.Bid.house == bidder.team.house)
    #     )

    #     for bid in previous_bids:
    #         if bid.amount >= amount:
    #             await interaction.response.send_message(f'Your house has already bid {bid.amount} on this player, you cannot lower your bid!', ephemeral=True)
    #             return

    #     models.Bid.create(auction=current_auction, amount=amount, player=p, bidder=bidder, house=bidder.team.house)
    #     await interaction.response.send_message(f'You bid {amount} on {player.display_name}.', ephemeral=True)


    def get_auction_clean_bids(self, auction, include_bidder: bool = False):
        # Removes redundant lower bids from houses that have a higher bid on the same player
        bids = models.Bid.select().where(models.Bid.auction == auction)
        player_bids = {}

        for bid in bids:
            player = bid.player.discord_member.discord_id
            if player not in player_bids:
                player_bids[player] = []

            existing_bid = next((x for x in player_bids[player] if x[0] == bid.house.name), None)
            new_bid = (bid.house.name, bid.amount, bid.bidder) if include_bidder else (bid.house.name, bid.amount)
            if not existing_bid:
                player_bids[player].append(new_bid)
            elif existing_bid[1] < bid.amount:
                player_bids[player].remove(existing_bid)
                player_bids[player].append(new_bid)

        return player_bids

    async def dm_auction_ranking(self, auction):
        player_bids = self.get_auction_clean_bids(auction, include_bidder=True)

        messages = {}
        for player_id, bids in player_bids.items():
            bids.sort(key=lambda x: x[1], reverse=True)
            rank = 1

            for i, bid in enumerate(bids, 1):
                if not i == 1 and bid[1] != bids[i-2][1]:
                    rank = i

                tied = i > 1 and bid[1] == bids[i-2][1] or i < len(bids) and bid[1] == bids[i][1]

                bidder_id = bid[2].discord_member.discord_id
                if bidder_id not in messages:
                    messages[bidder_id] = f"Your ranks for this auction currently are:\n"

                name = models.DiscordMember.get(discord_id=player_id).name
                messages[bidder_id] += f"{self.get_number_ordinal(rank)}{' tied' if tied else ''} on {name} ({bid[1]} FAT)\n"

        for user_id, message in messages.items():
            try:
                user = await self.bot.fetch_user(user_id)
                await user.send(message)
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.error(f"Failed to DM auction ranking to {user_id}: {e}")

    def get_number_ordinal(self, n):
        ordinals = {1: "st", 2: "nd", 3: "rd"}
        if 10 <= n % 100 <= 20:
            suffix = "th"
        else:
            suffix = ordinals.get(n % 10, "th")
        return f"{n}{suffix}"

    def get_single_bid_players(self, auction):
        player_bids = self.get_auction_clean_bids(auction)
        single_bid_players = [(player, bids[0][0], bids[0][1]) for player, bids in player_bids.items() if len(bids) == 1]
        return single_bid_players

    def get_players_highest_bids(self, auction):
        player_bids = self.get_auction_clean_bids(auction)
        highest_bids = []
        tied_highest_bids = []

        for player, bids in player_bids.items():
            bids.sort(key=lambda x: x[1], reverse=True)
            second_highest_bid = bids[1][1] if len(bids) > 1 else bids[0][1]
            highest_teams = [bid[0] for bid in bids if bid[1] == bids[0][1]]

            if len(highest_teams) > 1:
                tied_highest_bids.append((player, highest_teams, second_highest_bid))
            else:
                highest_bids.append((player, highest_teams[0], second_highest_bid))

        return highest_bids, tied_highest_bids

    async def conclude_players_auction(self, players):
        guild = self.bot.get_guild(settings.server_ids['polychampions'])
        done = []
        for player, house_name, price in players:
            member = guild.get_member(player)
            if member:
                roles_to_remove = utilities.get_matching_roles(member, [novas_role_name, free_agent_role_name, grad_role_name])
                roles_to_remove = [discord.utils.get(guild.roles, name=role) for role in roles_to_remove]
                if not roles_to_remove:
                    # Player's auction was concluded in the previous round
                    continue

                house = models.House.get(name=house_name)
                teams = models.Team.select().where(
                    (models.Team.house == house) & (models.Team.is_hidden == 0) & (models.Team.is_archived == 0)
                ).order_by(models.Team.league_tier.desc())

                team_role = None
                for t in teams:
                    team_role = utilities.guild_role_by_name(guild, name=t.name, allow_partial=False)
                    if team_role and any(member for member in team_role.members):
                        break

                await member.remove_roles(*roles_to_remove)
                if team_role:
                    await member.add_roles(team_role)
            else:
                logger.warning(f"Free agent {player} not found in guild when concluding auction.")

            done.append((player, house_name, price))

        return done

    # @tasks.loop(hours=1)
    # async def auction_task(self):
    #     await self.bot.wait_until_ready()
    #     now = datetime.datetime.now(tz=datetime.timezone.utc)
    #     week_num = now.isocalendar()[1]

    #     auction_channel = self.bot.get_channel(1327702121130233969)  # free-agent-picks
    #     current_auction = models.Auction.select().where(models.Auction.ongoing == True).first()
    #     if (now.weekday() == 6 and now.hour == 10 and week_num % 2 == 0) or (now.weekday() == 1 and now.hour == 10 and week_num % 2 == 1):
    #         # Start auction
    #         if current_auction:
    #             return

    #         models.Auction.create(ongoing=True)
    #         auction_name = "Free Agent Auction"
    #         if now.weekday() == 1:
    #             auction_name = "Secondary Free Agent Auction"

    #         message = f"<@&1327333445180985398> <@&1327333522389602397> <@&1327547367590989855>\nThe {auction_name} is now open. Feel free to place your bids using /bid"
    #         await auction_channel.send(message)
    #     # elif (now.weekday() == 6 and now.hour == 22 and week_num % 2 == 0) or (now.weekday() == 1 and now.hour == 10 and week_num % 2 == 1):
    #     #     # Send rankings & conclude auction for free agents with 1 bid
    #     #     if not current_auction:
    #     #         return
            
    #     #     if now.weekday() == 6 and current_auction.r1_done or now.weekday() == 1 and current_auction.r2_done:
    #     #         return
            
    #     #     await self.dm_auction_ranking(current_auction)
    #     #     single_bid_players = self.get_single_bid_players(current_auction)
    #     #     players = await self.conclude_players_auction(single_bid_players)
    #     #     for player, house_name, price in players:
    #     #         await auction_channel.send(f"<@{player}> to {house_name} for {price} FAT!")
            
    #     #     if current_auction.r1_done:
    #     #         current_auction.r2_done = True
    #     #     else:
    #     #         current_auction.r1_done = True
    #     #     current_auction.save()
    #     elif (now.weekday() == 0 and now.hour == 10 and week_num % 2 == 1) or (now.weekday() == 2 and now.hour == 10 and week_num % 2 == 1):
    #         # Conclude auction
    #         if not current_auction:
    #             return
            
    #         highest_bids, tied_highest_bids = self.get_players_highest_bids(current_auction)
    #         players = await self.conclude_players_auction(highest_bids)
    #         for player, house_name, price in players:
    #             await auction_channel.send(f"<@{player}> to {house_name} for {price} FAT!")
            
    #         for player, houses, price in tied_highest_bids:
    #             await auction_channel.send(f"<@{player}> has tied bids from {', '.join(houses)} ({price} FAT). Please DM <@1327775289115152484> to choose which house you want to join.")
            
    #         if now.weekday() == 0:
    #             await auction_channel.send("The Secondary Free Agent Auction will open in 24 hours. Free agents may update their house preferences during this period if they wish.")
            
    #         current_auction.ongoing = False
    #         current_auction.save()

    @tasks.loop(hours=1)  # Check every hour
    async def task_draft_reminders(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now()
        channel_id = 447883341463814146  # mod-talk
        channel = self.bot.get_channel(channel_id)

        # Get the week number of the year (ISO week number)
        week_num = now.isocalendar()[1]  # This returns a tuple: (year, week number, weekday)
        logger.debug(f"Running task_draft_reminders: {now.hour} hours, {now.weekday()} days, {week_num} weeks")
        
        if not channel:
            logger.error(f"Could not find reminder channel with ID {channel_id}")
            return

        # Check if it's between 12:00 PM and 12:59 PM GMT
        if now.hour == 12:
            if now.weekday() == 0 and week_num % 2 == 0:  # Every other Monday
                await channel.send(f"@here Reminder: It's time to open the draft signups. Use the `$newfreeagent` command to start the process.")
                logger.info("Sent reminder to open draft signups")
            
            elif now.weekday() == 4 and week_num % 2 == 0:  # The following Friday
                await channel.send(f"@here Reminder: It's time to close the draft signups. Please review and close the current draft.")
                logger.info("Sent reminder to close draft signups")
            else:
                logger.debug("Not the correct day to send a reminder")
        else:
            logger.debug(f"Not the correct time of day to send a reminder: {now.hour} hours")


    @tasks.loop(minutes=120.0)
    async def task_send_polychamps_invite(self):
        await self.bot.wait_until_ready()

        message = ('You have met the qualifications to be invited to the **PolyChampions** discord server! '
                   'PolyChampions is a competitive Polytopia server organized into a league, with a focus on team (2v2 and 3v3) games.'
                   '\n To join use this invite link: https://discord.gg/YcvBheS')
        logger.info('Running task task_send_polychamps_invite')
        guild = self.bot.get_guild(settings.server_ids['main'])
        if not guild:
            logger.warning('Could not load guild via server_id')
            return
        utilities.connect()
        dms = models.DiscordMember.members_not_on_polychamps()
        logger.info(f'{len(dms)} discordmember results')
        for dm in dms:
            wins_count, losses_count = dm.wins().count(), dm.losses().count()
            logger.debug(f'Evaluating {dm.name} - W:{wins_count} L:{losses_count} ELO_MAX_MOONRISE: {dm.elo_max_moonrise}')
            if wins_count < 5:
                logger.debug(f'Skipping {dm.name} - insufficient winning games {wins_count}')
                continue
            recent_count = dm.games_played(in_days=15).count()
            if recent_count < 1:
                logger.debug(f'Skipping {dm.name} - insufficient recent games ({recent_count})')
                continue
            if dm.elo_max_moonrise > 1150:
                logger.debug(f'{dm.name} qualifies due to higher ELO > 1150')
            elif wins_count > losses_count:
                logger.debug(f'{dm.name} qualifies due to positive win ratio')
            else:
                logger.debug(f'Skipping {dm.name} - ELO or W/L record insufficient')
                continue

            if not dm.polytopia_id and not dm.polytopia_name:
                logger.debug(f'Skipping {dm.name} - no mobile code or name')
                continue

            logger.debug(f'Sending invite to {dm.name}')
            guild_member = guild.get_member(dm.discord_id)
            if not guild_member:
                logger.debug(f'Could not load {dm.name} from guild {guild.id}')
                continue
            try:
                await guild_member.send(message)
            except discord.DiscordException as e:
                logger.warning(f'Error DMing member: {e}')
            else:
                dm.date_polychamps_invite_sent = datetime.datetime.today()
                dm.save()


class HouseSelectMenu(Select):
    def __init__(self):
        houses = models.House.select(models.House.name, models.House.emoji, models.House.id)
        options = [discord.SelectOption(label=house.name, emoji=house.emoji if house.emoji else None, value=house.id) for house in houses]
        super().__init__(placeholder="Choose your preferred house(s)...", min_values=1, max_values=len(houses), options=options)

    async def callback(self, interaction: discord.Interaction):
        player, _ = models.Player.get_by_discord_id(discord_id=interaction.user.id, discord_name=interaction.user.name, discord_nick=interaction.user.nick, guild_id=interaction.guild.id)
        selected_houses = ", ".join(
            house.name for house in models.House.select().where(models.House.id.in_(self.values))
        )
        models.PlayerHousePreference.clear_preferences(player.id)
        models.PlayerHousePreference.add_or_update_preferences(player.id, self.values)

        self.view.stop()
        await interaction.response.edit_message(content="You have selected the following houses: " + selected_houses, view=None)


class ClearPreferencesButton(Button):
    def __init__(self):
        super().__init__(label="Clear Preferences", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        player, _ = models.Player.get_by_discord_id(
            discord_id=interaction.user.id,
            discord_name=interaction.user.name,
            discord_nick=interaction.user.nick,
            guild_id=interaction.guild.id
        )
        models.PlayerHousePreference.clear_preferences(player.id)

        self.view.stop()
        await interaction.response.edit_message(
            content="Your house preferences have been cleared.",
            view=None
        )


async def broadcast_team_game_to_server(ctx, game):
    # When a PolyChamps game is created with a role-lock matching a league team, it will broadcast a message about the game
    # to that team's server, if it has a league_game_announce_channel channel configured.

    if ctx.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
        return

    role_locks = [gs.required_role_id for gs in game.gamesides if gs.required_role_id]
    roles = [ctx.guild.get_role(r_id) for r_id in role_locks if ctx.guild.get_role(r_id)]

    if not roles:
        return

    house_roles = get_house_roles(guild=ctx.guild)
    team_roles = get_team_roles(guild=ctx.guild)
    
    for role in roles:
        team_name, house_name = '', ''
        if role in team_roles:
            team_name = role.name
            game_type = f'Team {team_name.replace("The ", "")}'
        elif role in house_roles:
            house_name = role.name
            game_type = f'House {house_name}'
        else:
            logger.debug(f'broadcast_team_game_to_server: no team name found to match role {role.name}')
            continue

        try:
            if team_name:
                team = models.Team.get_or_except(team_name=team_name, guild_id=ctx.guild.id)
            if house_name:
                house = models.House.get_or_except(house_name=house_name)
                team = house.teams[0]  # Just setting team to first related house team - this might cause problems
        except exceptions.NoSingleMatch:
            logger.warning(f'broadcast_team_game_to_server: valid team name found to match role {role.name} but no database match')
            continue

        team_server = settings.bot.get_guild(team.external_server)
        team_channel = discord.utils.get(team_server.text_channels, name='polychamps-game-announcements') if team_server else None

        if settings.bot.user.id == 479029527553638401:
            team_channel = discord.utils.get(team_server.text_channels, name='beta-bot-tests') if team_server else None

        if not team_channel:
            logger.warning(f'broadcast_team_game_to_server: could not load guild or announce channel for {team.name}')
            continue
        notes_str = f'\nNotes: *{game.notes}*' if game.notes else ''

        bot_member = team_server.get_member(settings.bot.user.id)
        if team_channel.permissions_for(bot_member).add_reactions:
            join_str = game.reaction_join_string()
        else:
            join_str = ':warning: *Missing add reactions permission*'

        message_content = f'New PolyChampions game `{game.id}` for {game_type} created by {game.host.name}\n{game.size_string()} {game.get_headline()}{notes_str}\n{ctx.message.jump_url}'
        if game.is_uncaught_season_game():
            message_content += '\n(*This appears to be a **Season Game** so join reactions are disabled.*)'
        else:
            message_content += f'\n{join_str}.'

        try:
            message = await team_channel.send(message_content)
            models.TeamServerBroadcastMessage.create(game=game, channel_id=team_channel.id, message_id=message.id)
        except discord.DiscordException as e:
            logger.warning(f'Could not send broadcast message: {e}')
        logger.debug(f'broadcast_team_game_to_server - sending message to channel {team_channel.name} on server {team_server.name}\n{message_content}')


async def auto_grad_novas(guild, game, output_channel = None):
    # called from post_newgame_messaging() - check if any member of the newly-started game now meets Nova graduation requirements

    if guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
        return
    
    logger.debug(f'auto_grad_novas for game {game.id}')

    role = discord.utils.get(guild.roles, name=novas_role_name)
    grad_role = discord.utils.get(guild.roles, name=grad_role_name)

    if not role or not grad_role:
        logger.warning('Could not load required roles to complete auto_grad_novas')
        return

    player_id_list = [l.player.discord_member.discord_id for l in game.lineup]
    for player_id in player_id_list:
        member = guild.get_member(player_id)
        if not member:
            logger.warning(f'Could not load guild member matching discord_id {player_id} for game {game.id} in auto_grad_novas')
            continue

        if role not in member.roles or grad_role in member.roles:
            continue  # skip non-novas or people who are already graduates

        logger.debug(f'Checking league graduation status for player {member.name} in auto_grad_novas')

        try:
            dm = models.DiscordMember.get(discord_id=member.id)
            player = models.Player.get(discord_member=dm, guild_id=guild.id)
        except peewee.DoesNotExist:
            logger.warning(f'Player {member.name} not registered.')
            continue

        qualifying_games = []
        has_completed_game = False

        for lineup in player.games_played():
            game = lineup.game
            logger.debug(f'Evaluating game {game.id} is_pending {game.is_pending} is_completed {game.is_completed}')
            if game.smallest_team() > 1:
                logger.debug('Team game')
                if not game.is_pending:
                    qualifying_games.append(str(game.id))
                if game.is_completed:
                    has_completed_game = True

        if len(qualifying_games) < 2:
            logger.debug(f'Player {player.name} has insufficient qualifying games. Games that qualified: {qualifying_games}')
            continue
    
        if not has_completed_game:
            logger.debug(f'Player {player.name} has no completed team games.')
            continue

        wins, losses = dm.get_record()
        logger.debug(f'Player {player.name} meets qualifications: {qualifying_games}')

        try:
            await member.add_roles(grad_role)
        except discord.DiscordException as e:
            logger.error(f'Could not assign league graduation role: {e}')
            break

        config, _ = models.Configuration.get_or_create(guild_id=guild.id)
        announce_str = 'Free Agent signups open regularly - pay attention to server announcements for a notification of the next one.'
        if config.polychamps_draft['draft_open']:
            try:
                channel = guild.get_channel(config.polychamps_draft['announcement_channel'])
                if channel and await channel.fetch_message(config.polychamps_draft['announcement_message']):
                    announce_str = f'Free Agent signups are currently open in <#{channel.id}>'
            except discord.NotFound:
                pass  # Draft signup message no longer exists - assume its been deleted intentionally and closed
            except discord.DiscordException as e:
                logger.warning(f'Error loading existing draft announcement message in auto_grad_novas: {e}')

        grad_announcement = (f'Player {member.mention} (*Global ELO: {dm.elo_moonrise} \u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}*) '
                f'has met the qualifications and is now a **{grad_role.name}**\n'
                f'{announce_str}')

        await utilities.send_to_log_channel(guild, grad_announcement)
        if output_channel:
            await output_channel.send(grad_announcement)


def populate_league_team_channels():
    # maintain a list of channel IDs associated with PolyChamps team games
    global league_team_channels
    league_teams = models.Team.select(models.Team.id).where(
        (models.Team.guild_id == settings.server_ids['polychampions']) & (models.Team.is_hidden == 0)
    )
    query = models.GameSide.select(models.GameSide.team_chan).join(models.Game).where(
        (models.GameSide.team_chan.is_null(False)) &
        (models.GameSide.game.guild_id == settings.server_ids['polychampions']) &
        (models.GameSide.game.is_confirmed == 0) &
        (models.GameSide.team.in_(league_teams))
    ).tuples()

    league_team_channels = [tc[0] for tc in query]
    logger.debug(f'updating league_team_channels, len {len(league_team_channels)}')
    return len(league_team_channels)


async def setup(bot):
    await bot.add_cog(league(bot))
