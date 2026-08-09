"""Focused P8.19 inactive-member removal coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import datetime
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_inactive_kick_workers')
service = import_offline_runtime('modules.league_inactive_kick')
views = import_offline_runtime('modules.league_inactive_kick_views')
league = import_offline_runtime('modules.league')
administration = import_offline_runtime('modules.administration')

NOW = 1_800_000_000.0


class FakeRole:
    def __init__(self, role_id, name, *, managed=False, members=()):
        self.id = role_id
        self.name = name
        self.managed = managed
        self.members = list(members)


class FakeMember:
    def __init__(
        self,
        member_id,
        name,
        *,
        roles=(),
        joined_days=100,
        bot=False,
        dm_error=None,
        kick_error=None,
    ):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.mention = f'<@{member_id}>'
        self.roles = list(roles)
        self.joined_at = (
            datetime.datetime.fromtimestamp(
                NOW - joined_days * 86400,
                tz=datetime.timezone.utc,
            )
            if joined_days is not None else None
        )
        self.bot = bot
        self.dm_error = dm_error
        self.kick_error = kick_error
        self.send_calls = []
        self.kick_calls = []

    async def send(self, content):
        self.send_calls.append(content)
        if self.dm_error:
            raise self.dm_error

    async def kick(self, *, reason):
        self.kick_calls.append(reason)
        if self.kick_error:
            raise self.kick_error

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, *, members, roles, guild_id=300):
        self.id = guild_id
        self.members = list(members)
        self.roles = list(roles)

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)


def role_snapshot(role_id, name, *, managed=False):
    return workers.KickRoleSnapshot(role_id, name, managed)


def member_snapshot(
    member_id,
    *,
    joined_days=100,
    roles=None,
    bot=False,
    owner=False,
):
    roles = roles or (
        role_snapshot(1, '@everyone'),
        role_snapshot(99, 'Inactive'),
    )
    return workers.KickMemberSnapshot(
        member_id=member_id,
        display_name=f'Member {member_id}',
        joined_timestamp=(
            NOW - joined_days * 86400 if joined_days is not None else None
        ),
        roles=tuple(roles),
        is_bot=bot,
        is_owner=owner,
    )


def request(*, members=(), requester_is_mod=True):
    return workers.InactiveKickPreviewRequest(
        guild_id=300,
        requester_id=10,
        requester_is_mod=requester_is_mod,
        league_scope=True,
        now_timestamp=NOW,
        inactive_role_id=99,
        inactive_role_name='Inactive',
        starter_role_names=('Newbie', 'ELO Rookie'),
        protected_role_names=('Mod', 'Helper', 'Team Leader'),
        members=tuple(members),
    )


def decision(member_id, *, eligible=True, reason='eligible', team=False):
    return workers.InactiveKickDecision(
        member_id=member_id,
        display_name=f'Member {member_id}',
        joined_days=100,
        eligible=eligible,
        reason=reason,
        has_team_role=team,
    )


def preview(*, decisions=()):
    return workers.InactiveKickPreviewResult(
        guild_id=300,
        requester_id=10,
        generated_timestamp=NOW,
        inactive_role_id=99,
        inactive_role_name='Inactive',
        starter_role_names=('Newbie', 'ELO Rookie'),
        protected_role_names=('Mod', 'Helper', 'Team Leader'),
        team_role_names=('The Ronin',),
        decisions=tuple(decisions),
    )


def league_root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


class RegistrationAndCaptureTests(unittest.TestCase):
    def test_native_no_option_shape_and_prefix_retirement(self):
        command = league_root().get_command('maintenance').get_command(
            'kick-inactive'
        )
        self.assertIsNotNone(command)
        self.assertEqual(command.parameters, [])
        prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertNotIn('kick_inactive', prefix)

    def test_access_is_mod_only_and_league_only(self):
        actor = SimpleNamespace(id=10)
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=False
        ), mock.patch.object(service.settings, 'is_mod', return_value=True):
            self.assertIn('configured league', service.access_error(actor, 300))
        with mock.patch.object(
            service.league_user_commands, 'league_scope', return_value=True
        ), mock.patch.object(service.settings, 'is_mod', return_value=False):
            self.assertIn('Mod', service.access_error(actor, 300))

    def test_capture_uses_only_inactive_members_and_freezes_role_metadata(self):
        everyone = FakeRole(1, '@everyone')
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(everyone, mod))
        target = FakeMember(20, 'Target', roles=(everyone, inactive))
        other = FakeMember(21, 'Other', roles=(everyone,))
        inactive.members = [target]
        guild = FakeGuild(
            members=(actor, target, other),
            roles=(everyone, inactive, mod),
        )
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service.settings,
                    'guild_setting',
                    side_effect=lambda _guild_id, key: (
                        'Inactive' if key == 'inactive_role'
                        else ['Mod'] if key == 'mod_roles'
                        else ['Helper'] if key == 'helper_roles'
                        else None
                    ),
                ), mock.patch.object(
                    service.discord.utils,
                    'utcnow',
                    return_value=datetime.datetime.fromtimestamp(
                        NOW, tz=datetime.timezone.utc
                    ),
                ):
            captured = service.capture_request(member=actor, guild=guild)
        self.assertEqual([row.member_id for row in captured.members], [20])
        self.assertIn('Mod', captured.protected_role_names)
        self.assertEqual(captured.members[0].roles[1].name, 'Inactive')
        with self.assertRaises(FrozenInstanceError):
            captured.guild_id = 1


class WorkerPolicyTests(unittest.TestCase):
    def load(self, members, *, registered=(), recent=(), blocked=(), teams=()):
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers,
            '_database_state',
            return_value=(set(registered), set(recent), set(blocked), tuple(teams)),
        ):
            return workers._load_preview(request(members=members))

    def test_7_30_60_rules_and_pending_incomplete_block(self):
        result = self.load(
            (
                member_snapshot(1, joined_days=8),
                member_snapshot(2, joined_days=6),
                member_snapshot(3, joined_days=31),
                member_snapshot(4, joined_days=29),
                member_snapshot(5, joined_days=90),
                member_snapshot(6, joined_days=90),
            ),
            registered=(3, 4, 5, 6),
            recent=(5,),
            blocked=(6,),
        )
        self.assertEqual(result.candidate_ids, (3, 1))
        reasons = {row.member_id: row.reason for row in result.decisions}
        self.assertIn('fewer than 7', reasons[2])
        self.assertIn('fewer than 30', reasons[4])
        self.assertIn('tracked game', reasons[5])
        self.assertIn('pending or incomplete', reasons[6])

    def test_current_team_and_starter_roles_are_allowed_but_unknown_managed_and_staff_protect(self):
        base = (role_snapshot(1, '@everyone'), role_snapshot(99, 'Inactive'))
        result = self.load(
            (
                member_snapshot(1, roles=base + (role_snapshot(3, 'The Ronin'),)),
                member_snapshot(2, roles=base + (role_snapshot(4, 'Newbie'),)),
                member_snapshot(3, roles=base + (role_snapshot(5, 'VIP'),)),
                member_snapshot(4, roles=base + (role_snapshot(6, 'Integration', managed=True),)),
                member_snapshot(5, roles=base + (role_snapshot(7, 'Mod'),)),
            ),
            teams=('The Ronin',),
        )
        self.assertEqual(result.candidate_ids, (1, 2))
        rows = {row.member_id: row for row in result.decisions}
        self.assertTrue(rows[1].has_team_role)
        self.assertIn('unrecognized', rows[3].reason)
        self.assertIn('managed', rows[4].reason)
        self.assertIn('staff', rows[5].reason)

    def test_action_cap_and_typed_count(self):
        loaded = preview(decisions=tuple(decision(i) for i in range(30)))
        self.assertEqual(len(loaded.action_candidates), 25)
        self.assertEqual(loaded.deferred_candidate_count, 5)
        self.assertEqual(loaded.confirmation_text, 'KICK 25')

    def test_permission_and_audit_transaction(self):
        with self.assertRaises(workers.InactiveKickPermissionError):
            workers._load_preview(request(requester_is_mod=False))
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        logs = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
        audit_request = workers.InactiveKickAuditRequest(
            guild_id=300,
            actor_id=10,
            actor_description='Actor (`10`)',
            rows=(workers.KickAuditRow(20, 'First'), workers.KickAuditRow(21, 'Second')),
        )
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=connection
        ), mock.patch.object(
            workers.models.db, 'atomic', return_value=atomic
        ), mock.patch.object(
            workers.models.GameLog, 'write', side_effect=logs
        ) as write:
            result = workers._write_kick_audit(audit_request)
        self.assertEqual(result.log_ids, (11, 12))
        self.assertEqual(write.call_count, 2)
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_audit_failure_rolls_back_graph(self):
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.db, 'atomic', return_value=atomic
        ), mock.patch.object(
            workers.models.GameLog,
            'write',
            side_effect=[SimpleNamespace(id=1), peewee.OperationalError('audit failed')],
        ):
            with self.assertRaises(peewee.OperationalError):
                workers._write_kick_audit(workers.InactiveKickAuditRequest(
                    300, 10, 'Actor',
                    (workers.KickAuditRow(20, 'One'), workers.KickAuditRow(21, 'Two')),
                ))
        atomic.__exit__.assert_called_once()


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, *, guild, actor, channel=None):
        return SimpleNamespace(
            guild=guild,
            user=actor,
            channel=channel or SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_wrong_typed_confirmation_has_no_effect(self):
        inactive = FakeRole(99, 'Inactive')
        actor = FakeMember(10, 'Actor', roles=(FakeRole(2, 'Mod'),))
        target = FakeMember(20, 'Target', roles=(FakeRole(1, '@everyone'), inactive))
        guild = FakeGuild(members=(actor, target), roles=(inactive,))
        with mock.patch.object(service, 'access_error', return_value=None):
            with self.assertRaisesRegex(workers.InactiveKickError, 'exactly'):
                await service._execute(
                    self.interaction(guild=guild, actor=actor),
                    preview(decisions=(decision(20),)),
                    'KICK 2',
                )
        self.assertEqual(target.kick_calls, [])

    async def test_changed_candidates_refresh_without_effects(self):
        actor = FakeMember(10, 'Actor')
        guild = FakeGuild(members=(actor,), roles=(FakeRole(99, 'Inactive'),))
        prior = preview(decisions=(decision(20),))
        refreshed = preview(decisions=(decision(21),))
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=refreshed)
                ):
            outcome = await service._execute(
                self.interaction(guild=guild, actor=actor), prior, 'KICK 1'
            )
        self.assertEqual(outcome.state, 'refreshed')

    async def test_dm_failure_is_nonfatal_kick_failures_continue_and_audit_successes(self):
        everyone = FakeRole(1, '@everyone')
        inactive = FakeRole(99, 'Inactive')
        mod = FakeRole(2, 'Mod')
        actor = FakeMember(10, 'Actor', roles=(everyone, mod))
        first = FakeMember(
            20, 'First', roles=(everyone, inactive),
            dm_error=discord.DiscordException('closed'),
        )
        second = FakeMember(
            21, 'Second', roles=(everyone, inactive),
            kick_error=discord.DiscordException('denied'),
        )
        guild = FakeGuild(members=(actor, first, second), roles=(inactive, mod))
        current = preview(decisions=(decision(20), decision(21)))
        interaction = self.interaction(guild=guild, actor=actor)
        audit = workers.InactiveKickAuditResult(log_ids=(100,))
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=current)
                ), mock.patch.object(
                    service.workers, 'record_kicks', new=mock.AsyncMock(return_value=audit)
                ) as record:
            outcome = await service._execute(interaction, current, 'KICK 2')
        self.assertEqual(outcome.state, 'complete')
        self.assertEqual(outcome.kicked_count, 1)
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(outcome.dm_failed_count, 1)
        self.assertEqual(len(first.kick_calls), 1)
        self.assertEqual(len(second.kick_calls), 1)
        self.assertEqual(len(record.call_args.args[0].rows), 1)
        interaction.channel.send.assert_awaited_once()
        self.assertIn('**1** removed', interaction.channel.send.call_args.args[0])

    async def test_live_unknown_role_skips_and_all_skips_remain_retryable(self):
        inactive = FakeRole(99, 'Inactive')
        unknown = FakeRole(5, 'VIP')
        actor = FakeMember(10, 'Actor')
        target = FakeMember(20, 'Target', roles=(inactive, unknown))
        guild = FakeGuild(members=(actor, target), roles=(inactive, unknown))
        current = preview(decisions=(decision(20),))
        interaction = self.interaction(guild=guild, actor=actor)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=current)
                ):
            outcome = await service._execute(interaction, current, 'KICK 1')
        self.assertEqual(outcome.state, 'retryable')
        self.assertEqual(outcome.skipped_count, 1)
        self.assertEqual(target.kick_calls, [])
        interaction.channel.send.assert_not_awaited()

    async def test_audit_failure_is_terminal_reconciliation_after_kick(self):
        everyone = FakeRole(1, '@everyone')
        inactive = FakeRole(99, 'Inactive')
        actor = FakeMember(10, 'Actor')
        target = FakeMember(20, 'Target', roles=(everyone, inactive))
        guild = FakeGuild(members=(actor, target), roles=(inactive,))
        current = preview(decisions=(decision(20),))
        interaction = self.interaction(guild=guild, actor=actor)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=current)
                ), mock.patch.object(
                    service.workers,
                    'record_kicks',
                    new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
                ):
            outcome = await service._execute(interaction, current, 'KICK 1')
        self.assertEqual(outcome.state, 'reconciliation')
        self.assertEqual(outcome.audit_failed_count, 1)
        self.assertIn('Do not retry', outcome.private_message)
        interaction.channel.send.assert_awaited_once()

    async def test_execution_is_single_flight_and_cancellation_drains(self):
        started = asyncio.Event()
        release = asyncio.Event()
        completed = False

        async def blocked(_interaction, previous, _confirmation):
            nonlocal completed
            started.set()
            await release.wait()
            completed = True
            return service.InactiveKickConfirmationOutcome(
                state='complete',
                preview=previous,
                private_message='done',
                kicked_count=1,
            )

        interaction = SimpleNamespace()
        current = preview(decisions=(decision(20),))
        with mock.patch.object(service, '_execute', side_effect=blocked):
            first = asyncio.create_task(
                service.confirm_and_publish(interaction, current, 'KICK 1')
            )
            await started.wait()
            with self.assertRaises(workers.InactiveKickBusyError):
                await service.confirm_and_publish(
                    interaction, current, 'KICK 1'
                )
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertTrue(completed)
        self.assertTrue(workers.claim_execution())
        workers.release_execution()


class ViewAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_serializes_preview_and_typed_confirmation_button(self):
        workspace = views.InactiveKickWorkspace(
            result=preview(decisions=(
                decision(20),
                decision(21, eligible=False, reason='protected role'),
            )),
            requester_id=10,
            confirmer=mock.AsyncMock(),
        )
        payload = workspace.to_components()
        self.assertTrue(payload)
        buttons = [
            item for item in workspace.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        self.assertEqual(
            {button.label for button in buttons},
            {'Continue to typed confirmation', 'Cancel'},
        )
        modal = views.KickConfirmationModal(workspace)
        self.assertEqual(modal.confirmation.placeholder, 'KICK 1')

    async def test_native_defers_and_publishes_private_workspace(self):
        command = league_root().get_command('maintenance').get_command(
            'kick-inactive'
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        loaded = preview()
        cog = league.league.__new__(league.league)
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(
                    service, 'load_preview', new=mock.AsyncMock(return_value=loaded)
                ), mock.patch.object(
                    views, 'publish_private', new=mock.AsyncMock()
                ) as publish:
            returned = await command.callback(cog, interaction)
        self.assertEqual(returned, loaded)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        publish.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
