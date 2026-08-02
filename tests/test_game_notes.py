"""Focused offline coverage for the P4.2b game-notes workspace."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
game_notes = import_offline_runtime('modules.game_notes')
game_notes_views = import_offline_runtime('modules.game_notes_views')
games = import_offline_runtime('modules.games')
matchmaking = import_offline_runtime('modules.matchmaking')


def make_member(*, member_id=100, level=3, staff=False):
    return SimpleNamespace(
        id=member_id,
        name='Player',
        display_name='Player',
        guild=SimpleNamespace(id=300),
        roles=(),
        notes_level=level,
        notes_staff=staff,
    )


def make_snapshot(*, notes=None, pending=True, completed=False):
    return game_workers.GameNotesReadResult(
        game_id=42,
        guild_id=300,
        notes=notes,
        is_pending=pending,
        is_completed=completed,
        host_discord_id=100,
    )


def notes_request(
    *,
    game_id=42,
    guild_id=300,
    channel_id=900,
    requester_id=100,
    requester_level=3,
    requester_is_staff=False,
    notes='New notes',
    clear=False,
    expected_notes=None,
    check_expected_notes=False,
    truncate=False,
    legacy_none=False,
    mention_warning=False,
    allow_related_channel=False,
    legacy_tokens=(),
):
    return game_workers.GameNotesMutationRequest(
        game_id=game_id,
        guild_id=guild_id,
        channel_id=channel_id,
        requester_id=requester_id,
        requester_level=requester_level,
        requester_is_staff=requester_is_staff,
        requester_description='**Player** (`100`)',
        notes=notes,
        clear=clear,
        expected_notes=expected_notes,
        check_expected_notes=check_expected_notes,
        legacy_tokens=tuple(legacy_tokens),
        allow_related_channel=allow_related_channel,
        invoked_with='gamenotes',
        prefix='$',
        truncate=truncate,
        legacy_none=legacy_none,
        mention_warning=mention_warning,
    )


class FakeGame:
    def __init__(
        self,
        state,
        *,
        game_id=42,
        guild_id=300,
        notes=None,
        pending=True,
        completed=False,
        host_id=100,
    ):
        self.id = game_id
        self.guild_id = guild_id
        self.notes = notes
        self.is_pending = pending
        self.is_completed = completed
        self.is_confirmed = completed
        self.announcement_channel = None
        self.announcement_message = None
        self.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=host_id),
        ) if host_id is not None else None
        self._state = state

    def is_hosted_by(self, requester_id):
        host_id = (
            self.host.discord_member.discord_id
            if self.host is not None
            else None
        )
        return requester_id == host_id, self.host

    def uses_channel_id(self, channel_id):
        return channel_id == 900

    def save(self):
        self._state['notes'] = self.notes


class Response:
    def __init__(self, events=None):
        self.done = False
        self.events = events if events is not None else []

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


class Followup:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    async def send(self, content, *, ephemeral=False, wait=False, **kwargs):
        self.events.append(('followup', content, ephemeral, kwargs))
        if wait:
            return Message()
        return None


class Channel:
    def __init__(self, events=None):
        self.id = 900
        self.events = events if events is not None else []

    async def send(self, content, **kwargs):
        self.events.append(('channel', content, kwargs))
        return Message()


class StrictChannel:
    def __init__(self, events=None):
        self.id = 900
        self.events = events if events is not None else []

    async def send(self, content, *, view):
        self.events.append(('strict-channel', content, view))
        return Message()


class Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class Interaction:
    def __init__(self, *, user_id=100, events=None):
        events = events if events is not None else []
        self.user = make_member(member_id=user_id)
        self.guild = SimpleNamespace(id=300)
        self.channel_id = 900
        self.channel = Channel(events)
        self.response = Response(events)
        self.followup = Followup(events)
        self.events = events
        self.deleted_original = 0

    async def delete_original_response(self):
        self.deleted_original += 1
        self.events.append(('delete-original',))


class GameNotesWorkerTests(unittest.TestCase):
    def setUp(self):
        self.state = {'notes': 'Existing', 'logs': []}
        self.database = FakeDatabase(self.state)
        self.game = FakeGame(self.state, notes=self.state['notes'])
        self.patches = [
            mock.patch.object(game_workers.models, 'db', self.database),
            mock.patch.object(
                game_workers.models.Game,
                'get_by_id',
                return_value=self.game,
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
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_requests_are_frozen_and_primitive(self):
        request = notes_request()
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 99
        self.assertIsInstance(request.requester_description, str)
        self.assertIsInstance(request.legacy_tokens, tuple)
        self.assertNotIn('member', request.__dataclass_fields__)

    def test_read_returns_unset_value_and_owns_connection(self):
        self.game.notes = None
        result = game_workers.read_game_notes(
            game_workers.GameNotesReadRequest(
                game_id=42,
                guild_id=300,
                channel_id=900,
                requester_id=100,
            )
        )
        self.assertIsNone(result.notes)
        self.assertEqual(game_notes.read_message(result), 'Current notes for game 42: None')
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_edit_and_audit_commit_atomically(self):
        result = game_workers.set_game_notes(notes_request(notes='Updated'))
        self.assertEqual(result.old_notes, 'Existing')
        self.assertEqual(result.notes, 'Updated')
        self.assertEqual(self.state['notes'], 'Updated')
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertIn('edited game notes: Updated', self.state['logs'][0]['message'])

    def test_explicit_clear_and_none_alias(self):
        result = game_workers.set_game_notes(
            notes_request(notes=None, clear=True)
        )
        self.assertIsNone(result.notes)
        self.assertTrue(result.cleared)
        self.assertIsNone(self.state['notes'])

        self.game.notes = 'Again'
        result = game_workers.set_game_notes(
            notes_request(notes='none', legacy_none=True)
        )
        self.assertIsNone(result.notes)
        self.assertTrue(result.cleared)

    def test_native_boundary_rejects_151_and_prefix_truncates(self):
        with self.assertRaisesRegex(
            game_workers.GameNotesValidationError,
            '150 characters or fewer',
        ):
            game_workers.set_game_notes(
                notes_request(notes='x' * 151)
            )
        result = game_workers.set_game_notes(
            notes_request(notes='x' * 151, truncate=True)
        )
        self.assertEqual(len(result.notes), 150)

    def test_permission_completion_pending_and_registration_are_rechecked(self):
        with mock.patch.object(
            game_workers.models.DiscordMember,
            'get_or_none',
            return_value=None,
        ):
            with self.assertRaisesRegex(
                game_workers.GameNotesPermissionError,
                'registration',
            ):
                game_workers.set_game_notes(notes_request())

        self.game.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=200),
        )
        with self.assertRaisesRegex(
            game_workers.GameNotesPermissionError,
            'Only the game host',
        ):
            game_workers.set_game_notes(notes_request())

        self.game.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=200),
        )
        result = game_workers.set_game_notes(
            notes_request(requester_level=5, requester_is_staff=True)
        )
        self.assertEqual(result.notes, 'New notes')

        self.game.is_completed = True
        with self.assertRaisesRegex(
            game_workers.GameNotesValidationError,
            'completed',
        ):
            game_workers.set_game_notes(
                notes_request(requester_level=5, requester_is_staff=True)
            )

        self.game.is_completed = False
        self.game.is_pending = False
        self.game.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=100),
        )
        with self.assertRaisesRegex(
            game_workers.GameNotesPermissionError,
            'in-progress',
        ):
            game_workers.set_game_notes(notes_request())

    def test_guild_and_host_are_reloaded_from_game_not_requester_objects(self):
        self.game.guild_id = 301
        with self.assertRaisesRegex(
            game_workers.GameNotesValidationError,
            'different discord server',
        ):
            game_workers.set_game_notes(notes_request(guild_id=300))

        self.game.guild_id = 300
        self.game.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=999),
        )
        with self.assertRaisesRegex(
            game_workers.GameNotesPermissionError,
            'Only the game host',
        ):
            game_workers.set_game_notes(notes_request(requester_id=100))

    def test_stale_snapshot_conflicts_before_save(self):
        with self.assertRaisesRegex(
            game_workers.GameNotesConflictError,
            'changed after',
        ):
            game_workers.set_game_notes(
                notes_request(
                    expected_notes='Old value',
                    check_expected_notes=True,
                )
            )
        self.assertEqual(self.state['notes'], 'Existing')
        self.assertEqual(self.database.commits, 0)

    def test_audit_failure_rolls_back_notes(self):
        self.patches[-1].stop()
        failing_log = mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('audit failure'),
        )
        failing_log.start()
        self.addCleanup(failing_log.stop)
        with self.assertRaisesRegex(peewee.OperationalError, 'audit failure'):
            game_workers.set_game_notes(notes_request(notes='Should roll back'))
        self.assertEqual(self.state['notes'], 'Existing')
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_slow_worker_does_not_block_event_loop(self):
        started = threading.Event()
        original = game_workers.set_game_notes

        def slow(request):
            started.set()
            time.sleep(0.08)
            return game_workers.GameNotesMutationResult(
                game_id=42,
                guild_id=300,
                old_notes='Existing',
                notes=request.notes,
            )

        async def run():
            with mock.patch.object(game_workers, 'set_game_notes', slow):
                heartbeat = asyncio.create_task(asyncio.sleep(0.01))
                task = asyncio.create_task(
                    game_workers.run_game_notes_mutation(notes_request())
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(heartbeat, timeout=0.04)
                self.assertFalse(task.done())
                # Restricted headless runners may need a timer wake-up before
                # delivering the executor completion callback.
                await asyncio.sleep(0.10)
                await task

        asyncio.run(run())
        self.assertIs(game_workers.set_game_notes, original)

    def test_repeated_cancellation_drains_before_claim_release(self):
        started = threading.Event()
        finish = threading.Event()
        events = []
        active_games = set()

        def slow(request):
            started.set()
            finish.wait(1)
            events.append('worker-finished')
            return game_workers.GameNotesMutationResult(
                game_id=42,
                guild_id=300,
                old_notes='Existing',
                notes=request.notes,
            )

        async def run():
            with mock.patch.object(game_workers, 'set_game_notes', slow), \
                    mock.patch.object(
                        game_notes.utilities,
                        'lock_game',
                        side_effect=lambda game_id: (
                            (_ for _ in ()).throw(
                                game_workers.exceptions.RecordLocked('locked')
                            )
                            if game_id in active_games
                            else (active_games.add(game_id), events.append('lock'))
                        ),
                    ), mock.patch.object(
                        game_notes.utilities,
                        'unlock_game',
                        side_effect=lambda game_id: (
                            active_games.remove(game_id),
                            events.append('unlock'),
                        ),
                    ):
                task = asyncio.create_task(
                    game_notes.run_notes_mutation(notes_request())
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertNotIn('unlock', events)
                with self.assertRaises(game_workers.exceptions.RecordLocked):
                    await game_notes.run_notes_mutation(notes_request())
                finish.set()
                await asyncio.sleep(0.05)
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(events[-2:], ['worker-finished', 'unlock'])

        asyncio.run(run())


class GameNotesServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actor_snapshot_has_mention_and_safe_textual_fallback(self):
        member = make_member()
        member.display_name = '@everyone `unsafe`'

        actor = game_notes.capture_actor(member)

        self.assertEqual(actor.mention, '<@100>')
        self.assertIn('100', actor.identity)
        self.assertNotEqual(
            actor.identity,
            '**@everyone `unsafe`** (`100`)',
        )

    async def test_public_sender_clears_private_defer_without_ephemeral_followup(self):
        events = []
        interaction = Interaction(events=events)
        await interaction.response.defer(ephemeral=True)
        sender = game_notes.public_interaction_sender(interaction)
        await sender('Current notes for game 42: None')
        await sender('A second public message')
        self.assertEqual(
            events,
            [
                ('defer', True),
                ('delete-original',),
                ('channel', 'Current notes for game 42: None', {}),
                ('channel', 'A second public message', {}),
            ],
        )
        self.assertEqual(interaction.deleted_original, 1)

    async def test_post_commit_order_and_mention_warning(self):
        events = []
        result = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Old',
            notes='New <@123>',
            mention_warning=True,
        )

        async def send(content):
            events.append(('send', content))

        async def refresh(value):
            events.append(('refresh', value.game_id))

        await game_notes.publish_mutation_result(
            result,
            send=send,
            refresh_card=refresh,
        )
        self.assertEqual(
            [event[0] for event in events],
            ['send', 'refresh', 'send'],
        )
        self.assertIn('Updated notes for game 42 to: New <@123>', events[0][1])
        self.assertIn('Warning', events[2][1])

    async def test_post_commit_refresh_failure_is_observable(self):
        sent = []

        async def send(content):
            sent.append(content)

        async def refresh(_result):
            raise RuntimeError('refresh failure')

        await game_notes.publish_mutation_result(
            game_workers.GameNotesMutationResult(
                game_id=42,
                guild_id=300,
                old_notes=None,
                notes='New',
            ),
            send=send,
            refresh_card=refresh,
        )
        self.assertEqual(len(sent), 2)
        self.assertIn('Updated notes', sent[0])
        self.assertIn('refresh failed', sent[1])


class GameNotesComponentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.snapshot = make_snapshot(notes='Prefilled note')
        self.events = []
        self.edits = []
        self.results = []

    def workspace(self, *, edit_result=None, clear_result=None):
        async def on_edit(interaction, notes, snapshot):
            self.results.append(('edit', notes, snapshot.notes))
            return edit_result

        async def on_clear(interaction, snapshot):
            self.results.append(('clear', None, snapshot.notes))
            return clear_result

        view = game_notes_views.GameNotesWorkspaceView(
            self.snapshot,
            requester_id=100,
            on_edit=on_edit,
            on_clear=on_clear,
            timeout=60,
        )
        view.message = Message()
        return view

    async def test_workspace_has_small_requester_controls_and_modal_prefill(self):
        view = self.workspace()
        self.assertEqual(len(view.children), 2)
        self.assertEqual(view.edit_button.label, 'Edit notes')
        self.assertEqual(view.clear_button.label, 'Clear notes')

        interaction = Interaction(events=self.events)
        await view._edit_clicked(interaction)
        modal = next(event[1] for event in self.events if event[0] == 'modal')
        self.assertEqual(modal.notes.default, 'Prefilled note')
        self.assertEqual(modal.notes.max_length, 150)
        self.assertEqual(modal.notes.style, discord.TextStyle.paragraph)
        self.assertEqual(len(modal.children), 1)

    async def test_modal_accepts_exact_boundary_and_only_submits_once(self):
        result = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Prefilled note',
            notes='x' * 150,
        )
        view = self.workspace(edit_result=result)
        modal = game_notes_views.GameNotesEditModal(view, self.snapshot)
        modal.notes._value = 'x' * 150
        interaction = Interaction(events=self.events)
        await modal.on_submit(interaction)
        await modal.on_submit(Interaction(events=self.events))
        self.assertEqual(self.results, [('edit', 'x' * 150, 'Prefilled note')])
        self.assertEqual(view.current_notes, 'x' * 150)
        self.assertEqual(view.message.edits[-1]['content'], 'Current notes for game 42: ' + 'x' * 150)

    async def test_modal_rejects_long_value_visibly(self):
        view = self.workspace()
        modal = game_notes_views.GameNotesEditModal(view, self.snapshot)
        modal.notes._value = 'x' * 151
        interaction = Interaction(events=self.events)
        await modal.on_submit(interaction)
        self.assertIn('150 characters or fewer', interaction.followup.events[-1][1])
        self.assertEqual(self.results, [])

    async def test_clear_requires_confirmation_and_cancel_has_no_mutation(self):
        view = self.workspace()
        interaction = Interaction(events=self.events)
        await view._clear_clicked(interaction)
        confirmation = view._confirmations[-1]
        self.assertEqual(len(confirmation.children), 2)
        self.assertEqual(self.results, [])
        await confirmation._cancel_clicked(Interaction(events=self.events))
        self.assertEqual(self.results, [])
        self.assertIn(
            'Notes clear cancelled.',
            [event[1] for event in self.events if event[0] == 'followup'],
        )

    async def test_confirm_clear_uses_loaded_snapshot_and_updates_public_state(self):
        result = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Prefilled note',
            notes=None,
        )
        view = self.workspace(clear_result=result)
        await view._clear_clicked(Interaction(events=self.events))
        confirmation = view._confirmations[-1]
        await confirmation._confirm_clicked(Interaction(events=self.events))
        self.assertEqual(self.results, [('clear', None, 'Prefilled note')])
        self.assertIsNone(view.current_notes)

    async def test_requester_only_timeout_and_rerun_hint(self):
        view = self.workspace()
        other = Interaction(user_id=999, events=self.events)
        self.assertFalse(await view.interaction_check(other))
        self.assertIn('Only the member', self.events[-1][1])
        await view.on_timeout()
        self.assertTrue(view.edit_button.disabled)
        self.assertTrue(view.clear_button.disabled)
        self.assertIn('Run `/game notes 42` again', view.message.edits[-1]['content'])


class GameNotesRegistrationTests(unittest.TestCase):
    def test_native_registration_and_prefix_aliases(self):
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        notes_command = game_group.get_command('notes')
        self.assertEqual(notes_command.name, 'notes')
        self.assertEqual(
            [parameter.name for parameter in notes_command.parameters],
            ['game_id'],
        )

        prefix_command = next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'gamenotes'
        )
        self.assertEqual(
            set(prefix_command.aliases),
            {'notes', 'matchnotes'},
        )


class NativeGameNotesAdapterTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('notes')

    async def test_read_defers_private_then_publishes_current_value(self):
        events = []
        interaction = Interaction(events=events)
        interaction.channel = StrictChannel(events)
        snapshot = make_snapshot(notes=None)
        cog = games.polygames.__new__(games.polygames)

        with mock.patch.object(
            games.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(return_value=snapshot),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            workspace = await self.command().callback(cog, interaction, 42)

        self.assertEqual(events[0], ('defer', True))
        self.assertEqual(events[1], ('delete-original',))
        self.assertEqual(events[2][0], 'strict-channel')
        self.assertEqual(
            events[2][1],
            'Current notes for game 42: None\n'
            'Requested by <@100> / **Player** (`100`).',
        )
        self.assertIs(events[2][2], workspace)
        self.assertEqual(len(workspace.children), 2)
        self.assertFalse(any(event[0] == 'followup' for event in events))

    async def test_read_failure_stays_ephemeral_and_does_not_publish(self):
        events = []
        interaction = Interaction(events=events)
        cog = games.polygames.__new__(games.polygames)

        with mock.patch.object(
            games.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(
                side_effect=game_workers.GameNotesPermissionError(
                    'Only registered members can use this command.'
                )
            ),
        ):
            await self.command().callback(cog, interaction, 42)

        self.assertEqual(events[0], ('defer', True))
        self.assertFalse(any(event[0] == 'delete-original' for event in events))
        self.assertFalse(any(event[0] == 'channel' for event in events))
        self.assertEqual(events[1][0], 'followup')
        self.assertTrue(events[1][2])

    async def test_native_edit_passes_immutable_expected_snapshot_and_publishes_after_commit(self):
        events = []
        initial = make_snapshot(notes='Before')
        committed = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Before',
            notes='After',
        )
        initial_interaction = Interaction(events=events)
        mutation_interaction = Interaction(events=events)
        captured = []
        refresh = mock.AsyncMock()

        async def run_mutation(request, *, after_commit):
            captured.append(request)
            events.append(('commit',))
            await after_commit(committed)
            return committed

        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(return_value=initial),
        ), mock.patch.object(
            games.game_notes,
            'run_notes_mutation',
            side_effect=run_mutation,
        ), mock.patch.object(
            games.game_notes,
            'refresh_game_card',
            new=refresh,
        ), mock.patch.object(
            games.game_notes,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            games.game_notes,
            '_requester_is_staff',
            return_value=False,
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            workspace = await self.command().callback(
                cog,
                initial_interaction,
                42,
            )
            await mutation_interaction.response.defer(ephemeral=True)
            result = await workspace.on_edit(
                mutation_interaction,
                'After',
                initial,
            )

        self.assertIs(result, committed)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].game_id, 42)
        self.assertEqual(captured[0].expected_notes, 'Before')
        self.assertTrue(captured[0].check_expected_notes)
        self.assertEqual(captured[0].notes, 'After')
        self.assertEqual(
            [event[0] for event in events if event[0] in ('commit', 'channel')],
            ['channel', 'commit', 'channel'],
        )
        refresh.assert_awaited_once()
        self.assertEqual(
            [event[1] for event in events if event[0] == 'channel'],
            [
                'Current notes for game 42: Before\n'
                'Requested by <@100> / **Player** (`100`).',
                '<@100> / **Player** (`100`) edited notes for game 42 to: '
                'After',
            ],
        )

    async def test_native_clear_public_message_identifies_actor_and_action(self):
        events = []
        initial = make_snapshot(notes='Before')
        committed = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Before',
            notes=None,
            cleared=True,
        )
        initial_interaction = Interaction(events=events)
        clear_interaction = Interaction(events=events)
        captured = []

        async def run_mutation(request, *, after_commit):
            captured.append(request)
            events.append(('commit',))
            await after_commit(committed)
            return committed

        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(return_value=initial),
        ), mock.patch.object(
            games.game_notes,
            'run_notes_mutation',
            side_effect=run_mutation,
        ), mock.patch.object(
            games.game_notes,
            'refresh_game_card',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            workspace = await self.command().callback(
                cog,
                initial_interaction,
                42,
            )
            await clear_interaction.response.defer(ephemeral=True)
            result = await workspace.on_clear(clear_interaction, initial)

        self.assertIs(result, committed)
        self.assertTrue(captured[0].clear)
        public_messages = [
            event[1] for event in events if event[0] == 'channel'
        ]
        self.assertEqual(
            public_messages,
            [
                'Current notes for game 42: Before\n'
                'Requested by <@100> / **Player** (`100`).',
                '<@100> / **Player** (`100`) cleared notes for game 42.',
            ],
        )
        self.assertNotIn('Updated notes', public_messages[1])

    async def test_native_mutation_failure_is_private_and_has_no_post_effect(self):
        events = []
        initial = make_snapshot(notes='Before')
        interaction = Interaction(events=events)
        cog = games.polygames.__new__(games.polygames)

        with mock.patch.object(
            games.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(return_value=initial),
        ), mock.patch.object(
            games.game_notes,
            'run_notes_mutation',
            new=mock.AsyncMock(
                side_effect=game_workers.GameNotesConflictError('stale')
            ),
        ), mock.patch.object(
            games.game_notes,
            'refresh_game_card',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            workspace = await self.command().callback(cog, interaction, 42)
            await interaction.response.defer(ephemeral=True)
            result = await workspace.on_edit(interaction, 'After', initial)

        self.assertIsNone(result)
        self.assertFalse(any(event[0] == 'channel' for event in events[3:]))
        self.assertTrue(any(event[0] == 'followup' for event in events))


class PrefixGameNotesAdapterTests(unittest.IsolatedAsyncioTestCase):
    def context(self, raw_args, *, mentions=(), role_mentions=()):
        return SimpleNamespace(
            author=make_member(),
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=900),
            prefix='$',
            invoked_with='gamenotes',
            message=SimpleNamespace(
                mentions=list(mentions),
                role_mentions=list(role_mentions),
            ),
            send=mock.AsyncMock(),
            raw_args=raw_args,
        )

    def command(self):
        return next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'gamenotes'
        )

    async def run_command(self, raw_args, *, result=None, **kwargs):
        ctx = self.context(raw_args, **kwargs)
        result = result or game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Old',
            notes='New',
        )
        captured = []

        async def run_mutation(request, *, after_commit):
            captured.append(request)
            await after_commit(result)
            return result

        with mock.patch.object(
            matchmaking.game_notes,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.game_notes,
            '_requester_is_staff',
            return_value=False,
        ), mock.patch.object(
            matchmaking.game_notes,
            'run_notes_mutation',
            side_effect=run_mutation,
        ), mock.patch.object(
            matchmaking.game_notes,
            'refresh_game_card',
            new=mock.AsyncMock(),
        ):
            await self.command().callback(
                matchmaking.matchmaking.__new__(matchmaking.matchmaking),
                ctx,
                args=raw_args,
            )
        return ctx, captured

    async def test_explicit_id_none_and_mention_warning_use_shared_service(self):
        result = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes='Old',
            notes=None,
            mention_warning=True,
        )
        ctx, captured = await self.run_command(
            '42 none',
            result=result,
            mentions=(SimpleNamespace(id=123),),
        )
        request = captured[0]
        self.assertEqual(request.game_id, 42)
        self.assertTrue(request.clear)
        self.assertIsNone(request.notes)
        self.assertTrue(request.truncate)
        self.assertTrue(request.legacy_none)
        self.assertTrue(request.mention_warning)
        self.assertEqual(
            [call.args[0] for call in ctx.send.await_args_list],
            [
                'Updated notes for game 42 to: None',
                '**Warning**: Updated notes included role/user mentions. This '
                'will not impact who is allowed to join the game and will only '
                'change the content of the notes.',
            ],
        )

    async def test_prefix_truncates_and_infers_game_from_channel(self):
        target = game_workers.GameNotesTarget(game_id=42)
        result = game_workers.GameNotesMutationResult(
            game_id=42,
            guild_id=300,
            old_notes=None,
            notes='Long note',
        )
        with mock.patch.object(
            matchmaking.game_workers,
            'run_prepare_legacy_game_notes',
            new=mock.AsyncMock(return_value=target),
        ) as prepare:
            ctx, captured = await self.run_command(
                'Inferred note from a game channel',
                result=result,
            )
        prepare.assert_awaited_once()
        request = captured[0]
        self.assertEqual(request.game_id, 42)
        self.assertTrue(request.allow_related_channel)
        self.assertEqual(request.notes, 'Inferred note from a game channel')
        self.assertTrue(request.truncate)
        self.assertEqual(ctx.send.await_args_list[0].args[0], 'Updated notes for game 42 to: Long note')

    async def test_prefix_usage_read_never_clears_and_preserves_output(self):
        ctx = self.context('42')
        with mock.patch.object(
            matchmaking.game_notes,
            'run_notes_read',
            new=mock.AsyncMock(return_value=make_snapshot(notes='Current')),
        ), mock.patch.object(
            matchmaking.game_notes,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.game_notes,
            '_requester_is_staff',
            return_value=False,
        ):
            await self.command().callback(
                matchmaking.matchmaking.__new__(matchmaking.matchmaking),
                ctx,
                args='42',
            )
        ctx.send.assert_awaited_once_with(
            'Include new note or *none* to delete existing note. Usage: '
            '`$gamenotes 42 These are my new notes`'
        )

    async def test_prefix_database_failure_has_no_post_commit_refresh(self):
        ctx = self.context('42 New')
        refresh = mock.AsyncMock()
        with mock.patch.object(
            matchmaking.game_notes,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.game_notes,
            '_requester_is_staff',
            return_value=False,
        ), mock.patch.object(
            matchmaking.game_notes,
            'run_notes_mutation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database down')
            ),
        ), mock.patch.object(
            matchmaking.game_notes,
            'refresh_game_card',
            new=refresh,
        ):
            await self.command().callback(
                matchmaking.matchmaking.__new__(matchmaking.matchmaking),
                ctx,
                args='42 New',
            )
        refresh.assert_not_awaited()
        self.assertIn('rolled back', ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
