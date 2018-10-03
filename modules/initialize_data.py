# from discord.ext import commands
# from bot import config
from modules import models
import peewee


def initialize_data():
    # import modules.models as models

    polychamps_list = [('The Ronin', ':spy:', 'https://media.discordapp.net/attachments/471128500338819072/471941775142158346/neworange.png'),
                        ('The Jets', ':airplane:', 'https://media.discordapp.net/attachments/471128500338819072/471941513241427968/newpurple.png'),
                        ('The Lightning', ':cloud_lightning:', 'https://media.discordapp.net/attachments/471128500338819072/471941648499081217/teamyellow.png'),
                        ('The Sparkies', ':dog:', 'https://media.discordapp.net/attachments/471128500338819072/471941823900942347/newbrown.png'),
                        ('The Mallards', ':duck:', 'https://media.discordapp.net/attachments/471128500338819072/471941726139973663/newgreen.png'),
                        ('The Cosmonauts', ':space_invader:', 'https://media.discordapp.net/attachments/471128500338819072/471941440797278218/newmagenta.png'),
                        ('The Wildfire', ':fire:', 'https://media.discordapp.net/attachments/471128500338819072/471941893371199498/newred.png'),
                        ('The Bombers', ':bomb:', 'https://media.discordapp.net/attachments/471128500338819072/471941345842298881/newblue.png'),
                        ('The Plague', ':nauseated_face:', 'https://media.discordapp.net/attachments/471128500338819072/471941955933306900/theplague.png'),
                        ('The Crawfish', ':fried_shrimp:', 'https://media.discordapp.net/attachments/471128500338819072/481290788261855232/red-crawfish.png'),
                        ('Home', ':stadium:', None),
                        ('Away', ':airplane:', None)]

    tribe_list = ['Bardur', 'Imperius', 'Xin-Xi', 'Oumaji', 'Kickoo', 'Hoodrick', 'Luxidoor', 'Vengir', 'Zebasi', 'Ai-Mo', 'Quetzali', 'Aquarion', 'Elyrion']

    for guild_id in [478571892832206869, 447883341463814144]:
        # 447883341463814144 = Polychampions
        # 478571892832206869 = Nelluk Test Server

        for team, emoji, image_url in polychamps_list:
            print(f'gid:{guild_id}')
            try:
                print(f'Adding team{team}')
                # logger.debug(f'Adding team{team}')
                with models.db.atomic():
                    team = models.Team.create(name=team, emoji=emoji, image_url=image_url, guild_id=int(guild_id))
            except peewee.IntegrityError:
                pass

    for tribe in tribe_list:
        try:
            print(f'Adding tribe{tribe}')
            # logger.debug(f'Adding tribe{tribe}')
            with models.db.atomic():
                models.Tribe.create(name=tribe)
        except peewee.IntegrityError:
            pass
