"""Offline lookup SQL and private chooser coverage; no configured DB access."""
from types import SimpleNamespace
import unittest
import tempfile
from unittest import mock
import peewee

from tests.test_player_workspace import games, player_workers, player_views, app_group, snapshot


class NameSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = peewee.SqliteDatabase(self.temp.name + '/lookup.sqlite')
        self.db.connect()
        self.binding = self.db.bind_ctx(
            [player_workers.models.Player, player_workers.models.DiscordMember],
            bind_refs=False, bind_backrefs=False,
        )
        self.binding.__enter__()
        self.patch = mock.patch.object(player_workers.models, 'db', self.db)
        self.patch.start()
        self.db.execute_sql('CREATE TABLE discordmember (id INTEGER PRIMARY KEY, discord_id INTEGER, polytopia_name TEXT, name TEXT)')
        self.db.execute_sql('CREATE TABLE player (id INTEGER PRIMARY KEY, discord_member_id INTEGER, guild_id INTEGER)')
        self.addCleanup(self.db.close)
        self.addCleanup(self.binding.__exit__, None, None, None)
        self.addCleanup(self.patch.stop)

    def add(self, identity, name, guild=300):
        self.db.execute_sql('INSERT INTO discordmember VALUES (?, ?, ?, ?)', (identity, identity, name, f'Discord{identity}'))
        self.db.execute_sql('INSERT INTO player VALUES (?, ?, ?)', (identity, identity, guild))

    def test_exact_partial_duplicates_and_guild_boundary(self):
        self.add(1, 'Alpha')
        self.add(2, 'Alpha Two')
        self.add(3, 'Alpha', guild=400)
        self.assertEqual([m.discord_id for m in player_workers.load_player_name_matches(300, ' alpha ')], [1])
        self.assertEqual(len(player_workers.load_player_name_matches(300, 'alp')), 2)
        self.add(4, 'ALPHA')
        self.assertEqual([m.discord_id for m in player_workers.load_player_name_matches(300, 'Alpha')], [1, 4])
        self.assertEqual(player_workers.load_player_name_matches(300, 'missing'), ())

    def test_bound_and_literal_wildcards(self):
        for identity in range(30):
            self.add(identity, f'Alpha {identity}')
        self.add(99, '100% real_name')
        self.assertEqual(len(player_workers.load_player_name_matches(300, 'Alpha')), 26)
        self.assertEqual([m.discord_id for m in player_workers.load_player_name_matches(300, '%')], [99])
        self.assertEqual([m.discord_id for m in player_workers.load_player_name_matches(300, '_')], [99])
        for query in (' ', 'a' * 201):
            with self.assertRaises(ValueError):
                player_workers.load_player_name_matches(300, query)


class NameCommandTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=100), guild=SimpleNamespace(id=300), guild_id=300,
            response=SimpleNamespace(defer=mock.AsyncMock(), send_message=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()), edit_original_response=mock.AsyncMock(),
        )

    async def invoke(self, matches, *, member=None, name='Alpha'):
        interaction = self.interaction()
        cog = games.polygames.__new__(games.polygames)
        cog.player = SimpleNamespace(can_run=mock.AsyncMock(return_value=True))
        cog._publish_player_profile = mock.AsyncMock()
        with (mock.patch.object(games.commands.Context, 'from_interaction', new=mock.AsyncMock(return_value=SimpleNamespace())),
              mock.patch.object(games.settings, 'guild_setting', return_value='$'),
              mock.patch.object(player_workers, 'run_player_name_matches', new=mock.AsyncMock(return_value=matches))):
            await app_group(games.polygames, 'player').get_command('show').callback(cog, interaction, member, name)
        return cog, interaction

    async def test_conflict_single_missing_and_overflow(self):
        match = player_workers.PlayerNameMatch(200, 'Alpha', 'Discord')
        cog, interaction = await self.invoke((match,), member=SimpleNamespace(id=200))
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        cog, interaction = await self.invoke((match,))
        cog._publish_player_profile.assert_awaited_once_with(interaction, 200, public_followup=True)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        for matches, expected in (((), 'No registered'), ((match,) * 26, 'More than 25')):
            cog, interaction = await self.invoke(matches)
            self.assertIn(expected, interaction.edit_original_response.await_args.kwargs['content'])
            cog._publish_player_profile.assert_not_awaited()

    async def test_chooser_identity_guards_and_single_use(self):
        matches = (player_workers.PlayerNameMatch(200, 'Alpha', 'One'), player_workers.PlayerNameMatch(201, 'Alpha', 'Two'))
        _, interaction = await self.invoke(matches)
        chooser = interaction.edit_original_response.await_args.kwargs['view']
        self.assertEqual([option.value for option in chooser.select.options], ['200', '201'])
        chooser.on_select = mock.AsyncMock()
        self.assertTrue(await chooser.interaction_check(interaction))
        interaction.user.id = 999
        self.assertFalse(await chooser.interaction_check(interaction))
        interaction.user.id = 100
        interaction.guild_id = 400
        self.assertFalse(await chooser.interaction_check(interaction))
        interaction.guild_id = 300
        chooser.select._values = ['201']
        await chooser.choose(interaction)
        await chooser.choose(interaction)
        chooser.on_select.assert_awaited_once_with(interaction, 201)
        self.assertTrue(chooser.select.disabled)

    async def test_public_publish_and_stale_registration(self):
        cog = games.polygames.__new__(games.polygames)
        cog._load_player_workspace = mock.AsyncMock(return_value=snapshot(200))
        interaction = self.interaction()
        with mock.patch.object(games.settings, 'is_staff', return_value=False):
            await cog._publish_player_profile(interaction, 200, public_followup=True)
        self.assertEqual(cog._load_player_workspace.await_args.args[0].guild_id, 300)
        self.assertFalse(interaction.followup.send.await_args.kwargs['ephemeral'])
        cog._load_player_workspace.side_effect = player_workers.PlayerNotFound('That member is not registered here.')
        interaction = self.interaction()
        await cog._publish_player_profile(interaction, 200, public_followup=True)
        interaction.edit_original_response.assert_awaited_once_with(
            content='That member is not registered here.', view=None)
        interaction.followup.send.assert_not_awaited()

    async def test_worker_is_off_event_loop(self):
        import threading
        main_thread = threading.get_ident()
        with mock.patch.object(player_workers, 'load_player_name_matches', side_effect=lambda *args: threading.get_ident()):
            worker_thread = await player_workers.run_player_name_matches(300, 'Alpha')
        self.assertNotEqual(main_thread, worker_thread)
