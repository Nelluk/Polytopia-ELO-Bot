import datetime
import discord
import json
import logging
import gspread_asyncio

from discord.ext import commands
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo

import settings
import modules.models as models

logger = logging.getLogger("polybot." + __name__)


def polychampions_only():
    def predicate(ctx):
        if ctx.guild.id == settings.server_ids["polychampions"]:
            return True
        return False

    return commands.check(predicate)


class bullet(commands.Cog):
    brackets = ["GMT", "EST", "SGT"]
    templates = [8, 16, 32]
    win_words = ["win", "won", "beat"]
    lose_words = ["lose", "lost"]

    questionmark_emoji = "❓"
    checkmark_emoji = "✅"

    def __init__(self, bot):
        self.bot = bot
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(self.get_creds)
        self.form_tz = ZoneInfo("America/Chicago")

        with open("./spreadsheet_creds.json", "r", encoding="utf-8") as json_file:
            data = json.load(json_file)
            self.spreadsheet_key = data["spreadsheet_key"]

    def get_creds(self):
        creds = Credentials.from_service_account_file("./spreadsheet_creds.json")
        scoped = creds.with_scopes(
            [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
        )
        return scoped

    @commands.command(usage="bracket")
    @polychampions_only()
    @models.is_registered_member()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def bullet(self, ctx, bracket: str = None):
        """Sign up for the next bullet tournament

        **Examples**
        `[p]bullet GMT`
        """
        if not bracket:
            return await ctx.send(f"Bracket was not provided! *Example:* `{ctx.prefix}bullet GMT`")

        bracket = bracket.upper()
        if bracket not in self.brackets:
            return await ctx.send(f"There are no bullet brackets for {discord.utils.escape_mentions(bracket)}!")

        spreadsheet = await self.open_bullet_sheet()
        if not spreadsheet:
            return await ctx.send("Something wrong happened, please contact the bot owner.")

        signup_sheet = await spreadsheet.get_worksheet(0)

        dt = datetime.datetime.now(self.form_tz).strftime("%m/%d/%Y %H:%M:%S")
        await signup_sheet.append_row([dt, ctx.author.name, bracket], value_input_option="USER_ENTERED")

        bullet_role = discord.utils.get(ctx.guild.roles, id=794810159104131082)
        if bullet_role not in ctx.author.roles:
            await ctx.author.add_roles(bullet_role)

        await ctx.send(f"You have signed up for the {bracket} bracket!")

    @commands.command(usage="bracket", aliases=["unbullet"])
    @polychampions_only()
    @models.is_registered_member()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def nobullet(self, ctx, arg: str = None):
        """Cancel your signup in the bullet tournament

        **Examples**
        `[p]nobullet GMT`
        `[p]nobullet role` - removes the bullet role
        """
        if not arg:
            return await ctx.send(f"Bracket was not provided! *Example:* `{ctx.prefix}{ctx.invoked_with} GMT`")

        if arg.lower() == "role":
            bullet_role = discord.utils.get(ctx.guild.roles, id=794810159104131082)
            if bullet_role in ctx.author.roles:
                await ctx.author.remove_roles(bullet_role)

            return await ctx.send("You no longer have the bullet role.")

        bracket = arg.upper()
        if bracket not in self.brackets:
            return await ctx.send(f"There are no bullet brackets for {discord.utils.escape_mentions(bracket)}!")

        spreadsheet = await self.open_bullet_sheet()
        if not spreadsheet:
            return await ctx.send("Something wrong happened, please contact the bot owner.")

        signup_sheet = await spreadsheet.get_worksheet(0)
        last_row = int((await signup_sheet.acell("A1")).value)
        signups = await signup_sheet.get(f"B3:C{last_row}")
        row = -1
        for i, p in enumerate(reversed(signups)):
            if p and p[0] == ctx.author.name and p[1] == bracket:
                row = last_row - i
                break

        if row == -1:
            return await ctx.send(f"You have not signed up in the {bracket} bracket!")

        await signup_sheet.update_acell(f"D{row}", "withdrawn")
        await ctx.send(f"You have been removed from the {bracket} bracket.")

    @commands.command(hidden=True, usage="bracket startrow endrow", aliases=["startbullet"])
    @polychampions_only()
    @commands.has_role("Bullet Director")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def bulletstart(self, ctx, bracket: str, start: int, end: int):
        """Starts a bullet bracket

        **Examples**
        `[p]bulletstart GMT 1132 1190`
        """
        bracket = bracket.upper()
        if bracket not in self.brackets:
            return await ctx.send(f"There are no bullet brackets for {discord.utils.escape_mentions(bracket)}!")

        spreadsheet = await self.open_bullet_sheet()
        if not spreadsheet:
            return await ctx.send("Something wrong happened, please contact the bot owner.")

        all_sheets = await spreadsheet.worksheets()
        signup_sheet = all_sheets[0]

        signups = await signup_sheet.get(f"B{start}:D{end}")
        participants = []
        invalid = []

        champion_role = discord.utils.get(ctx.guild.roles, id=771916616077803540)
        for p in signups:
            if len(p) == 3 and "withdraw" in p[2].lower():
                participants = [x for x in participants if x[0] != p[0]]
            elif len(p) == 2 and p[1] == bracket:
                p[0] = p[0].lower()  # All discord usernames are lowercase
                member = discord.utils.get(ctx.guild.members, name=p[0])
                if not member:
                    invalid.append(p[0])
                    continue

                is_bullet_champion = champion_role in member.roles
                dm = models.DiscordMember.get(discord_id=member.id)
                player = models.Player.get(discord_member=dm, guild_id=ctx.guild.id)
                house = player.team.house.name if player.team and player.team.house else "Novas"
                is_league_member = house != "Novas"
                participant = [p[0], house, player.elo_moonrise, is_bullet_champion, is_league_member]
                if participant not in participants:
                    participants.append(participant)

        if invalid:
            invalid = ", ".join(invalid)
            return await ctx.send(
                "Command failed because the bot could not find all signed up members in the server!"
                f"\nPlease remove or update the following names in the sheet: {invalid}"
            )

        participants.sort(key=lambda p: p[4], reverse=True)
        subs = participants[template:]
        participants = participants[:template]

        participants.sort(key=lambda p: (p[3], p[2]), reverse=True)
        for p in participants:
            del p[2:]

        for s in subs:
            del s[2:]

        template = self.templates[-1]
        for t in self.templates:
            if len(participants) < t * 1.5:
                template = t
                break

        for sheet in all_sheets:
            if sheet.title.lower() == f"template {template}":
                template_sheet = sheet
                break

        new_sheet_name = f"{bracket} {datetime.datetime.now(self.form_tz).strftime('%b %d')} ({template})"
        try:
            bracket_sheet = await spreadsheet.duplicate_sheet(template_sheet.id, 1, new_sheet_name=new_sheet_name)
        except gspread_asyncio.gspread.exceptions.APIError:
            return await ctx.send("There is already a sheet for this bracket!")

        await bracket_sheet.update(participants, f"A2:B{1 + template}")
        if subs:
            await bracket_sheet.update(subs, f"A{4 + template}:B{4 + template + len(subs)}")

        return await ctx.send(f"Bracket sheet for {bracket} have been created!")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.channel.id != 794885986143830037:  # bullet-results
            return

        if len(message.mentions) != 2:
            return await message.add_reaction(self.questionmark_emoji)

        if "https://share.polytopia.io/" not in message.content:
            await message.reply("Please include the game replay in the message.")
            return await message.add_reaction(self.questionmark_emoji)

        if any(x in message.content.lower() for x in self.win_words):
            winner = message.mentions[0]
            loser = message.mentions[1]
        elif any(x in message.content.lower() for x in self.lose_words):
            winner = message.mentions[1]
            loser = message.mentions[0]
        else:
            await message.reply("Please clearly specify who won or lost.")
            return await message.add_reaction(self.questionmark_emoji)

        spreadsheet = await self.open_bullet_sheet()
        if not spreadsheet:
            return

        bracket_sheet = await spreadsheet.get_worksheet(1)
        try:
            name = bracket_sheet.title.split("(")
            template = int(name[1].split(")")[0])
            day = int(name[0].split(" ")[2])
        except IndexError:
            return

        if not (-1 <= datetime.datetime.now(self.form_tz).day - day <= 1):
            return

        columns = list(" ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        # Write the winner's team name to the right of the winner's name and a formula in the sheet will update the brackets
        winner_info = await self.find_player_info(bracket_sheet, winner.name, template)

        if not winner_info:  # Can happen when 2 brackets are active simultaneously, needs to be updated manually
            return await message.add_reaction(self.questionmark_emoji)

        w_column = int(winner_info[2])
        w_row = int(winner_info[3])
        opponent_row = w_row + 1 if w_row % 2 == 0 else w_row - 1
        opponent = (await bracket_sheet.acell(f"{columns[w_column]}{opponent_row}")).value
        if opponent != loser.name:
            await message.reply(f"{winner.name} is not matched up against {loser.name}!")
            return await message.add_reaction(self.questionmark_emoji)

        await bracket_sheet.update_acell(f"{columns[w_column+1]}{w_row}", winner_info[1])

        # Check if winner/loser's next games are ready
        channel = message.guild.get_channel(789307611514273843)  # bullet-chat

        loser_info = await self.find_player_info(bracket_sheet, loser.name, template)
        if not loser_info:
            return await message.add_reaction(self.questionmark_emoji)

        l_column = int(loser_info[2])
        l_row = int(loser_info[3])
        opponent_row = l_row + 1 if l_row % 2 == 0 else l_row - 1
        opponent = (await bracket_sheet.acell(f"{columns[l_column]}{opponent_row}")).value
        if (opponent and opponent != winner.name and opponent != "-"):  # Anybody in the loser bracket with a '-' has withdrawn
            opponent = discord.utils.get(message.guild.members, name=opponent)
            await channel.send(f"New round: {loser.mention} vs {opponent.mention}")

        winner_info = await self.find_player_info(bracket_sheet, winner.name, template)
        if not winner_info:
            return await message.add_reaction(self.questionmark_emoji)

        w_column = int(winner_info[2])
        w_row = int(winner_info[3])
        opponent_row = w_row + 1 if w_row % 2 == 0 else w_row - 1
        opponent = (await bracket_sheet.acell(f"{columns[w_column]}{opponent_row}")).value
        if opponent and opponent != loser.name:
            opponent = discord.utils.get(message.guild.members, name=opponent)
            await channel.send(f"New round: {winner.mention} vs {opponent.mention}")

        await message.add_reaction(self.checkmark_emoji)

    async def open_bullet_sheet(self):
        try:
            agc = await self.agcm.authorize()
            spreadsheet = await agc.open_by_key(self.spreadsheet_key)
        except gspread_asyncio.gspread.exceptions.GSpreadException:
            logging.error("Failed to open bullet spreadsheet")
            return None

        return spreadsheet

    async def find_player_info(self, worksheet, name, template):
        participants = await worksheet.get(f"A2:D{1 + template}")
        winner = [p for p in participants if p[0] == name]
        if not winner:
            return None

        return winner[0]


async def setup(bot):
    await bot.add_cog(bullet(bot))
