import discord
from discord.ext import commands, tasks
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
import random
import modules.imgen as imgen

logger = logging.getLogger('polybot.' + __name__)


grad_role_name = 'Nova Grad'           # met graduation requirements and is eligible to sign up for draft
draftable_role_name = 'Draftable'      # signed up for current draft
free_agent_role_name = 'Free Agent'    # signed up for a prior draft but did not get drafted
novas_role_name = 'The Novas'          # Umbrella newbie role that all of above should also have
league_role_name = 'League Member'     # Umbrella role for all Pro+Junior members
pro_member_role_name = 'Pro Player'    # Umbrella role for all Pro members
jr_member_role_name = 'Junior Player'  # Umbrella role for all Junior memebrs

league_teams = [('Ronin', ['The Ronin', 'The Bandits']),
    ('Jets', ['The Jets', 'The Cropdusters']),
    ('Bombers', ['The Bombers', 'The Dynamite']),
    ('Lightning', ['The Lightning', 'The Pulse']),
    ('Cosmonauts', ['The Cosmonauts', 'The Space Cadets']),
    ('Crawfish', ['The Crawfish', 'The Shrimps']),
    ('Sparkies', ['The Sparkies', 'The Pups']),
    ('Wildfire', ['The Wildfire', 'The Flames']),
    ('Mallards', ['The Mallards', 'The Drakes']),
    ('Plague', ['The Plague', 'The Rats']),
    ('Dragons', ['The Dragons', 'The Narwhals']),
    ('Jalapenos', ['The Jalapenos', 'The Chili Peppers'])
]

league_team_channels = []
next_nova_newbie = 'Nova Red'  # Alternates between Red/Blue. Seeded randomly by Cog.on_ready()

def get_league_roles(guild=None):

    if not guild:
        guild = settings.bot.get_guild(settings.server_ids['polychampions']) or settings.bot.get_guild(settings.server_ids['test'])

    pro_role_names = [a[1][0] for a in league_teams]
    junior_role_names = [a[1][1] for a in league_teams]
    team_role_names = [a[0] for a in league_teams]

    pro_roles = [discord.utils.get(guild.roles, name=r) for r in pro_role_names]
    junior_roles = [discord.utils.get(guild.roles, name=r) for r in junior_role_names]
    team_roles = [discord.utils.get(guild.roles, name=r) for r in team_role_names]

    if None in pro_roles or None in junior_roles or None in team_roles:
        logger.warning(f'Problem loading at least one role in get_league_roles: {pro_roles} {junior_roles} {team_roles}')

    return team_roles, pro_roles, junior_roles


def get_umbrella_team_role(team_name: str):
    # given a team name like 'The Ronin' return the correspondng 'umbrella' team role object (Ronin)
    league_guild = settings.bot.get_guild(settings.server_ids['polychampions']) or settings.bot.get_guild(settings.server_ids['test'])
    if not league_guild:
        raise exceptions.CheckFailedError('PolyChampions guild not loaded in `league.py`')

    target_team_role = utilities.guild_role_by_name(league_guild, name=team_name, allow_partial=False)
    if not target_team_role:
        raise ValueError(f'No matching role found for team name "{team_name}"')

    team_roles, pro_roles, junior_roles = get_league_roles()

    if target_team_role in pro_roles:
        team_umbrella_role = team_roles[pro_roles.index(target_team_role)]
    elif target_team_role in junior_roles:
        team_umbrella_role = team_roles[junior_roles.index(target_team_role)]
    else:
        raise exceptions.CheckFailedError(f'Unexpected error in get_umbrella_team_role for input "{team_name}')

    return team_umbrella_role

def get_team_leadership(team_role):

    try:
        umbrella_role = get_umbrella_team_role(team_role.name)
    except exceptions.CheckFailedError as e:
        logger.warning(f'Could not get_team_leadership for team role {team_role}: {e}')
        return [], [], []

    leaders, coleaders, recruiters = [], [], []

    leader_role = utilities.guild_role_by_name(team_role.guild, name='Team Leader', allow_partial=False)
    coleader_role = utilities.guild_role_by_name(team_role.guild, name='Team Co-Leader', allow_partial=False)
    recruiter_role = utilities.guild_role_by_name(team_role.guild, name='Team Recruiter', allow_partial=False)

    for member in umbrella_role.members:
        if leader_role in member.roles:
            leaders.append(member)
        if coleader_role in member.roles:
            coleaders.append(member)
        if recruiter_role in member.roles:
            recruiters.append(member)

    return leaders, coleaders, recruiters


class league(commands.Cog):
    """
    Commands specific to the PolyChampions league, such as drafting-related commands
    """

    emoji_draft_signup = '🔆'
    emoji_draft_close = '⏯'
    emoji_draft_conclude = '❎'
    emoji_draft_list = [emoji_draft_signup, emoji_draft_close, emoji_draft_conclude]

    draft_open_format_str = f'The draft is open for signups! {{0}}\'s can react with a {emoji_draft_signup} below to sign up. {{1}} who have not graduated have until the end of the draft signup period to meet requirements and sign up.\n\n{{2}}'
    draft_closed_message = f'The draft is closed to new signups. Mods can use the {emoji_draft_conclude} reaction after players have been drafted to clean up the remaining players and delete this message.'

    def __init__(self, bot):

        self.bot = bot
        self.announcement_message = None  # Will be populated from db if exists

        if settings.run_tasks:
            self.task_send_polychamps_invite.start()

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

        if before.roles == after.roles:
            return

        if after.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
            return

        team_roles, pro_roles, junior_roles = get_league_roles(after.guild)
        league_role = discord.utils.get(after.guild.roles, name=league_role_name)
        pro_member_role = discord.utils.get(after.guild.roles, name=pro_member_role_name)
        jr_member_role = discord.utils.get(after.guild.roles, name=jr_member_role_name)
        player, team = None, None

        before_member_team_roles = [x for x in before.roles if x in pro_roles or x in junior_roles]
        member_team_roles = [x for x in after.roles if x in pro_roles or x in junior_roles]

        if before_member_team_roles == member_team_roles:
            return

        if len(member_team_roles) > 1:
            return logger.debug(f'Member has more than one team role. Abandoning League.on_member_update. {member_team_roles}')

        roles_to_remove = team_roles + [jr_member_role] + [pro_member_role] + [league_role]

        if member_team_roles:
            try:
                player = models.Player.get_or_except(player_string=after.id, guild_id=after.guild.id)
                team = models.Team.get_or_except(team_name=member_team_roles[0].name, guild_id=after.guild.id)
                player.team = team
                player.save()
            except exceptions.NoSingleMatch as e:
                logger.warning(f'League.on_member_update: could not load Player or Team for changing league member {after.display_name}: {e}')

            if member_team_roles[0] in pro_roles:
                team_umbrella_role = team_roles[pro_roles.index(member_team_roles[0])]
                roles_to_add = [team_umbrella_role, pro_member_role, league_role]
                log_message = f'{models.GameLog.member_string(after)} had pro team role **{member_team_roles[0].name}** added.'
            elif member_team_roles[0] in junior_roles:
                team_umbrella_role = team_roles[junior_roles.index(member_team_roles[0])]
                roles_to_add = [team_umbrella_role, jr_member_role, league_role]
                log_message = f'{models.GameLog.member_string(after)} had junior team role **{member_team_roles[0].name}** added.'
        else:
            roles_to_add = []  # No team role
            log_message = f'{models.GameLog.member_string(after)} had team role **{before_member_team_roles[0].name}** removed and is teamless.'

        member_roles = after.roles.copy()
        member_roles = [r for r in member_roles if r not in roles_to_remove]

        roles_to_add = [r for r in roles_to_add if r]  # remove any Nones
        if roles_to_add:
            member_roles = member_roles + roles_to_add

        logger.debug(f'Attempting to update member {after.display_name} role set to {member_roles}')
        # using member.edit() sets all the roles in one API call, much faster than using add_roles and remove_roles which uses one API call per role change, or two calls total if atomic=False
        await after.edit(roles=member_roles, reason='Detected change in team membership')

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
        global next_nova_newbie
        next_nova_newbie = random.choice(['Nova Red', 'Nova Blue'])

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
        draftable_role = discord.utils.get(member.guild.roles, name=draftable_role_name)

        confirm_message = await channel.send(f'{member.mention}, react below to confirm the conclusion of the current draft. '
            f'{len(free_agent_role.members)} members will lose the **{free_agent_role_name}** role and {len(draftable_role.members)} members with the **{draftable_role_name}** role will lose that role and become the current crop with the **{free_agent_role_name}** role.\n'
            '*If you do not react within 30 seconds the draft will remain open.*', delete_after=35)
        await confirm_message.add_reaction('✅')

        logger.debug('waiting for reaction confirmation')

        def check(reaction, user):
            e = str(reaction.emoji)
            return ((user == member) and (reaction.message.id == confirm_message.id) and e == '✅')

        try:
            reaction, user = await self.bot.wait_for('reaction_add', check=check, timeout=33)

        except asyncio.TimeoutError:
            logger.debug(f'No reaction to confirmation message.')
            return

        result_message_list = [f'Draft successfully closed by {member.mention}']
        self.announcement_message = None

        async with channel.typing():
            old_free_agents = free_agent_role.members.copy()
            new_free_agents_count = len(draftable_role.members)
            for old_free_agent in free_agent_role.members:
                await old_free_agent.remove_roles(free_agent_role, reason='Purging old free agents')
                logger.debug(f'Removing free agent role from {old_free_agent.name}')

                result_message_list.append(f'Removing free agent role from {old_free_agent.name} {old_free_agent.mention}')

            for new_free_agent in draftable_role.members:
                await new_free_agent.add_roles(free_agent_role, reason='New crop of free agents')
                logger.debug(f'Adding free agent role to {new_free_agent.name}')

                await new_free_agent.remove_roles(draftable_role, reason='Purging old free agents')
                logger.debug(f'Removing draftable role from {new_free_agent.name}')
                if new_free_agent in old_free_agents:
                    result_message_list.append(f'Removing draftable role from and applying free agent role to {new_free_agent.name} {new_free_agent.mention}. They had it last week, too!')
                else:
                    result_message_list.append(f'Removing draftable role from and applying free agent role to {new_free_agent.name} {new_free_agent.mention}')

        for log_message in result_message_list:
            models.GameLog.write(guild_id=member.guild.id, message=log_message)
        await utilities.send_to_log_channel(member.guild, '\n'.join(result_message_list))
        self.delete_draft_config(member.guild.id)

        try:
            await message.clear_reactions()
            new_message = message.content.replace(self.draft_closed_message, f'~~{self.draft_closed_message}~~') + f'\nThis draft is concluded. {new_free_agents_count} members went undrafted and became free agents.'
            await message.edit(content=new_message)
        except discord.DiscordException as e:
            logger.warning(f'Could not clear reactions or edit content in concluded draft message: {e}')

    async def close_draft_emoji_added(self, member, channel, message):
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft close reaction added by {member.name} to draft announcement {announce_message_link}')
        grad_role = discord.utils.get(member.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(member.guild.roles, name=novas_role_name)

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
            new_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, draft_config['draft_message'])
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
        draftable_role = discord.utils.get(member.guild.roles, name=draftable_role_name)
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft signup reaction added by {member.name} to draft announcement {announce_message_link}')

        if reaction_added:
            if draft_opened and grad_role in member.roles:
                # An eligible member signing up for the draft
                try:
                    await member.add_roles(draftable_role, reason='Member added themselves to draft')
                except discord.DiscordException as e:
                    logger.error(f'Could not add draftable role in signup_emoji_clicked: {e}')
                    return
                else:
                    member_message = f'You are now signed up for the next draft. If you would like to remove yourself, just remove the reaction you just placed.\nAlthough you may have a preference on which team drafts you, be aware that you may be chosen by **any** team. You must make a good faith effort to play and integrate with that team to avoid a penalty for poor sportsmanship.\n{announce_message_link}'
                    log_message = f'{member.mention} ({member.name}) reacted to the draft and received the {draftable_role.name} role.'
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
            if draftable_role in member.roles:
                # Removing member from draft, same behavior whether draft is opened or closed
                try:
                    await member.remove_roles(draftable_role, reason='Member removed from draft')
                except discord.DiscordException as e:
                    logger.error(f'Could not remove draftable role in signup_emoji_clicked: {e}')
                    return
                else:
                    member_message = f'You have been removed from the next draft. You can sign back up at the announcement message:\n{announce_message_link}'
                    log_message = f'{member.mention} ({member.name}) removed their draft reaction and has lost the {draftable_role.name} role.'
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

    @commands.command(aliases=['ds'], usage=None)
    @settings.is_mod_check()
    async def newdraft(self, ctx, channel_override: typing.Optional[discord.TextChannel], *, added_message: str = ''):

        """
        *Mod:* Post a new draft signup announcement

        Will post a default draft signup announcement into a default announcement channel.

        Three emoji reactions are used to interact with the draft.
        The first can be used by any member who has the Nova Grad role, and they will receive the Draftable role when they react. They can also unreact to lose the role.

        The play/pause reaction is mod-only and can be used to close or re-open the draft to new signups.
        A draftable member can remove themselves from the draft while it is closed, but any new signups will be rejected.

        The ❎ reaction should be used by a mod after the draft has been performed and members have been put onto their new teams.
        Any current Free Agents will be removed from that role. Anyone remaining as Draftable will lose that role and gain the Free Agent role.

        Hitting this reaction will tell you exactly how many members will be affected by role changes and ask for a confirmation.

        You can optionally direct the announcement to a non-default channel, and add an optional message to the end of the announcement message.

        **Examples**
        `[p]newdraft` Normal usage with a generic message
        `[p]newdraft #special-channel` Direct message to a non-standard channel
        `[p]newdraft Signups will be closing on Sunday and the draft will occur the following Sunday` Add an extra message to the announcement.

        """

        # post message in announcements (optional argument of a different channel if mod wants announcement to go elsewhere?)
        # listen for reactions in a check
        # if reactor has Free Agent role, PM success message and apply Draftable role
        # if not, PM failure message and remove reaction
        # remove Draftable role if user removes their reaction

        # when draft is concluded, everyone who has free agent role has it removed, everyone who has draftable has that removed and is given free agent role

        if channel_override:
            announcement_channel = channel_override
        else:
            # use default channel for announcement
            if ctx.guild.id == settings.server_ids['polychampions']:
                announcement_channel = ctx.guild.get_channel(447986488152686594)  # #server-announcements
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
                logger.warning(f'Error loading existing draft announcement message in newdraft command: {e}')

        grad_role = discord.utils.get(ctx.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(ctx.guild.roles, name=novas_role_name)

        formatted_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, added_message)
        announcement_message = await announcement_channel.send(formatted_message)

        await announcement_message.add_reaction(self.emoji_draft_signup)
        await announcement_message.add_reaction(self.emoji_draft_close)
        await announcement_message.add_reaction(self.emoji_draft_conclude)

        await utilities.send_to_log_channel(ctx.guild, f'Draft created by {ctx.author.mention}\n'
            f'https://discord.com/channels/{ctx.guild.id}/{announcement_channel.id}/{announcement_message.id}')

        if announcement_channel.id != ctx.message.channel.id:
            await ctx.send(f'Draft announcement has been posted in the announcement channel.')

        draft_config['announcement_message'] = announcement_message.id
        draft_config['announcement_channel'] = announcement_message.channel.id
        draft_config['date_opened'] = str(datetime.datetime.today())
        draft_config['draft_open'] = True
        draft_config['draft_message'] = added_message

        self.announcement_message = announcement_message.id
        self.save_draft_config(ctx.guild.id, draft_config)

    @commands.command(aliases=['league_balance'])
    @settings.in_bot_channel()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def balance(self, ctx, *, arg=None):
        """ Print some stats on PolyChampions league balance

            Default sort is the Draft Score. Include arguments d2 or d3 or d4 to see alternate draft scores.
            ie: `[p]balance d3`
        """
        # import statistics

        league_balance = []
        indent_str = '\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0'
        guild_id = settings.server_ids['polychampions']
        mia_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(guild_id, 'inactive_role'))
        # season_inactive_role = discord.utils.get(ctx.guild.roles, name='Season Inactive')

        draft_str = 'combined ELO of top 10 players (Senior or Junior)'

        async with ctx.typing():
            for team, team_roles in league_teams:

                pro_role = discord.utils.get(ctx.guild.roles, name=team_roles[0])
                junior_role = discord.utils.get(ctx.guild.roles, name=team_roles[1])
                junior_only_handicap = False  # Set to true for teams with a junior team but no pro team (Jalapenos)

                if pro_role:
                    pro_role_members = pro_role.members
                else:
                    logger.debug(f'Could not load pro role matching {team_roles[0]}')
                    pro_role_members = []
                if junior_role:
                    junior_role_members = junior_role.members
                    if not pro_role:
                        junior_only_handicap = True
                else:
                    logger.warning(f'Could not load junior role matching {team_roles[1]} - skipping loop')
                    continue

                try:
                    if junior_only_handicap:
                        pro_team = None
                    else:
                        pro_team = models.Team.get_or_except(team_roles[0], guild_id)
                    junior_team = models.Team.get_or_except(team_roles[1], guild_id)
                except exceptions.NoSingleMatch:
                    logger.warning(f'Could not load one team from database, using args: {team_roles}')
                    continue

                pro_members, junior_members, pro_discord_ids, junior_discord_ids, mia_count = [], [], [], [], 0

                logger.debug(f'Processing team matching {team_roles[0]} {team_roles[1]}')
                for member in pro_role_members:
                    if mia_role in member.roles:
                        mia_count += 1
                    else:
                        pro_members.append(member)
                        pro_discord_ids.append(member.id)
                for member in junior_role_members:
                    if mia_role in member.roles:
                        mia_count += 1
                    else:
                        junior_members.append(member)
                        junior_discord_ids.append(member.id)

                combined_elo, player_games_total = models.Player.average_elo_of_player_list(list_of_discord_ids=junior_discord_ids + pro_discord_ids, guild_id=guild_id, weighted=True)

                pro_elo, _ = models.Player.average_elo_of_player_list(list_of_discord_ids=pro_discord_ids, guild_id=guild_id, weighted=False)
                junior_elo, _ = models.Player.average_elo_of_player_list(list_of_discord_ids=junior_discord_ids, guild_id=guild_id, weighted=False)

                sorted_elo_list = models.Player.discord_ids_to_elo_list(list_of_discord_ids=junior_discord_ids + pro_discord_ids, guild_id=guild_id)
                draft_score = sum(sorted_elo_list[:10])
                logger.debug(f'sorted_elo_list: {sorted_elo_list}')
                if junior_only_handicap:
                    logger.debug('Applying 20% junior_only_handicap')
                    draft_score = int(draft_score * 1.2)
                logger.debug(f'draft_score: {draft_score}')
                league_balance.append(
                    (team,  # 0
                     pro_team,  # 1
                     junior_team,  # 2
                     len(pro_members),  # 3
                     len(junior_members),  # 4
                     mia_count,  # 5
                     combined_elo,  # 6
                     player_games_total,  # 7
                     pro_elo,  # 8
                     junior_elo,  # 9
                     draft_score)  # 10
                )

        league_balance.sort(key=lambda tup: tup[10], reverse=True)     # sort by draft score

        embed = discord.Embed(title='PolyChampions League Balance Summary')
        for team in league_balance:
            if team[1]:
                # normal pro+junior entry
                field_name = (f'{team[1].emoji} {team[0]} ({team[3] + team[4]}) {team[2].emoji}\n{indent_str} \u00A0\u00A0 ActiveELO™: {team[6]}'
                                  f' \u00A0 - \u00A0  Draft Score: {team[10]}'
                                  f'\n{indent_str} \u00A0\u00A0 Recent member-games: {team[7]}')
                field_value = (f'-{indent_str}__**{team[1].name}**__ ({team[3]}) **ELO: {team[1].elo}** (Avg: {team[8]})\n'
                       f'-{indent_str}__**{team[2].name}**__ ({team[4]}) **ELO: {team[2].elo}** (Avg: {team[9]})\n')
            else:
                # junior only entry
                field_name = (f'{team[2].emoji} {team[0]} ({team[3] + team[4]}) {team[2].emoji}\n{indent_str} \u00A0\u00A0 ActiveELO™: {team[6]}'
                                  f' \u00A0 - \u00A0  Draft Score: {team[10]}'
                                  f'\n{indent_str} \u00A0\u00A0 Recent member-games: {team[7]}')
                field_value = (f'-{indent_str}__**{team[2].name}**__ ({team[4]}) **ELO: {team[2].elo}** (Avg: {team[9]})\n')

            embed.add_field(name=field_name, value=field_value, inline=False)

        embed.set_footer(text=f'ActiveELO™ is the mean ELO of members weighted by how many games each member has played in the last 30 days. Draft Score is {draft_str}.')

        await ctx.send(embed=embed)

    @commands.command(aliases=['jrseason'], usage='[season #]')
    @settings.in_bot_channel()
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def season(self, ctx, *, arg=None):
        """
        Display team records for one or all seasons

        **Examples**
        `[p]season` Records for all seasons (Pro teams)
        `[p]jrseason` Records for all seasons (Junior teams)
        `[p]season 7` Records for a specific season (Pro teams)
        `[p]jrseason 7` Records for a specific season (Junior teams)
        `[p]season 7 games` List all games from Season 7 in summary form (very spammy)
        """

        season, list_games = None, False
        if arg:
            if 'games' in arg:
                list_games = True
                arg = arg.replace('games', '').strip()
            try:
                season = int(arg)
            except ValueError:
                return await ctx.send(f'Invalid argument. Leave blank for all seasons or use an integer like `{ctx.prefix}{ctx.invoked_with} 8`')

        standings = []

        if season and (season == 1 or season == 2):
            return await ctx.send(f'Records from the first two seasons (ie. the dark ages when I did not exist) are mostly lost to antiquity, but some information remains:\n'
                f'**The Sparkies** won Season 1 and **The Jets** won season 2, and if you squint you can just make out the records below:\nhttps://i.imgur.com/L7FPr1d.png')
        if ctx.invoked_with == 'jrseason':
            pro_value = 0
            pro_str = 'Junior'
        else:
            pro_value = 1
            pro_str = 'Pro'

        if arg:
            title = f'Season {arg} {pro_str} Records'
        else:
            title = f'{pro_str} Records - All Seasons'

        poly_teams = models.Team.select().where(
            (models.Team.guild_id == settings.server_ids['polychampions']) & (models.Team.is_hidden == 0) & (models.Team.pro_league == pro_value)
        )

        async with ctx.typing():
            if list_games:
                # list all games of this season
                if not season:
                    return await ctx.send(f'You must specify a season to list games. **Example**: `{ctx.prefix}{ctx.invoked_with} 7 games`')
                season_games, _, _ = models.Game.polychamps_season_games(league=pro_str.lower(), season=season)
                season_games = season_games.order_by(models.Game.id)
                output = [f'__**Season {season} Games**__']
                for game in season_games:
                    if game.is_confirmed:
                        losing_side = game.gamesides[0] if game.gamesides[1] == game.winner else game.gamesides[1]
                        winning_side = game.winner
                        winning_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in winning_side.roster()]
                        losing_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in losing_side.roster()]
                        output_str = f'`{game.id}` *{game.name}* - **{winning_side.name()}** ({" / ".join(winning_roster)}) defeats **{losing_side.name()}** ({" / ".join(losing_roster)})'
                        output.append(output_str)
                    else:
                        side1, side2 = game.gamesides[0], game.gamesides[1]
                        side1_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in side1.roster()]
                        side2_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in side2.roster()]
                        output.append(f'`{game.id}` *{game.name}* - **{side1.name()}** ({" / ".join(side1_roster)}) currently battling **{side2.name()}** ({" / ".join(side2_roster)}) ')
            else:
                # regular standings summary
                for team in poly_teams:
                    season_record = team.get_season_record(season=season)  # (win_count_reg, loss_count_reg, incomplete_count_reg, win_count_post, loss_count_post, incomplete_count_post)
                    if not season_record:
                        logger.warning(f'No season record returned for team {team.name}')
                        continue

                    standings.append((team, season_record[0], season_record[1], season_record[2], season_record[3], season_record[4], season_record[5]))

                standings = sorted(standings, key=lambda x: (-x[4], -x[1], x[2]))  # should sort first by post-season wins desc, then wins descending then losses ascending

                output = [f'__**{title}**__\n`Regular \u200b \u200b \u200b \u200b \u200b Post-Season`']

                for standing in standings:
                    team_str = f'{standing[0].emoji} {standing[0].name}\n'
                    line = f'{team_str}`{str(standing[1]) + "W":.<3} {str(standing[2]) + "L":.<3} {str(standing[3]) + "I":.<3} - {str(standing[4]) + "W":.<3} {str(standing[5]) + "L":.<3} {standing[6]}I`'
                    output.append(line.replace(".", "\u200b "))

        await utilities.buffered_send(destination=ctx, content='\n'.join(output))

    @commands.command(aliases=['joinnovas'])
    async def novas(self, ctx, *, arg=None):
        """ Join yourself to the Novas team
        """

        player, _ = models.Player.get_by_discord_id(discord_id=ctx.author.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick, guild_id=ctx.guild.id)
        if not player:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'*{ctx.author.name}* was found in the server but is not registered with me. '
                f'Players can register themselves with `{ctx.prefix}setcode POLYTOPIA_CODE`.')

        on_team, player_team = models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=ctx.author)
        if on_team:
            return await ctx.send(f'You are already a member of team *{player_team.name}* {player_team.emoji}. Server staff is required to remove you from a team.')

        red_role = discord.utils.get(ctx.guild.roles, name='Nova Red')
        blue_role = discord.utils.get(ctx.guild.roles, name='Nova Blue')
        novas_role = discord.utils.get(ctx.guild.roles, name='The Novas')
        newbie_role = discord.utils.get(ctx.guild.roles, name='Newbie')

        if not red_role or not blue_role or not novas_role:
            return await ctx.send(f'Error finding Novas roles. Searched for *Nova Red* and *Nova Blue* and *The Novas*.')

        # TODO: team numbers may be inflated due to inactive members. Can either count up only player recency, or easier but less effective way
        # would be to have $deactivate remove novas roles and make them rejoin if they come back

        global next_nova_newbie
        if next_nova_newbie == 'Nova Blue':
            await ctx.author.add_roles(blue_role, novas_role, reason='Joining Nova Blue')
            await ctx.send(f'Congrats, you are now a member of the **Nova Blue** team! To join the fight go to a bot channel and type `{ctx.prefix}novagames`')
            next_nova_newbie = 'Nova Red'
        else:
            await ctx.author.add_roles(red_role, novas_role, reason='Joining Nova Red')
            await ctx.send(f'Congrats, you are now a member of the **Nova Red** team! To join the fight go to a bot channel and type `{ctx.prefix}novagames`')
            next_nova_newbie = 'Nova Blue'
        if newbie_role:
            await ctx.author.remove_roles(newbie_role, reason='Joining Novas')

    @commands.command(usage='', aliases=['trade'])
    # @settings.is_mod_check()
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
        args = shlex.split(args)
        for arg in args:
            print(arg)

        if len(args) != 4:
            return await ctx.send(f'Usage error (expected 4 arguments and found {len(args)})\n**Example**: `{ctx.prefix}{ctx.invoked_with} "Top Text" "Bottom Text" @PromotedPlayer Ronin`')

        top_string = '' if args[0].upper() == 'NONE' else args[0]
        bottom_string = '' if args[1].upper() == 'NONE' else args[1]

        async def arg_to_image_url(image_arg: str):
            if image_arg[:4] == 'http':
                # passed raw image url
                return image_arg
            else:
                team_matches = models.Team.get_by_name(team_name=image_arg, guild_id=ctx.guild.id, require_exact=False)
                if len(team_matches) == 1:
                    # passed name of team. use team image url.
                    return team_matches[0].image_url
                else:
                    guild_matches = await utilities.get_guild_member(ctx, image_arg)
                    if len(guild_matches) == 1:
                        # passed member mention. use profile picture/avatar
                        return guild_matches[0].avatar_url_as(size=256, format='png')

                    else:
                        raise ValueError(f'Cannot convert *{image_arg}* to an image.')

        try:
            left_image = await arg_to_image_url(args[2])
            right_image = await arg_to_image_url(args[3])
        except ValueError as e:
            return await ctx.send(f'Cannot convert one of your arguments to an image: {e}\nMust be an image URL, member name, or team name.')

        if ctx.invoked_with == 'promote':
            arrows = [['r', '#00ff00']]
        else:
            arrows = [['r', '#00ff00'], ['l', '#ff0000']]

        print(left_image, right_image)
        fs = imgen.arrow_card(top_string, bottom_string, left_image, right_image, arrows)
        await ctx.send(file=fs)

    @commands.command(usage='@Draftee TeamName')
    @settings.is_mod_check()
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

        team_roles, pro_roles, junior_roles = get_league_roles(ctx.guild)

        draft_team_role = utilities.guild_role_by_name(ctx.guild, name=team.name, allow_partial=False)
        if not draft_team_role:
            return await ctx.send(f'Found matching team but no matching role with name *{team.name}*!')

        if draft_team_role in pro_roles:
            team_umbrella_role = team_roles[pro_roles.index(draft_team_role)]
        elif draft_team_role in junior_roles:
            team_umbrella_role = team_roles[junior_roles.index(draft_team_role)]
        else:
            return await ctx.send(f'Found matching team and role but `league_teams` is misconfigured. Notify <@{settings.owner_id}>.')

        selecting_string = team_umbrella_role.name if team_umbrella_role else draft_team_role.name
        fs = imgen.player_draft_card(member=draftee, team_role=draft_team_role, selecting_string=selecting_string)

        await ctx.send(file=fs)

    @commands.command(aliases=['freeagents', 'draftable', 'ble', 'bge', 'roleeloany'], usage='[sort] [role name list]')
    @settings.in_bot_channel_strict()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def roleelo(self, ctx, *, arg=None):
        """Prints list of players with a given role and their ELO stats

        You can check more tha one role at a time by separating them with a comma.
        By default will return members with ALL of the specified roles.
        Use `[p]roleeloany` to list members with ANY of the roles.

        Use one of the following options as the first argument to change the sorting:
        **g_elo** - Global ELO (default)
        **elo** - Local ELO
        **games** - Total number of games played
        **recent** - Recent games played (14 days)

        Members with the Inactive role will be skipped unless it is explicitly listed.
        Include `-file` in the argument for a CSV attachment.

        This command has some shortcuts:
        `[p]draftable` - List members with the Draftable role
        `[p]freeagents` - List members with the Free Agent role

        **Examples**
        `[p]roleelo novas` - List all members with a role matching 'novas'
        `[p]roleelo novas -file` - Load all 'nova' members into a CSV file
        `[p]roleelo elo novas` - List all members with a role matching 'novas', sorted by local elo
        `[p]draftable recent` - List all members with the Draftable role sorted by recent games
        `[p]roleelo g_elo crawfish, ronin` - List all members with any of two roles, sorted by global elo
        """
        args = arg.split() if arg else []
        usage = (f'**Example usage:** `{ctx.prefix}roleelo Ronin`\n'
                    f'See `{ctx.prefix}help roleelo` for sorting options and more examples.')

        if ctx.invoked_with in ['ble', 'bge']:
            return await ctx.send(f'The `{ctx.prefix}{ctx.invoked_with}` command has been replaced by `{ctx.prefix}roleelo`\n{usage}')

        if args and '-file' in args:
            args.remove('-file')
            file_export = True
        else:
            file_export = False

        if args and args[0].upper() == 'G_ELO':
            sort_key = 1
            args = args[1:]
            sort_str = 'Global ELO'
        elif args and args[0].upper() == 'ELO':
            sort_key = 2
            args = args[1:]
            sort_str = 'Local ELO'
        elif args and args[0].upper() == 'GAMES':
            sort_key = 3
            args = args[1:]
            sort_str = 'total games played'
        elif args and args[0].upper() == 'RECENT':
            sort_key = 4
            args = args[1:]
            sort_str = 'recent games played'
        else:
            sort_key = 1  # No argument supplied, use g_elo default
            # args = ' '.join(args)
            sort_str = 'Global ELO'

        if ctx.invoked_with == 'draftable':
            args = [draftable_role_name]
        elif ctx.invoked_with == 'freeagents':
            args = [free_agent_role_name]
        elif ctx.invoked_with == 'roleelo':
            if not args:
                return await ctx.send(f'No role name was supplied.\n{usage}')

        player_list = []
        player_obj_list, member_obj_list = [], []

        args = [a.strip().title() for a in ' '.join(args).split(',')]  # split arguments by comma

        roles = [discord.utils.find(lambda r: arg.upper() in r.name.upper(), ctx.guild.roles) for arg in args]
        roles = [r for r in roles if r]  # remove Nones

        if ctx.invoked_with == 'roleeloany':
            members = list(set(member for role in roles if role for member in role.members))
            method = 'any'
        else:
            members = [member for member in ctx.guild.members if all(role in member.roles for role in roles)]
            method = 'all'

        if not roles:
            return await ctx.send(f'Could not load roles from the guild matching **{"/".join(args)}**. Multiple roles should be separated by a comma.')

        inactive_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        for member in members:
            if inactive_role and inactive_role in member.roles and inactive_role not in roles:
                logger.debug(f'Skipping {member.name} since they have Inactive role')
                continue

            try:
                dm = models.DiscordMember.get(discord_id=member.id)
                player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
                player_obj_list.append(player)
                member_obj_list.append(member)
            except peewee.DoesNotExist:
                logger.debug(f'Player {member.name} not registered.')
                continue

            g_wins, g_losses = dm.get_record()
            wins, losses = player.get_record()
            recent_games = dm.games_played(in_days=14).count()
            all_games = dm.games_played().count()

            # TODO: Mention players without pinging them once discord.py 1.4 is out https://discordpy.readthedocs.io/en/latest/api.html#discord.TextChannel.send

            message = (f' {dm.mention()} **{player.name}**'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 {recent_games} games played in last 14 days, {all_games} all-time'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 ELO:  {dm.elo} *global* / {player.elo} *local*\n'
                f'\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 __W {g_wins} / L {g_losses}__ *global* \u00A0\u00A0 - \u00A0\u00A0 __W {wins} / L {losses}__ *local*\n')

            player_list.append((message, dm.elo, player.elo, all_games, recent_games, member, player))

        player_list.sort(key=lambda tup: tup[sort_key], reverse=False)     # sort the list by argument supplied

        message = []
        for player in player_list:
            message.append(player[0])

        if not player_list:
            await ctx.send(f'No matching players found.')
        elif file_export:
            import io

            player_obj_list = [p[6] for p in player_list]
            member_obj_list = [p[5] for p in player_list]
            def async_call_export_func():

                filename = utilities.export_player_data(player_list=player_obj_list, member_list=member_obj_list)
                return filename

            async with ctx.typing():
                filename = await self.bot.loop.run_in_executor(None, async_call_export_func)
                with open(filename, 'rb') as f:
                    file = io.BytesIO(f.read())
                file = discord.File(file, filename=filename)
                await ctx.send(f'Exporting {len(player_list)} active players with {method} of the following roles: **{"/".join([r.name for r in roles])}**\nLoaded into a file `{filename}`, sorted by {sort_str}', file=file)
        else:
            await ctx.send(f'Listing {len(player_list)} active members with {method} of the following roles: **{"/".join([r.name for r in roles])}** (sorted by {sort_str})...')

            message = []
            am = discord.AllowedMentions(everyone=False, users=False, roles=False)
            for player in player_list:
                message.append(player[0])
            async with ctx.typing():

                await utilities.buffered_send(destination=ctx, content=''.join(message).replace(".", "\u200b "), allowed_mentions=am)

    @commands.command()
    # @settings.in_bot_channel()
    @commands.cooldown(1, 120, commands.BucketType.channel)
    async def league_export(self, ctx, *, arg=None):
        """
        Export all league games to a CSV file

        Specifically includes all ranked 2v2 or 3v3 games
        """

        import io
        query = models.Game.select().where(
            (models.Game.is_confirmed == 1) & (models.Game.guild_id == settings.server_ids['polychampions']) & (models.Game.is_ranked == 1) &
            ((models.Game.size == [2, 2]) | (models.Game.size == [3, 3]))
        ).order_by(models.Game.date)

        def async_call_export_func():

            filename = utilities.export_game_data_brief(query=query)
            return filename

        if query:
            await ctx.send(f'Exporting {len(query)} game records. This might take a little while...')
        else:
            return await ctx.send(f'No matching games found.')

        async with ctx.typing():
            filename = await self.bot.loop.run_in_executor(None, async_call_export_func)
            with open(filename, 'rb') as f:
                file = io.BytesIO(f.read())
            file = discord.File(file, filename=filename)
            await ctx.send(f'{ctx.author.mention}, your export is complete. Wrote to `{filename}`', file=file)

    @tasks.loop(minutes=30.0)
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
            if wins_count < 5:
                logger.debug(f'Skipping {dm.name} - insufficient winning games')
                continue
            if dm.games_played(in_days=15).count() < 1:
                logger.debug(f'Skipping {dm.name} - insufficient recent games')
                continue
            if dm.elo_max > 1150:
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


async def broadcast_team_game_to_server(ctx, game):
    # When a PolyChamps game is created with a role-lock matching a league team, it will broadcast a message about the game
    # to that team's server, if it has a league_game_announce_channel channel configured.

    if ctx.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
        return

    role_locks = [gs.required_role_id for gs in game.gamesides if gs.required_role_id]
    roles = [ctx.guild.get_role(r_id) for r_id in role_locks if ctx.guild.get_role(r_id)]

    if not roles:
        return

    pro_role_names = [a[1][0] for a in league_teams]
    junior_role_names = [a[1][1] for a in league_teams]
    team_role_names = [a[0] for a in league_teams]

    for role in roles:
        if role.name in pro_role_names:
            team_name = role.name
            game_type = 'Pro Team'
        elif role.name in junior_role_names:
            team_name = role.name
            game_type = 'Junior Team'
        elif role.name in team_role_names:
            # Umbrella name like Ronin/Jets
            game_type = 'Full Team (Pros *and* Juniors)'
            if pro_role_names[team_role_names.index(role.name)]:
                team_name = pro_role_names[team_role_names.index(role.name)]
            else:
                # For junior-only teams
                team_name = junior_role_names[team_role_names.index(role.name)]
        else:
            logger.debug(f'broadcast_team_game_to_server: no team name found to match role {role.name}')
            continue

        try:
            team = models.Team.get_or_except(team_name=team_name, guild_id=ctx.guild.id)
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
            message_content += f'\n(*This appears to be a **Season Game** so join reactions are disabled.*)'
        else:
            message_content += f'\n{join_str}.'

        try:
            message = await team_channel.send(message_content)
            models.TeamServerBroadcastMessage.create(game=game, channel_id=team_channel.id, message_id=message.id)
        except discord.DiscordException as e:
            logger.warning(f'Could not send broadcast message: {e}')
        logger.debug(f'broadcast_team_game_to_server - sending message to channel {team_channel.name} on server {team_server.name}\n{message_content}')


async def auto_grad_novas(ctx, game):
    # called from post_newgame_messaging() - check if any member of the newly-started game now meets Nova graduation requirements

    if ctx.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
        return

    role = discord.utils.get(ctx.guild.roles, name=novas_role_name)
    grad_role = discord.utils.get(ctx.guild.roles, name=grad_role_name)

    if not role or not grad_role:
        logger.warning(f'Could not load required roles to complete auto_grad_novas')
        return

    player_id_list = [l.player.discord_member.discord_id for l in game.lineup]
    for player_id in player_id_list:
        member = ctx.guild.get_member(player_id)
        if not member:
            logger.warning(f'Could not load guild member matching discord_id {player_id} for game {game.id} in auto_grad_novas')
            continue

        if role not in member.roles or grad_role in member.roles:
            continue  # skip non-novas or people who are already graduates

        logger.debug(f'Checking league graduation status for player {member.name} in auto_grad_novas')

        try:
            dm = models.DiscordMember.get(discord_id=member.id)
            player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
        except peewee.DoesNotExist:
            logger.warning(f'Player {member.name} not registered.')
            continue

        qualifying_games = []

        for lineup in player.games_played():
            game = lineup.game
            if game.notes and 'NOVA RED' in game.notes.upper() and 'NOVA BLUE' in game.notes.upper():
                if not game.is_pending:
                    qualifying_games.append(str(game.id))

        if len(qualifying_games) < 3:
            logger.debug(f'Player {player.name} has insufficient qualifying games. Games that qualified: {qualifying_games}')
            continue

        wins, losses = dm.get_record()
        logger.debug(f'Player {player.name} meets qualifications: {qualifying_games}')

        try:
            await member.add_roles(grad_role)
        except discord.DiscordException as e:
            logger.error(f'Could not assign league graduation role: {e}')
            break

        config, _ = models.Configuration.get_or_create(guild_id=ctx.guild.id)
        announce_str = f'Draft signups open regularly - pay attention to server announcements for a notification of the next one.'
        if config.polychamps_draft['draft_open']:
            try:
                channel = ctx.guild.get_channel(config.polychamps_draft['announcement_channel'])
                if channel and await channel.fetch_message(config.polychamps_draft['announcement_message']):
                    announce_str = f'Draft signups are currently open in <#{channel.id}>'
            except discord.NotFound:
                pass  # Draft signup message no longer exists - assume its been deleted intentionally and closed
            except discord.DiscordException as e:
                logger.warning(f'Error loading existing draft announcement message in auto_grad_novas: {e}')

        grad_announcement = (f'Player {member.mention} (*Global ELO: {dm.elo} \u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}*) '
                f'has met the qualifications and is now a **{grad_role.name}**\n'
                f'{announce_str}')

        await ctx.send(grad_announcement)
        await utilities.send_to_log_channel(ctx.guild, grad_announcement)


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


def setup(bot):
    bot.add_cog(league(bot))
