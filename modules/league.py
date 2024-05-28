from collections import defaultdict

import discord
from discord.ext import commands, tasks
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
pro_member_role_name = 'Pro Player'    # Umbrella role for all Pro members
jr_member_role_name = 'Junior Player'  # Umbrella role for all Junior memebrs

league_teams = [
    ('Ronin', ['The Ronin', 'The Bandits']),
    ('Jets', ['The Jets', 'The Cropdusters']),
    # ('Bombers', ['The Bombers', 'The Dynamite']),
    ('Lightning', ['The Lightning', 'The ThunderCats']),
    ('Vikings', ['The Vikings', 'The Valkyries']),
    # ('Crawfish', ['The Crawfish', 'The Shrimps']),
    # ('Sparkies', ['The Sparkies', 'The Pups']),
    ('Wildfire', ['The Wildfire', 'The Flames']),
    # ('Mallards', ['The Mallards', 'The Drakes']),
    # ('OldPlague', ['The OldPlague', 'The Rats']),
    ('Dragons', ['The Dragons', 'The Narwhals']),
    # ('Jalapenos', ['The OldReapers', 'The Jalapenos']),
    ('Kraken', ['The Kraken', 'The Squids']),
    ('ArcticWolves', ['The ArcticWolves', 'The Huskies']),
    ('Plague', ['The Plague', 'The Reapers']),
    ('Tempest', ['The Tempest', 'The Rainclouds']),
] ## TODO: Should be able to remove this hardcoding with May 2024 changes - will need to update the code that relies on this

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

    house_role = utilities.guild_role_by_name(guild, name=team.house.name, allow_partial=False)
    team_role = utilities.guild_role_by_name(guild, name=team.name, allow_partial=False)
    leader_role = utilities.guild_role_by_name(guild, name='House Leader', allow_partial=False)
    coleader_role = utilities.guild_role_by_name(guild, name='House Co-Leader', allow_partial=False)
    recruiter_role = utilities.guild_role_by_name(guild, name='House Recruiter', allow_partial=False)
    captain_role = utilities.guild_role_by_name(guild, name='Team Captain', allow_partial=False)
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

    draft_open_format_str = f'The league is now open for Free Agent signups! {{0}}s can react with a {emoji_draft_signup} below to sign up. {{1}} who have not graduated have until the end of the signup period to meet requirements and sign up.\n\n{{2}}'
    draft_closed_message = f'The league is closed to new Free Agent signups. Mods can use the {emoji_draft_conclude} reaction to clean up and delete this message.'

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

        # logger.debug(f'before roles: {before.roles} / after roles: {after.roles}')
        if before.roles == after.roles:
            return

        if after.guild.id not in [settings.server_ids['polychampions'], settings.server_ids['test']]:
            return

        team_roles = get_team_roles(after.guild)
        league_role = discord.utils.get(after.guild.roles, name=league_role_name)
        # pro_member_role = discord.utils.get(after.guild.roles, name=pro_member_role_name)
        # jr_member_role = discord.utils.get(after.guild.roles, name=jr_member_role_name)
        player, team = None, None

        before_member_team_roles = [x for x in before.roles if x in team_roles]
        member_team_roles = [x for x in after.roles if x in team_roles]

        if before_member_team_roles == member_team_roles:
            return

        if len(member_team_roles) > 1:
            return logger.debug(f'Member has more than one team role. Abandoning League.on_member_update. {member_team_roles}')

        tier_roles = get_tier_roles(after.guild)
        house_roles = get_house_roles(after.guild)
        roles_to_remove =  tier_roles + house_roles + [league_role]
        logger.debug(f'on_member_update roles_to_remove: {roles_to_remove}')

        if member_team_roles:
            try:
                player = models.Player.get_or_except(player_string=after.id, guild_id=after.guild.id)
                team = models.Team.get_or_except(team_name=member_team_roles[0].name, guild_id=after.guild.id)
                player.team = team
                player.save()
                house_name = team.house.name if team.house else None
                team_tier = team.league_tier
                house_role = discord.utils.get(after.guild.roles, name=house_name) if house_name else None
                tier_role = tier_roles[team_tier - 1]
            except exceptions.NoSingleMatch as e:
                logger.warning(f'League.on_member_update: could not load Player or Team for changing league member {after.display_name}: {e}')
                house_name, team_tier, house_role, tier_role = None, None, None, None

            roles_to_add = [house_role, tier_role, league_role]
            log_message = f'{models.GameLog.member_string(after)} had tier {team_tier} team role **{member_team_roles[0].name}** added.'
        else:
            roles_to_add = []  # No team role
            log_message = f'{models.GameLog.member_string(after)} had team role **{before_member_team_roles[0].name}** removed and is teamless.'

        member_roles = after.roles.copy()
        member_roles = [r for r in member_roles if r not in roles_to_remove]

        roles_to_add = [r for r in roles_to_add if r]  # remove any Nones
        logger.debug(f'on_member_update roles_to_add: {roles_to_add}')
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
                    member_message = f'**Important - Please Read.** You are signed up for the PolyChampions Auction, typically held every other Saturday. Between now and the auction, you may be contacted by Team recruiters. It is in your best interest to speak with all these recruiters.\n\nBe open minded. Do not tell recruiter you\'re already committed to a team. Also, any recruiter who tries to force you to chose a team before the auction happens should be reported, as this is against league rules.\n\nRemember, the team choose the player, not the other way around.\n{announce_message_link}'
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


    @settings.is_superuser_check()
    @commands.command()
    async def migrate_teams(self, ctx):

        import re
        pro_role_names = [a[1][0] for a in league_teams]
        junior_role_names = [a[1][1] for a in league_teams]
        team_role_names = [a[0] for a in league_teams]

        async with ctx.typing():
            logger.info('Migrating Polychampions teams and games')
            poly_teams = models.Team.select().where(
                (models.Team.guild_id == settings.server_ids['polychampions']) & (models.Team.is_hidden == 0) 
            )

            full_season_games, regular_season_games, post_season_games = models.Game.polychamps_season_games()
            saved_counter = 0

            with models.db.atomic():
                # for team in poly_teams:
                #     logger.info(f'Checking team {team.name}')
                #     if team.pro_league:
                #         team.league_tier = 2
                #         if team.name in pro_role_names:
                #             house_name = team_role_names[pro_role_names.index(team.name)]
                #             house = models.House.upsert(name=house_name)
                #             team.house = house
                #             logger.debug(f'Associating team with house {house.name} is_archived: {team.is_archived}')
                #         else:
                #             logger.warn(f'No pro role for team {team.name}')
                #     else:
                #         team.league_tier = 3
                #         if team.name in junior_role_names:
                #             house_name = team_role_names[junior_role_names.index(team.name)]
                #             house = models.House.upsert(name=house_name)
                #             team.house = house
                #             logger.debug(f'Associating team with house {house.name}')
                #         else:
                #             logger.warn(f'No junior role for team {team.name} - is_archived: {team.is_archived}')
                #     logger.info(f'Setting team {team.name} tier to {team.league_tier}')
                #     team.save()



                for rsgame in full_season_games:
                    logger.info(f'Checking {rsgame.id} {rsgame.name}')
                    # rsgame.league_playoff = False
                    m = re.match(r"([PJ]?)S(\d+)", rsgame.name.upper())
                    if not m:
                        logger.warn(f'Could not parse name for game {rsgame.id} {rsgame.name}, skipping')
                        continue
                    season = int(m[2])
                    if season <= 4:
                        league = 'P'
                    else:
                        league = m[1].upper()
                    if league == 'P':
                        tier = 2
                    elif league == 'J':
                        tier = 3
                    else:
                        logger.warn(f'Could not detect season status for game {rsgame.id}')
                        continue

                    if rsgame in post_season_games:
                        rsgame.league_playoff = True
                    logger.info(f'Setting game {rsgame.id} {rsgame.name} to tier {tier} and season {season} playoff {rsgame.league_playoff}')
                    rsgame.league_tier = tier
                    rsgame.league_season = season
                    rsgame.save()
                    saved_counter += 1

            await ctx.send(f'Updated database fields for {saved_counter} out of {len(full_season_games)} possible games')  

    
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
                logger.warning(f'Error loading existing draft announcement message in newfreeagent command: {e}')

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
        Display or update house tokens. The house name must be identified by a single word.

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

    
    @commands.command()
    @settings.in_bot_channel()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def house(self, ctx, *, arg=None):
        # args = arg.split() if arg else []
        if not arg:
            return await ctx.send(f'House name not provided. *Example:* `{ctx.prefix}{ctx.invoked_with} ronin`')
        try:
            house = models.House.get_or_except(house_name=arg)
        except (exceptions.TooManyMatches, exceptions.NoMatches) as e:
            return await ctx.send(e)
        
        # members, players = utilities.active_members_and_players(ctx.guild, active_role_name=house.name, inactive_role_name=settings.guild_setting(ctx.guild.id, 'inactive_role'))

        leaders, coleaders, recruiters = [], [], []
        
        house_role = utilities.guild_role_by_name(ctx.guild, name=house.name, allow_partial=False)
        leader_role = utilities.guild_role_by_name(ctx.guild, name='House Leader', allow_partial=False)
        coleader_role = utilities.guild_role_by_name(ctx.guild, name='House Co-Leader', allow_partial=False)
        recruiter_role = utilities.guild_role_by_name(ctx.guild, name='House Recruiter', allow_partial=False)
        captain_role = utilities.guild_role_by_name(ctx.guild, name='Team Captain', allow_partial=False)

        # inactive_role = utilities.guild_role_by_name(ctx.guild, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        
        message_list = [f':PolyChampions: PolyChampions House :PolyChampions:\n{house_role.mention} {house.emoji}']
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

        message_list.append(f'**Leaders**: {", ".join(leaders)}')
        message_list.append(f'\n**Co-Leaders**: {", ".join(coleaders)}')
        message_list.append(f'\n**Recruiters**: {", ".join(recruiters)}')

        for team in house_teams:
            captains, player_list = [], []
            tier_name = settings.tier_lookup(team.league_tier)[1]
            team_role = utilities.guild_role_by_name(ctx.guild, name=team.name, allow_partial=False)
            message_list.append(f'\n__{tier_name} Tier Team__ {team_role.mention if team_role else team.name} {team.emoji} `{team.elo} ELO`')

            members, players = await utilities.active_members_and_players(ctx.guild, active_role_name=team.name, inactive_role_name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
            for member, player in zip(members, players):
                if captain_role in member.roles:
                    captains.append(f'{em(member.display_name)} ({member.mention})')
                player_list.append(f'{em(member.display_name)} `{player.elo}`')
            if captains:
                message_list.append(f'**Team Captains**: {", ".join(captains)}')
            message_list = message_list + player_list
        
        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content='\n'.join(message_list), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))

    @commands.command()
    @settings.in_bot_channel()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def houses(self, ctx, *, arg=None):
        
        houses_with_teams = peewee.prefetch(models.House.select(), models.Team.select().order_by(models.Team.league_tier))
        house_list = []
        leader_role = utilities.guild_role_by_name(ctx.guild, name='House Leader', allow_partial=False)

        # TODO: logging messages, error handling, help text, clean up output a little
        # alternate command to focus display on one house `$house dragons`? (if there is any utility there)
        for emoji in ctx.guild.emojis:
            logger.info(f'{emoji} {emoji.id}')

        for house in houses_with_teams:
            team_list, team_message = [], ''

            house_role = utilities.guild_role_by_name(ctx.guild, name=house.name, allow_partial=False)
            house_leaders = [f'{member.display_name}' for member in leader_role.members if house_role in member.roles] if (house_role and leader_role) else []
            leaders_str = f'\nHouse Leader: {", ".join(house_leaders)}' if house_leaders else ''

            if house.teams:
                for hteam in house.teams:
                    team_list.append(f'- {hteam.name} {hteam.emoji} - Tier {hteam.league_tier} - ELO: {hteam.elo}')
                team_message = '\n'.join(team_list)
            else:
                team_message = '*No related Teams*'
            house_message = f'House {house_role.mention if house_role else house.name} {house.emoji} - Tokens: {house.league_tokens}{leaders_str} \n {team_message}'
            house_list.append(f'{house_message}\n')
        
        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content='\n'.join(house_list), allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))
    
    @commands.command(aliases=['house_rename'], usage='')
    @settings.is_mod_check()
    async def house_add(self, ctx, *, arg=None):
        """*Mod*: Create or rename a league House
        **Example:**
        `[p]house_add Amphibian Party` - Add a new house named "Amphibian Party"
        `[p]house_rename amphibian Mammal Kingdom` - Rename them to "Mammal Kingdom"
        """
        args = arg.split() if arg else []
        if not args:
            return await ctx.send(f'See {ctx.prefix}help {ctx.invoked_with} for usage examples.')
        
        if ctx.invoked_with == 'house_add':
            house_name = ' '.join(args)
            try:
                logger.debug(f'Trying to create a house with name {house_name}')
                house = models.House.create(name=house_name)
            except peewee.IntegrityError:
                return await ctx.send(f':warning: There is already a House with the name "{house_name}". No changes saved.')
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} created a new House with name "{house.name}"')
            return await ctx.send(f'New league House created with name "{house.name}". You can add a team to it using `{ctx.prefix}house_team`.')
        
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

    @commands.command()
    @settings.is_mod_check()
    async def gtest(self, ctx, *, arg=None):
        args = arg.split() if arg else []
        print(get_house_roles())
        house_roles = [hr for hr in get_house_roles() if hr and hr.name == 'Ronin']
        print(house_roles)
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
        `[p]team_tier ronin 2` - Change league tier of team. Does not impact current or past games from this team.
        
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
            return await ctx.send(f'Changed House affiliation of team  **{team.name}** to {new_house_name}. Previous affiliation was "{old_house_name}".{tier_warning}')

        if ctx.invoked_with == 'team_tier':
            try:
                new_tier = int(args[1])
            except ValueError:
                return await ctx.send(f'Second argument should be an integer representing the new tier.')
            
            if not team.house:
                return await ctx.send(f'Team **{team.name}** does not have a House affiliation. Set one with `{ctx.prefix}team_house` first.')
            
            logger.debug(f'Processing tier change for team {team.name}')
            old_tier = str(team.league_tier) if team.league_tier else 'NONE'
            team.league_tier = new_tier
            team.save()
            models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} set the league tier of Team {team.name} to {new_tier} from {old_tier}')
            return await ctx.send(f'Changed league tier of team  **{team.name}** to {new_tier}. Previous tier was {old_tier}.')

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

        draft_str = 'combined alltime local ELO of top 10 players (Senior or Junior)'

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
            return await ctx.send('No matching games found.')

        async with ctx.typing():
            filename = await self.bot.loop.run_in_executor(None, async_call_export_func)
            with open(filename, 'rb') as f:
                file = io.BytesIO(f.read())
            file = discord.File(file, filename=filename)
            await ctx.send(f'{ctx.author.mention}, your export is complete. Wrote to `{filename}`', file=file)


    
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
            if game.smallest_team() > 1:
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
