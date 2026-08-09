"""Focused coverage for P8.17 league CSV exports."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import gzip
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_export_workers')
service = import_offline_runtime('modules.league_export')
league = import_offline_runtime('modules.league')
utilities = import_offline_runtime('modules.utilities')


def root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


def member(member_id=10):
    return SimpleNamespace(
        id=member_id,
        mention=f'<@{member_id}>',
        roles=(),
        guild=SimpleNamespace(id=300),
    )


def request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        requester_is_staff=True,
        league_scope=True,
        include_logs=False,
        attachment_limit=8 * 1024 * 1024,
    )
    values.update(overrides)
    return workers.LeagueExportRequest(**values)


def result(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        include_logs=False,
        game_count=4,
        filename='league-games.csv.gz',
        payload=b'payload',
    )
    values.update(overrides)
    return workers.LeagueExportResult(**values)


class FakeQuery:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def count(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


class RegistrationAndServiceTests(unittest.TestCase):
    def test_native_shape_and_retained_prefix(self):
        maintenance = root().get_command('maintenance')
        self.assertIsNotNone(maintenance)
        command = maintenance.get_command('export')
        self.assertEqual(
            [(item.name, item.type, item.required) for item in command.parameters],
            [('include_logs', discord.AppCommandOptionType.boolean, False)],
        )
        prefix = next(
            command for command in league.league.__cog_commands__
            if command.name == 'league_export'
        )
        self.assertTrue(prefix.enabled)

    def test_access_preserves_league_staff_boundary(self):
        actor = member()
        with mock.patch.object(
            service.house_show, '_league_scope', return_value=False
        ), mock.patch.object(service.settings, 'is_staff', return_value=True):
            self.assertIn('configured league', service.access_error(actor, 300))
        with mock.patch.object(
            service.house_show, '_league_scope', return_value=True
        ), mock.patch.object(service.settings, 'is_staff', return_value=False):
            self.assertIn('staff', service.access_error(actor, 300))
        with mock.patch.object(
            service.house_show, '_league_scope', return_value=True
        ), mock.patch.object(service.settings, 'is_staff', return_value=True):
            self.assertIsNone(service.access_error(actor, 300))


class CsvCompatibilityTests(unittest.TestCase):
    def test_gzip_csv_preserves_legacy_columns_and_filters_null_logs(self):
        winner = mock.Mock()
        winner.name.return_value = 'Alpha'
        winner.elo_strings.return_value = ('1500 +12', None)
        winner.roster.return_value = [
            (SimpleNamespace(name='One'), '1400 +8', ':xinxi:')
        ]
        loser = mock.Mock()
        loser.name.return_value = 'Beta'
        loser.elo_strings.return_value = ('1450 -12', None)
        loser.roster.return_value = [
            (SimpleNamespace(name='Two'), '1300 -8', ':bardur:')
        ]
        game = SimpleNamespace(
            id=77,
            guild_id=300,
            name='League Final',
            size=[2, 2],
            gamesides=[winner, loser],
            winner=winner,
            is_completed=True,
            is_confirmed=True,
            is_ranked=True,
            date='2026-08-09',
            completed_ts='2026-08-09 12:00:00',
            map_type='Dryland',
            gamelogs=[None, '__77__ - created', '__77__ - completed'],
            is_season_game=lambda: (4, 1, False),
            size_string=lambda: '2v2',
            get_gamesides_string=lambda: 'Alpha vs Beta',
        )
        with mock.patch.object(
            utilities.settings, 'guild_setting', return_value='PolyChampions'
        ):
            payload = utilities.export_game_data_brief_bytes(
                [game], export_logs=True
            )
        text = gzip.decompress(payload).decode('utf-8')
        self.assertIn('game_id,server,season', text)
        self.assertIn('77,PolyChampions,4,League Final,2v2', text)
        self.assertIn('__77__ - created\n__77__ - completed', text)
        self.assertNotIn('None', text)


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitive_boundaries(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        export = result()
        self.assertIsInstance(export.payload, bytes)

    def test_worker_owns_connection_and_returns_bytes_without_file_write(self):
        query = FakeQuery([object(), object()])
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=connection
        ), mock.patch.object(workers, '_query', return_value=query), mock.patch.object(
            workers.utilities,
            'export_game_data_brief_bytes',
            return_value=b'gzip-bytes',
        ) as exporter:
            export = workers._generate(request(include_logs=True))
        self.assertEqual(export.game_count, 2)
        self.assertEqual(export.payload, b'gzip-bytes')
        self.assertEqual(export.filename, 'league-games-with-logs.csv.gz')
        exporter.assert_called_once_with(query=query, export_logs=True)
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once()

    def test_worker_rejects_permission_empty_and_attachment_overflow(self):
        with self.assertRaises(workers.LeagueExportPermissionError):
            workers._generate(request(requester_is_staff=False))
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(workers, '_query', return_value=FakeQuery(())):
            with self.assertRaises(workers.LeagueExportEmptyError):
                workers._generate(request())
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_query', return_value=FakeQuery((object(),))
        ), mock.patch.object(
            workers.utilities,
            'export_game_data_brief_bytes',
            return_value=b'oversize',
        ):
            with self.assertRaises(workers.LeagueExportTooLargeError):
                workers._generate(request(attachment_limit=2))

    def test_slow_export_is_responsive_and_conflict_rejects_promptly(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(workers, '_generate', side_effect=slow):
                first = asyncio.create_task(workers.run_league_export(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.02)
                responsive = not first.done()
                with self.assertRaises(workers.LeagueExportBusyError):
                    await workers.run_league_export(request())
                release.set()
                completed = await first
            return responsive, completed

        responsive, completed = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(completed.game_count, 4)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_defers_and_delivers_private_attachment(self):
        command = root().get_command('maintenance').get_command('export')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300, filesize_limit=9_000_000),
            user=member(),
            response=SimpleNamespace(
                defer=mock.AsyncMock(), send_message=mock.AsyncMock()
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = league.league.__new__(league.league)
        export = result()
        with mock.patch.object(
            service, 'access_error', return_value=None
        ), mock.patch.object(
            service, 'request', return_value=request()
        ), mock.patch.object(
            service, 'run_export', new=mock.AsyncMock(return_value=export)
        ), mock.patch.object(
            service, 'discord_file', return_value='file'
        ):
            returned = await command.callback(cog, interaction, True)
        self.assertEqual(returned, export)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertTrue(kwargs['ephemeral'])
        self.assertEqual(kwargs['file'], 'file')

    async def test_prefix_logs_mode_reuses_worker_and_public_attachment(self):
        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        command = next(
            command for command in league.league.__cog_commands__
            if command.name == 'league_export'
        )
        ctx = SimpleNamespace(
            author=member(),
            guild=SimpleNamespace(id=300, filesize_limit=9_000_000),
            typing=lambda: Typing(),
            send=mock.AsyncMock(),
        )
        export = result(
            include_logs=True,
            filename='league-games-with-logs.csv.gz',
        )
        captured_request = request(include_logs=True)
        cog = league.league.__new__(league.league)
        with mock.patch.object(
            service, 'request', return_value=captured_request
        ) as builder, mock.patch.object(
            service, 'run_export', new=mock.AsyncMock(return_value=export)
        ), mock.patch.object(
            service, 'discord_file', return_value='file'
        ):
            await command.callback(cog, ctx, arg='logs')
        builder.assert_called_once_with(
            member=ctx.author, guild=ctx.guild, include_logs=True
        )
        ctx.send.assert_awaited_once()
        self.assertEqual(ctx.send.call_args.kwargs['file'], 'file')


if __name__ == '__main__':
    unittest.main()
