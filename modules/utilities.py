import discord
from discord.ext import commands
import logging
import asyncio
import settings
import modules.models as models
import re
# import peewee

logger = logging.getLogger('polybot.' + __name__)


def connect():
    if models.db.connect(reuse_if_open=True):
        logger.debug('new db connection opened')
        return True
    else:
        # logger.debug('reusing db connection')
        return False


async def buffered_send(destination, content, max_length=2000):
    # use to replace await ctx.send(message) if message could potentially be over the Discord limit of 2000 characters
    # will split message by \n characters and send in chunks up to max_length size

    if not content:
        return
    paginator = commands.Paginator(prefix='', suffix='', max_size=max_length)

    for line in content.split('\n'):
        paginator.add_line(line)

    for page in paginator.pages:
        await destination.send(page)


def escape_role_mentions(input: str):
    # like escape_mentions but allow user mentions. disallows everyone/here/role

    return re.sub(r'@(everyone|here|&[0-9]{17,21})', '@\u200b\\1', str(input))


def escape_everyone_here_roles(input: str):
    # escapes @everyone and @here

    return re.sub(r'@(everyone|here)', '@\u200b\\1', str(input))


def is_valid_poly_gamename(input: str):
    # key_words = ["War", "Spirit", "Faith", "Glory", "Blood", "Paradise", "Magical", "Jungle",
    #              "Empires", "Songs", "Dawn", "Prophecy", "Prophesy", "Gold",
    #              "Fire", "Swords", "Queens", "Kings", "Tribes", "Tribe", "Tales",
    #              "Hills", "Fields", "Lands", "Forest", "Ocean", "Fruit", "Mountain", "Lake",
    #              "Test", "Unknown"]
    key_words = ["War", "Spirit", "Faith", "Glory", "Blood", "Empires", "Songs", "Dawn", "Majestic", "Parade",
                 "Prophecy", "Prophesy", "Gold", "Fire", "Swords", "Queens", "Knights", "Kings", "Tribes",
                 "Tales", "Quests", "Change", "Games", "Throne", "Conquest", "Struggle", "Victory",
                 "Battles", "Legends", "Heroes", "Storms", "Clouds", "Gods", "Love", "Lords",
                 "Lights", "Wrath", "Destruction", "Whales", "Ruins", "Monuments", "Wonder", "Clowns",
                 "Bongo", "Duh!", "Squeal", "Squirrel", "Confusion", "Gruff", "Moan", "Chickens", "Spunge",
                 "Gnomes", "Bell boys", "Gurkins", "Commotion", "LOL", "Shenanigans", "Hullabaloo",
                 "Papercuts", "Eggs", "Mooni", "Gaami", "Banjo", "Flowers", "Fiddlesticks", "Fish Sticks", "Hills", "Fields", "Lands", "Forest", "Ocean", "Fruit", "Mountain",
                 "Lake", "Paradise", "Jungle", "Desert", "River", "Sea", "Shores", "Valley", "Garden", "Moon",
                 "Star", "Winter", "Spring", "Summer", "Autumn", "Divide", "Square", "Custard", "Goon", "Cat",
                 "Spagetti", "Fish", "Fame", "Popcorn", "Dessert", "Space", "Glacier", "Ice", "Frozen", "Superb", "Unknown", "Test",
                 "Beasts", "Birds", "Bugs", "Food", "Aliens", "Plains", "Volcano", "Cliff", "Rapids", "Reef", "Plateau", "Basin", "Oasis",
                 "Marsh", "Swamp", "Monsoon", "Atoll", "Fjord", "Tundra", "Map", "Strait", "Savanna", "Butte", "Bay", "Giants", "Warriors",
                 "Archers", "Defenders", "Catapults", "Riders", "Sleds", "Explorers", "Priests", "Ships", "Dragons", "Crabs", "Rebellion"]
    return any(word.upper() in input.upper() for word in key_words)


def string_to_user_id(input):
    # given a user @Mention or a raw user ID, returns just the raw user ID (does not validate the ID itself, but does sanity check length)
    match = re.match(r'([0-9]{15,21})$', input) or re.match(r'<@!?([0-9]+)>$', input)
    # regexp is from https://github.com/Rapptz/discord.py/blob/02397306b2ed76b3bc42b2b28e8672e839bdeaf5/discord/ext/commands/converter.py#L117

    try:
        return int(match.group(1))
    except (ValueError, AttributeError):
        return None


async def get_guild_member(ctx, input):

    # Find matching Guild member by @Mention or Name. Fall back to case-insensitive search
    # TODO: use exceptions.NoSingleMatch etc like Player.get_or_except()

    name_matches, nick_matches, substring_matches = [], [], []

    user_id_match = string_to_user_id(input)
    if user_id_match:
        result = ctx.guild.get_member(user_id_match) or discord.utils.get(ctx.message.mentions, id=user_id_match)
        if result:
            # input is a user id or mention, and a member matching that ID was retrieved
            return [result]
    if len(input) > 5 and input[-5] == '#':
        # The 5 length is checking to see if #0000 is in the string,
        # as a#0000 has a length of 6, the minimum for a potential
        # discriminator lookup.
        potential_discriminator = input[-4:]

        # do the actual lookup and return if found
        # if it isn't found then we'll do a full name lookup below.
        result = discord.utils.get(ctx.guild.members, name=input[:-5], discriminator=potential_discriminator)
        if result is not None:
            return [result]

    # No matches by user ID or Name#Discriminator. Move on to name/nick matches

    input = input.strip('@')  # Attempt to handle fake @Mentions that sometimes slip through
    for p in ctx.guild.members:

        if p.name.upper() == input.upper():
            name_matches.append(p)
        elif p.nick and p.nick.upper() == input.upper():
            nick_matches.append(p)
        else:
            full_name_str = p.nick.upper() + p.name.upper() if p.nick else p.name.upper()
            if input.upper() in full_name_str:
                substring_matches.append(p)

    if name_matches:
        # prioritize exact name matches first
        return name_matches
    if nick_matches:
        # prioritize exact nick matches second
        return nick_matches
    # lastly, partial matches against nick or name equally weighted
    return substring_matches


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
        # logger.debug(f'Parsed game {game_list[-1]}')
    return game_list


def export_game_data(query=None):
    import csv
    import gzip

    filename = 'games_export.csv.gz'
    connect()
    with gzip.open(filename, mode='wt') as export_file:
        game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        header = ['game_id', 'server', 'game_name', 'game_type', 'rank_unranked', 'game_date', 'completed_timestamp', 'side_id', 'side_name', 'player_name', 'winner', 'player_elo', 'player_elo_change', 'squad_elo', 'squad_elo_change', 'tribe']
        game_writer.writerow(header)

        if not query:
            # Default to all confirmed games
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
                   q.tribe.name if q.tribe else '']

            game_writer.writerow(row)

    print(f'Game data written to file {filename} in bot.py directory')
    return filename


def export_game_data_brief(query):
    import csv
    import gzip
    # only supports two-sided games, one winner and one loser

    filename = 'games_export-brief.csv.gz'
    connect()
    with gzip.open(filename, mode='wt') as export_file:
        game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        header = ['game_id', 'server', 'season', 'game_name', 'game_type', 'headline', 'rank_unranked', 'game_date', 'completed_timestamp', 'winning_side', 'winning_roster', 'winning_side_elo', 'losing_side', 'losing_roster', 'losing_side_elo']
        game_writer.writerow(header)

        for game in query:
            if len(game.size) != 2:
                logger.info(f'Skipping export of game {game.id} - this export requires two-sided games.')
                continue
            if not game.is_completed or not game.is_confirmed:
                logger.info(f'Skipping export of game {game.id} - this export completed and confirmed games.')
                continue

            losing_side = game.gamesides[0] if game.gamesides[1] == game.winner else game.gamesides[1]
            winning_side = game.winner
            ranked_status = 'Ranked' if game.is_ranked else 'Unranked'

            season_status = game.is_season_game()  # (Season, League) or ()
            season_str = str(season_status[0]) if season_status else ''

            winning_roster = [f'{p[0].name} {p[1]}' for p in winning_side.roster()]
            losing_roster = [f'{p[0].name} {p[1]}' for p in losing_side.roster()]

            row = [game.id, settings.guild_setting(game.guild_id, 'display_name'), season_str, game.name, game.size_string(),
                   game.get_gamesides_string(), ranked_status, str(game.date), str(game.completed_ts),
                   winning_side.name(), " / ".join(winning_roster), winning_side.elo_strings()[0],
                   losing_side.name(), " / ".join(losing_roster), losing_side.elo_strings()[0]]

            game_writer.writerow(row)

    print(f'Game data written to file {filename} in bot.py directory')
    return filename


async def paginate(bot, ctx, title, message_list, page_start=0, page_end=10, page_size=10):
    # Allows user to page through a long list of messages with reactions
    # message_list should be a [(List of, two-item tuples)]. Each tuple will be split into an embed field name/value

    page_end = page_end if len(message_list) > page_end else len(message_list)

    first_loop = True
    reaction, user = None, None

    while True:
        embed = discord.Embed(title=title)
        for entry in range(page_start, page_end):
            embed.add_field(name=message_list[entry][0][:256], value=message_list[entry][1][:1024], inline=False)
        if page_size < len(message_list):
            embed.set_footer(text=f'{page_start + 1} - {page_end} of {len(message_list)}')

        if first_loop is True:
            sent_message = await ctx.send(embed=embed)
            if len(message_list) > page_size:
                await sent_message.add_reaction('⏪')
                await sent_message.add_reaction('⬅')
                await sent_message.add_reaction('➡')
                await sent_message.add_reaction('⏩')
            else:
                return
        else:
            try:
                await reaction.remove(user)
            except (discord.ext.commands.errors.CommandInvokeError, discord.errors.Forbidden):
                logger.warn('Unable to remove message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
            await sent_message.edit(embed=embed)

        def check(reaction, user):
            e = str(reaction.emoji)
            compare = False
            if page_size < len(message_list):
                if page_start > 0 and e in '⏪⬅':
                    compare = True
                elif page_end < len(message_list) and e in '➡⏩':
                    compare = True
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
