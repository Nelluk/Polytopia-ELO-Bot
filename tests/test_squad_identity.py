"""Focused offline coverage for the P7.12 squad identity workspace."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.squad_identity_workers')
service = import_offline_runtime('modules.squad_identity')
show_workers = import_offline_runtime('modules.squad_show_workers')
show_views = import_offline_runtime('modules.squad_show_views')
identity_views = import_offline_runtime('modules.squad_identity_views')
show_service = import_offline_runtime('modules.squad_show')
games = import_offline_runtime('modules.games')


class FakeAtomic(AbstractContextManager):
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        self.database.atomic_entered += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.database.atomic_rolled_back += 1
        else:
            self.database.atomic_committed += 1
        return False


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.atomic_entered = 0
        self.atomic_committed = 0
        self.atomic_rolled_back = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return ConnectionContext()

    def atomic(self):
        return FakeAtomic(self)


class FakeSquad:
    def __init__(self, *, guild_id=300, squad_id=42, name='Old name'):
        self.id = squad_id
        self.guild_id = guild_id
        self.name = name
        self.member_ids = {999}
        self.save = mock.Mock()

    def has_player(self, *, discord_id):
        return int(discord_id) in self.member_ids


def request(**overrides):
    values = dict(
        guild_id=300,
        squad_id=42,
        requester_id=999,
        requester_is_staff=False,
        requester_description='**Actor** (`999`)',
    )
    values.update(overrides)
    return workers.SquadNameMutationRequest(**values)


def card(*, can_edit_name=False, squad_name='Old name', squad_id=42):
    return show_workers.SquadShowCard(
        guild_id=300,
        squad_id=squad_id,
        squad_name=squad_name,
        members=(),
        elo=1000,
        wins=2,
        losses=1,
        leaderboard_rank=1,
        leaderboard_length=1,
        recent_games=(),
        can_edit_name=can_edit_name,
    )


def show_result(*, can_edit_name=False, squad_name='Old name', squad_id=42):
    loaded_card = card(
        can_edit_name=can_edit_name,
        squad_name=squad_name,
        squad_id=squad_id,
    )
    return show_workers.SquadShowResult(
        guild_id=300,
        requester_id=999,
        member_ids=(999,),
        cards=(loaded_card,),
        selected_squad_id=squad_id,
        total_matches=1,
        truncated=False,
    )


class IdentityWorkerBoundaryTests(unittest.TestCase):
    def test_frozen_worker_dto_connection_and_atomic_audit_boundary(self):
        database = FakeDatabase()
        squad = FakeSquad()
        audit = mock.Mock()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(workers.models.GameLog, 'write', audit),
        ):
            result = workers.set_squad_name(
                request(name='  New   name\nwith tabs\t', captured_can_edit=True)
            )

        self.assertEqual(squad.name, 'New name with tabs')
        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertEqual(
            (database.atomic_entered, database.atomic_committed),
            (1, 1),
        )
        audit.assert_called_once()
        self.assertEqual(audit.call_args.kwargs['game_id'], 0)
        self.assertEqual(audit.call_args.kwargs['guild_id'], 300)
        self.assertIn('Actor', audit.call_args.kwargs['message'])
        self.assertIn('squad 42', audit.call_args.kwargs['message'])
        self.assertIsInstance(result, workers.SquadNameMutationResult)
        with self.assertRaises(FrozenInstanceError):
            result.name = 'changed'

    def test_save_failure_rolls_back_before_audit_and_has_no_result(self):
        database = FakeDatabase()
        squad = FakeSquad()
        squad.save.side_effect = RuntimeError('save failed')
        audit = mock.Mock()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(workers.models.GameLog, 'write', audit),
            self.assertRaises(RuntimeError),
        ):
            workers.set_squad_name(request(name='New'))
        self.assertEqual(database.atomic_rolled_back, 1)
        audit.assert_not_called()

    def test_audit_failure_rolls_back_name_and_audit_together(self):
        database = FakeDatabase()
        squad = FakeSquad()
        audit = mock.Mock(side_effect=RuntimeError('audit failed'))
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(workers.models.GameLog, 'write', audit),
            self.assertRaises(RuntimeError),
        ):
            workers.set_squad_name(request(name='New'))
        self.assertEqual(database.atomic_rolled_back, 1)
        self.assertEqual(squad.name, 'New')

    def test_worker_revalidates_guild_membership_and_ignores_forged_visibility(self):
        database = FakeDatabase()
        squad = FakeSquad()
        squad.member_ids.clear()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
        ):
            with self.assertRaises(workers.SquadNamePermissionError):
                workers.set_squad_name(
                    request(name='Forged', captured_can_edit=True)
                )

            squad.guild_id = 301
            with self.assertRaises(workers.SquadNameWrongGuild):
                workers.set_squad_name(
                    request(name='Wrong guild', requester_is_staff=True)
                )

    def test_staff_role_snapshot_is_revalidated_at_worker_boundary(self):
        database = FakeDatabase()
        squad = FakeSquad()
        squad.member_ids.clear()
        staff_request = request(
            name='Staff name',
            requester_role_names=('Helper',),
            requester_is_staff=True,
        )
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(
                workers.player_registration_workers,
                'is_staff_snapshot',
                return_value=True,
            ),
            mock.patch.object(workers.models.GameLog, 'write'),
        ):
            workers.set_squad_name(staff_request)

        squad = FakeSquad()
        squad.member_ids.clear()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(
                workers.player_registration_workers,
                'is_staff_snapshot',
                return_value=False,
            ),
            mock.patch.object(workers.models.GameLog, 'write'),
        ):
            with self.assertRaises(workers.SquadNamePermissionError):
                workers.set_squad_name(staff_request)

    def test_missing_and_stale_squad_are_private_worker_errors(self):
        database = FakeDatabase()
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(
                workers.models.Squad,
                'get',
                side_effect=workers.peewee.DoesNotExist,
            ),
        ):
            with self.assertRaises(workers.SquadNameNotFound):
                workers.set_squad_name(request(name='Missing'))

        squad = FakeSquad(name='Current')
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
        ):
            with self.assertRaises(workers.SquadNameConflictError):
                workers.set_squad_name(
                    request(
                        name='Stale edit',
                        expected_name='Old name',
                        check_expected_name=True,
                    )
                )

    def test_normalization_ceiling_and_unsafe_values_are_explicit(self):
        self.assertEqual(
            workers.normalize_squad_name('  Alpha\n\t Beta  '),
            ('Alpha Beta', False),
        )
        normalized, truncated = workers.normalize_squad_name('x' * 60)
        self.assertEqual(len(normalized), workers.MAX_SQUAD_NAME_LENGTH)
        self.assertTrue(truncated)
        with self.assertRaises(workers.SquadNameValidationError):
            workers.normalize_squad_name('   ')
        with self.assertRaises(workers.SquadNameValidationError):
            workers.normalize_squad_name('visible\u200binvisible')
        with self.assertRaises(workers.SquadNameValidationError):
            workers.set_squad_name(request(name=None, clear=False))

    def test_clear_is_explicit_and_contradictory_input_is_rejected(self):
        with self.assertRaises(workers.SquadNameValidationError):
            workers.set_squad_name(request(name='Name', clear=True))
        database = FakeDatabase()
        squad = FakeSquad(name='Name')
        with (
            mock.patch.object(workers.models, 'db', database),
            mock.patch.object(workers.models.Squad, 'get', return_value=squad),
            mock.patch.object(workers.models.GameLog, 'write'),
        ):
            result = workers.set_squad_name(request(name=None, clear=True))
        self.assertTrue(result.cleared)
        self.assertEqual(result.name, '')


class IdentityAsyncWorkerTests(unittest.TestCase):
    def test_cancellation_drains_slow_mutation(self):
        started = threading.Event()
        finished = threading.Event()

        def slow_mutation(_request):
            started.set()
            time.sleep(0.06)
            finished.set()
            return workers.SquadNameMutationResult(
                guild_id=300,
                squad_id=42,
                requester_id=999,
                requester_description='**Actor** (`999`)',
                old_name='Old',
                name='New',
                cleared=False,
                truncated=False,
                native=True,
            )

        async def run_case():
            with mock.patch.object(workers, 'set_squad_name', side_effect=slow_mutation):
                task = asyncio.create_task(
                    workers.run_squad_name_mutation(request(name='New'))
                )
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('the worker did not start')
                    await asyncio.sleep(0.001)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run_case())
        self.assertTrue(finished.is_set())

    def test_bounded_concurrency_and_event_loop_responsiveness(self):
        lock = threading.Lock()
        active = 0
        maximum = 0

        def slow_mutation(current_request):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return workers.SquadNameMutationResult(
                guild_id=300,
                squad_id=current_request.squad_id,
                requester_id=999,
                requester_description='**Actor** (`999`)',
                old_name='Old',
                name='New',
                cleared=False,
                truncated=False,
                native=True,
            )

        async def ticker():
            ticks = 0
            while ticks < 3:
                await asyncio.sleep(0.01)
                ticks += 1
            return ticks

        async def run_case():
            with mock.patch.object(workers, 'set_squad_name', side_effect=slow_mutation):
                results = await asyncio.gather(
                    workers.run_squad_name_mutation(request(name='One')),
                    workers.run_squad_name_mutation(request(name='Two')),
                    ticker(),
                )
            return results

        results = asyncio.run(run_case())
        self.assertEqual(results[-1], 3)
        self.assertEqual(maximum, 1)
        self.assertEqual(workers._squad_name_write_executor._max_workers, 1)


class IdentityPresentationAndModalTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, user_id=999):
        response = SimpleNamespace(
            done=False,
            defer=mock.AsyncMock(side_effect=self._mark_done),
            send_message=mock.AsyncMock(side_effect=self._mark_done),
            send_modal=mock.AsyncMock(side_effect=self._mark_done),
        )
        return SimpleNamespace(
            user=SimpleNamespace(
                id=user_id,
                display_name='Actor * [name]',
                name='Actor',
                mention=f'<@{user_id}>',
                roles=(),
            ),
            guild=SimpleNamespace(id=300),
            channel_id=123,
            channel=SimpleNamespace(
                send=mock.AsyncMock(return_value=SimpleNamespace(id=88)),
            ),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    async def _mark_done(self, *args, **kwargs):
        return None

    def _view(self, *, can_edit_name=False, name_mutator=None):
        return show_views.SquadShowWorkspace(
            requester_id=999,
            result=show_result(can_edit_name=can_edit_name),
            name_mutator=name_mutator,
        )

    def test_public_rendering_escapes_mentions_and_markdown(self):
        actor = service.capture_actor(
            SimpleNamespace(
                id=999,
                display_name='@everyone *actor*',
                mention='<@999>',
            )
        )
        rendered = service.read_message(
            card(squad_name='@everyone *squad*'),
            actor=actor,
        )
        self.assertNotIn('@everyone', rendered)
        self.assertNotIn('**@everyone', rendered)
        self.assertIn('squad 42', rendered)

    def test_edit_button_visibility_uses_captured_member_or_staff_state(self):
        callback = mock.AsyncMock()
        member_view = self._view(can_edit_name=True, name_mutator=callback)
        staff_view = self._view(can_edit_name=True, name_mutator=callback)
        nonmember_view = self._view(can_edit_name=False, name_mutator=callback)

        def edit_buttons(view):
            return [
                item
                for item in view.walk_children()
                if getattr(item, 'label', None) == 'Edit name'
            ]

        self.assertEqual(len(edit_buttons(member_view)), 1)
        self.assertEqual(len(edit_buttons(staff_view)), 1)
        self.assertEqual(len(edit_buttons(nonmember_view)), 0)

    async def test_modal_validation_and_shared_callback(self):
        callback = mock.AsyncMock()
        view = self._view(can_edit_name=True, name_mutator=callback)
        modal = identity_views.SquadNameEditModal(view, view.selected_card)
        modal.name_input._value = 'New squad'
        modal.clear_input._value = False
        interaction_value = self._interaction()

        await modal.on_submit(interaction_value)

        callback.assert_awaited_once_with(
            interaction_value,
            view.selected_card,
            'New squad',
            False,
        )
        interaction_value.response.defer.assert_awaited_once_with(ephemeral=True)

        callback.reset_mock()
        contradictory = identity_views.SquadNameEditModal(
            view,
            view.selected_card,
        )
        contradictory.name_input._value = 'New squad'
        contradictory.clear_input._value = True
        invalid_interaction = self._interaction()
        await contradictory.on_submit(invalid_interaction)
        callback.assert_not_awaited()
        invalid_interaction.followup.send.assert_awaited_once()
        self.assertTrue(
            invalid_interaction.followup.send.await_args.kwargs['ephemeral']
        )

        clear_callback = mock.AsyncMock()
        clear_view = self._view(can_edit_name=True, name_mutator=clear_callback)
        clear_modal = identity_views.SquadNameEditModal(
            clear_view,
            clear_view.selected_card,
        )
        clear_modal.clear_input._value = True
        clear_interaction = self._interaction()
        # The unchanged prefilled value is ignored only because clear is
        # explicitly selected; omission alone remains a read/validation case.
        await clear_modal.on_submit(clear_interaction)
        clear_callback.assert_awaited_once_with(
            clear_interaction,
            clear_view.selected_card,
            None,
            True,
        )

    async def test_modal_is_requester_only_and_expiry_is_private(self):
        callback = mock.AsyncMock()
        view = self._view(can_edit_name=True, name_mutator=callback)
        modal = identity_views.SquadNameEditModal(view, view.selected_card)
        unauthorized = self._interaction(user_id=888)
        await modal.on_submit(unauthorized)
        unauthorized.response.send_message.assert_awaited_once()
        callback.assert_not_awaited()

        expired_view = self._view(can_edit_name=True, name_mutator=callback)
        expired_view.stop()
        expired_modal = identity_views.SquadNameEditModal(
            expired_view,
            expired_view.selected_card,
        )
        expired = self._interaction()
        await expired_modal.on_submit(expired)
        expired.response.send_message.assert_awaited_once()

    async def test_forged_edit_button_is_denied_before_modal_open(self):
        callback = mock.AsyncMock()
        view = self._view(can_edit_name=False, name_mutator=callback)
        forged = self._interaction()
        await view._open_edit_name(forged)
        forged.response.send_message.assert_awaited_once()
        forged.response.send_modal.assert_not_awaited()


class CommandPublicationTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self):
        response = SimpleNamespace(
            done=False,
            defer=mock.AsyncMock(side_effect=self._mark_done),
        )
        return SimpleNamespace(
            user=SimpleNamespace(
                id=999,
                display_name='Actor * [name]',
                name='Actor',
                mention='<@999>',
                roles=(),
            ),
            guild=SimpleNamespace(id=300),
            channel_id=123,
            channel=SimpleNamespace(
                send=mock.AsyncMock(return_value=SimpleNamespace(id=88)),
            ),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    async def _mark_done(self, *args, **kwargs):
        return None

    def _command(self, name):
        return next(
            command
            for root in games.polygames.__cog_app_commands__
            if root.name == 'squad'
            for command in root.commands
            if command.name == name
        )

    async def test_public_read_uses_existing_bounded_squad_show_path(self):
        interaction_value = self._interaction()
        request_value = show_workers.SquadShowRequest(
            guild_id=300,
            requester_id=999,
            squad_id=42,
            member_ids=(),
        )
        loaded = show_result(squad_name='Visible name')
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(show_service, 'native_access_error', return_value=None),
            mock.patch.object(show_service, 'build_request', return_value=request_value),
            mock.patch.object(
                show_workers,
                'run_squad_show',
                new=mock.AsyncMock(return_value=loaded),
            ) as run,
        ):
            await self._command('name').callback(cog, interaction_value, 42)

        run.assert_awaited_once_with(request_value)
        interaction_value.delete_original_response.assert_awaited_once()
        interaction_value.channel.send.assert_awaited_once()
        content = interaction_value.channel.send.await_args.kwargs['content']
        self.assertIn('Visible name', content)
        self.assertFalse(
            interaction_value.channel.send.await_args.kwargs.get('ephemeral', False)
        )

    async def test_direct_mutation_and_modal_refresh_share_service_and_publish(self):
        interaction_value = self._interaction()
        request_value = workers.SquadNameMutationRequest(
            guild_id=300,
            squad_id=42,
            requester_id=999,
            requester_is_staff=False,
            requester_description='**Actor** (`999`)',
            name='New name',
        )
        committed = workers.SquadNameMutationResult(
            guild_id=300,
            squad_id=42,
            requester_id=999,
            requester_description='**Actor** (`999`)',
            old_name='Old name',
            name='New name',
            cleared=False,
            truncated=False,
            native=True,
        )
        refreshed = show_result(squad_name='New name', can_edit_name=True)
        workspace = SimpleNamespace(apply_refreshed_result=mock.AsyncMock())
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(show_service, 'native_access_error', return_value=None),
            mock.patch.object(
                service,
                'build_mutation_request',
                return_value=request_value,
            ) as build,
            mock.patch.object(
                service,
                'run_mutation',
                new=mock.AsyncMock(return_value=committed),
            ) as run,
            mock.patch.object(
                show_service,
                'build_request',
                return_value=mock.sentinel.refresh_request,
            ),
            mock.patch.object(
                show_workers,
                'run_squad_show',
                new=mock.AsyncMock(return_value=refreshed),
            ) as refresh,
        ):
            result = await cog._execute_squad_name_mutation(
                interaction_value,
                squad_id=42,
                name='New name',
                clear=False,
                workspace=workspace,
            )

        self.assertIs(result, committed)
        build.assert_called_once()
        run.assert_awaited_once_with(request_value)
        refresh.assert_awaited_once_with(mock.sentinel.refresh_request)
        workspace.apply_refreshed_result.assert_awaited_once_with(refreshed)
        interaction_value.channel.send.assert_awaited_once()
        content = interaction_value.channel.send.await_args.kwargs['content']
        self.assertIn('Actor', content)
        self.assertIn('squad 42', content)

    async def test_refresh_failure_reports_commit_without_database_failure_wording(self):
        interaction_value = self._interaction()
        committed = workers.SquadNameMutationResult(
            guild_id=300,
            squad_id=42,
            requester_id=999,
            requester_description='**Actor** (`999`)',
            old_name='Old name',
            name='New name',
            cleared=False,
            truncated=False,
            native=True,
        )
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(show_service, 'native_access_error', return_value=None),
            mock.patch.object(service, 'build_mutation_request'),
            mock.patch.object(
                service,
                'run_mutation',
                new=mock.AsyncMock(return_value=committed),
            ),
            mock.patch.object(
                show_service,
                'build_request',
                return_value=mock.sentinel.refresh_request,
            ),
            mock.patch.object(
                show_workers,
                'run_squad_show',
                new=mock.AsyncMock(side_effect=RuntimeError('refresh failed')),
            ),
        ):
            await cog._execute_squad_name_mutation(
                interaction_value,
                squad_id=42,
                name='New name',
                clear=False,
                workspace=SimpleNamespace(
                    apply_refreshed_result=mock.AsyncMock(),
                ),
            )

        content = interaction_value.channel.send.await_args.kwargs['content']
        self.assertIn('committed', content)
        self.assertIn('refreshed', content)
        self.assertNotIn('database failure', content.lower())

    async def test_contradictory_and_database_failures_have_no_public_effect(self):
        interaction_value = self._interaction()
        cog = object.__new__(games.polygames)
        with mock.patch.object(service, 'run_mutation', new=mock.AsyncMock()) as run:
            await self._command('name').callback(
                cog,
                interaction_value,
                42,
                'Name',
                True,
            )
        run.assert_not_awaited()
        interaction_value.followup.send.assert_awaited_once()
        interaction_value.channel.send.assert_not_awaited()

    async def test_direct_clear_is_public_and_uses_the_same_worker_service(self):
        interaction_value = self._interaction()
        request_value = workers.SquadNameMutationRequest(
            guild_id=300,
            squad_id=42,
            requester_id=999,
            requester_is_staff=False,
            requester_description='**Actor** (`999`)',
            clear=True,
        )
        committed = workers.SquadNameMutationResult(
            guild_id=300,
            squad_id=42,
            requester_id=999,
            requester_description='**Actor** (`999`)',
            old_name='Old name',
            name='',
            cleared=True,
            truncated=False,
            native=True,
        )
        cog = object.__new__(games.polygames)
        with (
            mock.patch.object(show_service, 'native_access_error', return_value=None),
            mock.patch.object(service, 'build_mutation_request', return_value=request_value),
            mock.patch.object(
                service,
                'run_mutation',
                new=mock.AsyncMock(return_value=committed),
            ) as run,
        ):
            await self._command('name').callback(cog, interaction_value, 42, None, True)

        run.assert_awaited_once_with(request_value)
        self.assertIn(
            'cleared',
            interaction_value.channel.send.await_args.kwargs['content'].lower(),
        )

        interaction_value = self._interaction()
        with mock.patch.object(
            service,
            'run_mutation',
            new=mock.AsyncMock(
                side_effect=workers.SquadNamePermissionError('denied')
            ),
        ), mock.patch.object(
            show_service,
            'native_access_error',
            return_value=None,
        ):
            await cog._execute_squad_name_mutation(
                interaction_value,
                squad_id=42,
                name='Name',
                clear=False,
            )
        interaction_value.followup.send.assert_awaited_once()
        interaction_value.channel.send.assert_not_awaited()
