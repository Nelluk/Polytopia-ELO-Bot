from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord.ui import Button, Select, View
from PIL import UnidentifiedImageError
import modules.models as models
import modules.utilities as utilities
import settings
import logging
import asyncio
import modules.exceptions as exceptions
# import re
import datetime
import peewee
import typing
# import random
import modules.imgen as imgen

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

league_team_channels = []

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
            house_name = team.house.name if team.house else None
            team_tier = team.league_tier
            house_role = discord.utils.get(member.guild.roles, name=house_name) if house_name else None
            tier_role = tier_roles[team_tier - 1]
        except exceptions.NoSingleMatch as e:
            logger.warning(f'League.update_member_league_roles: could not load Player or Team for changing league member {member.display_name}: {e}')
            house_name, team_tier, house_role, tier_role = None, None, None, None

        roles_to_add = [house_role, tier_role, league_role]
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

    draft_open_format_str = f'The league is now open for Free Agent signups! {{0}}s can react with a {emoji_draft_signup} below to sign up. {{1}} who have not graduated have until the end of the signup period to meet requirements and sign up. If Free Agents have favorite teams, they may use the `/select-houses` command to note those preferences.\n\n{{3}}'
    draft_closed_message = f'The league is closed to new Free Agent signups. Mods can use the {emoji_draft_conclude} reaction to clean up and delete this message.'

    def __init__(self, bot):

        self.bot = bot
        self.announcement_message = None  # Will be populated from db if exists
        self.auction_task.start()
        if settings.run_tasks:
            self.task_send_polychamps_invite.start()
            self.task_draft_reminders.start()

    async def cog_check(self, ctx):
        return ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']

    @commands.Cog.listener()
    async def on_message(self, message):

        if message.channel.id not in league_team_channels or not message.attachments:
            return

        try:
            game = models.Game.by_channel_id(chan_id=message.channel.id)
        except exceptions.MyBaseException as e:
            return logger.error(f'League.on_message: channel in league_team_channels but cannot load associated game by chan_id {message.channel.id} - {e}')

        logger.debug(f'League.on_message: handling message in league_team_channels {message.channel.id}')
        attachment_urls = '\n'.join([attachment.url for attachment in message.attachments])

        models.GameLog.write(guild_id=message.guild.id, is_protected=True, game_id=game.id, message=f'{models.GameLog.member_string(message.author)} posted images: {attachment_urls}')
    
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
        # assume polychampions
        self.announcement_message = self.get_draft_config(settings.server_ids['polychampions'])['announcement_message']
        if self.bot.user.id == 479029527553638401:
            # beta bot, using nelluk server to watch for messages
            self.announcement_message = self.get_draft_config(settings.server_ids['test'])['announcement_message']

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

        channel = payload.member.guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

        if payload.emoji.name not in self.emoji_draft_list:
            # Irrelevant reaction was added to relevant message. Clear it off.
            removal_emoji = self.bot.get_emoji(payload.emoji.id) if payload.emoji.id else payload.emoji.name

            try:
                await message.remove_reaction(removal_emoji, payload.member)
                logger.debug(f'Removing irrelevant {payload.emoji.name} reaction placed by {payload.member.name} on message {payload.message_id}')
            except discord.DiscordException as e:
                logger.debug(f'Unable to remove irrelevant reaction in on_raw_reaction_add(): {e}')
            return

        if payload.emoji.name == self.emoji_draft_signup:
            await self.signup_emoji_clicked(payload.member, channel, message, reaction_added=True)
        elif payload.emoji.name == self.emoji_draft_close:
            await self.close_draft_emoji_added(payload.member, channel, message)
        elif payload.emoji.name == self.emoji_draft_conclude:
            await self.conclude_draft_emoji_added(payload.member, channel, message)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        # Monitors all reactions being removed from all messages, looking for reactions added to relevant league announcement messages

        if payload.message_id != self.announcement_message:
            return

        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        channel = guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

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

        result_message_list = [f'Free Agent signup successfully closed by {member.mention}']
        self.announcement_message = None

        for log_message in result_message_list:
            models.GameLog.write(guild_id=member.guild.id, message=log_message)
        await utilities.send_to_log_channel(member.guild, '\n'.join(result_message_list))
        self.delete_draft_config(member.guild.id)

        try:
            await message.clear_reactions()
            new_message = message.content.replace(self.draft_closed_message, f'~~{self.draft_closed_message}~~') + f'\nThis signup is concluded. {len(free_agent_role.members)} members are currently Free Agents.'
            await message.edit(content=new_message)
        except discord.DiscordException as e:
            logger.warning(f'Could not clear reactions or edit content in concluded draft message: {e}')

    async def close_draft_emoji_added(self, member, channel, message):
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft close reaction added by {member.name} to draft announcement {announce_message_link}')
        grad_role = discord.utils.get(member.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(member.guild.roles, name=novas_role_name)
        free_agent_role = discord.utils.get(member.guild.roles, name=free_agent_role_name)

        try:
            await message.remove_reaction(self.emoji_draft_close, member)
            logger.debug(f'Removing {self.emoji_draft_close} reaction placed by {member.name} on message {message.id}')
        except discord.DiscordException as e:
            logger.warning(f'Unable to remove reaction in close_draft_emoji_added(): {e}')

        if not settings.is_mod(member):
            return

        draft_config = self.get_draft_config(member.guild.id)

        if draft_config['draft_open']:
            new_message = f'~~{message.content}~~\n{self.draft_closed_message}'
            log_message = f'Draft status closed by {member.mention}'
            draft_config['draft_open'] = False
        else:
            new_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, free_agent_role.mention, draft_config['draft_message'])
            log_message = f'Draft status opened by {member.mention}'
            draft_config['draft_open'] = True

        self.save_draft_config(member.guild.id, draft_config)
        await utilities.send_to_log_channel(member.guild, log_message)
        try:
            await message.edit(content=new_message)
        except discord.DiscordException as e:
            return logger.error(f'Could not update message in close_draft_emoji_added: {e}')

    async def signup_emoji_clicked(self, member, channel, message, reaction_added=True):

        draft_opened = self.get_draft_config(member.guild.id)['draft_open']
        member_message, log_message = '', ''
        grad_role = discord.utils.get(member.guild.roles, name=grad_role_name)
        # draftable_role = discord.utils.get(member.guild.roles, name=draftable_role_name)
        free_agent_role = discord.utils.get(member.guild.roles, name=free_agent_role_name)
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft signup reaction added by {member.name} to draft announcement {announce_message_link}')

        if reaction_added:
            if draft_opened and grad_role in member.roles:
                # An eligible member signing up for the draft
                try:
                    await member.add_roles(free_agent_role, reason='Member signed up as Free Agent')
                except discord.DiscordException as e:
                    logger.error(f'Could not add free_agent_role in signup_emoji_clicked: {e}')
                    return
                else:
                    member_message = f'You now are signed up for the PolyChampions Auction 🎉\n\nYou may now be contacted by recruiters. It is in your best interest to chat and get to know the different houses. Be open minded. Ask questions. (If a recruiter trashes another team or forces you to choose a team before the auction, please report this to mods.)\n\nIf you have a preference for certain houses, please use the `/select-houses` command in ⁠bot-commands to note your favorite(s). Only the house(s) you select will be allowed to place a bid on you. If you don\'t select, then any house may bid on you.\n{announce_message_link}'
                    log_message = f'{member.mention} ({member.name}) reacted to the signup message and received the {free_agent_role.name} role.'
            else:
                # Ineligible signup - either draft is closed or member does not have grad_role
                try:
                    await message.remove_reaction(self.emoji_draft_signup, member)
                    logger.debug(f'Removing {self.emoji_draft_signup} reaction placed by {member.name} on message {message.id}')
                except discord.DiscordException as e:
                    logger.warning(f'Unable to remove irrelevant reaction in signup_emoji_clicked(): {e}')
                if not draft_opened:
                    member_message = 'The draft has been closed to new signups - your signup has been rejected.'
                    logger.debug(f'{member.id}> reacted to the draft but was rejected since it is closed.')
                else:
                    member_message = f'Your signup has been rejected. You do not have the **{grad_role.name}** role. Try again once you have met the graduation requirements.'
                    logger.debug(f'Rejected {member.name} from the draft since they lack the {grad_role.name} role.')
        else:
            # Reaction removed
            if free_agent_role in member.roles:
                # Removing member from draft, same behavior whether draft is opened or closed
                try:
                    await member.remove_roles(free_agent_role, reason='Member removed from Free Agent signup')
                except discord.DiscordException as e:
                    logger.error(f'Could not remove Free Agent role in signup_emoji_clicked: {e}')
                    return
                else:
                    member_message = f'You have been removed from the Free Agent list. You can sign back up at the announcement message:\n{announce_message_link}'
                    log_message = f'{member.mention} ({member.name}) removed their Free Agent reaction and has lost the {free_agent_role.name} role.'
            else:
                return
                # member_message = (f'You removed your signup reaction from the draft announcement, but you did not have the **{draftable_role.name}** :thinking:\n'
                # f'Add your reaction back to attempt to get the role and sign up for the draft.\n{announce_message_link}')
                # Fail silently, otherwise a user whose reaction is being rejected will get two PMs
                # the bot removing the reaction will trigger a second one - currently no way to distinguish a reaction being removed by
                # the original author or an admin/bot. Could kinda solve by storing timestamp when removing role and ignoring role removal
                # if it has a nearly-same timestamp

        if log_message:
            await utilities.send_to_log_channel(member.guild, log_message)
            models.GameLog.write(guild_id=member.guild.id, message=log_message)
        if member_message:
            try:
                await member.send(member_message)
            except discord.DiscordException as e:
                logger.warning(f'Could not message member in signup_emoji_clicked: {e}')

    def get_draft_config(self, guild_id):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        return record.polychamps_draft

    def save_draft_config(self, guild_id, config_obj):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        record.polychamps_draft = config_obj
        return record.save()

    def delete_draft_config(self, guild_id):
        q = models.Configuration.delete().where(models.Configuration.guild_id == guild_id)
        return q.execute()

    @commands.command(usage=None)
    # @settings.in_bot_channel_strict()
    async def tutorial(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        tutorial_message = (
            "# The Basics\n"
            "__To set your polytopia name with the bot <:Midjiwan:938642183093907496>:__\n"
            "`$setname` followed by your polytopia in app name\n\n"
            
            "__To join the new starters team <:novas:1327341237665005669>:__\n"
            "`$novas`\n\n"
            
            "__To open a game 👋 :__\n"
            "`$open 2v2 drylands` (Can be changed to any map type or other size games like 1v1 or 3v3)\n\n"
            
            "__To see a list of games you can join ⚔️ <:star:390477609131048962>:__\n"
            "`$novagames` or `$games`\n\n"
            
            "__To see games you are in 👀:__\n"
            "`$incomplete`\n\n"
            
            "__To start a game you created 💪:__\n"
            "`$start 111222 prediscussion change 111222 for the game ID you want to start`\n\n"
            
            "__To see a full list 📜 of commands:__\n"
            "`$help`\n\n"
            
            "Watch a YT tutorial on how to use the PolyElo bot and the match making system "
            "https://youtu.be/_KsDd0LT54M"
        )
        await ctx.send(tutorial_message)

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

        # post message in announcements (optional argument of a different channel if mod wants announcement to go elsewhere?)
        # listen for reactions in a check
        # if reactor has Nova Grad role, PM success message and apply Free Agent role
        # if not, PM failure message and remove reaction
        # remove Free Agent role if user removes their reaction

        if channel_override:
            announcement_channel = channel_override
        else:
            # use default channel for announcement
            if ctx.guild.id == settings.server_ids['polychampions']:
                announcement_channel = ctx.guild.get_channel(1326604735863721984)  # #announcements
            else:
                announcement_channel = ctx.guild.get_channel(480078679930830849)  # #admin-spam

        draft_config = self.get_draft_config(ctx.guild.id)

        if self.announcement_message:
            try:
                channel = ctx.guild.get_channel(draft_config['announcement_channel'])
                if channel and await channel.fetch_message(self.announcement_message):
                    return await ctx.send(f'There is already an existing announcement message. Use the {self.emoji_draft_conclude} reaction on that message (preferred) '
                        f'or delete the message.\nhttps://discord.com/channels/{ctx.guild.id}/{channel.id}/{self.announcement_message}')
            except discord.NotFound:
                pass  # Message no longer exists - assume deleted and create a fresh draft message
            except discord.DiscordException as e:
                logger.warning(f'Error loading existing draft announcement message in newfreeagent command: {e}')

        grad_role = discord.utils.get(ctx.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(ctx.guild.roles, name=novas_role_name)
        free_agent_role = discord.utils.get(ctx.guild.roles, name=free_agent_role_name)

        formatted_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, free_agent_role.mention, added_message)
        announcement_message = await announcement_channel.send(formatted_message)

        await announcement_message.add_reaction(self.emoji_draft_signup)
        await announcement_message.add_reaction(self.emoji_draft_close)
        await announcement_message.add_reaction(self.emoji_draft_conclude)

        await utilities.send_to_log_channel(ctx.guild, f'Draft created by {ctx.author.mention}\n'
            f'https://discord.com/channels/{ctx.guild.id}/{announcement_channel.id}/{announcement_message.id}')

        if announcement_channel.id != ctx.message.channel.id:
            await ctx.send('Draft announcement has been posted in the announcement channel.')

        draft_config['announcement_message'] = announcement_message.id
        draft_config['announcement_channel'] = announcement_message.channel.id
        draft_config['date_opened'] = str(datetime.datetime.today())
        draft_config['draft_open'] = True
        draft_config['draft_message'] = added_message

        self.announcement_message = announcement_message.id
        self.save_draft_config(ctx.guild.id, draft_config)

    @commands.command(usage='team_name new_tokens [optional_note]')
    # @settings.is_mod_check()
    async def tokens(self, ctx, *, arg=None):
        """
        *Mod:* Display or update house tokens. 
        The house name must be identified by a single word.

        **Examples**
        `[p]tokens` Summarize tokens for all Houses and list last 5 changes
        `[p]tokens ronin` Print log of all token updates regarding house Ronin
        `[p]tokens ronin 5 removed bonus` Set tokens for House Ronin to 5, and log an optional note
        """
        args = arg.split() if arg else []

        if len(args) == 0:
            logger.debug('Summarizing league tokens')
            message = ['**League Tokens Summary**']
            houses = models.House().select().order_by(models.House.league_tokens)
            for house in houses:
                message.append(f'House **{house.name}** {house.emoji} - {house.league_tokens} tokens')
            
            entries = models.GameLog.search(keywords=f'FATS id=', guild_id=ctx.guild.id, limit=5)
            message.append('\n**Last 5 changes:**')
            for entry in entries:
                message.append(f'`- {entry.message_ts.strftime("%Y-%m-%d %H:%M")}` {entry.message[:500]}')
            return await ctx.send('\n'.join(message))

        try:
            logger.debug(f'Attempting to load house "{args[0]}"')
            house = models.House.get_or_except(house_name=args[0])
        except exceptions.TooManyMatches:
            return await ctx.send(f'Too many matches found for house *{args[0]}*. The first argument must be a single word identifying a House.')
        except exceptions.NoMatches:
            return await ctx.send(f'No matches found for house *{args[0]}*. The first argument must be a single word identifying a House.')
        
        # if len(args) != 2:
        #     return await ctx.send(f'Incorrect number of arguments. Example: `{ctx.prefix}{ctx.invoked_with} housename 5` to set tokens to 5.')

        if len(args) == 1:
            # Just a house name supplied. Print gamelogs for token updates for that house.
            entries = models.GameLog.search(keywords=f'FATS id={house.id}', guild_id=ctx.guild.id)
            paginated_message_list = []
            for entry in entries:
                paginated_message_list.append((f'`{entry.message_ts.strftime("%Y-%m-%d %H:%M:%S")}`', entry.message[:500]))

            return await utilities.paginate(self.bot, ctx, title=f'Searched logs for token updates on House ID={house.id}', message_list=paginated_message_list, page_start=0, page_end=10, page_size=10)
        if settings.get_user_level(ctx.author) <= 4:
            return await ctx.send(f'You are not authorized to alter tokens.')
        
        token_notes = f' - Note: {" ".join(args[2:])}' if len(args) > 2 else ''
        logger.debug(f'Attempting to update tokens for house ID {house.id} to {args[1]} {token_notes}')
        try:
            old_count, new_count = house.update_tokens(int(args[1]))
        except ValueError:
            return await ctx.send(f'Could not translate "{args[1]}" into an integer.')
        
        models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} updated league tokens (FATs) for House ID={house.id} {house.name} from {old_count} to {new_count} {token_notes}')
        return await ctx.send(f'House **{house.name}** has {old_count} tokens. Updating to {new_count}. :coin: {token_notes}')

    
    @commands.command(usage='house_name')
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
            return await ctx.send(f'House name not provided. *Example:* `{ctx.prefix}{ctx.invoked_with} ronin`')
        try:
            house = models.House.get_or_except(house_name=arg)
        except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
            return await ctx.send(e)
        
        # members, players = utilities.active_members_and_players(ctx.guild, active_role_name=house.name, inactive_role_name=settings.guild_setting(ctx.guild.id, 'inactive_role'))

        leaders, coleaders, recruiters, captains = [], [], [], []
        
        house_role = utilities.guild_role_by_name(ctx.guild, name=house.name, allow_partial=False)
        leader_role = utilities.guild_role_by_name(ctx.guild, name=leader_role_name, allow_partial=False)
        coleader_role = utilities.guild_role_by_name(ctx.guild, name=coleader_role_name, allow_partial=False)
        recruiter_role = utilities.guild_role_by_name(ctx.guild, name=recruiter_role_name, allow_partial=False)
        captain_role = utilities.guild_role_by_name(ctx.guild, name=captain_role_name, allow_partial=False)

        # inactive_role = utilities.guild_role_by_name(ctx.guild, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        
        message_list = [f'{pc_emoji} {house.emoji} House {house_role.mention if house_role else house.name} {house.emoji} {pc_emoji}']
        house_teams = models.Team.select().where((models.Team.house == house) & (models.Team.is_archived == 0)).order_by(models.Team.league_tier)
        
        def em(text):
            return discord.utils.escape_markdown(text, as_needed=False)
            
        if house_role:
            for member in house_role.members:
                if leader_role in member.roles:
                    leaders.append(f'{em(member.display_name)} ({member.mention})')
                if coleader_role in member.roles:
                    coleaders.append(f'{em(member.display_name)} ({member.mention})')
                if recruiter_role in member.roles:
                    recruiters.append(f'{em(member.display_name)} ({member.mention})')
                if captain_role in member.roles:
                    captains.append(f'{em(member.display_name)} ({member.mention})')

        message_list.append(f'**Leaders**: {", ".join(leaders)}')
        message_list.append(f'\n**Co-Leaders**: {", ".join(coleaders)}')
        message_list.append(f'\n**Recruiters**: {", ".join(recruiters)}')
        if captains:
            message_list.append(f'\n**Captains**: {", ".join(captains)}')

        for team in house_teams:
            player_list = []
            tier_name = settings.tier_lookup(team.league_tier)[1]
            team_role = utilities.guild_role_by_name(ctx.guild, name=team.name, allow_partial=False)
            message_list.append(f'\n__{tier_name} Tier Team__ {team_role.mention if team_role else team.name} {team.emoji} `{team.elo} ELO`')

            members, players = await utilities.active_members_and_players(ctx.guild, active_role_name=team.name, inactive_role_name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
            for member, player in zip(members, players):
                player_list.append(f'{em(member.display_name)} `{player.elo_moonrise}`')
            message_list = message_list + player_list
        
        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content='\n'.join(message_list), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))

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
        
        houses_with_teams = peewee.prefetch(models.House.select().order_by(models.House.league_tokens), models.Team.select().order_by(models.Team.league_tier))
        house_list = [f'{pc_emoji} **PolyChampions Houses** {pc_emoji}']
        leader_role = utilities.guild_role_by_name(ctx.guild, name=leader_role_name, allow_partial=False)

        for house in houses_with_teams:
            team_list, team_message = [], ''

            house_role = utilities.guild_role_by_name(ctx.guild, name=house.name, allow_partial=False)
            house_leaders = [f'{member.display_name}' for member in leader_role.members if house_role in member.roles] if (house_role and leader_role) else []
            leaders_str = f'\n**House Leader:** {", ".join(house_leaders)}' if house_leaders else ''

            if house.teams:
                for hteam in house.teams:
                    tier_name = settings.tier_lookup(hteam.league_tier)[1]
                    team_role = utilities.guild_role_by_name(ctx.guild, name=hteam.name, allow_partial=False)
                    team_list.append(f'- {team_role.mention if team_role else hteam.name} {hteam.emoji} - {tier_name} Tier - ELO: {hteam.elo}')
                team_message = '\n'.join(team_list)
            else:
                team_message = '*No related Teams*'
            house_message = f'**House** {house_role.mention if house_role else house.name} {house.emoji} - Tokens: {house.league_tokens}{leaders_str} \n {team_message}'
            house_list.append(f'{house_message}\n')
        
        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content='\n'.join(house_list), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))
    
    @commands.command(aliases=['house_rename', 'house_image'], usage='')
    @settings.is_mod_check()
    async def house_add(self, ctx, *, arg=None):
        """*Mod*: Create or rename a league House
        **Example:**
        `[p]house_add Amphibian Party` - Add a new house named "Amphibian Party"
        `[p]house_rename amphibian Mammal Kingdom` - Rename them to "Mammal Kingdom"
        `[p]house_image amphibian http://www.path.to/image.png` - Set house image URL
        """
        args = arg.split() if arg else []
        if not args:
            return await ctx.send(f'See {ctx.prefix}help {ctx.invoked_with} for usage examples.')
        
        if ctx.invoked_with == 'house_image':
            if len(args) < 2:
                return await ctx.send(f'Please provide both a house name and image URL. Example: `{ctx.prefix}house_image housename http://url_to_image.png`')
            
            house_name, image_url = args[0], ' '.join(args[1:])
            
            try:
                house = models.House.get_or_except(house_name=house_name)
            except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
                return await ctx.send(e)

            if 'http' not in image_url:
                return await ctx.send(f'Valid image URL not detected. Example usage: `{ctx.prefix}house_image name http://url_to_image.png`')

            old_url = house.image_url if house.image_url else "None"
            house.image_url = image_url
            house.save()

            logger.info(f'house_image set for {house.id} {house.name} to {house.image_url}')
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} updated image URL for House {house.name} from {old_url} to {image_url}')
            
            await ctx.send(f'House {house.name} updated with new image_url. Old URL was: {old_url}\nNew image should appear below:')
            await ctx.send(house.image_url)
            return

        if ctx.invoked_with == 'house_add':
            house_name = ' '.join(args)
            try:
                logger.debug(f'Trying to create a house with name {house_name}')
                house = models.House.create(name=house_name)
            except peewee.IntegrityError:
                return await ctx.send(f':warning: There is already a House with the name "{house_name}". No changes saved.')
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} created a new House with name "{house.name}"')
            return await ctx.send(f'New league House created with name "{house.name}". You can add a team to it using `{ctx.prefix}team_house`.')
        
        if ctx.invoked_with == 'house_rename':
            example_string = f'**Example**: `{ctx.prefix}{ctx.invoked_with} ronin New Team Name`'
            if len(args) < 2:
                return await ctx.send(f'The first argument should be a single word identifying an existing House. The rest will be used for the new name. {example_string}')
            house_identifier, house_newname = args[0], ' '.join(args[1:])
            logger.debug(f'Attempting to rename house identified by string "{house_identifier}" to "{house_newname}"')

            try:
                house = models.House.get_or_except(house_name=house_identifier)
            except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
                return await ctx.send(e)
            
            house_oldname = house.name
            house.name = house_newname
            house.save()
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} renamed a House from "{house_oldname}" to "{house_newname}"')

            return await ctx.send(f'Successfully renamed a House from "{house_oldname}" to "{house_newname}". It has {house.league_tokens} tokens.')

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
    
    @commands.command(aliases=['team_house', 'team_tier'], usage='team_name arguments')
    @settings.is_mod_check()
    async def team_edit(self, ctx, *, arg=None):
        """*Mod*: Edit a team's house affiliation or league tier
        **Example:**
        `[p]team_house ronin Ninjas` - Put team Ronin into house Ninjas
        `[p]team_house ronin NONE` - Remove team Ronin from any house affiliation. NONE must be in all caps.
        `[p]team_edit ronin ARCHIVE` - Mark a defunct team as archived. This cannot be undone via the bot. Team must first have no house affiliation and no incomplete games.
        `[p]team_tier ronin gold` - Change league tier of team. Does not impact current or past games from this team.
        
        See also: `team_add`, `team_name`, `team_server`, `team_image`, `team_emoji`, `house_add`, `house_rename`
        """
        args = arg.split() if arg else []
        if not args or len(args) != 2:
            return await ctx.send(f'See `{ctx.prefix}help {ctx.invoked_with}` for usage examples. Teams and Houses must be each identified by a single word.')
        
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
        
        if ctx.invoked_with == 'team_house':
            old_house_name = team.house.name if team.house else 'NONE'
            if args[1] == 'NONE':
                logger.info(f'Processing house removal')
                new_house, new_house_name = None, 'NONE'
            else:
                logger.info(f'Processing house affiliation change')
                try:
                    new_house = models.House.get_or_except(house_name=args[1])
                    new_house_name = new_house.name
                except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
                    return await ctx.send(e)
        
            team.house = new_house
            team.save()
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} set the House affiliation of Team {team.name} to {new_house_name} from {old_house_name}')
            tier_warning = '' if team.league_tier else f'\n:warning:Team tier not set. You probably want to set one with `{ctx.prefix}team_tier`'
            
            async with ctx.typing():
                for member in team_role.members:
                    logger.debug(f'team_edit updating league roles for member {member.display_name}')
                    await update_member_league_roles(member)

                return await ctx.send(f'Changed House affiliation of team  **{team.name}** to {new_house_name}. Previous affiliation was "{old_house_name}".{tier_warning}. Team members have had their House roles refreshed.')

        if ctx.invoked_with == 'team_tier':
            try:
                new_tier, new_tier_name = settings.tier_lookup(args[1])
            except exceptions.NoMatches:
                return await ctx.send(f'Could not set team tier based on "{args[1]}". You can use a name ("gold") or tier number ("2"). ')
            
            if not team.house:
                return await ctx.send(f'Team **{team.name}** does not have a House affiliation. Set one with `{ctx.prefix}team_house` first.')
            
            logger.debug(f'Processing tier change for team {team.name}')
            old_tier = str(team.league_tier) if team.league_tier else 'NONE'
            team.league_tier = new_tier
            team.save()
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} set the league tier of Team {team.name} to {new_tier} from {old_tier}')
            
            async with ctx.typing():
                for member in team_role.members:
                    logger.debug(f'team_edit updating league roles for member {member.display_name}')
                    await update_member_league_roles(member)

                return await ctx.send(f'Changed league tier of team  **{team.name}** to {new_tier_name} ({new_tier}). Previous tier was {old_tier}. Team members have had their tier roles refreshed.')

        if ctx.invoked_with == 'team_edit' and args[1] == 'ARCHIVE':
            logger.debug(f'Attempting to archive team {team.name}')
            if team.house:
               logger.warn(f'Cannot archive due to house affiliation')
               return await ctx.send(f'Remove the house affiliation of team **{team.name}** first with `{ctx.prefix}team_house {args[0]} NONE`. Currently in {team.house.name}.')
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

        if season and (season == 1 or season == 2):
            return await ctx.send('Records from the first two seasons (ie. the dark ages when I did not exist) are mostly lost to antiquity, but some information remains:\n'
                '**The Sparkies** won Season 1 and **The Jets** won season 2, and if you squint you can just make out the records below:\nhttps://i.imgur.com/L7FPr1d.png')
        
        if season:
            title = f'Season {season} Records'
        else:
            title = f'League Records - All Seasons'

        tiers_list = models.Game.polychamps_tiers_by_season(season=season)  # List of league_tiers that had games in the given season
        output = [f'__**{title}**__']
        for tier in tiers_list:

            tier_name = settings.tier_lookup(tier)[1]
            output.append(f'\n__**{tier_name} Tier**__\n`Regular \u200b \u200b \u200b \u200b \u200b Post-Season`')
            season_records = models.Team.polychamps_tier_records(league_tier=tier, league_season=season)
            for sr in season_records:
                team_str = f'{sr.emoji} {sr.name}\n'
                line = f'{team_str}`{str(sr.regular_season_wins) + "W":.<3} {str(sr.regular_season_losses) + "L":.<3} {str(sr.regular_season_incomplete) + "I":.<3} - {str(sr.post_season_wins) + "W":.<3} {str(sr.post_season_losses) + "L":.<3} {sr.post_season_incomplete}I`'
                output.append(line.replace(".", "\u200b "))

        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content='\n'.join(output))


    @commands.command(aliases=['joinnovas'])
    async def novas(self, ctx, *, arg=None):
        """ Join yourself to the Novas team
        """

        player, _ = models.Player.get_by_discord_id(discord_id=ctx.author.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick, guild_id=ctx.guild.id)
        if not player:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'*{ctx.author.name}* was found in the server but is not registered with me. '
                f'Players can register themselves with `{ctx.prefix}setname Your Mobile Name`.')

        on_team, player_team = models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=ctx.author)
        if on_team:
            return await ctx.send(f'You are already a member of team *{player_team.name}* {player_team.emoji}. Server staff is required to remove you from a team.')

        novas_role = discord.utils.get(ctx.guild.roles, name='The Novas')
        newbie_role = discord.utils.get(ctx.guild.roles, name='Newbie')

        if not novas_role:
            return await ctx.send('Error finding Novas role. Searched *The Novas*.')


        await ctx.author.add_roles(novas_role, reason='Joining Novas')
        await ctx.send(f'Congrats, you are now a member of the **The Novas**! To join the fight go to a bot channel and type `{ctx.prefix}novagames`')

        if newbie_role:
            await ctx.author.remove_roles(newbie_role, reason='Joining Novas')

    @commands.command(usage='', aliases=['trade'])
    # @settings.is_mod_check()
    @settings.in_bot_channel_strict()
    async def promote(self, ctx, *, args=None):
        """
        *Mod:* Generate a trade or promotion image

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

        async def arg_to_image_url(image_arg: str, position: int = 0):
            if image_arg[:4] == 'http':
                # passed raw image url
                return image_arg, '#00ff00' if position == 0 else '#ff0000'
            else:
                team_matches = models.Team.get_by_name(team_name=image_arg, guild_id=ctx.guild.id, require_exact=False)
                if len(team_matches) == 1:
                    # passed name of team. use team image url.
                    team_role = utilities.guild_role_by_name(ctx.guild, name=team_matches[0].name, allow_partial=False)
                    return team_matches[0].image_url, team_role.colour.to_rgb()
                else:
                    guild_matches = await utilities.get_guild_member(ctx, image_arg)
                    if len(guild_matches) == 1:
                        # passed member mention. use profile picture/avatar
                        return guild_matches[0].display_avatar.replace(size=512), \
                            '#00ff00' if position == 0 else '#ff0000'
                    else:
                        raise ValueError(f'Cannot convert *{image_arg}* to an image.')

        try:
            left_image, right_arrow_colour = await arg_to_image_url(args[2])
            right_image, left_arrow_colour = await arg_to_image_url(args[3], position=1)
        except ValueError as e:
            return await ctx.send(f'Cannot convert one of your arguments to an image: {e}\nMust be an image URL, member name, or team name.')

        if ctx.invoked_with == 'promote':
            arrows = [['u', '#00ff00']]
        else:
            arrows = [['r', right_arrow_colour], ['l', left_arrow_colour]]

        try:
            fs = imgen.arrow_card(top_string, bottom_string, left_image, right_image, arrows)
        except UnidentifiedImageError as e:
            logger.warn(f'UnidentifiedImageError: {e}')
            return await ctx.send(f'Image is formatted incorrectly. Use an image URL that links directly to a file. {e}')
        await ctx.send(file=fs)

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

        if not team.image_url:
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
        fs = imgen.player_draft_card(member=draftee, team_role=draft_team_role, selecting_string=selecting_string)

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
            filename = await self.bot.loop.run_in_executor(None, async_call_export_func)
            with open(filename, 'rb') as f:
                file = io.BytesIO(f.read())
            file = discord.File(file, filename=filename)
            await ctx.send(f'{ctx.author.mention}, your export is complete. Wrote to `{filename}`', file=file)

    @discord.app_commands.command(name="bid", description="Bid on a free agent")
    @discord.app_commands.describe(amount="Amount of FAT to bid", player="The free agent you are bidding on")
    @discord.app_commands.guilds(discord.Object(settings.server_ids['polychampions']))
    async def bid(self, interaction: discord.Interaction, amount: discord.app_commands.Range[int, 1, None], player: discord.Member):
        is_leader = len(utilities.get_matching_roles(interaction.user, [leader_role_name, coleader_role_name])) > 0
        if not is_leader:
            await interaction.response.send_message(f'You must be a house leader or co-leader to bid.', ephemeral=True)
            return

        is_freeagent = len(utilities.get_matching_roles(player, [free_agent_role_name])) > 0
        if not is_freeagent:
            await interaction.response.send_message(f'{player.display_name} is not a free agent.', ephemeral=True)
            return

        current_auction = models.Auction.select().where(models.Auction.ongoing == True).first()
        if not current_auction:
            await interaction.response.send_message(f'There is no ongoing auction.', ephemeral=True)
            return

        bidder, _ = models.Player.get_by_discord_id(interaction.user.id, interaction.guild.id)
        p, _ = models.Player.get_by_discord_id(player.id, interaction.guild.id)

        in_preferred_houses = models.PlayerHousePreference.player_prefers_house(p.id, bidder.team.house.id)
        if not in_preferred_houses:
            await interaction.response.send_message(
                f'Your house is not in {player.display_name}\'s preferred houses.',
                ephemeral=True
            )
            return

        previous_bids = models.Bid.select().where(
            (models.Bid.auction == current_auction) &
            (models.Bid.player == p) &
            (models.Bid.house == bidder.team.house)
        )

        for bid in previous_bids:
            if bid.amount >= amount:
                await interaction.response.send_message(f'Your house has already bid {bid.amount} on this player, you cannot lower your bid!', ephemeral=True)
                return

        models.Bid.create(auction=current_auction, amount=amount, player=p, bidder=bidder, house=bidder.team.house)
        await interaction.response.send_message(f'You bid {amount} on {player.display_name}.', ephemeral=True)

    @discord.app_commands.command(name="select-houses", description="Select the houses that you are interested in joining")
    @discord.app_commands.guilds(discord.Object(settings.server_ids['polychampions']))
    async def select_houses(self, interaction: discord.Interaction):
        is_freeagent = len(utilities.get_matching_roles(interaction.user, [free_agent_role_name])) > 0
        if not is_freeagent:
            await interaction.response.send_message(f'You must be a free agent to use this command.', ephemeral=True)
            return

        current_auction = models.Auction.select().where(models.Auction.ongoing == True).first()
        if current_auction:
            await interaction.response.send_message("You cannot select your preferences while an auction is ongoing.", ephemeral=True)
            return

        select_menu = HouseSelectMenu()
        clear_button = ClearPreferencesButton()

        view = View()
        view.add_item(select_menu)
        view.add_item(clear_button)

        await interaction.response.send_message(
            content="Select the houses you are interested in joining:",
            view=view,
            ephemeral=True
        )

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

    @tasks.loop(hours=1)
    async def auction_task(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        week_num = now.isocalendar()[1]

        auction_channel = self.bot.get_channel(1327702121130233969)  # free-agent-picks
        current_auction = models.Auction.select().where(models.Auction.ongoing == True).first()
        if now.weekday() == 5 and now.hour == 10 and week_num % 2 == 0:
            # Start auction
            if current_auction:
                return

            models.Auction.create(ongoing=True)
            message = "<@&1327333445180985398> <@&1327333522389602397> <@&1327547367590989855>\nThe Free Agent Auction is now open. Feel free to place your bids using /bid"
            await auction_channel.send(message)
        elif (now.weekday() == 6 and now.hour == 22 and week_num % 2 == 0) or (now.weekday() == 1 and now.hour == 10 and week_num % 2 == 1):
            # Send rankings & conclude auction for free agents with 1 bid
            if not current_auction:
                return
            
            if now.weekday() == 6 and current_auction.r1_done or now.weekday() == 1 and current_auction.r2_done:
                return
            
            await self.dm_auction_ranking(current_auction)
            single_bid_players = self.get_single_bid_players(current_auction)
            players = await self.conclude_players_auction(single_bid_players)
            for player, house_name, price in players:
                await auction_channel.send(f"<@{player}> to {house_name} for {price}!")
            
            if current_auction.r1_done:
                current_auction.r2_done = True
            else:
                current_auction.r1_done = True
            current_auction.save()
        elif now.weekday() == 2 and now.hour == 22 and week_num % 2 == 1:
            # Conclude auction
            if not current_auction:
                return
            
            highest_bids, tied_highest_bids = self.get_players_highest_bids(current_auction)
            players = await self.conclude_players_auction(highest_bids)
            for player, house_name, price in players:
                await auction_channel.send(f"<@{player}> to {house_name} for {price} FAT!")
            
            for player, houses, price in tied_highest_bids:
                await auction_channel.send(f"<@{player}> has tied bids from {', '.join(houses)} ({price} FAT). Please DM <@1327775289115152484> to choose which house you want to join.")
            
            current_auction.ongoing = False
            current_auction.save()

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
