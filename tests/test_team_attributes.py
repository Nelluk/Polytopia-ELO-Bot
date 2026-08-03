"""Focused offline coverage for the P8.2 team-attribute workflows."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.team_attributes_workers')
service = import_offline_runtime('modules.team_attributes')
team_emoji = import_offline_runtime('modules.team_emoji')
administration = import_offline_runtime('modules.administration')
league = import_offline_runtime('modules.league')


class Condition:
    def __init__(self, predicate):
        self.predicate = predicate

    def __and__(self, other):
        return Condition(
            lambda record: self.predicate(record) and other.predicate(record)
        )


class Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return Condition(lambda record: getattr(record, self.name) == value)

    def __ne__(self, value):
        return Condition(lambda record: getattr(record, self.name) != value)

    def contains(self, value):
        return Condition(
            lambda record: str(value) in str(getattr(record, self.name))
        )


class Query:
    def __init__(self, records):
        self.records = list(records)

    def where(self, condition):
        if isinstance(condition, Condition):
            self.records = [
                record for record in self.records if condition.predicate(record)
            ]
        return self

    def order_by(self, *fields):
        names = [field.name for field in fields]
        self.records.sort(key=lambda record: tuple(
            str(getattr(record, name)) for name in names
        ))
        return self

    def limit(self, amount):
        self.records = self.records[:amount]
        return self

    def exists(self):
        return bool(self.records)

    def __iter__(self):
        return iter(self.records)


class FakeDatabase:
    def __init__(self, team):
        self.team = team
        self.events = []
        self.logs = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_save = False
        self.fail_audit = False

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                database.events.append('connection-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                database.events.append('connection-close')
                return False

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.snapshot = (
                    database.team.name,
                    database.team.external_server,
                    database.team.league_tier,
                    list(database.logs),
                )
                database.events.append('atomic-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.rollbacks += 1
                (
                    database.team.name,
                    database.team.external_server,
                    database.team.league_tier,
                    logs,
                ) = self.snapshot
                database.logs = list(logs)
                database.events.append('rollback')
                return False

        return AtomicContext()


class HouseRecord:
    def __init__(self, name):
        self.name = name


class FakeHouseModel:
    name = Field('name')
    records = []

    @classmethod
    def select(cls, *fields):
        return Query(cls.records)


class TeamRecord:
    def __init__(
        self,
        database,
        *,
        team_id=42,
        name='Ronin',
        guild_id=300,
        external_server=None,
        league_tier=2,
        house=None,
        is_hidden=False,
        is_archived=False,
    ):
        self.database = database
        self.id = team_id
        self.name = name
        self.guild_id = guild_id
        self.external_server = external_server
        self.league_tier = league_tier
        self.house = house or HouseRecord('Ninjas')
        self.is_hidden = is_hidden
        self.is_archived = is_archived

    def save(self):
        self.database.events.append('save')
        if self.database.fail_save:
            raise peewee.OperationalError('save failed')


class FakeTeamModel:
    id = Field('id')
    name = Field('name')
    guild_id = Field('guild_id')
    is_hidden = Field('is_hidden')
    is_archived = Field('is_archived')
    record = None
    records = []
    responses = {}

    @classmethod
    def get_by_name(cls, team_name, guild_id, **kwargs):
        return cls.responses.get(team_name, (cls.record,))

    @classmethod
    def select(cls, *fields):
        return Query(cls.records or ([cls.record] if cls.record else []))


class FakeGameLog:
    database = None

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('audit failed')
        cls.database.logs.append(kwargs)


def read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        league_scope=True,
        team_lookup='Ronin',
        attribute=workers.TEAM_ATTRIBUTE_SERVER,
        requester_description='**Mod** (`100`)',
        include_hidden=True,
        invoked_with='/team server',
    )
    values.update(overrides)
    return workers.TeamAttributeReadRequest(**values)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        league_scope=True,
        team_lookup='Ronin',
        attribute=workers.TEAM_ATTRIBUTE_SERVER,
        server_id=987654,
        requester_description='**Mod** (`100`)',
        include_hidden=True,
        invoked_with='/team server',
        native=False,
    )
    values.update(overrides)
    return workers.TeamAttributeMutationRequest(**values)


class TeamAttributeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase(None)
        self.team = TeamRecord(self.database)
        self.database.team = self.team
        FakeTeamModel.record = self.team
        FakeTeamModel.records = [self.team]
        FakeTeamModel.responses = {}
        FakeHouseModel.records = [self.team.house]
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'Team', FakeTeamModel)
        )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'House', FakeHouseModel)
        )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'GameLog', FakeGameLog)
        )
        self.addCleanup(self.patches.close)

    def test_requests_are_immutable_and_worker_reads_are_guild_scoped(self):
        request = mutation_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999

        self.assertEqual(
            workers.read_team_attribute(read_request()).external_server,
            None,
        )
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

        other_guild = TeamRecord(
            self.database,
            team_id=99,
            name='Other Team',
            guild_id=999,
        )
        hidden = TeamRecord(
            self.database,
            team_id=100,
            name='Hidden Team',
            is_hidden=True,
        )
        archived = TeamRecord(
            self.database,
            team_id=101,
            name='Archived Team',
            is_archived=True,
        )
        visible = [
            TeamRecord(self.database, team_id=200 + index, name=f'Team {index:02}')
            for index in range(30)
        ]
        FakeTeamModel.records = [
            *visible,
            hidden,
            archived,
            other_guild,
        ]
        results = workers.team_emoji_workers.list_team_autocomplete(
            workers.team_emoji_workers.TeamAutocompleteRequest(
                guild_id=300,
                current='Team',
                limit=50,
            )
        )
        self.assertEqual(len(results), 25)
        self.assertTrue(all('Other' not in result.team_name for result in results))
        self.assertTrue(all('Hidden' not in result.team_name for result in results))
        self.assertTrue(all('Archived' not in result.team_name for result in results))

    def test_name_server_and_tier_mutations_are_atomic_and_audited(self):
        renamed = workers.set_team_attribute(
            mutation_request(
                attribute=workers.TEAM_ATTRIBUTE_NAME,
                team_lookup='Ronin',
                name='The Ronin',
                include_hidden=True,
                invoked_with='team_name',
            )
        )
        self.assertEqual(renamed.old_team_name, 'Ronin')
        self.assertEqual(renamed.new_team_name, 'The Ronin')

        server = workers.set_team_attribute(
            mutation_request(
                attribute=workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup='The Ronin',
                server_id=123456,
            )
        )
        self.assertEqual(server.value, 123456)

        cleared = workers.set_team_attribute(
            mutation_request(
                attribute=workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup='The Ronin',
                server_id=None,
                clear=True,
            )
        )
        self.assertTrue(cleared.cleared)
        self.assertIsNone(cleared.value)

        tier = workers.set_team_attribute(
            mutation_request(
                attribute=workers.TEAM_ATTRIBUTE_TIER,
                team_lookup='The Ronin',
                tier='Gold',
                include_hidden=False,
                league_scope=True,
                expected_value=2,
                expected_value_present=True,
                team_role_id=700,
                team_role_name='The Ronin',
            )
        )
        self.assertEqual(tier.new_tier, 2)
        self.assertEqual(tier.new_tier_name, 'Gold')
        self.assertEqual(self.database.commits, 4)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(len(self.database.logs), 4)
        self.assertEqual(self.database.connection_opened, 4)
        self.assertEqual(self.database.connection_closed, 4)

    def test_name_boundary_duplicate_and_tier_lookup_preconditions_rollback(self):
        before_name = self.team.name
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_NAME,
                    name='Tiny',
                )
            )
        self.assertEqual(self.team.name, before_name)

        duplicate = TeamRecord(
            self.database,
            team_id=77,
            name='Another',
        )
        FakeTeamModel.records = [self.team, duplicate]
        FakeTeamModel.responses['Ronin'] = (self.team,)
        with self.assertRaises(workers.TeamAttributeConflictError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_NAME,
                    name='Another',
                )
            )
        self.assertEqual(self.team.name, before_name)

        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    tier='not-a-tier',
                    include_hidden=False,
                    team_role_id=700,
                    team_role_name='Ronin',
                )
            )
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_SERVER,
                    server_id='not-an-integer',
                )
            )
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    tier='Gold',
                    clear=True,
                    include_hidden=False,
                    team_role_id=700,
                    team_role_name='Ronin',
                )
            )

        self.team.house = None
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    tier='Gold',
                    include_hidden=False,
                    team_role_id=700,
                    team_role_name='Ronin',
                )
            )
        self.team.house = HouseRecord('Ninjas')
        self.team.is_archived = True
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    tier='Gold',
                    include_hidden=False,
                    team_role_id=700,
                    team_role_name='Ronin',
                )
            )
        self.assertEqual(self.database.rollbacks, 7)

    def test_permission_inference_conflict_and_failed_audit_are_private(self):
        with self.assertRaises(workers.TeamAttributePermissionError):
            workers.read_team_attribute(read_request(requester_is_mod=False))
        with self.assertRaises(workers.TeamAttributePermissionError):
            workers.read_team_attribute(read_request(team_enabled=False))
        with self.assertRaises(workers.TeamAttributePermissionError):
            workers.read_team_attribute(
                read_request(attribute=workers.TEAM_ATTRIBUTE_TIER, league_scope=False)
            )

        FakeTeamModel.responses['R'] = (
            self.team,
            TeamRecord(self.database, team_id=43, name='Ravens'),
        )
        with self.assertRaises(workers.TeamAttributeLookupError):
            workers.read_team_attribute(read_request(team_lookup='R'))

        with mock.patch.object(
            workers.team_emoji_workers,
            '_inferred_team_matches',
            return_value=(self.team,),
        ):
            inferred = workers.read_team_attribute(read_request(team_lookup=None))
        self.assertEqual(inferred.team_name, 'Ronin')

        with self.assertRaises(workers.TeamAttributeConflictError):
            workers.set_team_attribute(
                mutation_request(
                    expected_value=123,
                    expected_value_present=True,
                )
            )
        self.database.fail_save = True
        with self.assertRaises(peewee.PeeweeException):
            workers.set_team_attribute(mutation_request())
        self.database.fail_save = False
        self.database.fail_audit = True
        with self.assertRaises(peewee.PeeweeException):
            workers.set_team_attribute(mutation_request())
        self.assertEqual(self.team.external_server, None)
        self.assertEqual(self.database.logs, [])
        self.assertEqual(self.database.connection_opened, 8)
        self.assertEqual(self.database.connection_closed, 8)

    def test_worker_keeps_loop_responsive_and_drains_cancelled_mutation(self):
        async def check():
            release = threading.Event()
            executor = ThreadPoolExecutor(max_workers=1)

            def blocked(_request):
                release.wait(1)
                return 'committed'

            try:
                with mock.patch.object(
                    workers.team_emoji_workers,
                    '_team_emoji_executor',
                    executor,
                ), \
                    mock.patch.object(workers, 'set_team_attribute', side_effect=blocked):
                    task = asyncio.create_task(
                        workers.run_team_attribute_mutation(mutation_request())
                    )
                    await asyncio.sleep(0)
                    start = time.monotonic()
                    await asyncio.sleep(0.02)
                    self.assertLess(time.monotonic() - start, 0.2)
                    task.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(task.done())
                    release.set()
                    for _ in range(100):
                        if task.done():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(task.done())
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                executor.shutdown(wait=True)

        asyncio.run(check())


class Role:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class TeamAttributeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=100,
            display_name='Mod',
            name='Mod',
            mention='<@100>',
        )

    async def test_messages_are_public_actor_attributed_and_name_warns_about_role(self):
        actor = service.capture_actor(self.member)
        read = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_SERVER,
            value=123,
            external_server=123,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=False,
            house_role_names=(),
        )
        self.assertIn('<@100>', service.read_message(read, actor=actor))
        result = workers.TeamAttributeMutationResult(
            guild_id=300,
            team_id=42,
            attribute=workers.TEAM_ATTRIBUTE_NAME,
            team_name='The Ronin',
            old_team_name='Ronin',
            new_team_name='The Ronin',
            old_value='Ronin',
            value='The Ronin',
            old_tier=2,
            new_tier=2,
            old_tier_name='Gold',
            new_tier_name='Gold',
            old_house_name='Ninjas',
            house_name='Ninjas',
            team_role_id=None,
            house_role_names=(),
            cleared=False,
            native=True,
        )
        self.assertIn('not renamed automatically', service.native_mutation_message(result, actor=actor))

    async def test_native_denial_is_private_before_defer_and_success_is_public(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=self.member,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = administration.administration.__new__(administration.administration)
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('name')
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value='private denial',
        ):
            await command.callback(cog, interaction, None, None)
        interaction.response.send_message.assert_awaited_once_with(
            'private denial',
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

        interaction.response.send_message.reset_mock()
        interaction.response.defer.reset_mock()
        result = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_NAME,
            value='Ronin',
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=False,
            house_role_names=(),
        )
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_attributes_service,
            'build_read_request',
            return_value=SimpleNamespace(),
        ), mock.patch.object(
            administration.team_attributes_service,
            'run_read',
            new=mock.AsyncMock(return_value=result),
        ):
            await command.callback(cog, interaction, None, None)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('<@100>', interaction.channel.send.await_args.args[0])

    async def test_tier_reconciliation_is_post_commit_and_reports_partial_failures(self):
        old_tier = Role(10, 'Silver Player')
        team_role = Role(20, 'Ronin')
        house_role = Role(30, 'Ninjas')
        league_role = Role(40, 'League Member')
        tier_roles = [
            Role(50 + number, f'{name} Player')
            for number, name in service.settings.league_tiers
        ]
        members = []
        for member_id in (101, 102):
            member = SimpleNamespace(
                id=member_id,
                roles=[team_role, old_tier, house_role],
                edit=mock.AsyncMock(),
            )
            members.append(member)
        members[1].edit.side_effect = RuntimeError('Discord failure')
        team_role.members = members
        guild = SimpleNamespace(
            roles=[team_role, house_role, league_role, *tier_roles],
            get_role=lambda role_id: next(
                (role for role in [team_role, house_role, league_role, *tier_roles]
                 if role.id == role_id),
                None,
            ),
        )
        result = workers.TeamAttributeMutationResult(
            guild_id=300,
            team_id=42,
            attribute=workers.TEAM_ATTRIBUTE_TIER,
            team_name='Ronin',
            old_team_name='Ronin',
            new_team_name='Ronin',
            old_value=3,
            value=2,
            old_tier=3,
            new_tier=2,
            old_tier_name='Silver',
            new_tier_name='Gold',
            old_house_name='Ninjas',
            house_name='Ninjas',
            team_role_id=20,
            house_role_names=('Ninjas',),
            cleared=False,
            native=True,
        )
        reconciliation = await service.reconcile_tier_roles(guild, result)
        self.assertEqual(reconciliation.attempted, 2)
        self.assertEqual(reconciliation.updated, 1)
        self.assertEqual(reconciliation.failed_member_ids, (102,))
        self.assertIn('partial', reconciliation.warning)
        edited_roles = members[0].edit.await_args.kwargs['roles']
        self.assertIn(next(role for role in tier_roles if role.name == 'Gold Player'), edited_roles)
        self.assertIn(league_role, edited_roles)
        self.assertNotIn(old_tier, edited_roles)


class TeamAttributePrefixTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_commands_remain_registered(self):
        commands_by_name = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertIn('team_name', commands_by_name)
        self.assertIn('team_server', commands_by_name)
        league_commands = {
            command.name: command for command in league.league.__cog_commands__
        }
        self.assertEqual(
            league_commands['team_edit'].aliases,
            ['team_house', 'team_tier'],
        )

    async def test_prefix_name_routes_through_shared_mutation_service(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_name'
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            prefix='$',
            invoked_with='team_name',
            send=mock.AsyncMock(),
        )
        result = workers.TeamAttributeMutationResult(
            guild_id=300,
            team_id=42,
            attribute=workers.TEAM_ATTRIBUTE_NAME,
            team_name='New Team',
            old_team_name='Ronin',
            new_team_name='New Team',
            old_value='Ronin',
            value='New Team',
            old_tier=2,
            new_tier=2,
            old_tier_name='Gold',
            new_tier_name='Gold',
            old_house_name='Ninjas',
            house_name='Ninjas',
            team_role_id=None,
            house_role_names=(),
            cleared=False,
            native=False,
        )
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.team_attributes_service,
            'build_mutation_request',
            return_value=SimpleNamespace(),
        ) as build, mock.patch.object(
            administration.team_attributes_service,
            'run_mutation',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.team_attributes_service,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, ctx, 'Ronin', 'New Team')
        build.assert_called_once_with(
            member=ctx.author,
            guild_id=300,
            attribute=workers.TEAM_ATTRIBUTE_NAME,
            team_lookup='Ronin',
            name='New Team',
            native=False,
            invoked_with='team_name',
            prefix='$',
        )

    async def test_prefix_server_read_preserves_public_legacy_wording(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_server'
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            prefix='$',
            invoked_with='team_server',
            send=mock.AsyncMock(),
        )
        result = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_SERVER,
            value=123456,
            external_server=123456,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=False,
            house_role_names=(),
        )
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.team_attributes_service,
            'build_read_request',
            return_value=SimpleNamespace(),
        ) as build, mock.patch.object(
            administration.team_attributes_service,
            'run_read',
            new=mock.AsyncMock(return_value=result),
        ):
            await command.callback(cog, ctx, 'Ronin', None)
        build.assert_called_once_with(
            member=ctx.author,
            guild_id=300,
            attribute=workers.TEAM_ATTRIBUTE_SERVER,
            team_lookup='Ronin',
            invoked_with='team_server',
        )
        ctx.send.assert_awaited_once_with(
            'Team **Ronin** has been assigned an external server of `123456`.'
        )

    async def test_prefix_tier_alias_routes_through_post_commit_service(self):
        command = next(
            command
            for command in league.league.__cog_commands__
            if command.name == 'team_edit'
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300, roles=[]),
            author=SimpleNamespace(id=100),
            prefix='$',
            invoked_with='team_tier',
            send=mock.AsyncMock(),
        )
        team = SimpleNamespace(
            name='Ronin',
            is_archived=False,
            house=SimpleNamespace(name='Ninjas'),
        )
        role = SimpleNamespace(id=700, name='Ronin')
        current = SimpleNamespace(team_id=42, value=3)
        preflight = SimpleNamespace(
            current=current,
            team_role_id=700,
            team_role_name='Ronin',
        )
        result = SimpleNamespace(team_id=42)
        cog = league.league.__new__(league.league)
        with mock.patch.object(
            league.models.Team,
            'get_or_except',
            return_value=team,
        ), mock.patch.object(
            league.utilities,
            'guild_role_by_name',
            return_value=role,
        ), mock.patch.object(
            league.team_attributes_service,
            'run_tier_preflight',
            new=mock.AsyncMock(return_value=preflight),
        ) as preflight_call, mock.patch.object(
            league.team_attributes_service,
            'build_mutation_request',
            return_value=SimpleNamespace(),
        ) as build, mock.patch.object(
            league.team_attributes_service,
            'run_mutation',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            league.team_attributes_service,
            'reconcile_tier_roles',
            new=mock.AsyncMock(return_value=SimpleNamespace(warning=None)),
        ) as reconcile, mock.patch.object(
            league.team_attributes_service,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, ctx, arg='Ronin Gold')
        preflight_call.assert_awaited_once_with(
            member=ctx.author,
            guild=ctx.guild,
            team_lookup='Ronin',
            invoked_with='team_tier',
        )
        build.assert_called_once_with(
            member=ctx.author,
            guild_id=300,
            attribute=workers.TEAM_ATTRIBUTE_TIER,
            team_lookup='Ronin',
            tier='Gold',
            expected_team_id=42,
            expected_value=3,
            expected_value_present=True,
            team_role_id=700,
            team_role_name='Ronin',
            native=False,
            invoked_with='team_tier',
            prefix='$',
        )
        reconcile.assert_awaited_once_with(ctx.guild, result)
        publish.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
