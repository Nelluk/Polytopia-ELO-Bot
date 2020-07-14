import discord
from discord.ext import commands
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

logger = logging.getLogger('polybot.' + __name__)


grad_role_name = 'Nova Grad'         # met graduation requirements and is eligible to sign up for draft
draftable_role_name = 'Draftable'    # signed up for current draft
free_agent_role_name = 'Free Agent'  # signed up for a prior draft but did not get drafted
novas_role_name = 'The Novas'        # Umbrella newbie role that all of above should also have


class league(commands.Cog):
    """
    Commands specific to the PolyChampions league, such as drafting-related commands
    """

    emoji_draft_signup = '🔆'
    emoji_draft_close = '⏯'
    emoji_draft_conclude = '❎'
    emoji_list = [emoji_draft_signup, emoji_draft_close, emoji_draft_conclude]

    # TODO: method to get role objects and return them in a dict?

    draft_open_format_str = f'The draft is open for signups! {{0}}\'s can react with a {emoji_draft_signup} below to sign up. {{1}} who have not graduated have until the end of the draft signup period to meet requirements and sign up.\n\n{{2}}'
    draft_closed_message = f'The draft is closed to new signups. Mods can use the {emoji_draft_conclude} reaction after players have been drafted to clean up the remaining players and delete this message.'

    def __init__(self, bot):

        self.bot = bot
        self.announcement_message = None  # Will be populated from db if exists

        if settings.run_tasks:
            pass

    async def cog_check(self, ctx):
        return ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']

    @commands.Cog.listener()
    async def on_ready(self):
        utilities.connect()
        if self.bot.user.id == 479029527553638401:
            # beta bot, using nelluk server to watch for messages
            self.announcement_message = self.get_draft_config(settings.server_ids['test'])['announcement_message']
        else:
            # assume polychampions
            self.announcement_message = self.get_draft_config(settings.server_ids['polychampions'])['announcement_message']

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        # Monitors all reactions being added to all messages, looking for reactions added to relevant league announcement messages

        if payload.message_id != self.announcement_message:
            return

        if payload.user_id == self.bot.user.id:
            return

        channel = payload.member.guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

        if payload.emoji.name not in self.emoji_list:
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

        guild = discord.utils.get(self.bot.guilds, id=payload.guild_id)
        member = guild.get_member(payload.user_id)
        channel = guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

        if payload.emoji.name not in self.emoji_list:
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
            logger.warn(f'Unable to remove reaction in conclude_draft_emoji_added(): {e}')

        if not settings.is_mod(member):
            return

        free_agent_role = discord.utils.get(member.guild.roles, name=free_agent_role_name)
        draftable_role = discord.utils.get(member.guild.roles, name=draftable_role_name)

        confirm_message = await channel.send(f'<@{member.id}>, react below to confirm the conclusion of the current draft. '
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

        result_message_list = [f'Draft successfully closed by <@{member.id}>']
        self.announcement_message = None
        await message.delete()

        async with channel.typing():
            for old_free_agent in free_agent_role.members:
                await old_free_agent.remove_roles(free_agent_role, reason='Purging old free agents')
                logger.debug(f'Removing free agent role from {old_free_agent.name}')

                result_message_list.append(f'Removing free agent role from {old_free_agent.name} <@{old_free_agent.id}>')

            for new_free_agent in draftable_role.members:
                await new_free_agent.add_roles(free_agent_role, reason='New crop of free agents')
                logger.debug(f'Adding free agent role to {new_free_agent.name}')

                await new_free_agent.remove_roles(draftable_role, reason='Purging old free agents')
                logger.debug(f'Removing draftable role from {new_free_agent.name}')

                result_message_list.append(f'Removing draftable role from and applying free agent role to {new_free_agent.name} <@{new_free_agent.id}>')

        await self.send_to_log_channel(member.guild, '\n'.join(result_message_list))

    async def close_draft_emoji_added(self, member, channel, message):
        announce_message_link = f'https://discord.com/channels/{member.guild.id}/{channel.id}/{message.id}'
        logger.debug(f'Draft close reaction added by {member.name} to draft announcement {announce_message_link}')
        grad_role = discord.utils.get(member.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(member.guild.roles, name=novas_role_name)

        try:
            await message.remove_reaction(self.emoji_draft_close, member)
            logger.debug(f'Removing {self.emoji_draft_close} reaction placed by {member.name} on message {message.id}')
        except discord.DiscordException as e:
            logger.warn(f'Unable to remove reaction in close_draft_emoji_added(): {e}')

        if not settings.is_mod(member):
            return

        draft_config = self.get_draft_config(member.guild.id)

        if draft_config['draft_open']:
            new_message = f'~~{message.content}~~\n{self.draft_closed_message}'
            log_message = f'Draft status closed by <@{member.id}>'
            draft_config['draft_open'] = False
        else:
            new_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, draft_config['draft_message'])
            log_message = f'Draft status opened by <@{member.id}>'
            draft_config['draft_open'] = True

        self.save_draft_config(member.guild.id, draft_config)
        await self.send_to_log_channel(member.guild, log_message)
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
                    member_message = f'You are now signed up for the next draft. If you would like to remove yourself, just remove the reaction you just placed.\n{announce_message_link}'
                    log_message = f'<@{member.id}> ({member.name}) reacted to the draft and received the {draftable_role.name} role.'
            else:
                # Ineligible signup - either draft is closed or member does not have grad_role
                try:
                    await message.remove_reaction(self.emoji_draft_signup, member)
                    logger.debug(f'Removing {self.emoji_draft_signup} reaction placed by {member.name} on message {message.id}')
                except discord.DiscordException as e:
                    logger.warn(f'Unable to remove irrelevant reaction in signup_emoji_clicked(): {e}')
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
                    log_message = f'<@{member.id}> removed their draft reaction and has lost the {draftable_role.name} role.'
            else:
                return
                # member_message = (f'You removed your signup reaction from the draft announcement, but you did not have the **{draftable_role.name}** :thinking:\n'
                # f'Add your reaction back to attempt to get the role and sign up for the draft.\n{announce_message_link}')
                # Fail silently, otherwise a user whose reaction is being rejected will get two PMs
                # the bot removing the reaction will trigger a second one - currently no way to distinguish a reaction being removed by
                # the original author or an admin/bot. Could kinda solve by storing timestamp when removing role and ignoring role removal
                # if it has a nearly-same timestamp

        if log_message:
            await self.send_to_log_channel(member.guild, log_message)
        if member_message:
            try:
                await member.send(member_message)
            except discord.DiscordException as e:
                logger.warn(f'Could not message member in signup_emoji_clicked: {e}')

    def get_draft_config(self, guild_id):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        return record.polychamps_draft

    def save_draft_config(self, guild_id, config_obj):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        record.polychamps_draft = config_obj
        return record.save()

    async def send_to_log_channel(self, guild, message):

        logger.debug(f'Sending log message to game_request_channel: {message}')
        staff_output_channel = guild.get_channel(settings.guild_setting(guild.id, 'game_request_channel'))
        if not staff_output_channel:
            logger.warn(f'Could not load game_request_channel for server {guild.id} - skipping')
        else:
            await utilities.buffered_send(destination=staff_output_channel, content=message)

    @commands.command(aliases=['ds'], usage=None)
    @settings.is_mod_check()
    async def newdraft(self, ctx, channel_override: typing.Optional[discord.TextChannel], *, added_message: str = ''):

        """
        Post a new draft signup announcement

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
        `[p]newdraft` Normal usage
        `[p]newdraft #special-channel` Direct message to a non-standard channel
        `[p]newdraft Signups close early this week!` Add an extra message to the announcement.

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
                logger.warn(f'Error loading existing draft announcement message in newdraft command: {e}')

        grad_role = discord.utils.get(ctx.guild.roles, name=grad_role_name)
        novas_role = discord.utils.get(ctx.guild.roles, name=novas_role_name)

        formatted_message = self.draft_open_format_str.format(grad_role.mention, novas_role.mention, added_message)
        announcement_message = await announcement_channel.send(formatted_message)

        await announcement_message.add_reaction(self.emoji_draft_signup)
        await announcement_message.add_reaction(self.emoji_draft_close)
        await announcement_message.add_reaction(self.emoji_draft_conclude)

        await self.send_to_log_channel(ctx.guild, f'Draft created by <@{ctx.author.id}>\n'
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

    @commands.command(aliases=['balance'])
    @settings.in_bot_channel()
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def league_balance(self, ctx, *, arg=None):
        """ Print some stats on PolyChampions league balance
        """
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
                        ('Dragons', ['The Dragons', 'The Narwhals'])
                        ]

        league_balance = []
        indent_str = '\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0'
        mia_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))

        for team, team_roles in league_teams:

            pro_role = discord.utils.get(ctx.guild.roles, name=team_roles[0])
            junior_role = discord.utils.get(ctx.guild.roles, name=team_roles[1])

            if not pro_role or not junior_role:
                logger.warn(f'Could not load one team role from guild, using args: {team_roles}')
                continue

            try:
                pro_team = models.Team.get_or_except(team_roles[0], ctx.guild.id)
                junior_team = models.Team.get_or_except(team_roles[1], ctx.guild.id)
            except exceptions.NoSingleMatch:
                logger.warn(f'Could not load one team from database, using args: {team_roles}')
                continue

            pro_members, junior_members, pro_discord_ids, junior_discord_ids, mia_count = [], [], [], [], 0

            for member in pro_role.members:
                if mia_role in member.roles:
                    mia_count += 1
                else:
                    pro_members.append(member)
                    pro_discord_ids.append(member.id)
            for member in junior_role.members:
                if mia_role in member.roles:
                    mia_count += 1
                else:
                    junior_members.append(member)
                    junior_discord_ids.append(member.id)

            logger.info(team)
            combined_elo, player_games_total = models.Player.average_elo_of_player_list(list_of_discord_ids=junior_discord_ids + pro_discord_ids, guild_id=ctx.guild.id, weighted=True)

            pro_elo, _ = models.Player.average_elo_of_player_list(list_of_discord_ids=pro_discord_ids, guild_id=ctx.guild.id, weighted=False)
            junior_elo, _ = models.Player.average_elo_of_player_list(list_of_discord_ids=junior_discord_ids, guild_id=ctx.guild.id, weighted=False)

            league_balance.append(
                (team,
                 pro_team,
                 junior_team,
                 len(pro_members),
                 len(junior_members),
                 mia_count,
                 combined_elo,
                 player_games_total,
                 pro_elo,
                 junior_elo)
            )

        league_balance.sort(key=lambda tup: tup[6], reverse=True)     # sort by combined_elo

        embed = discord.Embed(title='PolyChampions League Balance Summary')
        for team in league_balance:
            embed.add_field(name=(f'{team[1].emoji} {team[0]} ({team[3] + team[4]}) {team[2].emoji}\n{indent_str} \u00A0\u00A0 ActiveELO™: {team[6]}'
                                  f'\n{indent_str} \u00A0\u00A0 Recent member-games: {team[7]}'),
                value=(f'-{indent_str}__**{team[1].name}**__ ({team[3]}) **ELO: {team[1].elo}** (Avg: {team[8]})\n'
                       f'-{indent_str}__**{team[2].name}**__ ({team[4]}) **ELO: {team[2].elo}** (Avg: {team[9]})\n'), inline=False)

        embed.set_footer(text='ActiveELO™ is the mean ELO of members weighted by how many games each member has played in the last 30 days.')

        await ctx.send(embed=embed)

    @commands.command(aliases=['jrseason'], usage='[season #]')
    # @settings.in_bot_channel()
    async def season(self, ctx, *, arg=None):
        """
        Display team records for one or all seasons

        **Examples**
        `[p]season` Records for all seasons (Pro teams)
        `[p]jrseason` Records for all seasons (Junior teams)
        `[p]season 7` Records for a specific season (Pro teams)
        `[p]jrseason 7` Records for a specific season (Junior teams)
        """

        season = int(arg) if arg else None
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
            for team in poly_teams:
                season_record = team.get_season_record(season=season)  # (win_count_reg, loss_count_reg, incomplete_count_reg, win_count_post, loss_count_post, incomplete_count_post)
                if not season_record:
                    logger.warn(f'No season record returned for team {team.name}')
                    continue

                standings.append((team, season_record[0], season_record[1], season_record[2], season_record[3], season_record[4], season_record[5]))

            standings = sorted(standings, key=lambda x: (-x[4], -x[1], x[2]))  # should sort first by post-season wins desc, then wins descending then losses ascending

            output = [f'__**{title}**__\n`Regular \u200b \u200b \u200b \u200b \u200b Post-Season`']

            for standing in standings:
                team_str = f'{standing[0].emoji} {standing[0].name}\n'
                line = f'{team_str}`{str(standing[1]) + "W":.<3} {str(standing[2]) + "L":.<3} {str(standing[3]) + "I":.<3} - {str(standing[4]) + "W":.<3} {str(standing[5]) + "L":.<3} {standing[6]}I`'
                output.append(line.replace(".", "\u200b "))

        await utilities.buffered_send(destination=ctx, content='\n'.join(output))

    @commands.command(aliases=['nova', 'joinnovas'])
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

        if len(red_role.members) > len(blue_role.members):
            await ctx.author.add_roles(blue_role, novas_role, reason='Joining Nova Blue')
            await ctx.send(f'Congrats, you are now a member of the **Nova Blue** team! To join the fight go to a bot channel and type `{ctx.prefix}novagames`')
        else:
            await ctx.author.add_roles(red_role, novas_role, reason='Joining Nova Red')
            await ctx.send(f'Congrats, you are now a member of the **Nova Red** team! To join the fight go to a bot channel and type `{ctx.prefix}novagames`')

        if newbie_role:
            await ctx.author.remove_roles(newbie_role, reason='Joining Novas')

    @commands.command(aliases=['freeagents', 'draftable', 'ble', 'bge'], usage='[sort] [role name]')
    @settings.in_bot_channel_strict()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def roleelo(self, ctx, *, arg=None):
        """Prints list of players with a given role and their ELO stats

        Use one of the following options as the first argument to change the sorting:
        **g_elo** - Global ELO (default)
        **elo** - Local ELO
        **games** - Total number of games played
        **recent** - Recent games played (14 days)

        Members with the Inactive role will be skipped.

        This command has some shortcuts:
        `[p]draftable` - List members with the Draftable role
        `[p]freeagents` - List members with the Free Agent role

        **Examples**
        `[p]roleelo novas` - List all members with a role matching 'novas'
        `[p]roleelo elo novas` - List all members with a role matching 'novas', sorted by local elo
        `[p]draftable recent` - List all members with the Draftable role sorted by recent games
        """
        args = arg.split() if arg else []
        usage = (f'**Example usage:** `{ctx.prefix}roleelo Ronin`\n'
                    f'See `{ctx.prefix}help roleelo` for sorting options and more examples.')

        if ctx.invoked_with in ['ble', 'bge']:
            return await ctx.send(f'The `{ctx.prefix}{ctx.invoked_with}` command has been replaced by `{ctx.prefix}roleelo`\n{usage}')

        if args and args[0].upper() == 'G_ELO':
            sort_key = 1
            args = ' '.join(args[1:])
            sort_str = 'Global ELO'
        elif args and args[0].upper() == 'ELO':
            sort_key = 2
            args = ' '.join(args[1:])
            sort_str = 'Local ELO'
        elif args and args[0].upper() == 'GAMES':
            sort_key = 3
            args = ' '.join(args[1:])
            sort_str = 'total games played'
        elif args and args[0].upper() == 'RECENT':
            sort_key = 4
            args = ' '.join(args[1:])
            sort_str = 'recent games played'
        else:
            sort_key = 1  # No argument supplied, use g_elo default
            args = ' '.join(args)
            sort_str = 'Global ELO'

        if ctx.invoked_with == 'draftable':
            role_check_name = draftable_role_name
        elif ctx.invoked_with == 'freeagents':
            role_check_name = free_agent_role_name
        elif ctx.invoked_with == 'roleelo':
            role_check_name = args
            if not args:
                return await ctx.send(f'No role name was supplied.\n{usage}')

        player_list = []
        checked_role = None

        for role in ctx.guild.roles:
            if role_check_name.upper() in role.name.upper():
                checked_role = role

        if not checked_role:
            return await ctx.send(f'Could not load a role from the guild with the name **{role_check_name}**. Make sure the match is exact.')

        inactive_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        for member in checked_role.members:
            if inactive_role and inactive_role in member.roles and inactive_role != checked_role:
                logger.debug(f'Skipping {member.name} since they have Inactive role')
                continue

            try:
                dm = models.DiscordMember.get(discord_id=member.id)
                player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
            except peewee.DoesNotExist:
                logger.debug(f'Player {member.name} not registered.')
                continue

            g_wins, g_losses = dm.get_record()
            wins, losses = player.get_record()
            recent_games = dm.games_played(in_days=14).count()
            all_games = dm.games_played().count()

            # TODO: Mention players without pinging them once discord.py 1.4 is out https://discordpy.readthedocs.io/en/latest/api.html#discord.TextChannel.send

            message = (f'**{player.name}**'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 {recent_games} games played in last 14 days, {all_games} all-time'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 ELO:  {dm.elo} *global* / {player.elo} *local*\n'
                f'\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 __W {g_wins} / L {g_losses}__ *global* \u00A0\u00A0 - \u00A0\u00A0 __W {wins} / L {losses}__ *local*\n')

            player_list.append((message, dm.elo, player.elo, all_games, recent_games))

        await ctx.send(f'Listing {len(player_list)} active members with the **{utilities.escape_role_mentions(checked_role.name)}** role (sorted by {sort_str})...')
        # without the escape then 'everyone.name' still is a mention

        player_list.sort(key=lambda tup: tup[sort_key], reverse=False)     # sort the list by argument supplied

        message = []
        for grad in player_list:
            # await ctx.send(grad[0])
            message.append(grad[0])

        async with ctx.typing():
            await utilities.buffered_send(destination=ctx, content=''.join(message).replace(".", "\u200b "))

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
            await ctx.send(f'Wrote to `{filename}`', file=file)


async def auto_grad_novas(ctx, game):
    # called from post_newgame_messaging() - check if any member of the newly-started game now meets Nova graduation requirements

    if ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']:
        pass
    else:
        logger.debug(f'Ignoring auto_grad_novas for game {game.id}')
        return

    role = discord.utils.get(ctx.guild.roles, name=novas_role_name)
    grad_role = discord.utils.get(ctx.guild.roles, name=grad_role_name)
    grad_chan = ctx.guild.get_channel(540332800927072267)  # Novas draft talk
    if ctx.guild.id == settings.server_ids['test']:
        grad_chan = ctx.guild.get_channel(479292913080336397)  # bot spam

    if not role or not grad_role:
        logger.warn(f'Could not load required roles to complete auto_grad_novas')
        return

    player_id_list = [l.player.discord_member.discord_id for l in game.lineup]
    for player_id in player_id_list:
        member = ctx.guild.get_member(player_id)
        if not member:
            logger.warn(f'Could not load guild member matching discord_id {player_id} for game {game.id} in auto_grad_novas')
            continue

        if role not in member.roles or grad_role in member.roles:
            continue  # skip non-novas or people who are already graduates

        logger.debug(f'Checking league graduation status for player {member.name} in auto_grad_novas')

        try:
            dm = models.DiscordMember.get(discord_id=member.id)
            player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
        except peewee.DoesNotExist:
            logger.warn(f'Player {member.name} not registered.')
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
                logger.warn(f'Error loading existing draft announcement message in auto_grad_novas: {e}')

        grad_announcement = (f'Player {member.mention} (*Global ELO: {dm.elo} \u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}*) '
                f'has met the qualifications and is now a **{grad_role.name}**\n'
                f'{announce_str}')
        if grad_chan:
            await grad_chan.send(f'{grad_announcement}')
        else:
            await ctx.send(f'{grad_announcement}')


def setup(bot):
    bot.add_cog(league(bot))
