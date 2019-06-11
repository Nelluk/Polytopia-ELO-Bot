import discord
from discord.ext import commands
import logging
import asyncio
import settings
import modules.models as models
# import peewee

logger = logging.getLogger('polybot.' + __name__)


def is_valid_poly_gamename(input: str):
    # key_words = ["War", "Spirit", "Faith", "Glory", "Blood", "Paradise", "Magical", "Jungle",
    #              "Empires", "Songs", "Dawn", "Prophecy", "Prophesy", "Gold",
    #              "Fire", "Swords", "Queens", "Kings", "Tribes", "Tribe", "Tales",
    #              "Hills", "Fields", "Lands", "Forest", "Ocean", "Fruit", "Mountain", "Lake",
    #              "Test", "Unknown"]
    key_words = ["War", "Spirit", "Faith", "Glory", "Blood", "Empires", "Songs", "Dawn",
                 "Prophecy", "Prophesy", "Gold", "Fire", "Swords", "Queens", "Knights", "Kings", "Tribes",
                 "Tales", "Quests", "Change", "Games", "Throne", "Conquest", "Struggle", "Victory",
                 "Battles", "Legends", "Heroes", "Storms", "Clouds", "Gods", "Love", "Lords",
                 "Lights", "Wrath", "Destruction", "Whales", "Ruins", "Monuments", "Wonder", "Clowns",
                 "Bongo", "Duh!", "Squeal", "Squirrel", "Confusion", "Gruff", "Moan", "Chickens", "Spunge",
                 "Gnomes", "Bell boys", "Gurkins", "Commotion", "LOL", "Shenanigans", "Hullabaloo",
                 "Papercuts", "Eggs", "Mooni", "Gaami", "Hills", "Fields", "Lands", "Forest", "Ocean", "Fruit", "Mountain",
                 "Lake", "Paradise", "Jungle", "Desert", "River", "Sea", "Shores", "Valley", "Garden", "Moon",
                 "Star", "Winter", "Spring", "Summer", "Autumn", "Divide", "Square", "Custard", "Goon", "Cat",
                 "Spagetti", "Fish", "Fame", "Popcorn", "Dessert", "Space", "Glacier", "Ice", "Frozen", "Superb", "Unknown", "Test"]
    return any(word.upper() in input.upper() for word in key_words)


async def get_guild_member(ctx, input):

        # Find matching Guild member by @Mention or Name. Fall back to case-insensitive search
        # TODO: support Username#Discriminator (ie an @mention without the @)
        # TODO: use exceptions.NoSingleMatch etc like Player.get_or_except()

        guild_matches, substring_matches = [], []
        try:
            guild_matches.append(await commands.MemberConverter().convert(ctx, input))
        except commands.errors.BadArgument:
            pass
            # No matches in standard MemberConverter. Move on to a case-insensitive search.
            input = input.strip('@')  # Attempt to handle fake @Mentions that sometimes slip through
            for p in ctx.guild.members:
                name_str = p.nick.upper() + p.name.upper() if p.nick else p.name.upper()
                if p.name.upper() == input.upper():
                    guild_matches.append(p)
                elif input.upper() in name_str:
                    substring_matches.append(p)

            return guild_matches + substring_matches
            # if len(guild_matches) > 0:
            #     return guild_matches
            # if len(input) > 2:
            #     return substring_matches

        return guild_matches


def get_matching_roles(discord_member, list_of_role_names):
        # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.polytopia_id
        member_roles = [x.name for x in discord_member.roles]
        return set(member_roles).intersection(list_of_role_names)


def summarize_game_list(games_query):
    # Turns a list/query-result of several games (or GameSide) into a List of Tuples that can be sent to the pagination function
    # ie. [('Game 330   :nauseated_face: DrippyIsGod vs Nelluk :spy: Mountain Of Songs', '2018-10-05 - 1v1 - WINNER: Nelluk')]
    game_list = []

    # for counter, game in enumerate(games_query):
    # for game in peewee.prefetch(games_query, models.GameSide):
    for game in games_query:
        if isinstance(game, models.GameSide):
            game = game.game  # In case a list of GameSide is passed instead of a list of Games

        if game.is_pending:
            status_str = 'Not Started'
        elif game.is_completed is False:
            status_str = 'Incomplete'
        else:
            if game.is_confirmed is False:
                (confirmed_count, side_count, _) = game.confirmations_count()
                if side_count > 2:
                    status_str = f'**WINNER** (Unconfirmed by {side_count - confirmed_count} of {side_count}): {game.winner.name()}'
                else:
                    status_str = f'**WINNER** (Unconfirmed): {game.winner.name()}'
            else:
                status_str = f'**WINNER:** {game.winner.name()}'

        rank_str = 'Unranked - ' if not game.is_ranked else ''
        game_list.append((
            f'{game.get_headline()}'[:255],
            f'{(str(game.date))} - {rank_str}{game.size_string()} - {status_str}'
        ))
    return game_list


def export_game_data():
    import csv
    filename = 'games_export.csv'
    with open(filename, mode='w') as export_file:
        game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        header = ['game_id', 'server', 'game_name', 'game_type', 'game_date', 'rank_unranked', 'completed_timestamp', 'side_id', 'side_name', 'player_name', 'winner', 'player_elo', 'player_elo_change', 'squad_elo', 'squad_elo_change', 'tribe']
        game_writer.writerow(header)

        query = models.Lineup.select().join(models.Game).where(
            (models.Game.is_confirmed == 1)
        ).order_by(models.Lineup.gameside_id).order_by(models.Lineup.game_id)

        for q in query:
            is_winner = True if q.game.winner_id == q.gameside_id else False
            ranked_status = 'Ranked' if q.game.is_ranked else 'Unranked'
            row = [q.game_id, settings.guild_setting(q.game.guild_id, 'display_name'), q.game.name, q.game.size_string(),
                   ranked_status, str(q.game.date), str(q.game.completed_ts), q.gameside_id,
                   q.gameside.name(), q.player.name, is_winner, q.elo_after_game,
                   q.elo_change_player, q.gameside.squad_id if q.gameside.squad else '', q.gameside.squad.elo if q.gameside.squad else '',
                   q.tribe.tribe.name if q.tribe else '']

            game_writer.writerow(row)

    print(f'Game data written to file {filename} in bot.py directory')


async def paginate(bot, ctx, title, message_list, page_start=0, page_end=10, page_size=10):
    # Allows user to page through a long list of messages with reactions
    # message_list should be a [(List of, two-item tuples)]. Each tuple will be split into an embed field name/value

    page_end = page_end if len(message_list) > page_end else len(message_list)

    first_loop = True
    while True:
        embed = discord.Embed(title=title)
        for entry in range(page_start, page_end):
            embed.add_field(name=message_list[entry][0], value=message_list[entry][1], inline=False)
        if page_size < len(message_list):
            embed.set_footer(text=f'{page_start + 1} - {page_end} of {len(message_list)}')

        if first_loop is True:
            sent_message = await ctx.send(embed=embed)
        else:
            try:
                await sent_message.clear_reactions()
            except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                logger.warn('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
            await sent_message.edit(embed=embed)

        if page_start > 0:
            await sent_message.add_reaction('⏪')
            await sent_message.add_reaction('⬅')
        if page_end < len(message_list):
            await sent_message.add_reaction('➡')
            await sent_message.add_reaction('⏩')

        def check(reaction, user):
            e = str(reaction.emoji)

            if page_size < len(message_list):
                compare = e.startswith(('⏪', '⏩', '➡', '⬅'))
            else:
                compare = False
            return ((user == ctx.message.author) and (reaction.message.id == sent_message.id) and compare)

        try:
            reaction, user = await bot.wait_for('reaction_add', timeout=45.0, check=check)
        except asyncio.TimeoutError:
            try:
                await sent_message.clear_reactions()
            except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                logger.warn('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
            finally:
                break
        else:

            if '⏪' in str(reaction.emoji):
                # all the way to beginning
                page_start = 0
                page_end = page_start + page_size

            if '⏩' in str(reaction.emoji):
                # last page
                page_end = len(message_list)
                page_start = page_end - page_size

            if '➡' in str(reaction.emoji):
                # next page
                page_start = page_start + page_size
                page_end = page_start + page_size

            if '⬅' in str(reaction.emoji):
                # previous page
                page_start = page_start - page_size
                page_end = page_start + page_size

            if page_start < 0:
                page_start = 0
                page_end = page_start + page_size

            if page_end > len(message_list):
                page_end = len(message_list)
                page_start = page_end - page_size if (page_end - page_size) >= 0 else 0

            first_loop = False
