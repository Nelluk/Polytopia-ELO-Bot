import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
import logging
# import asyncio
import modules.exceptions as exceptions
# import re
# import datetime
import peewee

logger = logging.getLogger('polybot.' + __name__)


class league(commands.Cog):
    """
    Commands specific to the PolyChampions league, such as drafting-related commands
    """

    emoji_draft_signup = '🚸'
    emoji_draft_close = '✅'
    emoji_draft_conclude = '❎'
    emoji_list = [emoji_draft_signup, emoji_draft_close, emoji_draft_conclude]

    grad_role_name = 'Nova Grad'         # met graduation requirements and is eligible to sign up for draft
    draftable_role_name = 'Draftable'    # signed up for current draft
    free_agent_role_name = 'Free Agent'  # eligible for prior draft but did not get drafted

    def __init__(self, bot):
        self.bot = bot
        self.announcement_messages = []  # populate from DB, to check from reactions_add (safe to limit to PolyChamps i think)
        if settings.run_tasks:
            pass

    async def cog_check(self, ctx):
        return ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        # Monitors all reactions being added to all messages, looking for reactions added to relevant league announcement messages

        if payload.message_id not in self.announcement_messages:
            return

        channel = payload.member.guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

        if payload.emoji.name not in self.emoji_list:
            # Irrelevant reaction was added to relevant message. Clear it off.
            try:
                await message.remove_reaction(payload.emoji.name, payload.member)
            except discord.DiscordException:
                logger.debug('Unable to remove irrelevant reaction in on_raw_reaction_add()')
            return

        if payload.emoji.name == self.emoji_draft_signup:
            await self.signup_emoji_clicked(payload.member, channel, message)
        elif payload.emoji.name == self.emoji_draft_close:
            pass
        elif payload.emoji.name == self.emoji_draft_conclude:
            pass

    async def signup_emoji_clicked(member, channel, message):
        pass

    def get_draft_config(self, guild_id):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        return record.polychamps_draft

    def save_draft_config(self, guild_id, config_obj):
        record, _ = models.Configuration.get_or_create(guild_id=guild_id)
        record.polychamps_draft = config_obj
        return record.save()

    @commands.command(aliases=['ds'], usage=None)
    async def newdraft(self, ctx, *, arg: str = None):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """

        # post message in announcements (optional argument of a different channel if mod wants announcement to go elsewhere?)
        # listen for reactions in a check
        # if reactor has Free Agent role, PM success message and apply Draftable role
        # if not, PM failure message and remove reaction
        # remove Draftable role if user removes their reaction

        """ luna suggestions
        $draftable - displays list of people that signed up for the draft
        $newdraft (staff only) - anyone who still has free agent role is added to a new list 'fatable' - for people who can be bought with fats, the rest are cleared from the $draftable list
        $fatable - displays list of people that can be bought with fats
        """

        announcement_channel = ctx.guild.get_channel(480078679930830849)  # admin-spam

        draft_config = self.get_draft_config(ctx.guild.id)
        announcement_message = await announcement_channel.send('New draft!!!!')

        draft_config['announcement_message'] = announcement_message.id
        draft_config['draft_opened'] = True
        self.announcement_messages.append(announcement_message.id)
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

    @commands.command(aliases=['nova', 'joinnovas'])
    async def novas(self, ctx, *, arg=None):
        """ Join yourself to the Novas team
        """

        player, _ = models.Player.get_by_discord_id(discord_id=ctx.author.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick, guild_id=ctx.guild.id)
        if not player:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'*{ctx.author.name}* was found in the server but is not registered with me. '
                f'Players can be register themselves with `{ctx.prefix}setcode POLYTOPIA_CODE`.')

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

    @commands.command(aliases=['undrafted'])
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def undrafted_novas(self, ctx, *, arg=None):
        """Prints list of Novas who meet graduation requirements but have not been drafted

        Use `[p]undrafted_novas elo` to sort by global elo
        """

        grad_list = []
        grad_role = discord.utils.get(ctx.guild.roles, name='Nova Grad')
        inactive_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        # recruiter_role = discord.utils.get(ctx.guild.roles, name='Team Recruiter')
        if ctx.guild.id == settings.server_ids['test']:
            grad_role = discord.utils.get(ctx.guild.roles, name='Team Leader')

        for member in grad_role.members:
            if inactive_role and inactive_role in member.roles:
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

            message = (f'**{player.name}**'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 {recent_games} games played in last 14 days, {all_games} all-time'
                f'\n\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 ELO:  {dm.elo} *global* / {player.elo} *local*\n'
                f'\u00A0\u00A0 \u00A0\u00A0 \u00A0\u00A0 __W {g_wins} / L {g_losses}__ *global* \u00A0\u00A0 - \u00A0\u00A0 __W {wins} / L {losses}__ *local*\n')

            grad_list.append((message, all_games, dm.elo))

        await ctx.send(f'Listing {len(grad_list)} active members with the **{grad_role.name}** role...')

        if arg and arg.upper() == 'ELO':
            grad_list.sort(key=lambda tup: tup[2], reverse=False)     # sort the list ascending by num games played
        else:
            grad_list.sort(key=lambda tup: tup[1], reverse=False)     # sort the list ascending by num games played

        message = []
        for grad in grad_list:
            # await ctx.send(grad[0])
            message.append(grad[0])

        await utilities.buffered_send(destination=ctx, content=''.join(message))


async def auto_grad_novas(ctx, game):
    # called from post_newgame_messaging() - check if any member of the newly-started game now meets Nova graduation requirements

    if ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']:
        pass
    else:
        logger.debug(f'Ignoring auto_grad_novas for game {game.id}')
        return

    role = discord.utils.get(ctx.guild.roles, name='The Novas')
    grad_role = discord.utils.get(ctx.guild.roles, name='Nova Grad')
    grad_chan = ctx.guild.get_channel(540332800927072267)  # Novas draft talk
    if ctx.guild.id == settings.server_ids['test']:
        role = discord.utils.get(ctx.guild.roles, name='testers')
        grad_role = discord.utils.get(ctx.guild.roles, name='Team Leader')
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

        logger.debug(f'Checking league graduation status for player {member.name}')

        try:
            dm = models.DiscordMember.get(discord_id=member.id)
            player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
        except peewee.DoesNotExist:
            logger.warn(f'Player {member.name} not registered.')
            continue

        qualifying_games = []

        for lineup in player.games_played():
            game = lineup.game
            if game.notes and 'Nova Red' in game.notes and 'Nova Blue' in game.notes:
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

        grad_announcement = (f'Player {member.mention} (*Global ELO: {dm.elo} \u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}*) '
                f'has met the qualifications and is now a **{grad_role.name}**\n'
                'You will be notified soon of the next draft signup.')
        if grad_chan:
            await grad_chan.send(f'{grad_announcement}')
        else:
            await ctx.send(f'{grad_announcement}')


def setup(bot):
    bot.add_cog(league(bot))
