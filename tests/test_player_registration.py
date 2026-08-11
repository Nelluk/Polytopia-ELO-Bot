"""Focused offline coverage for canonical player registration."""

import asyncio
import copy
from contextlib import AbstractContextManager, ExitStack
import dataclasses
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.player_registration_workers')
registration = import_offline_runtime('modules.player_registration')
views = import_offline_runtime('modules.player_registration_views')
games = import_offline_runtime('modules.games')


def member(
    discord_id=100,
    *,
    name='AccountUser',
    display_name='Account User',
    nick=None,
    roles=(),
):
    return SimpleNamespace(
        id=discord_id,
        name=name,
        display_name=display_name,
        nick=nick,
        roles=tuple(SimpleNamespace(name=role) for role in roles),
        guild=SimpleNamespace(id=478571892832206869),
    )


def request(*, target_id=100, actor_roles=(), name='Poly Name'):
    actor = workers.MemberSnapshot(
        discord_id=100,
        discord_name='Actor',
        discord_nick='Act',
        display_name='Actor Display',
        role_names=tuple(actor_roles),
    )
    target = workers.MemberSnapshot(
        discord_id=target_id,
        discord_name='Target',
        discord_nick=None,
        display_name='Target Display',
        role_names=('The Ronin',),
    )
    return workers.PlayerRegistrationRequest(
        guild_id=300,
        requester_id=100,
        actor=actor,
        target=target,
        canonical_name=name,
        requester_is_staff=bool(actor_roles),
        invoked_with='player register',
    )


def unknown_message_not_found():
    return discord.NotFound(
        SimpleNamespace(status=404, reason='Not Found'),
        {'message': 'Unknown Message', 'code': 10008},
    )


class TransactionDatabase:
    def __init__(self, state):
        self.state = state
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.savepoints = 0
        self.atomic_depth = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                return False

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.snapshot = copy.deepcopy(database.state)
                self.depth = database.atomic_depth
                database.atomic_depth += 1
                if self.depth:
                    database.savepoints += 1
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                database.atomic_depth -= 1
                if exc_type is None:
                    if self.depth == 0:
                        database.commits += 1
                else:
                    database.rollbacks += 1
                    database.state.clear()
                    database.state.update(self.snapshot)
                return False

        return Atomic()


class FakeMemberModel:
    class _Field:
        pass

    name = _Field()
    polytopia_name = _Field()

    def __init__(self, state, discord_id, name):
        self._state = state
        self.discord_id = discord_id
        self.name = name
        self.name_steam = 'Legacy Steam Value'
        self.polytopia_id = 'legacy-id'
        self.polytopia_name = None

    @classmethod
    def _state_ref(cls):
        return cls.state

    @classmethod
    def get_or_none(cls, *, discord_id):
        return cls.state['members'].get(discord_id) or cls.race_existing

    @classmethod
    def get_or_create(cls, **kwargs):
        defaults = kwargs.pop('defaults', {})
        if cls.race_conflict:
            cls.race_conflict = False
            cls.race_existing = cls(
                cls.state,
                kwargs['discord_id'],
                'Concurrent Discord Name',
            )
            raise peewee.IntegrityError('simulated DiscordMember conflict')
        existing = cls.get_or_none(**kwargs)
        if existing is not None:
            return existing, False
        return cls.create(**kwargs, **defaults), True

    @classmethod
    def create(cls, **kwargs):
        result = cls(cls.state, kwargs['discord_id'], kwargs['name'])
        cls.state['members'][result.discord_id] = result
        return result

    def save(self, only=None):
        del only
        self._state['members'][self.discord_id] = self


class FakeTeam:
    def __init__(self, name):
        self.name = name


class FakePlayerModel:
    class _Field:
        def __eq__(self, other):
            del other
            return self

        def __and__(self, other):
            del other
            return self

    discord_member = _Field()
    guild_id = _Field()

    def __init__(self, state, **kwargs):
        self._state = state
        self.__dict__.update(kwargs)
        self.id = len(state['players']) + 1
        self.team = kwargs.get('team')

    @classmethod
    def generate_display_name(cls, *, player_name, player_nick):
        return f'{player_name} ({player_nick})' if player_nick else player_name

    @classmethod
    def get_or_none(cls, query=None, **kwargs):
        # The production query is represented by the fake's current member
        # and guild fields in the patched test path.
        del query, kwargs
        return cls.existing or cls.race_existing

    @classmethod
    def get_or_create(cls, **kwargs):
        defaults = kwargs.pop('defaults', {})
        if cls.race_conflict:
            cls.race_conflict = False
            conflict_values = {**kwargs, **defaults}
            conflict_values['name'] = 'Concurrent Discord Label'
            cls.race_existing = cls(
                cls.state,
                **conflict_values,
            )
            raise peewee.IntegrityError('simulated Player conflict')
        existing = cls.get_or_none(**kwargs)
        if existing is not None:
            return existing, False
        result = cls(cls.state, **kwargs, **defaults)
        cls.state['players'].append(result)
        cls.existing = result
        return result, True

    @classmethod
    def create(cls, **kwargs):
        result = cls(cls.state, **kwargs)
        cls.state['players'].append(result)
        cls.existing = result
        return result

    def save(self):
        if self not in self._state['players']:
            self._state['players'].append(self)


class FakeGameLog:
    @classmethod
    def write(cls, **kwargs):
        cls.state['logs'].append(kwargs)
        if cls.fail:
            raise peewee.OperationalError('simulated audit failure')


class PlayerRegistrationValidationTests(unittest.TestCase):
    def test_name_validation_trims_bounds_unicode_and_rejects_placeholders(self):
        self.assertEqual(
            workers.validate_canonical_name('  Πολύτα  '),
            'Πολύτα',
        )
        self.assertEqual(
            len(workers.validate_canonical_name('x' * 250)),
            workers.MAX_NAME_LENGTH,
        )
        for value in ('', '   ', 'none', 'Your Mobile Name', 'name\nvalue', 'a\tb'):
            with self.subTest(value=value):
                with self.assertRaises(workers.PlayerRegistrationValidationError):
                    workers.validate_canonical_name(value)

    def test_snapshot_and_request_contain_only_primitive_values(self):
        discord_member = member(
            name='raw-name',
            display_name='Shown Name',
            nick='Nick',
            roles=('The Ronin', 'Helper'),
        )
        with mock.patch.object(registration.settings, 'is_staff', return_value=False):
            request_value = registration.build_request(
                actor=discord_member,
                target=discord_member,
                guild_id=300,
                canonical_name='Canonical',
            )
        self.assertTrue(dataclasses.is_dataclass(request_value))
        self.assertIsInstance(request_value, workers.PlayerRegistrationRequest)
        self.assertEqual(request_value.target.role_names, ('Helper', 'The Ronin'))
        self.assertEqual(request_value.target.discord_nick, 'Nick')
        self.assertNotIn(discord_member, dataclasses.asdict(request_value).values())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request_value.canonical_name = 'changed'

    def test_public_name_escapes_mentions_and_preserves_account_wide_wording(self):
        self.assertNotIn('<@123>', registration.safe_public_name('Name <@123>'))
        value = request(name='Name <@123>')
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            canonical_name='Name <@123>',
            player_created=True,
            member_created=True,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        message = registration.success_message(value, result)
        self.assertIn('account-wide', message)
        self.assertIn('across all Discord servers', message)


class PlayerRegistrationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.state = {'members': {}, 'players': [], 'logs': []}
        self.database = TransactionDatabase(self.state)
        FakeMemberModel.state = self.state
        FakeMemberModel.race_conflict = False
        FakeMemberModel.race_existing = None
        FakePlayerModel.state = self.state
        FakePlayerModel.existing = None
        FakePlayerModel.race_conflict = False
        FakePlayerModel.race_existing = None
        FakeGameLog.state = self.state
        FakeGameLog.fail = False

    def patches(self, *, matching_team=(), duplicate_count=0):
        stack = ExitStack()
        stack.enter_context(mock.patch.multiple(
            workers.models,
            db=self.database,
            DiscordMember=FakeMemberModel,
            Player=FakePlayerModel,
            GameLog=FakeGameLog,
        ))
        self.matching_team_mock = stack.enter_context(mock.patch.object(
            workers,
            '_matching_team',
            return_value=matching_team,
        ))
        stack.enter_context(mock.patch.object(
            workers,
            '_duplicate_count',
            return_value=duplicate_count,
        ))
        stack.enter_context(mock.patch.object(
            workers.settings,
            'guild_setting',
            side_effect=lambda guild_id, key: {
                'helper_roles': ['Helper'],
                'mod_roles': ['Mod'],
            }[key],
        ))
        return stack

    def test_worker_owns_connection_single_atomic_and_preserves_legacy_fields(self):
        team = FakeTeam('The Ronin')
        with self.patches(matching_team=(team,), duplicate_count=1):
            result = workers.register_player(request(actor_roles=('Helper',), target_id=200))

        self.assertEqual(result.guild_id, 300)
        self.assertEqual(result.target_id, 200)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.team_name, 'The Ronin')
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        saved = self.state['members'][200]
        self.assertEqual(saved.polytopia_name, 'Poly Name')
        self.assertEqual(saved.name_steam, 'Legacy Steam Value')
        self.assertEqual(saved.polytopia_id, 'legacy-id')
        self.assertEqual(self.state['logs'][0]['guild_id'], 300)
        self.assertIn('Actor Display', self.state['logs'][0]['message'])
        self.assertIn('duplicate warning', self.state['logs'][0]['message'])
        self.matching_team_mock.assert_called_once_with(300, ('The Ronin',))

    def test_audit_failure_rolls_back_registration_graph(self):
        FakeGameLog.fail = True
        with self.patches():
            with self.assertRaises(peewee.OperationalError):
                workers.register_player(request())
        self.assertEqual(self.state['members'], {})
        self.assertEqual(self.state['players'], [])
        self.assertEqual(self.state['logs'], [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_conflicting_member_and_player_inserts_reload_inside_outer_transaction(self):
        FakeMemberModel.race_conflict = True
        FakePlayerModel.race_conflict = True
        team = FakeTeam('The Ronin')

        with self.patches(matching_team=(team,)):
            result = workers.register_player(
                request(target_id=200, actor_roles=('Helper',)),
            )

        saved_member = self.state['members'][200]
        saved_player = self.state['players'][0]
        self.assertFalse(result.member_created)
        self.assertFalse(result.player_created)
        self.assertEqual(saved_member.name, 'Target')
        self.assertEqual(saved_member.polytopia_name, 'Poly Name')
        self.assertEqual(saved_member.name_steam, 'Legacy Steam Value')
        self.assertEqual(saved_member.polytopia_id, 'legacy-id')
        self.assertEqual(saved_player.name, 'Target')
        self.assertIs(saved_player.team, team)
        self.assertEqual(self.database.savepoints, 2)
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 2)
        self.assertEqual(len(self.state['logs']), 1)
        self.assertEqual(self.state['logs'][0]['guild_id'], 300)

    def test_worker_revalidates_exact_staff_parity_from_role_snapshot(self):
        with self.patches():
            with self.assertRaises(workers.PlayerRegistrationPermissionError):
                workers.register_player(request(target_id=200, actor_roles=('Member',)))
        self.assertEqual(self.database.connection_opened, 0)

    def test_canceled_coordinator_drains_worker_and_does_not_block_event_loop(self):
        original = workers.register_player
        started = threading.Event()
        release = threading.Event()

        def slow(request_value):
            del request_value
            started.set()
            release.wait(timeout=2)
            return workers.PlayerRegistrationResult(
                guild_id=300,
                requester_id=100,
                target_id=100,
                canonical_name='done',
                player_created=False,
                member_created=False,
                team_name=None,
                duplicate_count=0,
                warnings=(),
            )

        async def exercise():
            workers.register_player = slow
            try:
                heartbeat = asyncio.create_task(asyncio.sleep(0.01))
                task = asyncio.create_task(
                    workers.run_player_registration(request())
                )
                await asyncio.wait_for(heartbeat, timeout=0.04)
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(task.done())
            finally:
                release.set()
                workers.register_player = original

        asyncio.run(exercise())

    def test_worker_exception_propagates_and_executor_can_accept_next_job(self):
        original = workers.register_player
        calls = []

        def fail_once(request_value):
            del request_value
            calls.append('failed')
            raise peewee.OperationalError('worker failure')

        async def exercise():
            workers.register_player = fail_once
            try:
                with self.assertRaises(peewee.OperationalError):
                    await workers.run_player_registration(request())
            finally:
                workers.register_player = original

        asyncio.run(exercise())
        self.assertEqual(calls, ['failed'])


class PlayerRegistrationModalTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, *, user=None, target=None):
        user = user or member()
        target = target or user
        return SimpleNamespace(
            user=user,
            guild=SimpleNamespace(
                id=300,
                get_member=mock.Mock(return_value=target),
            ),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )

    def modal(self, target_id=100):
        return views.PlayerRegistrationModal(
            guild_id=300,
            requester_id=100,
            target_snapshot=registration.capture_member_snapshot(
                member(target_id, name='Target', display_name='Target')
            ),
        )

    async def test_modal_has_one_account_wide_field_and_public_success(self):
        modal = self.modal()
        modal.canonical_name._value = 'Name <@123>'
        interaction = self.interaction()
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            canonical_name='Name <@123>',
            player_created=True,
            member_created=True,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        with mock.patch.object(
            views.workers,
            'run_player_registration',
            new=mock.AsyncMock(return_value=result),
        ):
            await modal.on_submit(interaction)
        self.assertEqual(modal.canonical_name.max_length, 200)
        self.assertIn('account-wide', modal.canonical_name.label.lower())
        self.assertIn('**Selected member:** Target', modal.selected_target_text)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        public_message = interaction.channel.send.await_args.args[0]
        self.assertIn('account-wide', public_message)
        self.assertIn('across all Discord servers', public_message)
        self.assertNotIn('Name <@123>', public_message)

    async def test_selected_staff_target_survives_modal_submission_into_worker(self):
        actor = member()
        target = member(200, name='ChosenName', display_name='Chosen Display')
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'player'
        ).get_command('register')
        cog = games.polygames.__new__(games.polygames)
        opened = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_modal=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
        )
        with mock.patch.object(games.settings, 'is_staff', return_value=True):
            await command.callback(cog, opened, target)
        modal = opened.response.send_modal.await_args.args[0]
        modal.canonical_name._value = 'Chosen Canonical Name'
        submitted = self.interaction(user=actor, target=target)
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=200,
            canonical_name='Chosen Canonical Name',
            player_created=False,
            member_created=False,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        with (
            mock.patch.object(
                views.player_registration.settings,
                'is_staff',
                return_value=True,
            ),
            mock.patch.object(
                views.workers,
                'run_player_registration',
                new=mock.AsyncMock(return_value=result),
            ) as run,
        ):
            await modal.on_submit(submitted)

        request_value = run.await_args.args[0]
        self.assertEqual(request_value.target.discord_id, 200)
        self.assertEqual(request_value.target.display_name, 'Chosen Display')
        self.assertEqual(request_value.canonical_name, 'Chosen Canonical Name')
        self.assertIn('Chosen Display', modal.selected_target_text)

    async def test_unknown_message_cleanup_is_benign_and_publishes_once(self):
        modal = self.modal()
        modal.canonical_name._value = 'Valid Name'
        interaction = self.interaction()
        interaction.delete_original_response.side_effect = unknown_message_not_found()
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            canonical_name='Valid Name',
            player_created=True,
            member_created=True,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        with (
            mock.patch.object(
                views.workers,
                'run_player_registration',
                new=mock.AsyncMock(return_value=result),
            ),
            mock.patch.object(
                views.player_registration.interaction_lifecycle.logger,
                'exception',
            ) as logged,
        ):
            await modal.on_submit(interaction)

        logged.assert_not_called()
        interaction.channel.send.assert_awaited_once()

    async def test_unexpected_cleanup_failure_is_observable_but_success_remains_public(self):
        modal = self.modal()
        modal.canonical_name._value = 'Valid Name'
        interaction = self.interaction()
        interaction.delete_original_response.side_effect = RuntimeError(
            'cleanup unavailable'
        )
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            canonical_name='Valid Name',
            player_created=True,
            member_created=True,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        with (
            mock.patch.object(
                views.workers,
                'run_player_registration',
                new=mock.AsyncMock(return_value=result),
            ),
            mock.patch.object(
                views.player_registration.interaction_lifecycle.logger,
                'exception',
            ) as logged,
        ):
            await modal.on_submit(interaction)

        logged.assert_called_once()
        interaction.channel.send.assert_awaited_once()

    async def test_database_failure_stays_private_and_has_no_public_effect(self):
        modal = self.modal()
        modal.canonical_name._value = 'Valid Name'
        interaction = self.interaction()
        with mock.patch.object(
            views.workers,
            'run_player_registration',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('db down')),
        ):
            await modal.on_submit(interaction)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        interaction.delete_original_response.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()

    async def test_invalid_modal_value_is_private_before_worker(self):
        modal = self.modal()
        modal.canonical_name._value = 'Your Mobile Name'
        interaction = self.interaction()
        with mock.patch.object(
            views.workers,
            'run_player_registration',
            new=mock.AsyncMock(),
        ) as run:
            await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )
        run.assert_not_awaited()


class PlayerRegistrationCommandTests(unittest.IsolatedAsyncioTestCase):
    def group(self):
        return next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'player'
        )

    async def test_slash_registration_self_and_other_permission_parity(self):
        command = self.group().get_command('register')
        actor = member()
        other = member(200, name='Other', display_name='Other')
        cog = games.polygames.__new__(games.polygames)

        self_interaction = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_modal=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
        )
        with mock.patch.object(games.settings, 'is_staff', return_value=False):
            await command.callback(cog, self_interaction, None)
        self_interaction.response.send_modal.assert_awaited_once()

        denied = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_modal=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
        )
        with mock.patch.object(games.settings, 'is_staff', return_value=False):
            await command.callback(cog, denied, other)
        denied.response.send_message.assert_awaited_once_with(
            'Only server staff can register another member.',
            ephemeral=True,
        )
        denied.response.send_modal.assert_not_awaited()

        allowed = SimpleNamespace(
            user=actor,
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_modal=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
        )
        with mock.patch.object(games.settings, 'is_staff', return_value=True):
            await command.callback(cog, allowed, other)
        allowed.response.send_modal.assert_awaited_once()
        modal = allowed.response.send_modal.await_args.args[0]
        self.assertEqual(modal.target_snapshot.discord_id, 200)

    async def test_prefix_setname_uses_shared_service_and_aliases_do_not_write(self):
        prefix_command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'setname'
        )
        author = member()
        ctx = SimpleNamespace(
            author=author,
            guild=SimpleNamespace(id=300),
            prefix='$',
            invoked_with='setname',
            send=mock.AsyncMock(),
        )
        request_value = request()
        result = workers.PlayerRegistrationResult(
            guild_id=300,
            requester_id=100,
            target_id=100,
            canonical_name='Canonical',
            player_created=False,
            member_created=False,
            team_name=None,
            duplicate_count=0,
            warnings=(),
        )
        with (
            mock.patch.object(
                games.utilities,
                'get_guild_member',
                new=mock.AsyncMock(return_value=[author]),
            ),
            mock.patch.object(
                games.player_registration,
                'build_request',
                return_value=request_value,
            ) as build,
            mock.patch.object(
                games.player_registration_workers,
                'run_player_registration',
                new=mock.AsyncMock(return_value=result),
            ) as run,
        ):
            await prefix_command.callback(
                games.polygames.__new__(games.polygames),
                ctx,
                args='Canonical',
            )
        build.assert_called_once()
        run.assert_awaited_once_with(request_value)
        self.assertIn('account-wide', ctx.send.await_args.args[0])

        ctx.invoked_with = 'steamname'
        with mock.patch.object(
            games.player_registration_workers,
            'run_player_registration',
            new=mock.AsyncMock(),
        ) as run:
            await prefix_command.callback(
                games.polygames.__new__(games.polygames),
                ctx,
                args='Old Steam Name',
            )
        self.assertIn('deprecated', ctx.send.await_args.args[0])
        run.assert_not_awaited()

    async def test_legacy_code_read_is_deprecated_but_returns_transitional_name(self):
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'getname'
        )
        target = member()
        ctx = SimpleNamespace(
            author=target,
            guild=SimpleNamespace(id=300),
            prefix='$',
            invoked_with='getcode',
            send=mock.AsyncMock(),
        )
        stored = games.legacy_name_workers.AccountNameSnapshot(
            display_name='AccountUser',
            account_name='Canonical Name',
        )
        with (
            mock.patch.object(
                games.utilities,
                'get_guild_member',
                new=mock.AsyncMock(return_value=[target]),
            ),
            mock.patch.object(
                games.legacy_name_workers,
                'run_account_name',
                new=mock.AsyncMock(return_value=stored),
            ),
        ):
            await command.callback(games.polygames.__new__(games.polygames), ctx)
        output = '\n'.join(call.args[0] for call in ctx.send.await_args_list)
        self.assertIn('deprecated', output)
        self.assertIn('Canonical Name', output)
        self.assertNotIn('legacy-code', output)
        self.assertNotIn('Steam name:', output)


if __name__ == '__main__':
    unittest.main()
