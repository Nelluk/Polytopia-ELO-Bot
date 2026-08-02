"""Focused offline coverage for the P4.2c game-name workspace."""

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
game_name = import_offline_runtime('modules.game_name')
game_name_views = import_offline_runtime('modules.game_name_views')
games = import_offline_runtime('modules.games')


def make_member(*, member_id=100, level=3, staff=False):
    return SimpleNamespace(
        id=member_id,
        name='Player',
        display_name='Player',
        mention=f'<@{member_id}>',
        guild=SimpleNamespace(id=300),
        roles=(),
        name_level=level,
        name_staff=staff,
    )


def name_request(
    *,
    game_id=42,
    guild_id=300,
    channel_id=900,
    requester_id=100,
    requester_level=3,
    requester_is_staff=False,
    name='Warriors of Dawn',
    clear=False,
    expected_name=None,
    check_expected_name=False,
    allow_related_channel=False,
    legacy_tokens=(),
):
    return game_workers.GameNameMutationRequest(
        game_id=game_id,
        guild_id=guild_id,
        channel_id=channel_id,
        requester_id=requester_id,
        requester_level=requester_level,
        requester_is_staff=requester_is_staff,
        requester_description='**Player** (`100`)',
        name=name,
        clear=clear,
        expected_name=expected_name,
        check_expected_name=check_expected_name,
        legacy_tokens=tuple(legacy_tokens),
        allow_related_channel=allow_related_channel,
        invoked_with='rename',
        prefix='$',
    )


class FakeGame:
    def __init__(
        self,
        state,
        *,
        game_id=42,
        guild_id=300,
        name='Existing War',
        pending=False,
        completed=False,
        host_id=100,
        creator_id=100,
        league_changed=False,
    ):
        object.__setattr__(self, '_state', state)
        self.id = game_id
        self.guild_id = guild_id
        self.name = name
        self.is_pending = pending
        self.is_completed = completed
        self.is_confirmed = completed
        self.host = SimpleNamespace(
            discord_member=SimpleNamespace(discord_id=host_id),
        ) if host_id is not None else None
        self._creator = SimpleNamespace(
            name='Creator',
            discord_member=SimpleNamespace(discord_id=creator_id),
        ) if creator_id is not None else None
        self.announcement_channel = 901
        self.announcement_message = 902
        self.league_season = 4
        self.league_tier = 2
        self.league_playoff = False
        self._league_changed = league_changed

    def __setattr__(self, key, value):
        if key == 'name' and value:
            value = (
                str(value)
                .strip('"')
                .strip("'")
                .strip('”')
                .strip('“')
                .title()[:35]
                .strip()
            )
        object.__setattr__(self, key, value)

    def is_hosted_by(self, requester_id):
        host_id = (
            self.host.discord_member.discord_id
            if self.host is not None
            else None
        )
        return requester_id == host_id, self.host

    def is_created_by(self, *, discord_id):
        return bool(
            self._creator
            and self._creator.discord_member.discord_id == discord_id
        )

    def creating_player(self):
        return self._creator

    def uses_channel_id(self, channel_id):
        return channel_id == 900

    def save(self):
        self._state['name'] = self.name

    def update_league_fields(self):
        self._state['league_updated'] = self._league_changed
        return self._league_changed


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


class StrictChannel(Channel):
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


def make_snapshot(*, name='Existing War', pending=False):
    return game_workers.GameNameReadResult(
        game_id=42,
        guild_id=300,
        name=name,
        is_pending=pending,
        is_completed=False,
        announcement_channel_id=901,
        announcement_message_id=902,
    )


class GameNameWorkerTests(unittest.TestCase):
    def setUp(self):
        self.state = {'name': 'Existing War', 'logs': []}
        self.database = FakeDatabase(self.state)
        self.game = FakeGame(self.state, name=self.state['name'])
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
        request = name_request()
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 99
        self.assertIsInstance(request.requester_description, str)
        self.assertIsInstance(request.legacy_tokens, tuple)

    def test_read_returns_unset_value_and_owns_connection(self):
        self.game.name = None
        result = game_workers.read_game_name(
            game_workers.GameNameReadRequest(
                game_id=42,
                guild_id=300,
                channel_id=900,
                requester_id=100,
            )
        )
        self.assertIsNone(result.name)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertIn('**None**', game_name.read_message(result))

    def test_edit_commits_model_normalization_league_fields_and_audit(self):
        result = game_workers.set_game_name(
            name_request(name='"warriors of dawn"'),
        )
        self.assertEqual(result.name, 'Warriors Of Dawn')
        self.assertTrue(result.normalized)
        self.assertEqual(self.state['name'], 'Warriors Of Dawn')
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.state['logs'][0]['game_id'], 42)
        self.assertIn('renamed the game', self.state['logs'][0]['message'])

    def test_length_boundary_is_visible_and_prefix_overlength_truncates(self):
        exact = 'war ' + ('x' * 31)
        result = game_workers.set_game_name(name_request(name=exact))
        self.assertEqual(len(result.name), 35)
        self.assertFalse(result.truncated)

        long_value = 'war ' + ('x' * 32)
        result = game_workers.set_game_name(name_request(name=long_value))
        self.assertEqual(len(result.name), 35)
        self.assertTrue(result.truncated)
        self.assertIn('35 characters', game_name.native_mutation_message(
            result,
            actor=game_name.capture_actor(make_member()),
        ))

    def test_validation_override_warning_and_clear_conflict(self):
        with self.assertRaisesRegex(
            game_workers.GameNameValidationError,
            'That name looks made up',
        ):
            game_workers.set_game_name(
                name_request(name='Completely Imaginary', requester_level=2),
            )

        result = game_workers.set_game_name(
            name_request(name='Completely Imaginary', requester_level=3),
        )
        self.assertIn('allowed to override', result.name_warning)

        with self.assertRaisesRegex(
            game_workers.GameNameValidationError,
            'either a new game name or Clear name',
        ):
            game_workers.set_game_name(
                name_request(name='Warriors War', clear=True, requester_level=4),
            )

    def test_clear_requires_elevated_level_and_commits_empty_value(self):
        with self.assertRaisesRegex(
            game_workers.GameNamePermissionError,
            'permissions to delete',
        ):
            game_workers.set_game_name(name_request(clear=True, requester_level=3))
        result = game_workers.set_game_name(
            name_request(name=None, clear=True, requester_level=4),
        )
        self.assertTrue(result.cleared)
        self.assertIsNone(result.name)
        self.assertIsNone(self.state['name'])

    def test_pending_permission_stale_and_registration_are_rechecked(self):
        self.game.is_pending = True
        with self.assertRaisesRegex(
            game_workers.GameNameValidationError,
            'not started',
        ):
            game_workers.set_game_name(name_request())
        self.game.is_pending = False
        with self.assertRaises(game_workers.GameNameConflictError):
            game_workers.set_game_name(
                name_request(
                    expected_name='Stale Name',
                    check_expected_name=True,
                )
            )
        with mock.patch.object(
            game_workers.models.DiscordMember,
            'get_or_none',
            return_value=None,
        ), self.assertRaises(game_workers.GameNamePermissionError):
            game_workers.set_game_name(name_request())

    def test_audit_failure_rolls_back_name_and_league_update(self):
        self.game._league_changed = True
        with mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('name log failure'),
        ):
            with self.assertRaisesRegex(peewee.OperationalError, 'name log failure'):
                game_workers.set_game_name(name_request(name='Warriors War'))
        self.assertEqual(self.state['name'], 'Existing War')
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_same_guild_and_related_channel_rules_are_worker_authoritative(self):
        self.game.guild_id = 301
        self.game.uses_channel_id = lambda channel_id: channel_id == 900
        with self.assertRaises(game_workers.GameNameValidationError):
            game_workers.set_game_name(
                name_request(guild_id=300, allow_related_channel=False),
            )
        result = game_workers.set_game_name(
            name_request(guild_id=300, allow_related_channel=True),
        )
        self.assertEqual(result.game_id, 42)

    def test_slow_worker_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            return game_workers.GameNameMutationResult(
                game_id=42,
                guild_id=300,
                old_name='Existing War',
                name='Warriors War',
                requested_name='Warriors War',
            )

        async def run():
            with mock.patch.object(game_workers, 'set_game_name', side_effect=slow):
                task = asyncio.create_task(
                    game_workers.run_game_name_mutation(name_request())
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                release.set()
                # Give restricted headless runners a timer wake-up so the
                # executor completion callback can be delivered.
                await asyncio.sleep(0.05)
                return await task

        result = asyncio.run(run())
        self.assertEqual(result.name, 'Warriors War')


class GameNameComponentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.results = []

    def workspace(self, *, edit_result=None, clear_result=None, pending=False):
        async def on_edit(interaction, name, snapshot):
            self.results.append(('edit', name, snapshot.name))
            return edit_result

        async def on_clear(interaction, snapshot):
            self.results.append(('clear', None, snapshot.name))
            return clear_result

        view = game_name_views.GameNameWorkspaceView(
            make_snapshot(name='Prefilled War', pending=pending),
            requester_id=100,
            on_edit=on_edit,
            on_clear=on_clear,
            requester_actor=game_name.capture_actor(make_member()),
            timeout=60,
        )
        view.message = Message()
        return view

    async def test_workspace_controls_and_modal_prefill_use_actual_boundary(self):
        view = self.workspace()
        self.assertEqual(view.edit_button.label, 'Edit name')
        self.assertEqual(view.clear_button.label, 'Clear name')
        self.assertEqual(len(view.children), 2)
        modal = game_name_views.GameNameEditModal(view, view.snapshot)
        self.assertEqual(modal.name.default, 'Prefilled War')
        self.assertEqual(modal.name.max_length, 35)

    async def test_modal_accepts_exact_boundary_and_only_commits_after_callback(self):
        result = game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Prefilled War',
            name='War ' + ('x' * 31),
            requested_name='War ' + ('x' * 31),
        )
        view = self.workspace(edit_result=result)
        modal = game_name_views.GameNameEditModal(view, view.snapshot)
        modal.name._value = 'War ' + ('x' * 31)
        interaction = Interaction(events=self.events)
        await modal.on_submit(interaction)
        self.assertEqual(self.results[0][0], 'edit')
        self.assertEqual(len(self.results[0][1]), 35)
        self.assertEqual(view.current_name, result.name)
        self.assertEqual(view.message.edits[-1]['content'], game_name.workspace_message(
            42,
            result.name,
            actor=view.requester_actor,
        ))

    async def test_modal_rejects_overlength_value_privately(self):
        view = self.workspace()
        modal = game_name_views.GameNameEditModal(view, view.snapshot)
        modal.name._value = 'x' * 36
        interaction = Interaction(events=self.events)
        await modal.on_submit(interaction)
        self.assertFalse(self.results)
        self.assertEqual(self.events[0], ('defer', True))
        self.assertTrue(self.events[1][2])
        self.assertIn('35 characters', self.events[1][1])

    async def test_clear_requires_confirmation_and_cancel_has_no_mutation(self):
        view = self.workspace()
        interaction = Interaction(events=self.events)
        await view._clear_clicked(interaction)
        confirmation = view._confirmations[-1]
        await confirmation._cancel_clicked(Interaction(events=self.events))
        self.assertEqual(self.results, [])
        self.assertIn(
            'Game-name clear cancelled.',
            [event[1] for event in self.events if event[0] == 'followup'],
        )

    async def test_requester_only_pending_denial_and_timeout_rerun_hint(self):
        view = self.workspace()
        other = Interaction(user_id=999, events=self.events)
        self.assertFalse(await view.interaction_check(other))
        self.assertIn('Only the member', self.events[-1][1])
        pending = self.workspace(pending=True)
        self.assertFalse(await pending.interaction_check(Interaction(events=self.events)))
        await view.on_timeout()
        self.assertTrue(view.edit_button.disabled)
        self.assertTrue(view.clear_button.disabled)
        self.assertIn('Run `/game name 42` again', view.message.edits[-1]['content'])


class GameNameServiceTests(unittest.IsolatedAsyncioTestCase):
    def result(self, *, announcement=True):
        return game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Old War',
            name='New War',
            requested_name='new war',
            normalized=True,
            name_warning=':warning: name override',
            league_warning='\n:warning: league changed',
            announcement_channel_id=901 if announcement else None,
            announcement_message_id=902 if announcement else None,
        )

    async def test_claim_releases_before_post_commit_effects(self):
        events = []
        request = name_request()

        async def worker(_request):
            events.append('worker')
            return self.result(announcement=False)

        async def after(_result):
            events.append('after')

        with mock.patch.object(
            game_name.game_workers.utilities,
            'lock_game',
            side_effect=lambda game_id: events.append(('lock', game_id)),
        ), mock.patch.object(
            game_name.game_workers.utilities,
            'unlock_game',
            side_effect=lambda game_id: events.append(('unlock', game_id)),
        ), mock.patch.object(
            game_name.game_workers,
            'run_game_name_mutation',
            side_effect=worker,
        ):
            await game_name.run_name_mutation(request, after_commit=after)
        self.assertEqual(
            events,
            [('lock', 42), 'worker', ('unlock', 42), 'after'],
        )

    async def test_publish_orders_success_after_commit_and_observes_refresh_failure(self):
        events = []
        result = self.result()
        committed_game = SimpleNamespace(
            embed=lambda **kwargs: ('embed', 'content'),
            update_squad_channels=mock.AsyncMock(
                side_effect=lambda *args: events.append('squad')
            ),
            update_announcement=mock.AsyncMock(
                side_effect=lambda **kwargs: events.append('announcement')
            ),
        )

        async def send(content, **kwargs):
            events.append(('send', content))

        async def send_embed(*args, **kwargs):
            events.append('card')
            raise RuntimeError('card failure')

        await game_name.publish_mutation_result(
            result,
            send=send,
            destination=SimpleNamespace(),
            guild=SimpleNamespace(id=300),
            guild_list=(SimpleNamespace(id=300),),
            prefix='$',
            actor=game_name.capture_actor(make_member()),
            load_game=lambda **kwargs: committed_game,
            send_game_embed=send_embed,
        )
        self.assertEqual(events[0][0], 'send')
        self.assertIn('renamed game 42', events[0][1])
        self.assertIn('squad', events)
        self.assertIn('announcement', events)
        self.assertIn('card', events)
        self.assertTrue(any('dense game-card refresh failed' in item[1] for item in events if isinstance(item, tuple)))

    async def test_repeated_cancellation_keeps_same_game_claim_until_worker_drains(self):
        started = threading.Event()
        release = threading.Event()
        active_games = set()
        events = []

        def lock(game_id):
            if game_id in active_games:
                raise exceptions.RecordLocked('already locked')
            active_games.add(game_id)
            events.append(('lock', game_id))

        def unlock(game_id):
            self.assertIn(game_id, active_games)
            active_games.remove(game_id)
            events.append(('unlock', game_id))

        def slow_worker(_request):
            started.set()
            release.wait(timeout=2)
            events.append('worker-finished')
            return self.result(announcement=False)

        with mock.patch.object(game_name.game_workers.utilities, 'lock_game', side_effect=lock), \
                mock.patch.object(game_name.game_workers.utilities, 'unlock_game', side_effect=unlock), \
                mock.patch.object(game_name.game_workers, 'set_game_name', side_effect=slow_worker):
            task = asyncio.create_task(game_name.run_name_mutation(name_request()))
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
                    await game_name.run_name_mutation(name_request())
                release.set()
                await asyncio.sleep(0.05)
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        self.assertEqual(events[-2:], ['worker-finished', ('unlock', 42)])


class GameNameRegistrationTests(unittest.TestCase):
    def test_native_registration_and_prefix_registration_are_preserved(self):
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        name_command = game_group.get_command('name')
        self.assertEqual(name_command.name, 'name')
        self.assertEqual(
            [parameter.name for parameter in name_command.parameters],
            ['game_id'],
        )
        prefix_command = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'rename'
        )
        self.assertEqual(prefix_command.aliases, [])
        self.assertFalse(hasattr(prefix_command, 'commands'))


class NativeGameNameAdapterTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('name')

    async def test_read_defers_private_then_publishes_public_actor_workspace(self):
        events = []
        interaction = Interaction(events=events)
        interaction.channel = StrictChannel(events)
        snapshot = make_snapshot(name=None)
        cog = games.polygames.__new__(games.polygames)

        with mock.patch.object(
            games.game_name,
            'run_name_read',
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
        self.assertIn('Current tracked Polytopia game name for game 42: **None**', events[2][1])
        self.assertIn('Requested by <@100> / **Player** (`100`).', events[2][1])
        self.assertIs(events[2][2], workspace)
        self.assertEqual(len(workspace.children), 2)
        self.assertFalse(any(event[0] == 'followup' for event in events))

    async def test_read_failure_stays_private_without_public_effect(self):
        events = []
        interaction = Interaction(events=events)
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_name,
            'run_name_read',
            new=mock.AsyncMock(
                side_effect=game_workers.GameNamePermissionError('denied')
            ),
        ):
            await self.command().callback(cog, interaction, 42)
        self.assertEqual(events[0], ('defer', True))
        self.assertFalse(any(event[0] == 'delete-original' for event in events))
        self.assertFalse(any(event[0] == 'channel' for event in events))
        self.assertTrue(events[1][2])

    async def test_edit_uses_frozen_expected_name_and_publishes_after_commit(self):
        events = []
        initial = make_snapshot(name='Before War')
        committed = game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Before War',
            name='After War',
            requested_name='after war',
        )
        initial_interaction = Interaction(events=events)
        mutation_interaction = Interaction(events=events)
        captured = []

        async def run_mutation(request, *, after_commit):
            captured.append(request)
            events.append(('commit',))
            await after_commit(committed)
            return committed

        async def publish(result, *, send, actor, **kwargs):
            events.append(('publish', result.game_id))
            await send(game_name.native_mutation_message(result, actor=actor))

        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_name,
            'run_name_read',
            new=mock.AsyncMock(return_value=initial),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            side_effect=run_mutation,
        ), mock.patch.object(
            games.game_name,
            'publish_mutation_result',
            side_effect=publish,
        ), mock.patch.object(
            games.game_name,
            '_requester_level',
            return_value=3,
        ), mock.patch.object(
            games.game_name,
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
                'after war',
                initial,
            )

        self.assertIs(result, committed)
        self.assertEqual(captured[0].game_id, 42)
        self.assertEqual(captured[0].expected_name, 'Before War')
        self.assertTrue(captured[0].check_expected_name)
        self.assertEqual(captured[0].name, 'after war')
        self.assertEqual(
            [event[0] for event in events if event[0] in ('channel', 'commit', 'publish')],
            ['channel', 'commit', 'publish', 'channel'],
        )
        self.assertIn('renamed game 42 to', [
            event[1] for event in events if event[0] == 'channel'
        ][1])

    async def test_native_stale_or_database_failure_is_private_and_has_no_post_effect(self):
        events = []
        initial = make_snapshot(name='Before War')
        interaction = Interaction(events=events)
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_name,
            'run_name_read',
            new=mock.AsyncMock(return_value=initial),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            new=mock.AsyncMock(
                side_effect=game_workers.GameNameConflictError('stale')
            ),
        ), mock.patch.object(
            games.game_name,
            'refresh_game_card',
            new=mock.AsyncMock(),
        ):
            workspace = await self.command().callback(cog, interaction, 42)
            await interaction.response.defer(ephemeral=True)
            result = await workspace.on_edit(interaction, 'After War', initial)
        self.assertIsNone(result)
        self.assertFalse(any(event[0] == 'channel' for event in events[3:]))
        self.assertTrue(any(event[0] == 'followup' and event[2] for event in events))


class PrefixGameNameAdapterTests(unittest.IsolatedAsyncioTestCase):
    def command(self):
        return next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'rename'
        )

    def context(self, *, send=None, command=None):
        author = make_member()
        return SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=900),
            message=SimpleNamespace(channel=SimpleNamespace(id=900)),
            prefix='$',
            invoked_with='rename',
            command=command or SimpleNamespace(reset_cooldown=mock.Mock()),
            send=send or mock.AsyncMock(),
        )

    async def test_explicit_id_grammar_and_public_success_wording(self):
        target = game_workers.GameNameTarget(
            game_id=42,
            inferred_from_channel=False,
            explicit_game_id=42,
        )
        result = game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Old War',
            name='New War',
            requested_name='New War',
        )
        request_seen = []

        async def run(request, *, after_commit):
            request_seen.append(request)
            await after_commit(result)
            return result

        ctx = self.context()
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_guild=lambda guild_id: ctx.guild,
            guilds=(ctx.guild,),
        )
        with mock.patch.object(
            games.game_workers,
            'run_prepare_legacy_game_name',
            new=mock.AsyncMock(return_value=target),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_name,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            games.settings,
            'is_bot_channel_strict',
            new=mock.AsyncMock(return_value=True),
        ):
            await self.command().callback(cog, ctx, '42', 'New', 'War')

        self.assertEqual(request_seen[0].game_id, 42)
        self.assertEqual(request_seen[0].name, 'New War')
        self.assertFalse(request_seen[0].clear)
        self.assertEqual(request_seen[0].invoked_with, 'rename')

    async def test_none_clear_and_success_formatter_keep_prefix_contract(self):
        target = game_workers.GameNameTarget(
            game_id=42,
            inferred_from_channel=False,
            explicit_game_id=42,
        )
        result = game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Old War',
            name=None,
            requested_name=None,
            cleared=True,
        )
        request_seen = []
        sent = []

        async def run(request, *, after_commit):
            request_seen.append(request)
            await after_commit(result)
            return result

        async def publish(committed, *, send, **kwargs):
            await send(game_name.mutation_message(committed))

        async def send(content):
            sent.append(content)

        ctx = self.context(send=send)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_guild=lambda guild_id: ctx.guild,
            guilds=(ctx.guild,),
        )
        with mock.patch.object(
            games.game_workers,
            'run_prepare_legacy_game_name',
            new=mock.AsyncMock(return_value=target),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_name,
            'publish_mutation_result',
            side_effect=publish,
        ), mock.patch.object(
            games.settings,
            'is_bot_channel_strict',
            new=mock.AsyncMock(return_value=True),
        ):
            await self.command().callback(cog, ctx, '42', 'None')

        self.assertTrue(request_seen[0].clear)
        self.assertIsNone(request_seen[0].name)
        self.assertEqual(
            sent,
            ['Game ID 42 has been renamed to "**None**" from "**Old War**"'],
        )

    async def test_explicit_id_retains_bot_channel_restriction(self):
        target = game_workers.GameNameTarget(
            game_id=42,
            inferred_from_channel=False,
            explicit_game_id=42,
        )
        mutation = mock.AsyncMock()
        ctx = self.context()
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_workers,
            'run_prepare_legacy_game_name',
            new=mock.AsyncMock(return_value=target),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            new=mutation,
        ), mock.patch.object(
            games.settings,
            'is_bot_channel_strict',
            new=mock.AsyncMock(return_value=False),
        ):
            await self.command().callback(cog, ctx, '42', 'New', 'War')

        mutation.assert_not_awaited()
        ctx.send.assert_awaited_once_with(
            'This command must be used in a bot spam channel or in a '
            'game-specific channel.'
        )

    async def test_channel_inference_preserves_name_grammar_and_related_channel_flag(self):
        target = game_workers.GameNameTarget(
            game_id=42,
            inferred_from_channel=True,
        )
        result = game_workers.GameNameMutationResult(
            game_id=42,
            guild_id=300,
            old_name='Old War',
            name='Inferred War',
            requested_name='Inferred War',
        )
        request_seen = []

        async def run(request, *, after_commit):
            request_seen.append(request)
            return result

        ctx = self.context()
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(get_guild=lambda guild_id: ctx.guild, guilds=(ctx.guild,))
        with mock.patch.object(
            games.game_workers,
            'run_prepare_legacy_game_name',
            new=mock.AsyncMock(return_value=target),
        ), mock.patch.object(
            games.game_name,
            'run_name_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_name,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ):
            await self.command().callback(cog, ctx, 'Inferred', 'War')
        self.assertEqual(request_seen[0].name, 'Inferred War')
        self.assertTrue(request_seen[0].allow_related_channel)
