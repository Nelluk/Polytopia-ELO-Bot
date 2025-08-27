import discord
from discord.ext import commands
import logging
import asyncio
import settings
import modules.models as models
import modules.exceptions as exceptions
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


def lock_game(game_id: int):
    if game_id in settings.bot.locked_game_records:
        logger.warning(f'Tried to lock game {game_id} but it is already locked!')
        raise exceptions.RecordLocked(f'Game {game_id} is locked by another command. Try again in a few seconds. If this persists please inform **Nelluk**.')
    else:
        logger.debug(f'Locking game {game_id}')
        settings.bot.locked_game_records.add(game_id)


def unlock_game(game_id: int):
    if game_id in settings.bot.locked_game_records:
        settings.bot.locked_game_records.discard(game_id)
        logger.debug(f'Unlocking game {game_id}')
        return True
    else:
        logger.debug(f'Tried to unlock game {game_id} but it was already unlocked')
        return False


def guild_role_by_name(guild, name: str, allow_partial: bool = False):
    # match 'name' to a role in guild, ignoring case. Returns None if no match
    # https://discordpy.readthedocs.io/en/latest/api.html#discord.utils.find

    # Attempt an exact match
    role = discord.utils.find(lambda r: name == r.name, guild.roles)

    if role is None:
        # If no exact match is found, attempt a case-insensitive match
        role = discord.utils.find(lambda r: name.upper() == r.name.upper(), guild.roles)

        if allow_partial and role is None:
            # If partial matches are allowed, try a partial match
            role = discord.utils.find(lambda r: name.upper() in r.name.upper(), guild.roles)

    return role


async def buffered_send(destination, content, max_length=2000, allowed_mentions=None):
    # use to replace await ctx.send(message) if message could potentially be over the Discord limit of 2000 characters
    # will split message by \n characters and send in chunks up to max_length size
    # TODO: Could be handy to split by something like a ',' character IF there are no \n's in the text

    if not content:
        return
    paginator = commands.Paginator(prefix='', suffix='', max_size=max_length)

    for line in content.split('\n'):
        logger.debug(f'adding line to buffered_send: {line}')
        paginator.add_line(line[:max_length - 2])

    for page in paginator.pages:
        logger.debug(f'sending page with buffered_send: {page}')
        await destination.send(page, allowed_mentions=allowed_mentions)


async def send_to_log_channel(guild, message):

    logger.debug(f'Sending log message to log_channel: {message}')
    staff_output_channel = guild.get_channel(settings.guild_setting(guild.id, 'log_channel'))
    if not staff_output_channel:
        logger.warning(f'Could not load log_channel for server {guild.id} - skipping')
    else:
        await buffered_send(destination=staff_output_channel, content=message)


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
    key_words = [
        "War", "Spirit", "Faith", "Glory", "Blood", "Empires", "Songs", "Dawn", "Prophecy", "Gold",
        "Fire", "Swords", "Queens", "Knights", "Kings", "Tribes", "Tales", "Quests", "Change", "Games",
        "Throne", "Conquest", "Struggle", "Victory", "Battles", "Legends", "Heroes", "Storms", "Clouds", "Gods",
        "Love", "Lords", "Lights", "Wrath", "Destruction", "Ruins", "Monuments", "Wonder", "Giants", "Warriors",
        "Archers", "Defenders", "Catapults", "Riders", "Sleds", "Explorers", "Priests", "Ships", "Dragons", "Crabs",
        "Rebellion", "Mists", "Twilight", "Mysteries", "Reckoning", "Squires", "Battleships", "Scouts", "Cloaks", "Daggers",
        "Cities", "Houses", "Nests", "Wings", "Council", "Armies", "Adventure", "Musicians", "Temples", "Politics",
        "Bones", "Navies", "Tides", "Agreement", "Treaties", "Corruption", "Clowns", "Bongo", "Duh!", "Squeal",
        "Squirrel", "Confusion", "Gruff", "Moan", "Chickens", "Sponge", "Gnomes", "Bellboys", "Gherkins", "Commotion",
        "LOL", "Shenanigans", "Hullabaloo", "Papercuts", "Eggs", "Mooni", "Gaami", "Banjo", "Flowers", "Fiddlesticks",
        "Fish Sticks", "Politicians", "Giraffes", "Seagulls", "Graphs", "Accountants", "Eyeballs", "Cubes", "Hilarity", "Computers",
        "Fashion", "Apple", "ROFL", "Doink!", "Bang!", "Boop", "Bruises", "Hooxe", "Dice", "Ice Marimba",
        "Lighthouses", "Sharks", "Centipedes", "the Lirepacci", "Fungi", "Hexapods", "Squid", "Wave", "Explosions",
        "Epic", "Endless", "Glorious", "Brave", "Misty", "Mysterious", "Lost", "Cold", "Amazing", "Doomed",
        "Glowing", "Glimmering", "Magical", "Living", "Thriving", "Bold", "Dark", "Bright", "Majestic", "Shimmering",
        "Lucky", "Great", "Everlasting", "Eternal", "Superb", "Frozen", "Magnificent", "Evil", "Beautiful", "Surprising",
        "Timeless", "Classic", "Hot", "Destructive", "Perilous", "Burning", "Benevolent", "Tyrannical", "Unknowable", "Impressive",
        "Unbelievable", "Fantastic", "Creepy", "Cursed", "Stunning", "Immortal", "Enchanted", "Sunken", "Gruffy", "Slimy",
        "Silly", "Unwilling", "Stumbling", "Drunken", "Merry", "Mediocre", "Normal", "Stupid", "Moody", "Tipsy",
        "Trifling", "Rancid", "Numb", "Livid", "Smooth", "Nuclear", "Nifty", "Broken", "Inconceivable", "Disappointing",
        "Repulsive", "Ridiculous", "Decent", "Quaint", "Dreamy", "Digital", "Fake", "Apathetic", "Indifferent", "Depressing",
        "Illegal", "Annoying", "Worthless", "Mind-Bent", "Tedious", "Cool", "Goofy", "Hills", "Fields", "Lands",
        "Forest", "Ocean", "Fruit", "Mountain", "Lake", "Paradise", "Jungle", "Desert", "River", "Sea",
        "Shores", "Valley", "Garden", "Moon", "Star", "Winter", "Spring", "Summer", "Autumn", "Divide",
        "Square", "Glacier", "Ice", "Plains", "Volcano", "Cliff", "Rapids", "Reef", "Plateau", "Basin",
        "Oasis", "Marsh", "Swamp", "Monsoon", "Atoll", "Fjord", "Tundra", "Map", "Strait", "Savanna",
        "Butte", "Bay", "Wasteland", "Badland", "Pond", "Graveyard", "Battleground", "Embassy", "Custard", "Goon",
        "Cat", "Spaghetti", "Fish", "Fame", "Popcorn", "Dessert", "Space", "Beasts", "Birds", "Bugs",
        "Food", "Aliens", "Website", "Library", "Fans", "Nothingness", "Animals", "Fist", "Mind", "Imagination",
        "Implications", "Buffoonery", "Discuss", "Discussion", "Chat", "Pre",
    ]
    return any(word.upper() in input.upper() for word in key_words)

def get_map_type(query):
    # Convert an abbreviation into proper map type name. Lazily doing this instead of a proper
    # solution with a Maps database table
    query = query.lower()
    if query == 'ww' or query == 'waterworld':
        query = 'water world'
    elif query == 'drylands':
        query = 'dryland'

    if len(query) < 3:
        return None
    
    for map_type in settings.map_types:
        if query in map_type.lower():
            return map_type
    
    return None


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
    # Given a Discord.Member and a ['List of', 'Role names'], return set of role names that the Member has.
    member_roles = [x.name for x in discord_member.roles]
    return set(member_roles).intersection(list_of_role_names)


def summarize_game_list(games_query, player_discord_id: int = None):
    # Turns a list/query-result of several games (or GameSide) into a List of Tuples that can be sent to the pagination function
    # ie. [('Game 330   :nauseated_face: DrippyIsGod vs Nelluk :spy: Mountain Of Songs', '2018-10-05 - 1v1 - WINNER: Nelluk')]
    game_list = []

    # for counter, game in enumerate(games_query):
    # for game in peewee.prefetch(games_query, models.GameSide):
    for game in games_query:
        channel_link = ''
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
        platform_str = '' if game.is_mobile else f'{game.platform_emoji()} - '

        if player_discord_id:
            _, gameside = game.has_player(discord_id=player_discord_id)
            if gameside and gameside.team_chan:
                channel_link = f'\n<#{gameside.team_chan}>'

        game_list.append((
            f'{game.get_headline()}'[:255],
            f'{(str(game.date))} - {platform_str}{rank_str}{game.size_string()} - {status_str}{channel_link}'
        ))
        # logger.debug(f'Parsed game {game_list[-1]}')
    return game_list

def trade_price_formula(record, leadership):
    # GalC4's formula for calculating trade price
    # record is a list of (tier, games, wins) tuples of past 3 seasons
    import math

    # Variables
    leadership_weight = 0.07 
    played_weight = 3
    game_nerf = 3
    closer_avg = 0.8
    s1_weight = 9
    s2_weight = 8
    s3_weight = 7
    wr_weight = 8.35
    seasons_played_weight = {1: 1.3, 2: 1.05, 3: 1}
    inflation_factor = 0.809592

    def tier_weight(tier):
        if tier == 4 or tier == 5 or tier == 6:
            return 0.7
        elif tier == 3:
            return 2.5
        elif tier == 2:
            return 4
        elif tier == 1:
            return 5.6

        # tier is None, no games played that season
        return 0

    # Unpack record
    s1tier, s1games, s1wins = record[0]
    s2tier, s2games, s2wins = record[1]
    s3tier, s3games, s3wins = record[2]

    # Calculations
    leadership_weight = leadership_weight if leadership else 0
    seasons_played = sum(1 for games in [s1games, s2games, s3games] if games > 0)

    s1_w = ((((s1games + played_weight) / game_nerf) + closer_avg) * tier_weight(s1tier)) / (s1_weight * (s1games - s1wins + wr_weight))
    s2_w = ((((s2games + played_weight) / game_nerf) + closer_avg) * tier_weight(s2tier)) / (s2_weight * (s2games - s2wins + wr_weight))
    s3_w = ((((s3games + played_weight) / game_nerf) + closer_avg) * tier_weight(s3tier)) / (s3_weight * (s3games - s3wins + wr_weight))

    a = math.sqrt(math.sqrt((s1wins + s2wins + s3wins + 1) / (s1games + s2games + s3games + 1)))
    b = (s1_w + s2_w + s3_w) / math.sqrt(1.8 * seasons_played)
    c = (math.sqrt(math.sqrt(b)) + leadership_weight) / 4
    d = (a * b * c * 943) + (leadership_weight * 10)
    e = seasons_played_weight[seasons_played] * d

    return math.floor(e * inflation_factor)

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


def export_game_data_brief(query, export_logs=False):
    import csv
    import gzip
    # only supports two-sided games, one winner and one loser

    filename = 'games_export-brief.csv.gz'
    connect()
    with gzip.open(filename, mode='wt', encoding='utf-8') as export_file:
        game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        header = ['game_id', 'server', 'season', 'game_name', 'game_type', 'headline', 'rank_unranked', 'game_date', 'completed_timestamp', 'winning_side', 'winning_roster', 'winning_side_elo', 'losing_side', 'losing_roster', 'losing_side_elo', 'map_type']
        if export_logs:
            header.append('logs')
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

            winning_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in winning_side.roster()]
            losing_roster = [f'{p[0].name} {p[1]} {p[2]}' for p in losing_side.roster()]

            row = [game.id, settings.guild_setting(game.guild_id, 'display_name'), season_str, game.name, game.size_string(),
                   game.get_gamesides_string(), ranked_status, str(game.date), str(game.completed_ts),
                   winning_side.name(), " / ".join(winning_roster), winning_side.elo_strings()[0],
                   losing_side.name(), " / ".join(losing_roster), losing_side.elo_strings()[0], game.map_type]
            if export_logs:
                row.append("\n".join(game.gamelogs))

            game_writer.writerow(row)

    print(f'Game data written to file {filename} in bot.py directory')
    return filename


def export_player_data(player_list, member_list):
    import csv
    # only supports two-sided games, one winner and one loser

    filename = 'player-export.csv'
    connect()
    with open(filename, mode='w') as export_file:

        game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        header = ['name', 'discord_id', 'team', 'elo', 'elo_max', 'global_elo', 'global_elo_max', 'local_record', 'global_record', 'games_in_last_14d', 'poly_id', 'poly_name', 'profile_image']
        game_writer.writerow(header)

        for player, member in zip(player_list, member_list):

            dm = player.discord_member
            p_record = player.get_record()
            dm_record = dm.get_record()

            recent_games = dm.games_played(in_days=14).count()

            row = [player.name, dm.discord_id, player.team.name if player.team else '', player.elo, player.elo_max,
                   dm.elo, dm.elo_max, f'{p_record[0]} / {p_record[1]}', f'{dm_record[0]} / {dm_record[1]}',
                   recent_games, dm.polytopia_id, dm.polytopia_name, member.display_avatar.replace(format='png', size=512)]
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
                logger.warning('Unable to remove message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
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
                logger.warning('Unable to clear message reaction due to insufficient permissions. Giving bot \'Manage Messages\' permission will improve usability.')
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

async def active_members_and_players(guild, active_role_name: str, inactive_role_name: str = None):
    # Returns [List of Discord Members], [List of Matching models.Players()]
    # where each member has the active role but optionally does not have the inactive role
    # for example, give the name of a Team role, and get back all discord members with that role as well as their equivalent player objects
    # For now this is sorted by player.elo, descending.

    logger.info('active_members_and_players()')
    active_role = discord.utils.get(guild.roles, name=active_role_name)
    if not active_role:
        logger.error(f'active_members_and_players: Could not find matching role for active_role_name {active_role_name}')
        # raise exceptions.CheckFailedError(f'No matching guild role with name "{active_role_name}"')
        return [], []
    
    inactive_role = discord.utils.get(guild.roles, name=inactive_role_name) if inactive_role_name else None

    members_by_id = sorted([member for member in active_role.members if inactive_role not in member.roles], key=lambda member: member.id)
    # All discord members with role and not Inactive role, sorted by ID

    sorted_ids = [member.id for member in members_by_id]

    players_by_id = list((models.Player.select() .join(models.DiscordMember)
                    .where(
                        (models.DiscordMember.discord_id.in_(sorted_ids)) & (models.Player.guild_id == guild.id)
                    ) .order_by(models.DiscordMember.discord_id)))
    
    if len(members_by_id) != len(players_by_id):
        logger.warning(f'Mismatched lengths of members_by_id and players_by_id')

    logger.debug([member.name for member in members_by_id])
    logger.debug([player.name for player in players_by_id])

    sorted_zipped_lists = sorted(zip(members_by_id, players_by_id), key=lambda x: x[1].elo_moonrise, reverse=True)
    # Sort both lists by Player.elo

    for member, player in sorted_zipped_lists:
        logger.debug(f'{member.name} {member.id} {player.elo} {player.id}')
    
    members_by_id_sorted = [item[0] for item in sorted_zipped_lists]
    players_by_id_sorted = [item[1] for item in sorted_zipped_lists]

    return members_by_id_sorted, players_by_id_sorted
