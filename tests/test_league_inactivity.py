"""Focused P8.18 inactivity-preview and role-application coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import datetime
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_inactivity_workers')
service = import_offline_runtime('modules.league_inactivity')
views = import_offline_runtime('modules.league_inactivity_views')
league = import_offline_runtime('modules.league')
administration = import_offline_runtime('modules.administration')
games = import_offline_runtime('modules.games')


NOW = 1_800_000_000.0


class FakeRole:
    def __init__(self, role_id, name, *, managed=False):
        self.id = role_id
        self.name = name
        self.managed = managed


class FakeMember:
    def __init__(
        self,
        member_id,
        name,
        *,
        roles=(),
        joined_days=100,
        bot=False,
        add_error=None,
    ):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.mention = f'<@{member_id}>'
        self.roles = list(roles)
        self.joined_at = datetime.datetime.fromtimestamp(
            NOW - joined_days * 86400,
            tz=datetime.timezone.utc,
        ) if joined_days is not None else None
        self.bot = bot
        self.add_error = add_error
        self.add_calls = []

    async def add_roles(self, role, *, reason):
        self.add_calls.append((role, reason))
        if self.add_error is not None:
            raise self.add_error
        self.roles.append(role)


class FakeGuild:
    def __init__(self, *, members, roles, guild_id=300):
        self.id = guild_id
        self.members = list(members)
        self.roles = list(roles)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_member(self, member_id):
        return next((row for row in self.members if row.id == member_id), None)


def snapshot(member_id, *, name=None, joined_days=100, roles=(), bot=False, owner=False):
    return workers.InactivityMemberSnapshot(
        member_id=member_id,
        display_name=name or f'Member {member_id}',
        joined_timestamp=(
            NOW - joined_days * 86400 if joined_days is not None else None
        ),
        role_ids=tuple(role_id for role_id, _ in roles),
        role_names=tuple(role_name for _, role_name in roles),
        is_bot=bot,
        is_owner=owner,
    )


def request(*, members=(), requester_is_mod=True):
    return workers.InactivityPreviewRequest(
        guild_id=300,
        requester_id=10,
        requester_is_mod=requester_is_mod,
        league_scope=True,
        now_timestamp=NOW,
        inactive_role_id=99,
        inactive_role_name='Inactive',
        protected_role_names=('Mod', 'Team Leader'),
        missing_protected_role_names=(),
        members=tuple(members),
    )


def preview(*, candidates=(), **overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        generated_timestamp=NOW,
        inactive_role_id=99,
        inactive_role_name='Inactive',
        protected_role_names=('Mod', 'Team Leader'),
        missing_protected_role_names=(),
        candidates=tuple(candidates),
        active_count=0,
        recent_join_count=0,
        already_inactive_count=0,
        protected_count=0,
        omitted_count=0,
        total_member_count=len(candidates),
    )
    values.update(overrides)
    return workers.InactivityPreviewResult(**values)


def candidate(member_id, *, name=None, joined_days=100):
    return workers.InactivityCandidate(
        member_id=member_id,
        display_name=name or f'Member {member_id}',
        joined_days=joined_days,
        role_names=('@everyone',),
    )


def league_root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


class RegistrationAndCaptureTests(unittest.TestCase):
    def test_native_shape_retires_only_deactivate_prefix(self):
        command = league_root().get_command('maintenance').get_command(
            'mark-inactive'
        )
        self.assertIsNotNone(command)
        self.assertEqual(command.parameters, [])
        prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertNotIn('deactivate_players', prefix)
        self.assertFalse(any(
            'deactivate' in command.aliases for command in prefix.values()
        ))
        self.assertIn('kick_inactive', prefix)

    def test_access_is_mod_only_in_league_scope(self):
        actor = SimpleNamespace(id=10)
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=False
        ), mock.patch.object(service.settings, 'is_mod', return_value=True):
            self.assertIn('configured league', service.access_error(actor, 300))
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=True
        ), mock.patch.object(service.settings, 'is_mod', return_value=False):
            self.assertIn('Mod', service.access_error(actor, 300))

    def test_capture_freezes_members_and_reports_missing_policy_roles(self):
        everyone = FakeRole(1, '@everyone')
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(everyone, mod))
        target = FakeMember(20, 'Target', roles=(everyone,))
        guild = FakeGuild(
            members=(actor, target),
            roles=(everyone, inactive, mod),
        )
        actor.guild = guild
        with mock.patch.object(service.settings, 'is_mod', return_value=True), \
                mock.patch.object(
                    service.league_user_commands, 'league_scope', return_value=True
                ), mock.patch.object(
                    service.settings,
                    'guild_setting',
                    side_effect=lambda _guild_id, key: (
                        'Inactive' if key == 'inactive_role' else ['Mod']
                    ),
                ), mock.patch.object(service.discord.utils, 'utcnow', return_value=(
                    datetime.datetime.fromtimestamp(NOW, tz=datetime.timezone.utc)
                )):
            captured = service.capture_request(member=actor, guild=guild)
        self.assertEqual(captured.inactive_role_id, 99)
        self.assertEqual(len(captured.members), 2)
        self.assertIn('Team Leader', captured.missing_protected_role_names)
        with self.assertRaises(FrozenInstanceError):
            captured.guild_id = 1


class WorkerTests(unittest.TestCase):
    def test_selection_preserves_policy_and_category_counts(self):
        members = (
            snapshot(1, joined_days=100),
            snapshot(2, joined_days=100),
            snapshot(3, joined_days=10),
            snapshot(4, joined_days=100, roles=((99, 'Inactive'),)),
            snapshot(5, joined_days=100, roles=((2, 'Mod'),)),
            snapshot(6, joined_days=100, bot=True),
            snapshot(7, joined_days=100, owner=True),
            snapshot(8, joined_days=None),
        )
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(workers, '_active_member_ids', return_value={2}):
            result = workers._load_preview(request(members=members))
        self.assertEqual(result.candidate_ids, (1,))
        self.assertEqual(result.active_count, 1)
        self.assertEqual(result.recent_join_count, 1)
        self.assertEqual(result.already_inactive_count, 1)
        self.assertEqual(result.protected_count, 1)
        self.assertEqual(result.omitted_count, 3)

    def test_permission_and_candidate_cap(self):
        with self.assertRaises(workers.LeagueInactivityPermissionError):
            workers._load_preview(request(requester_is_mod=False))
        result = preview(candidates=tuple(candidate(i) for i in range(120)))
        self.assertEqual(len(result.action_candidates), 100)
        self.assertEqual(result.deferred_candidate_count, 20)
        with self.assertRaises(workers.LeagueInactivityError):
            workers._load_preview(request(members=tuple(
                snapshot(i)
                for i in range(workers.MAX_GUILD_MEMBER_SNAPSHOTS + 1)
            )))

    def test_slow_selection_is_responsive_and_rejects_conflict(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return preview()

            with mock.patch.object(workers, '_load_preview', side_effect=slow):
                first = asyncio.create_task(
                    workers.run_inactivity_preview(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not first.done()
                with self.assertRaises(workers.LeagueInactivityBusyError):
                    await workers.run_inactivity_preview(request())
                release.set()
                await first
            return responsive

        self.assertTrue(asyncio.run(scenario()))

    def test_audit_owns_connection_and_transaction(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        query = mock.MagicMock()
        query.join.return_value = query
        query.where.return_value = query
        discord_member = SimpleNamespace(name='Target', discord_id=20)
        query.get.return_value = discord_member
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=connection
        ), mock.patch.object(
            workers.models.db, 'atomic', return_value=atomic
        ), mock.patch.object(
            workers.models.DiscordMember, 'select', return_value=query
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            recorded = workers._write_role_audit(
                workers.InactiveRoleAuditRequest(300, 20, 'Inactive', True)
            )
        self.assertTrue(recorded)
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()
        self.assertEqual(write.call_args.kwargs['guild_id'], 300)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, *, guild, actor, channel=None):
        actor.guild = guild
        return SimpleNamespace(
            guild=guild,
            user=actor,
            channel=channel or SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_changed_candidates_refresh_without_role_effects(self):
        inactive = FakeRole(99, 'Inactive')
        actor = FakeMember(10, 'Actor', roles=(FakeRole(2, 'Mod'),))
        target = FakeMember(20, 'Target')
        guild = FakeGuild(members=(actor, target), roles=(inactive,))
        prior = preview(candidates=(candidate(20),))
        refreshed = preview(candidates=(candidate(21),))
        interaction = self.interaction(guild=guild, actor=actor)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=refreshed)
                ):
            outcome = await service.confirm_and_publish(interaction, prior)
        self.assertEqual(outcome.state, 'refreshed')
        self.assertEqual(target.add_calls, [])
        interaction.channel.send.assert_not_awaited()

    async def test_partial_role_failures_continue_and_publish_aggregate(self):
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(mod,))
        first = FakeMember(20, 'First')
        second = FakeMember(
            21,
            'Second',
            add_error=discord.DiscordException('denied'),
        )
        guild = FakeGuild(
            members=(actor, first, second),
            roles=(inactive, mod),
        )
        current = preview(candidates=(candidate(20), candidate(21)))
        interaction = self.interaction(guild=guild, actor=actor)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=current)
                ):
            outcome = await service.confirm_and_publish(interaction, current)
        self.assertEqual(outcome.state, 'applied')
        self.assertEqual(outcome.succeeded_count, 1)
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(len(first.add_calls), 1)
        interaction.channel.send.assert_awaited_once()
        self.assertIn('**1** member', interaction.channel.send.call_args.args[0])
        self.assertNotIn('remain beyond', interaction.channel.send.call_args.args[0])

    async def test_all_role_failures_remain_retryable_and_private(self):
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(mod,))
        target = FakeMember(
            20,
            'Target',
            add_error=discord.DiscordException('denied'),
        )
        guild = FakeGuild(members=(actor, target), roles=(inactive, mod))
        current = preview(candidates=(candidate(20),))
        interaction = self.interaction(guild=guild, actor=actor)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service,
                    'load_preview',
                    new=mock.AsyncMock(return_value=current),
                ):
            outcome = await service.confirm_and_publish(interaction, current)
        self.assertEqual(outcome.state, 'retryable')
        self.assertEqual(outcome.succeeded_count, 0)
        self.assertEqual(outcome.failed_count, 1)
        interaction.channel.send.assert_not_awaited()

    async def test_publication_failure_is_terminal_reconciliation(self):
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(mod,))
        target = FakeMember(20, 'Target')
        guild = FakeGuild(members=(actor, target), roles=(inactive, mod))
        current = preview(candidates=(candidate(20),))
        channel = SimpleNamespace(
            send=mock.AsyncMock(side_effect=discord.DiscordException('gone'))
        )
        interaction = self.interaction(
            guild=guild,
            actor=actor,
            channel=channel,
        )
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service,
                    'load_preview',
                    new=mock.AsyncMock(return_value=current),
                ):
            outcome = await service.confirm_and_publish(interaction, current)
        self.assertEqual(outcome.state, 'reconciliation')
        self.assertTrue(outcome.terminal)
        self.assertEqual(outcome.succeeded_count, 1)
        self.assertIn('Do not retry', outcome.private_message)
        self.assertEqual(len(target.add_calls), 1)


class ViewAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_view_serializes_valid_private_preview_controls(self):
        item = preview(candidates=(candidate(20), candidate(21)))
        view = views.InactivityPreviewWorkspace(
            result=item,
            requester_id=10,
            confirmer=mock.AsyncMock(),
        )
        payload = view.to_components()
        self.assertTrue(payload)
        buttons = [
            child
            for child in view.walk_children()
            if isinstance(child, discord.ui.Button)
        ]
        self.assertEqual(
            {button.label for button in buttons},
            {'Confirm refreshed plan', 'Cancel'},
        )

    async def test_native_defers_privately_and_publishes_private_preview(self):
        command = (
            league_root().get_command('maintenance').get_command('mark-inactive')
        )
        actor = SimpleNamespace(id=10)
        guild = SimpleNamespace(id=300)
        interaction = SimpleNamespace(
            guild=guild,
            user=actor,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        loaded = preview()
        cog = league.league.__new__(league.league)
        with mock.patch.object(
            service, 'access_error', return_value=None
        ), mock.patch.object(
            service, 'load_preview', new=mock.AsyncMock(return_value=loaded)
        ), mock.patch.object(
            views, 'publish_private', new=mock.AsyncMock()
        ) as publish:
            returned = await command.callback(cog, interaction)
        self.assertEqual(returned, loaded)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        publish.assert_awaited_once()


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_inactive_role_audit_delegates_to_worker(self):
        inactive = FakeRole(99, 'Inactive')
        guild = SimpleNamespace(id=300, roles=[inactive])
        before = SimpleNamespace(
            id=20,
            guild=guild,
            roles=[],
            nick=None,
            name='Target',
        )
        after = SimpleNamespace(
            id=20,
            guild=guild,
            roles=[inactive],
            nick=None,
            name='Target',
        )
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: (
                'Inactive' if key == 'inactive_role' else []
            ),
        ), mock.patch.object(
            games.league_inactivity_workers,
            'record_inactive_role_change',
            new=mock.AsyncMock(return_value=True),
        ) as record:
            await cog.on_member_update(before, after)
        record.assert_awaited_once()
        audit = record.call_args.args[0]
        self.assertEqual((audit.guild_id, audit.member_id, audit.applied), (300, 20, True))


if __name__ == '__main__':
    unittest.main()
