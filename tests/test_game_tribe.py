"""Focused offline coverage for the P4.2d game-tribe workflow."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee

from modules import exceptions
from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
game_tribe = import_offline_runtime('modules.game_tribe')
game_tribe_views = import_offline_runtime('modules.game_tribe_views')
games = import_offline_runtime('modules.games')


class FakeTribe:
    def __init__(self, tribe_id, name, emoji):
        self.id = tribe_id
        self.name = name
        self.emoji = emoji


class FakeLineup:
    def __init__(self, state, lineup_id, player, tribe):
        self._state = state
        self.id = lineup_id
        self.player = player
        self.tribe = tribe

    def save(self):
        rows = self._state['assignments']
        for index, (lineup_id, _tribe_id) in enumerate(rows):
            if lineup_id == self.id:
                rows[index] = (
                    lineup_id,
                    getattr(self.tribe, 'id', None),
                )
                return
        raise AssertionError(f'Unknown lineup {self.id}')


class FakeGame:
    def __init__(self, state, lineups, *, guild_id=300):
        self.id = 42
        self.guild_id = guild_id
        self.lineup = tuple(lineups)
        self.announcement_channel = 901
        self.announcement_message = 902
        self._state = state

    def uses_channel_id(self, channel_id):
        return int(channel_id) == 900


def member(member_id=100, *, name='Requester'):
    return SimpleNamespace(
        id=member_id,
        discord_id=member_id,
        name=name,
        display_name=name,
        mention=f'<@{member_id}>',
        guild=SimpleNamespace(id=300),
        roles=(),
    )


def make_tribe_request(
    *,
    game_id=42,
    guild_id=300,
    channel_id=900,
    requester_id=900,
    requester_level=5,
    requester_is_staff=True,
    assignments=(),
    raw_bulk=None,
    legacy_tokens=(),
    native=True,
    legacy_partial=False,
    require_elevated=False,
    expected_snapshots=(),
    check_expected_snapshots=False,
    allow_related_channel=False,
):
    return game_workers.GameTribeMutationRequest(
        game_id=game_id,
        guild_id=guild_id,
        channel_id=channel_id,
        requester_id=requester_id,
        requester_level=requester_level,
        requester_is_staff=requester_is_staff,
        requester_description='**Requester** (`900`)',
        assignments=tuple(assignments),
        expected_snapshots=tuple(expected_snapshots),
        check_expected_snapshots=check_expected_snapshots,
        raw_bulk=raw_bulk,
        legacy_tokens=tuple(legacy_tokens),
        allow_related_channel=allow_related_channel,
        native=native,
        legacy_partial=legacy_partial,
        require_elevated=require_elevated,
        invoked_with='settribe' if not native else '/game tribe',
    )


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeResponse:
    def __init__(self, events):
        self.events = events
        self.done = False

    def is_done(self):
        return self.done

    async def defer(self, *, ephemeral=False):
        self.done = True
        self.events.append(('defer', ephemeral))

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        self.done = True
        self.events.append(('response', content, ephemeral, kwargs))

    async def send_modal(self, modal):
        self.done = True
        self.events.append(('modal', modal))


class FakeFollowup:
    def __init__(self, events):
        self.events = events

    async def send(self, content, *, ephemeral=False, wait=False, **kwargs):
        self.events.append(('followup', content, ephemeral, kwargs))
        if wait:
            return FakeMessage()
        return None


class FakeChannel:
    def __init__(self, events):
        self.id = 900
        self.events = events

    async def send(self, content, **kwargs):
        self.events.append(('channel', content, kwargs))
        return FakeMessage()


class FakeInteraction:
    def __init__(self, *, user_id=100, events=None):
        self.events = events if events is not None else []
        self.user = member(user_id, name='Requester')
        self.guild = SimpleNamespace(id=300)
        self.channel_id = 900
        self.channel = FakeChannel(self.events)
        self.response = FakeResponse(self.events)
        self.followup = FakeFollowup(self.events)
        self.deleted_original = 0

    async def delete_original_response(self):
        self.deleted_original += 1
        self.events.append(('delete-original',))


class GameTribeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'assignments': [(10, 1), (11, 2), (12, None)],
            'logs': [],
        }
        self.catalog = (
            FakeTribe(1, 'Bardur', '🦌'),
            FakeTribe(2, 'Kickoo', '🐋'),
            FakeTribe(3, 'Xin-Xi', '⛰️'),
            FakeTribe(4, 'Elyrion', '🦄'),
            FakeTribe(5, 'Ai-Mo', '🧘'),
            FakeTribe(6, 'Aquarion', '🐚'),
        )
        players = (
            SimpleNamespace(
                id=1,
                name='Alpha',
                discord_member=member(100, name='Alpha Discord'),
            ),
            SimpleNamespace(
                id=2,
                name='Alphonse',
                discord_member=member(200, name='Alphonse Discord'),
            ),
            SimpleNamespace(
                id=3,
                name='Beta',
                discord_member=member(300, name='Beta Discord'),
            ),
        )
        initial_tribes = (
            self.catalog[0],
            self.catalog[1],
            None,
        )
        self.lineups = tuple(
            FakeLineup(self.state, lineup_id, player, tribe)
            for lineup_id, player, tribe in zip(
                (10, 11, 12), players, initial_tribes
            )
        )
        self.game = FakeGame(self.state, self.lineups)
        self.database = FakeDatabase(self.state)
        self.patchers = [
            mock.patch.object(game_workers.models, 'db', self.database),
            mock.patch.object(
                game_workers.models.Game,
                'get_by_id',
                return_value=self.game,
            ),
            mock.patch.object(
                game_workers.models.Tribe,
                'select',
                return_value=self.catalog,
            ),
            mock.patch.object(
                game_workers.models.DiscordMember,
                'get_or_none',
                return_value=object(),
            ),
            mock.patch.object(
                game_workers.models.GameLog,
                'write',
                side_effect=lambda **kwargs: self.state['logs'].append(kwargs),
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requests_and_preview_are_frozen_primitive_dtos(self):
        request = make_tribe_request(raw_bulk='Alpha Ely')
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 99
        self.assertIsInstance(request.assignments, tuple)
        self.assertIsInstance(request.requester_description, str)

        preview = game_workers.prepare_game_tribe_batch(request)
        with self.assertRaises(FrozenInstanceError):
            preview.assignments = ()
        self.assertEqual(preview.assignments[0].lineup_id, 10)
        self.assertEqual(preview.assignments[0].tribe_name, 'Elyrion')

    def test_flat_and_line_based_parsers_support_quotes_and_reject_odd_input(self):
        self.assertEqual(
            game_workers.parse_game_tribe_pairs(
                '"Alpha One" Xin-Xi Beta none'
            ),
            (('Alpha One', 'Xin-Xi'), ('Beta', 'none')),
        )
        self.assertEqual(
            game_workers.parse_game_tribe_pairs(
                '"Alpha One" Xin-Xi\nBeta none'
            ),
            (('Alpha One', 'Xin-Xi'), ('Beta', 'none')),
        )
        with self.assertRaisesRegex(
            game_workers.GameTribeValidationError,
            'alternating',
        ):
            game_workers.parse_game_tribe_pairs('Alpha Elyrion Beta')

    def test_player_and_tribe_matching_is_exact_case_insensitive_and_explicit(self):
        preview = game_workers.prepare_game_tribe_batch(
            make_tribe_request(raw_bulk='aLpHa xIn alphonse NONE')
        )
        self.assertEqual(
            [item.player_name for item in preview.resolved_assignments],
            ['Alpha', 'Alphonse'],
        )
        self.assertEqual(
            [item.tribe_name for item in preview.resolved_assignments],
            ['Xin-Xi', None],
        )

        cases = (
            ('Alpha A', 'ambiguous'),
            ('Al Bardur', 'ambiguous'),
            ('Unknown Elyrion', 'not found'),
            ('Alpha Zzz', 'not found'),
        )
        for raw_bulk, expected in cases:
            with self.subTest(raw_bulk=raw_bulk):
                with self.assertRaises(game_workers.GameTribeBatchValidationError) as ctx:
                    game_workers.prepare_game_tribe_batch(
                        make_tribe_request(raw_bulk=raw_bulk)
                    )
                self.assertIn(expected, str(ctx.exception))

    def test_native_invalid_pair_rolls_back_the_whole_batch_before_writes(self):
        with self.assertRaises(game_workers.GameTribeBatchValidationError):
            game_workers.set_game_tribes(
                make_tribe_request(raw_bulk='Alpha Elyrion Unknown Bardur')
            )
        self.assertEqual(self.state['assignments'], [(10, 1), (11, 2), (12, None)])
        self.assertEqual(self.state['logs'], [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)

    def test_native_multi_lineup_commit_writes_one_audit_row_per_change(self):
        result = game_workers.set_game_tribes(
            make_tribe_request(raw_bulk='Alpha Elyrion Alphonse none')
        )
        self.assertTrue(result.native)
        self.assertEqual(
            [(change.player_name, change.tribe_name) for change in result.changes],
            [('Alpha', 'Elyrion'), ('Alphonse', None)],
        )
        self.assertEqual(self.state['assignments'], [(10, 4), (11, None), (12, None)])
        self.assertEqual(len(self.state['logs']), 2)
        self.assertTrue(all(log['game_id'] == 42 for log in self.state['logs']))
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)

    def test_native_audit_failure_rolls_back_all_lineups_and_audits(self):
        with mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('tribe audit failure'),
        ):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'tribe audit failure',
            ):
                game_workers.set_game_tribes(
                    make_tribe_request(raw_bulk='Alpha Elyrion Beta Xin-Xi')
                )
        self.assertEqual(self.state['assignments'], [(10, 1), (11, 2), (12, None)])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)

    def test_legacy_partial_success_preserves_input_order_and_valid_subset(self):
        result = game_workers.set_game_tribes(
            make_tribe_request(
                requester_level=5,
                legacy_tokens=(
                    'Alpha', 'Elyrion',
                    'Unknown', 'Bardur',
                    'Alphonse', 'none',
                ),
                native=False,
                legacy_partial=True,
            )
        )
        self.assertEqual(
            [(outcome.player_token, outcome.valid) for outcome in result.outcomes],
            [('Alpha', True), ('Unknown', False), ('Alphonse', True)],
        )
        self.assertEqual(self.state['assignments'], [(10, 4), (11, None), (12, None)])
        self.assertEqual(len(self.state['logs']), 2)
        self.assertFalse(result.native)
        self.assertIn(
            'Player **Alpha** assigned to tribe *Elyrion*',
            game_tribe.legacy_pair_message(
                result.outcomes[0],
                game_id=42,
                permission_suffix='',
            ),
        )
        self.assertIn(
            'Matching player not found in game 42',
            game_tribe.legacy_pair_message(
                result.outcomes[1],
                game_id=42,
                permission_suffix='',
            ),
        )

    def test_legacy_ambiguous_values_are_reported_without_guessing(self):
        result = game_workers.set_game_tribes(
            make_tribe_request(
                legacy_tokens=('Al', 'Bardur'),
                native=False,
                legacy_partial=True,
            )
        )
        self.assertFalse(result.outcomes[0].valid)
        self.assertEqual(result.outcomes[0].error_detail, 'ambiguous')
        message = game_tribe.legacy_pair_message(
            result.outcomes[0],
            game_id=42,
            permission_suffix='',
        )
        self.assertIn('is ambiguous', message)
        self.assertIn('Alpha', message)
        self.assertIn('Alphonse', message)

    def test_self_permission_and_expected_snapshot_are_worker_authoritative(self):
        own = game_workers.GameTribeAssignmentInput('Alpha', 'Elyrion')
        game_workers.set_game_tribes(
            make_tribe_request(
                requester_id=100,
                requester_level=3,
                requester_is_staff=False,
                assignments=(own,),
            )
        )
        with self.assertRaises(game_workers.GameTribePermissionError):
            game_workers.set_game_tribes(
                make_tribe_request(
                    requester_id=100,
                    requester_level=3,
                    requester_is_staff=False,
                    assignments=(
                        game_workers.GameTribeAssignmentInput(
                            'Alphonse', 'Elyrion'
                        ),
                    ),
                )
            )
        with self.assertRaises(game_workers.GameTribeConflictError):
            game_workers.set_game_tribes(
                make_tribe_request(
                    assignments=(own,),
                    expected_snapshots=(
                        game_workers.GameTribeExpectedSnapshot(
                            lineup_id=10,
                            player_id=1,
                            tribe_name='Bardur',
                        ),
                    ),
                    check_expected_snapshots=True,
                )
            )

    def test_read_returns_canonical_choices_and_owns_connection(self):
        result = game_workers.read_game_tribes(
            game_workers.GameTribeReadRequest(
                game_id=42,
                guild_id=300,
                channel_id=900,
                requester_id=100,
            )
        )
        self.assertEqual(result.players[0].player_name, 'Alpha')
        self.assertEqual(result.players[0].tribe_emoji, '🦌')
        self.assertIn(('Xin-Xi', '⛰️'), result.tribe_choices)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_cross_guild_related_channel_is_only_allowed_for_legacy_target(self):
        self.game.guild_id = 301
        with mock.patch.object(
            game_workers.models.Game,
            'by_channel_or_arg',
            return_value=self.game,
        ):
            target = game_workers.prepare_legacy_game_tribe(
                make_tribe_request(
                    game_id=None,
                    guild_id=300,
                    legacy_tokens=('42', 'Alpha', 'Elyrion'),
                    native=False,
                    legacy_partial=True,
                    allow_related_channel=True,
                )
            )
        self.assertEqual(target.game_id, 42)
        self.assertTrue(target.inferred_from_channel)


class GameTribeExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = make_tribe_request(raw_bulk='Alpha Elyrion')

    async def test_worker_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow_worker(_request):
            started.set()
            release.wait(timeout=2)
            return game_workers.GameTribeMutationResult(
                game_id=42,
                guild_id=300,
                changes=(),
            )

        with mock.patch.object(
            game_workers,
            'set_game_tribes',
            side_effect=slow_worker,
        ):
            task = asyncio.create_task(
                game_workers.run_game_tribe_mutation(self.request)
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            release.set()
            result = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(result.game_id, 42)

    async def test_repeated_cancellation_drains_worker_before_releasing_claim(self):
        started = threading.Event()
        release = threading.Event()
        active = set()
        events = []

        def lock(game_id):
            if game_id in active:
                raise exceptions.RecordLocked('already locked')
            active.add(game_id)
            events.append(('lock', game_id))

        def unlock(game_id):
            self.assertIn(game_id, active)
            active.remove(game_id)
            events.append(('unlock', game_id))

        def slow_worker(_request):
            started.set()
            release.wait(timeout=2)
            events.append('worker-finished')
            return game_workers.GameTribeMutationResult(
                game_id=42,
                guild_id=300,
                changes=(),
            )

        with mock.patch.object(game_tribe.utilities, 'lock_game', side_effect=lock), \
                mock.patch.object(game_tribe.utilities, 'unlock_game', side_effect=unlock), \
                mock.patch.object(game_workers, 'set_game_tribes', side_effect=slow_worker):
            task = asyncio.create_task(
                game_tribe.run_tribe_mutation(self.request)
            )
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertNotIn(('unlock', 42), events)
                with self.assertRaises(exceptions.RecordLocked):
                    await game_tribe.run_tribe_mutation(self.request)
                release.set()
                await asyncio.sleep(0.05)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    self.fail('canceled tribe task completed successfully')
            finally:
                release.set()
                if not task.done():
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.assertEqual(events[-2:], ['worker-finished', ('unlock', 42)])


class GameTribeComponentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.results = []
        self.snapshot = game_workers.GameTribeReadResult(
            game_id=42,
            guild_id=300,
            players=(
                game_workers.GameTribePlayerSnapshot(
                    lineup_id=10,
                    player_id=1,
                    discord_id=100,
                    player_name='Alpha',
                    tribe_id=1,
                    tribe_name='Bardur',
                    tribe_emoji='🦌',
                ),
                game_workers.GameTribePlayerSnapshot(
                    lineup_id=11,
                    player_id=2,
                    discord_id=200,
                    player_name='Alphonse',
                    tribe_id=None,
                    tribe_name=None,
                    tribe_emoji='',
                ),
            ),
            expected_snapshots=(
                game_workers.GameTribeExpectedSnapshot(10, 1, 'Bardur'),
                game_workers.GameTribeExpectedSnapshot(11, 2, None),
            ),
            tribe_choices=(('Bardur', '🦌'), ('Elyrion', '🦄')),
            announcement_channel_id=901,
            announcement_message_id=902,
        )

    def make_view(self):
        async def on_self(interaction, token, snapshot):
            self.results.append(('self', token))
            return game_workers.GameTribeMutationResult(
                game_id=42,
                guild_id=300,
                changes=(
                    game_workers.GameTribeChange(
                        lineup_id=10,
                        player_id=1,
                        discord_id=100,
                        player_name='Alpha',
                        old_tribe_name='Bardur',
                        old_tribe_emoji='🦌',
                        tribe_id=4,
                        tribe_name='Elyrion',
                        tribe_emoji='🦄',
                    ),
                ),
            )

        async def on_single(interaction, player, token, snapshot):
            self.results.append(('single', player, token))
            return None

        async def on_bulk_preview(interaction, raw, snapshot):
            self.results.append(('preview', raw))
            return game_workers.GameTribeBatchPreview(
                game_id=42,
                guild_id=300,
                assignments=(
                    game_workers.GameTribeAssignmentInput(
                        'Alpha', 'Elyrion', lineup_id=10, player_id=1,
                        discord_id=100, tribe_id=4, tribe_name='Elyrion',
                    ),
                ),
                resolved_assignments=(
                    game_workers.GameTribeResolvedAssignment(
                        lineup_id=10,
                        player_id=1,
                        discord_id=100,
                        player_name='Alpha',
                        tribe_id=4,
                        tribe_name='Elyrion',
                        tribe_emoji='🦄',
                        old_tribe_id=1,
                        old_tribe_name='Bardur',
                        old_tribe_emoji='🦌',
                    ),
                ),
                expected_snapshots=self.snapshot.expected_snapshots,
                announcement_channel_id=901,
                announcement_message_id=902,
            )

        async def on_bulk_confirm(interaction, preview):
            self.results.append(('confirm', preview.game_id))
            return None

        view = game_tribe_views.GameTribeWorkspaceView(
            self.snapshot,
            requester_id=100,
            on_self=on_self,
            on_single=on_single,
            on_bulk_preview=on_bulk_preview,
            on_bulk_confirm=on_bulk_confirm,
            requester_actor=game_tribe.capture_actor(member()),
            timeout=60,
        )
        view.message = FakeMessage()
        return view

    async def test_workspace_has_three_paths_and_authorizes_requester(self):
        view = self.make_view()
        self.assertEqual(
            [child.label for child in view.children],
            ['Set my tribe', 'Edit player', 'Bulk edit'],
        )
        self.assertFalse(
            await view.interaction_check(
                FakeInteraction(user_id=999, events=self.events)
            )
        )
        self.assertTrue(self.events[-1][2])

    async def test_self_selector_applies_result_and_updates_public_workspace(self):
        view = self.make_view()
        await view._self_clicked(FakeInteraction(events=self.events))
        selector = next(
            event[3]['view']
            for event in self.events
            if event[0] == 'followup'
        )
        selector.select._values = ['Elyrion']
        await selector._select_clicked(FakeInteraction(events=self.events))
        self.assertEqual(self.results, [('self', 'Elyrion')])
        self.assertEqual(view.snapshot.players[0].tribe_name, 'Elyrion')
        self.assertTrue(any(edit.get('view') is view for edit in view.message.edits))

    async def test_single_modal_and_bulk_preview_confirmation_are_typed_paths(self):
        view = self.make_view()
        await view._single_clicked(FakeInteraction(events=self.events))
        modal = next(event[1] for event in self.events if event[0] == 'modal')
        modal.player._value = 'Alpha'
        modal.tribe._value = 'Elyrion'
        await modal.on_submit(FakeInteraction(events=self.events))
        self.assertEqual(self.results, [('single', 'Alpha', 'Elyrion')])

        view = self.make_view()
        await view._bulk_clicked(FakeInteraction(events=self.events))
        bulk_modal = next(
            event[1]
            for event in self.events
            if event[0] == 'modal'
            and isinstance(event[1], game_tribe_views.GameTribeBulkModal)
        )
        bulk_modal.assignments._value = 'Alpha Elyrion\nBeta none'
        await bulk_modal.on_submit(FakeInteraction(events=self.events))
        preview_view = view._previews[-1]
        await preview_view._confirm_clicked(FakeInteraction(events=self.events))
        self.assertIn(('preview', 'Alpha Elyrion\nBeta none'), self.results)
        self.assertIn(('confirm', 42), self.results)

    async def test_cancel_timeout_and_stale_controls_provide_rerun_guidance(self):
        view = self.make_view()
        await view._bulk_clicked(FakeInteraction(events=self.events))
        modal = next(event[1] for event in self.events if event[0] == 'modal')
        modal.assignments._value = 'Alpha Elyrion'
        await modal.on_submit(FakeInteraction(events=self.events))
        preview_view = view._previews[-1]
        await preview_view._cancel_clicked(FakeInteraction(events=self.events))
        self.assertFalse(view._busy)
        await view.on_timeout()
        self.assertTrue(view.self_button.disabled)
        self.assertTrue(view.single_button.disabled)
        self.assertTrue(view.bulk_button.disabled)
        self.assertIn('Run `/game tribe 42` again', view.message.edits[-1]['content'])


class GameTribePresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_public_and_reconciliation_failures_are_observable(self):
        events = []
        result = game_workers.GameTribeMutationResult(
            game_id=42,
            guild_id=300,
            changes=(
                game_workers.GameTribeChange(
                    lineup_id=10,
                    player_id=1,
                    discord_id=100,
                    player_name='Alpha',
                    old_tribe_name='Bardur',
                    old_tribe_emoji='🦌',
                    tribe_id=4,
                    tribe_name='Elyrion',
                    tribe_emoji='🦄',
                ),
            ),
        )
        async def send(content, **kwargs):
            events.append(('send', content))

        async def load_card(**kwargs):
            return SimpleNamespace()

        async def refresh(*args, **kwargs):
            raise RuntimeError('announcement failure')

        async def send_card(*args, **kwargs):
            events.append(('card',))
            raise RuntimeError('card failure')

        await game_tribe.publish_mutation_result(
            result,
            send=send,
            destination=SimpleNamespace(),
            guild=SimpleNamespace(id=300),
            bot=SimpleNamespace(),
            prefix='$',
            requester_id=100,
            channel_id=900,
            actor=game_tribe.capture_actor(member()),
            load_card=load_card,
            refresh_announcement=refresh,
            send_card=send_card,
        )
        self.assertEqual(events[0][0], 'send')
        self.assertIn('updated tribes for game 42', events[0][1])
        self.assertIn('card', [item[0] for item in events])
        self.assertTrue(any('announcement refresh failed' in item[1] for item in events[1:]))
        self.assertTrue(any(
            len(item) > 1 and 'dense game-card refresh failed' in item[1]
            for item in events[1:]
        ))


class GameTribeNativeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('tribe')

    def snapshot(self):
        return game_workers.GameTribeReadResult(
            game_id=42,
            guild_id=300,
            players=(),
            expected_snapshots=(),
            tribe_choices=(('Elyrion', '🦄'),),
            announcement_channel_id=901,
            announcement_message_id=902,
        )

    async def test_read_defer_is_replaced_by_public_actor_workspace(self):
        events = []
        interaction = FakeInteraction(events=events)
        with mock.patch.object(
            games.game_tribe,
            'run_tribe_read',
            new=mock.AsyncMock(return_value=self.snapshot()),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            workspace = await self.command().callback(
                games.polygames.__new__(games.polygames),
                interaction,
                42,
            )
        self.assertEqual(events[0], ('defer', True))
        self.assertEqual(events[1], ('delete-original',))
        self.assertEqual(events[2][0], 'channel')
        self.assertIn('Requested by <@100>', events[2][1])
        self.assertIs(events[2][2]['view'], workspace)

    async def test_native_database_failure_stays_private_without_public_effect(self):
        events = []
        interaction = FakeInteraction(events=events)
        with mock.patch.object(
            games.game_tribe,
            'build_mutation_request',
            return_value=make_tribe_request(
                raw_bulk='Alpha Elyrion',
                require_elevated=True,
            ),
        ), mock.patch.object(
            games.game_tribe,
            'run_tribe_mutation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database down')
            ),
        ), mock.patch.object(
            games.game_tribe,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await self.command().callback(
                games.polygames.__new__(games.polygames),
                interaction,
                42,
                'Alpha Elyrion',
            )
        self.assertEqual(events[0], ('defer', True))
        self.assertTrue(any(event[0] == 'followup' and event[2] for event in events))
        self.assertFalse(any(event[0] == 'channel' for event in events))

    async def test_direct_bulk_defers_once_and_does_not_open_second_interaction(self):
        events = []
        interaction = FakeInteraction(events=events)
        committed = game_workers.GameTribeMutationResult(
            game_id=42,
            guild_id=300,
            changes=(),
        )
        captured = []

        async def run(request, *, after_commit):
            captured.append(request)
            await after_commit(committed)
            return committed

        with mock.patch.object(
            games.game_tribe,
            'build_mutation_request',
            return_value=make_tribe_request(
                raw_bulk='Alpha Elyrion',
                require_elevated=True,
            ),
        ), mock.patch.object(
            games.game_tribe,
            'run_tribe_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_tribe,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await self.command().callback(
                games.polygames.__new__(games.polygames),
                interaction,
                42,
                'Alpha Elyrion',
            )

        self.assertEqual(events[0], ('defer', True))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].raw_bulk, 'Alpha Elyrion')
        self.assertTrue(captured[0].require_elevated)
        self.assertFalse(any(event[0] == 'modal' for event in events))
        self.assertFalse(any(event[0] == 'response' for event in events))


class GameTribePrefixAdapterTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        return next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'settribe'
        )

    def context(self, *, args=None, send=None):
        return SimpleNamespace(
            author=member(),
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=900),
            prefix='$',
            invoked_with='settribe',
            send=send or mock.AsyncMock(),
        )

    async def test_no_arguments_message_and_prefix_alias_are_preserved(self):
        ctx = self.context()
        await self.command().callback(
            games.polygames.__new__(games.polygames),
            ctx,
        )
        ctx.send.assert_awaited_once_with(
            'No arguments provided. **Example usage:** `$settribe 1234 bardur`'
        )
        self.assertEqual(self.command().aliases, ['settribes'])

    async def test_prefix_passes_raw_pairs_to_worker_and_formats_worker_count_error(self):
        ctx = self.context()
        captured = []

        async def run(request, *, after_commit):
            captured.append(request)
            raise game_workers.GameTribeValidationError('Wrong number of arguments.')

        with mock.patch.object(
            games.game_tribe,
            'run_tribe_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_tribe,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            games.game_tribe,
            '_requester_is_staff',
            return_value=False,
        ):
            await self.command().callback(
                games.polygames.__new__(games.polygames),
                ctx,
                args='42 Alpha Elyrion',
            )
        self.assertEqual(captured[0].legacy_tokens, ('42', 'Alpha', 'Elyrion'))
        ctx.send.assert_awaited_once_with(
            'Wrong number of arguments. See `$help settribe` for usage examples.'
        )


if __name__ == '__main__':
    unittest.main()
