# from modules.models import Tribe, Lineup

import modules.models as models
models.db.connect()
for g in models.Game.select():
    gamesides = g.ordered_side_list()
    size = [len(gs.lineup) for gs in gamesides]
    # print(g.id, size)
    g.size = size
    g.save()

for gs in models.GameSide.select().where(models.GameSide.size == 1):
    gs.size = len(gs.lineup)
    gs.save()
models.db.close()

print('done')
