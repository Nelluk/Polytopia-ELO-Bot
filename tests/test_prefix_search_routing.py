"""Regression coverage for prefix search grammar and registration guidance."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime
from tests.test_player_workspace import snapshot

games = import_offline_runtime('modules.games')
workers = import_offline_runtime('modules.player_workers')


class PrefixSearchRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_syntax_beats_colliding_player_for_every_alias(self):
        cases = (
            ('allgames', 'allgames', 'ALLGAMES'),
            ('incomplete', 'incomplete', 'INCOMPLETE'),
            ('incomplete', 'complete', 'COMPLETE'),
            ('incomplete', 'completed', 'COMPLETE'),
            ('wins', 'wins', 'WINS'),
            ('wins', 'loss', 'LOSSES'),
            ('wins', 'losses', 'LOSSES'),
        )
        for command_name, alias, mode in cases:
            for query in (
                'all', 'ALL', 'Jets', 'RONIN', 'Nelluk Ronin',
                '2v2', '2vs2', 'ab',
            ):
                with self.subTest(alias=alias, query=query):
                    cog = games.polygames.__new__(games.polygames)
                    cog.game_search = mock.AsyncMock()
                    ctx = SimpleNamespace(
                        guild=SimpleNamespace(id=300),
                        author=SimpleNamespace(id=100), invoked_with=alias,
                    )

                    async def load(request):
                        # Exercise routing inside the real worker resolver.
                        workers._resolve_player(request)
                        return snapshot(discord_id=200)

                    cog._load_player_workspace = mock.AsyncMock(side_effect=load)
                    teams = {'Jets': [SimpleNamespace(id=1)]}
                    with mock.patch.object(
                        workers.models.Team, 'get_by_name',
                        side_effect=lambda name, guild: teams.get(name, []),
                    ), mock.patch.object(
                        workers.models.Player, 'string_matches',
                        return_value=[SimpleNamespace(id=200)],
                    ) as player_match:
                        await getattr(games.polygames, command_name).callback(
                            cog, ctx, args=query,
                        )
                    cog.game_search.assert_awaited_once_with(
                        ctx=ctx, mode=mode, arg_list=query.split(),
                    )
                    player_match.assert_not_called()

    async def test_single_player_keeps_workspace_and_excludes_legacy_name_match(self):
        for name in ('allgames', 'incomplete', 'wins'):
            with self.subTest(command=name):
                cog = games.polygames.__new__(games.polygames)
                cog.game_search = mock.AsyncMock()
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=300, get_member=lambda _: None),
                    author=SimpleNamespace(id=100), invoked_with=name,
                    send=mock.AsyncMock(),
                )

                async def load(request):
                    self.assertEqual(workers._resolve_player(request).id, 200)
                    return snapshot(discord_id=200)

                cog._load_player_workspace = mock.AsyncMock(side_effect=load)
                with mock.patch.object(
                    workers.models.Team, 'get_by_name', return_value=[],
                ), mock.patch.object(
                    workers.models.Player, 'string_matches',
                    return_value=[SimpleNamespace(id=200)],
                ) as match, mock.patch.object(
                    games.settings, 'is_staff', return_value=False,
                ):
                    await getattr(games.polygames, name).callback(cog, ctx, args='Nelluk')
                match.assert_called_once_with(
                    player_string='Nelluk', guild_id=300, include_poly_info=False,
                )
                cog.game_search.assert_not_awaited()
                ctx.send.assert_awaited_once()

    async def test_bare_history_aliases_keep_self_workspace(self):
        for command_name, alias in (
            ('incomplete', 'incomplete'), ('incomplete', 'completed'),
            ('wins', 'wins'), ('wins', 'losses'),
        ):
            with self.subTest(alias=alias):
                cog = games.polygames.__new__(games.polygames)
                cog._load_player_workspace = mock.AsyncMock(
                    return_value=snapshot(discord_id=100),
                )
                cog.game_search = mock.AsyncMock()
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=300, get_member=lambda _: None),
                    author=SimpleNamespace(id=100), invoked_with=alias,
                    send=mock.AsyncMock(),
                )
                await getattr(games.polygames, command_name).callback(cog, ctx)
                request = cog._load_player_workspace.await_args.args[0]
                self.assertEqual(request.discord_id, 100)
                cog.game_search.assert_not_awaited()
                ctx.send.assert_awaited_once()

    def test_normal_player_lookup_still_accepts_polytopia_names(self):
        player = SimpleNamespace(id=200)
        with mock.patch.object(
            workers.models.Player, 'string_matches', return_value=[player],
        ) as match:
            self.assertIs(workers._resolve_player(workers.PlayerWorkspaceRequest(
                guild_id=300, player_query='PolytopiaName',
            )), player)
        self.assertTrue(match.call_args.kwargs.get('include_poly_info', True))


class RegistrationGuidanceTests(unittest.TestCase):
    def test_retired_registration_name_only_appears_in_deprecation_adapter(self):
        modules = Path(games.__file__).parent
        for path in modules.glob('*.py'):
            tree = ast.parse(path.read_text())
            if path.name == 'games.py':
                cog = next(
                    node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == 'polygames'
                )
                cog.body = [
                    node for node in cog.body
                    if getattr(node, 'name', None) != 'setname'
                ]
            stale = [
                node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str) and 'steamname' in node.value
            ]
            self.assertEqual(
                stale, [], f'{path.name} still recommends retired registration',
            )
