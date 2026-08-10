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
        self.fail_player_save = False
        self.fail_preference_clear = False
        self.players = []

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
                    database.team.house,
                    list(database.logs),
                )
                self.player_snapshot = [
                    (player, player.team)
                    for player in database.players
                ]
                self.preference_snapshot = list(FakePlayerHousePreference.calls)
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
                    database.team.house,
                    logs,
                ) = self.snapshot
                database.logs = list(logs)
                for player, team in self.player_snapshot:
                    player.team = team
                FakePlayerHousePreference.calls = list(self.preference_snapshot)
                database.events.append('rollback')
                return False

        return AtomicContext()


class HouseRecord:
    def __init__(self, name, house_id=601):
        self.id = house_id
        self.name = name


class FakeHouseModel:
    id = Field('id')
    name = Field('name')
    records = []

    @classmethod
    def select(cls, *fields):
        return Query(cls.records)

    @classmethod
    def get_or_except(cls, *, house_name):
        matches = [
            house for house in cls.records
            if str(house_name).lower() in str(house.name).lower()
        ]
        if not matches:
            raise workers.exceptions.NoMatches(
                f'No matching house was found for "{house_name}"'
            )
        if len(matches) > 1:
            raise workers.exceptions.TooManyMatches(
                f'More than one matching house was found for "{house_name}"'
            )
        return matches[0]


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


class PlayerRecord:
    def __init__(self, database, *, player_id=501, team=None):
        self.database = database
        self.id = player_id
        self.team = team

    def save(self):
        self.database.events.append('player-save')
        if self.database.fail_player_save:
            raise peewee.OperationalError('player save failed')


class FakePlayerModel:
    records = {}

    @classmethod
    def get_or_except(cls, *, player_string, guild_id):
        del guild_id
        try:
            return cls.records[int(player_string)]
        except KeyError as exc:
            raise workers.exceptions.NoMatches(
                f'No matching player was found for "{player_string}"'
            ) from exc


class FakePlayerHousePreference:
    calls = []
    database = None

    @classmethod
    def clear_preferences(cls, player_id):
        cls.calls.append(int(player_id))
        cls.database.events.append('clear-preferences')
        if cls.database.fail_preference_clear:
            raise peewee.OperationalError('preference clear failed')


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


def house_read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=False,
        team_enabled=True,
        league_scope=True,
        team_lookup='Ronin',
        attribute=workers.TEAM_ATTRIBUTE_HOUSE,
        requester_description='**Member** (`100`)',
        include_hidden=False,
        invoked_with='/team house',
    )
    values.update(overrides)
    return workers.TeamAttributeReadRequest(**values)


def house_mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        league_scope=True,
        team_lookup='Ronin',
        attribute=workers.TEAM_ATTRIBUTE_HOUSE,
        house='Ninjas',
        requester_description='**Mod** (`100`)',
        include_hidden=False,
        expected_team_id=42,
        expected_value='Ninjas',
        expected_value_present=True,
        team_role_id=700,
        team_role_name='Ronin',
        invoked_with='/team house',
        native=True,
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
        FakePlayerModel.records = {}
        FakePlayerHousePreference.calls = []
        FakePlayerHousePreference.database = self.database
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
        self.patches.enter_context(
            mock.patch.object(workers.models, 'Player', FakePlayerModel)
        )
        self.patches.enter_context(
            mock.patch.object(
                workers.models,
                'PlayerHousePreference',
                FakePlayerHousePreference,
            )
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

    def test_house_read_is_public_in_scoped_guild_and_skips_mutation_checks(self):
        result = workers.read_team_attribute(house_read_request())
        self.assertEqual(result.house_name, 'Ninjas')
        self.assertEqual(result.value, 'Ninjas')
        self.team.is_archived = True
        archived = workers.read_team_attribute(house_read_request())
        self.assertTrue(archived.is_archived)

        with self.assertRaises(workers.TeamAttributePermissionError):
            workers.read_team_attribute(
                house_read_request(team_enabled=False)
            )
        with self.assertRaises(workers.TeamAttributePermissionError):
            workers.read_team_attribute(
                house_read_request(league_scope=False)
            )
        self.assertEqual(self.database.connection_opened, 4)
        self.assertEqual(self.database.connection_closed, 4)

    def test_house_lookup_rejects_ambiguous_and_infers_only_one_target(self):
        FakeTeamModel.responses['R'] = (
            self.team,
            TeamRecord(self.database, team_id=43, name='Ravens'),
        )
        with self.assertRaises(workers.TeamAttributeLookupError):
            workers.read_team_attribute(house_read_request(team_lookup='R'))

        with mock.patch.object(
            workers.team_emoji_workers,
            '_inferred_team_matches',
            return_value=(self.team,),
        ):
            inferred = workers.read_team_attribute(
                house_read_request(team_lookup=None)
            )
        self.assertEqual(inferred.team_id, 42)
        self.assertEqual(inferred.house_name, 'Ninjas')

        with mock.patch.object(
            workers.team_emoji_workers,
            '_inferred_team_matches',
            return_value=(self.team, TeamRecord(self.database, team_id=43)),
        ), self.assertRaises(workers.TeamAttributeLookupError):
            workers.read_team_attribute(house_read_request(team_lookup=None))

    def test_house_autocomplete_is_bounded_and_scope_gated(self):
        FakeHouseModel.records = [
            HouseRecord(f'House {index:02}', 700 + index)
            for index in range(30)
        ]
        results = workers.team_emoji_workers.list_house_autocomplete(
            workers.team_emoji_workers.HouseAutocompleteRequest(
                guild_id=300,
                current='House',
                team_enabled=True,
                league_scope=True,
                limit=50,
            )
        )
        self.assertEqual(len(results), 25)
        self.assertEqual(results[0].house_name, 'House 00')
        self.assertEqual(
            workers.team_emoji_workers.list_house_autocomplete(
                workers.team_emoji_workers.HouseAutocompleteRequest(
                    guild_id=999,
                    current='',
                    team_enabled=True,
                    league_scope=False,
                    limit=25,
                )
            ),
            (),
        )

    def test_house_assignment_clear_persists_team_and_preferences_atomically(self):
        new_house = HouseRecord('Valkyries', 602)
        FakeHouseModel.records = [self.team.house, new_house]
        player = PlayerRecord(self.database, team=None)
        self.database.players = [player]
        FakePlayerModel.records = {player.id: player}

        assigned = workers.set_team_attribute(
            house_mutation_request(
                house='Valkyries',
                team_member_ids=(player.id,),
            )
        )
        self.assertIs(self.team.house, new_house)
        self.assertEqual(assigned.old_house_name, 'Ninjas')
        self.assertEqual(assigned.house_name, 'Valkyries')
        self.assertEqual(assigned.value, 'Valkyries')
        self.assertEqual(assigned.persisted_member_ids, (player.id,))
        self.assertIs(player.team, self.team)
        self.assertEqual(FakePlayerHousePreference.calls, [player.id])
        self.assertIn('audit', self.database.events)

        cleared = workers.set_team_attribute(
            house_mutation_request(
                house=None,
                clear=True,
                expected_value='Valkyries',
                team_member_ids=(player.id,),
            )
        )
        self.assertIsNone(self.team.house)
        self.assertTrue(cleared.cleared)
        self.assertIsNone(cleared.house_name)
        self.assertIsNone(cleared.value)
        self.assertEqual(self.database.commits, 2)
        self.assertEqual(len(self.database.logs), 2)

    def test_house_stale_missing_and_archived_mutations_fail_without_change(self):
        with self.assertRaises(workers.TeamAttributeConflictError):
            workers.set_team_attribute(
                house_mutation_request(expected_value='Different')
            )
        self.assertEqual(self.team.house.name, 'Ninjas')

        FakeHouseModel.records = [self.team.house]
        with self.assertRaises(workers.TeamAttributeLookupError):
            workers.set_team_attribute(
                house_mutation_request(house='Missing')
            )
        self.assertEqual(self.team.house.name, 'Ninjas')

        self.team.is_archived = True
        with self.assertRaises(workers.TeamAttributeValidationError):
            workers.set_team_attribute(house_mutation_request())
        self.assertEqual(self.team.house.name, 'Ninjas')

        FakeTeamModel.responses['Missing Team'] = ()
        with self.assertRaises(workers.TeamAttributeLookupError):
            workers.set_team_attribute(
                house_mutation_request(team_lookup='Missing Team')
            )

    def test_house_audit_failure_rolls_back_team_member_and_preference_state(self):
        new_house = HouseRecord('Valkyries', 602)
        FakeHouseModel.records = [self.team.house, new_house]
        player = PlayerRecord(self.database, team=None)
        self.database.players = [player]
        FakePlayerModel.records = {player.id: player}
        self.database.fail_audit = True

        with self.assertRaises(peewee.PeeweeException):
            workers.set_team_attribute(
                house_mutation_request(
                    house='Valkyries',
                    team_member_ids=(player.id,),
                )
            )
        self.assertEqual(self.team.house.name, 'Ninjas')
        self.assertIsNone(player.team)
        self.assertEqual(FakePlayerHousePreference.calls, [])
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_house_player_or_preference_failure_rolls_back_everything(self):
        for failure_flag in ('fail_player_save', 'fail_preference_clear'):
            with self.subTest(failure_flag=failure_flag):
                new_house = HouseRecord('Valkyries', 602)
                FakeHouseModel.records = [self.team.house, new_house]
                player = PlayerRecord(self.database, team=None)
                self.database.players = [player]
                FakePlayerModel.records = {player.id: player}
                setattr(self.database, failure_flag, True)

                with self.assertRaises(peewee.PeeweeException):
                    workers.set_team_attribute(
                        house_mutation_request(
                            house='Valkyries',
                            team_member_ids=(player.id,),
                        )
                    )

                self.assertEqual(self.team.house.name, 'Ninjas')
                self.assertIsNone(player.team)
                self.assertEqual(FakePlayerHousePreference.calls, [])
                setattr(self.database, failure_flag, False)

    def test_real_worker_reads_all_attributes_without_mutation_only_fields(self):
        expected = {
            workers.TEAM_ATTRIBUTE_NAME: 'Ronin',
            workers.TEAM_ATTRIBUTE_SERVER: None,
            workers.TEAM_ATTRIBUTE_TIER: 2,
        }
        for attribute, value in expected.items():
            result = workers.read_team_attribute(
                read_request(
                    attribute=attribute,
                    include_hidden=(attribute != workers.TEAM_ATTRIBUTE_TIER),
                    league_scope=True,
                )
            )
            self.assertEqual(result.value, value)
            self.assertEqual(result.attribute, attribute)

    def test_tier_worker_reconciles_persisted_member_team_and_preferences(self):
        player = PlayerRecord(self.database, team=None)
        self.database.players = [player]
        FakePlayerModel.records = {player.id: player}

        result = workers.set_team_attribute(
            mutation_request(
                attribute=workers.TEAM_ATTRIBUTE_TIER,
                tier='Gold',
                include_hidden=False,
                league_scope=True,
                expected_value=2,
                expected_value_present=True,
                team_role_id=700,
                team_role_name='Ronin',
                team_member_ids=(player.id,),
            )
        )

        self.assertIs(player.team, self.team)
        self.assertEqual(FakePlayerHousePreference.calls, [player.id])
        self.assertEqual(result.persisted_member_ids, (player.id,))
        self.assertEqual(result.persisted_member_failures, ())
        self.assertIn('player-save', self.database.events)
        self.assertIn('clear-preferences', self.database.events)

        old_team = SimpleNamespace(id=88)
        player.team = old_team
        self.database.fail_audit = True
        with self.assertRaises(peewee.PeeweeException):
            workers.set_team_attribute(
                mutation_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    tier='Silver',
                    include_hidden=False,
                    expected_value=2,
                    expected_value_present=True,
                    team_role_id=700,
                    team_role_name='Ronin',
                    team_member_ids=(player.id,),
                )
            )
        self.assertIs(player.team, old_team)
        self.assertEqual(FakePlayerHousePreference.calls, [player.id])

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
                read_request(
                    attribute=workers.TEAM_ATTRIBUTE_TIER,
                    include_hidden=False,
                    league_scope=False,
                )
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

    def test_house_access_is_public_for_reads_but_mod_only_for_mutations(self):
        with mock.patch.object(
            service,
            '_team_enabled',
            return_value=True,
        ), mock.patch.object(
            service,
            '_league_scope',
            return_value=True,
        ), mock.patch.object(
            service,
            '_requester_is_mod',
            return_value=False,
        ):
            self.assertIsNone(
                service.native_access_error(
                    self.member,
                    300,
                    workers.TEAM_ATTRIBUTE_HOUSE,
                )
            )
            self.assertEqual(
                service.native_access_error(
                    self.member,
                    300,
                    workers.TEAM_ATTRIBUTE_HOUSE,
                    mutation=True,
                ),
                'You do not have permission to manage team houses.',
            )

        with mock.patch.object(service, '_team_enabled', return_value=True), \
                mock.patch.object(service, '_league_scope', return_value=False):
            self.assertEqual(
                service.native_access_error(
                    self.member,
                    300,
                    workers.TEAM_ATTRIBUTE_HOUSE,
                ),
                'Team houses can only be viewed or managed in the '
                'PolyChampions league server.',
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

    def _house_interaction(self):
        return SimpleNamespace(
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

    def _house_command(self):
        return next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('house')

    def _house_result(self):
        return workers.TeamAttributeMutationResult(
            guild_id=300,
            team_id=42,
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
            team_name='Ronin',
            old_team_name='Ronin',
            new_team_name='Ronin',
            old_value='Ninjas',
            value='Valkyries',
            old_tier=2,
            new_tier=2,
            old_tier_name='Gold',
            new_tier_name='Gold',
            old_house_name='Ninjas',
            house_name='Valkyries',
            team_role_id=700,
            house_role_names=('Ninjas', 'Valkyries'),
            cleared=False,
            native=True,
        )

    async def test_house_read_is_public_and_actor_attributed(self):
        interaction = self._house_interaction()
        command = self._house_command()
        result = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
            value='Ninjas',
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=True,
            house_role_names=('Ninjas',),
        )
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value=None,
        ) as access, mock.patch.object(
            administration.team_attributes_service,
            'build_read_request',
            return_value=SimpleNamespace(),
        ) as build, mock.patch.object(
            administration.team_attributes_service,
            'run_read',
            new=mock.AsyncMock(return_value=result),
        ) as run_read:
            await command.callback(cog, interaction, None, None, False)

        access.assert_called_once_with(
            interaction.user,
            300,
            workers.TEAM_ATTRIBUTE_HOUSE,
            mutation=False,
        )
        build.assert_called_once_with(
            member=interaction.user,
            guild_id=300,
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
            team_lookup=None,
            invoked_with='/team house',
        )
        run_read.assert_awaited_once()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('Ninjas', interaction.channel.send.await_args.args[0])
        self.assertIn('<@100>', interaction.channel.send.await_args.args[0])

    async def test_house_mutation_conflict_and_mod_denial_stay_private(self):
        command = self._house_command()
        cog = administration.administration.__new__(administration.administration)

        denied = self._house_interaction()
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value='private denial',
        ):
            await command.callback(cog, denied, None, 'Valkyries', False)
        denied.response.send_message.assert_awaited_once_with(
            'private denial',
            ephemeral=True,
        )
        denied.response.defer.assert_not_awaited()
        denied.channel.send.assert_not_awaited()

        conflicting = self._house_interaction()
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value=None,
        ):
            await command.callback(cog, conflicting, None, 'Valkyries', True)
        conflicting.response.send_message.assert_awaited_once_with(
            'Choose either a House or `clear`, not both.',
            ephemeral=True,
        )
        conflicting.response.defer.assert_not_awaited()
        conflicting.channel.send.assert_not_awaited()

    async def test_house_reconciles_only_after_commit_and_publishes_actor(self):
        interaction = self._house_interaction()
        command = self._house_command()
        cog = administration.administration.__new__(administration.administration)
        result = self._house_result()
        events = []
        preflight = SimpleNamespace(
            current=SimpleNamespace(team_id=42, value='Ninjas'),
            team_role_id=700,
            team_role_name='Ronin',
            member_ids=(101, 102),
        )

        async def commit(_request):
            events.append('commit')
            return result

        async def reconcile(_guild, _result):
            events.append('reconcile')
            return SimpleNamespace(warning=None)

        async def publish(*_args, **_kwargs):
            events.append('publish')

        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_attributes_service,
            'run_house_preflight',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            administration.team_attributes_service,
            'build_mutation_request',
            side_effect=lambda **kwargs: events.append('build') or SimpleNamespace(**kwargs),
        ) as build, mock.patch.object(
            administration.team_attributes_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=commit),
        ), mock.patch.object(
            administration.team_attributes_service,
            'reconcile_tier_roles',
            new=mock.AsyncMock(side_effect=reconcile),
        ), mock.patch.object(
            administration.team_attributes_service,
            'publish_mutation_result',
            new=mock.AsyncMock(side_effect=publish),
        ):
            await command.callback(cog, interaction, 'Ronin', 'Valkyries', False)

        build.assert_called_once_with(
            member=interaction.user,
            guild_id=300,
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
            team_lookup='Ronin',
            house='Valkyries',
            clear=False,
            expected_team_id=42,
            expected_value='Ninjas',
            expected_value_present=True,
            team_role_id=700,
            team_role_name='Ronin',
            team_member_ids=(101, 102),
            native=True,
            invoked_with='/team house',
        )
        self.assertEqual(events, ['build', 'commit', 'reconcile', 'publish'])

    async def test_house_database_failure_has_no_public_discord_effect(self):
        interaction = self._house_interaction()
        command = self._house_command()
        cog = administration.administration.__new__(administration.administration)
        preflight = SimpleNamespace(
            current=SimpleNamespace(team_id=42, value='Ninjas'),
            team_role_id=700,
            team_role_name='Ronin',
            member_ids=(101,),
        )
        with mock.patch.object(
            administration.team_attributes_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_attributes_service,
            'run_house_preflight',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            administration.team_attributes_service,
            'build_mutation_request',
            return_value=SimpleNamespace(),
        ), mock.patch.object(
            administration.team_attributes_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('failed')),
        ) as run_mutation, mock.patch.object(
            administration.team_attributes_service,
            'reconcile_tier_roles',
            new=mock.AsyncMock(),
        ) as reconcile, mock.patch.object(
            administration.team_attributes_service,
            'publish_mutation_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, interaction, 'Ronin', 'Valkyries', False)

        run_mutation.assert_awaited_once()
        reconcile.assert_not_awaited()
        publish.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            'Team House operation failed and rolled back.',
            ephemeral=True,
        )
        interaction.channel.send.assert_not_awaited()
        interaction.delete_original_response.assert_not_awaited()

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

    async def test_house_reconciliation_survives_missing_house_roles_and_warns_publicly(self):
        team_role = Role(20, 'Ronin')
        old_house = Role(30, 'Ninjas')
        league_role = Role(40, 'League Member')
        tier_roles = [
            Role(50 + number, f'{name} Player')
            for number, name in service.settings.league_tiers
        ]
        member = SimpleNamespace(
            id=101,
            roles=[team_role, old_house],
            edit=mock.AsyncMock(),
        )
        team_role.members = [member]
        guild = SimpleNamespace(
            roles=[team_role, league_role, *tier_roles],
            get_role=lambda role_id: team_role if role_id == team_role.id else None,
        )
        result = self._house_result()

        reconciliation = await service.reconcile_tier_roles(guild, result)

        self.assertEqual(reconciliation.attribute, workers.TEAM_ATTRIBUTE_HOUSE)
        self.assertEqual(reconciliation.attempted, 1)
        self.assertEqual(reconciliation.updated, 1)
        self.assertIn('Ninjas', reconciliation.missing_role_names)
        self.assertIn('Valkyries', reconciliation.missing_role_names)
        self.assertIn('team house affiliation', reconciliation.warning)
        edited_roles = member.edit.await_args.kwargs['roles']
        self.assertNotIn(old_house, edited_roles)
        self.assertIn(league_role, edited_roles)
        self.assertNotIn('Valkyries', [role.name for role in edited_roles])

        send = mock.AsyncMock()
        await service.publish_mutation_result(
            result,
            send=send,
            actor=service.capture_actor(self.member),
            reconciliation=reconciliation,
        )
        self.assertEqual(send.await_count, 2)
        self.assertIn('<@100>', send.await_args_list[1].args[0])

    def test_reconciliation_warning_is_bounded_for_house_role_and_member_lists(self):
        warning = service.TierRoleReconciliation(
            team_id=42,
            attempted=20,
            updated=0,
            failed_member_ids=tuple(range(100, 120)),
            missing_role_names=tuple(f'House {index}' for index in range(20)),
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
        ).warning
        self.assertIn('House 0', warning)
        self.assertIn('House 9', warning)
        self.assertNotIn('House 10', warning)
        self.assertIn('and 10 more', warning)
        self.assertIn('team house affiliation', warning)

    async def test_tier_read_keeps_scope_and_skips_mutation_preconditions(self):
        with mock.patch.object(
            service,
            '_requester_is_mod',
            return_value=True,
        ):
            self.assertEqual(
                service.native_access_error(
                    self.member,
                    999,
                    workers.TEAM_ATTRIBUTE_TIER,
                ),
                'Team tiers can only be managed in the PolyChampions league server.',
            )
        allowed_guild_id = int(service.settings.server_ids['polychampions'])
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=allowed_guild_id),
            user=self.member,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = workers.TeamAttributeReadResult(
            guild_id=allowed_guild_id,
            team_id=42,
            team_name='Archived Ronin',
            attribute=workers.TEAM_ATTRIBUTE_TIER,
            value=2,
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name=None,
            is_hidden=False,
            is_archived=True,
            house_role_names=(),
        )
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('tier')
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.team_attributes_service,
            '_requester_is_mod',
            return_value=True,
        ), mock.patch.object(
            administration.team_attributes_service,
            'build_read_request',
            return_value=SimpleNamespace(),
        ) as build, mock.patch.object(
            administration.team_attributes_service,
            'run_read',
            new=mock.AsyncMock(return_value=result),
        ) as run_read, mock.patch.object(
            administration.team_attributes_service,
            'run_tier_preflight',
            new=mock.AsyncMock(side_effect=AssertionError('read used mutation preflight')),
        ) as preflight:
            await command.callback(cog, interaction, None, None)
        build.assert_called_once_with(
            member=interaction.user,
            guild_id=allowed_guild_id,
            attribute=workers.TEAM_ATTRIBUTE_TIER,
            team_lookup=None,
            invoked_with='/team tier',
        )
        run_read.assert_awaited_once()
        preflight.assert_not_awaited()

    async def test_tier_mutation_preflight_captures_only_role_member_ids(self):
        current = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_TIER,
            value=2,
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=False,
            house_role_names=('Ninjas',),
        )
        role = SimpleNamespace(
            id=700,
            name='Ronin',
            members=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
        )
        with mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            service,
            '_exact_team_role',
            return_value=role,
        ):
            preflight = await service.run_tier_preflight(
                member=self.member,
                guild=SimpleNamespace(id=300),
                team_lookup='Ronin',
                invoked_with='team_tier',
            )
        self.assertEqual(preflight.member_ids, (101, 102))

    async def test_house_mutation_preflight_captures_only_role_member_ids(self):
        current = workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=workers.TEAM_ATTRIBUTE_HOUSE,
            value='Ninjas',
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name='Ninjas',
            is_hidden=False,
            is_archived=False,
            house_role_names=('Ninjas',),
        )
        role = SimpleNamespace(
            id=700,
            name='Ronin',
            members=[SimpleNamespace(id=101), SimpleNamespace(id=102)],
        )
        with mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            service,
            '_exact_team_role',
            return_value=role,
        ):
            preflight = await service.run_house_preflight(
                member=self.member,
                guild=SimpleNamespace(id=300),
                team_lookup='Ronin',
                invoked_with='/team house',
            )
        self.assertEqual(preflight.member_ids, (101, 102))

        with mock.patch.object(
            service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            service,
            '_exact_team_role',
            side_effect=workers.TeamTierRoleError('missing role'),
        ), self.assertRaises(workers.TeamTierRoleError):
            await service.run_house_preflight(
                member=self.member,
                guild=SimpleNamespace(id=300),
                team_lookup='Ronin',
                invoked_with='/team house',
            )


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
        self.assertNotIn('team_edit', league_commands)
        self.assertIn('team_tier', league_commands)
        self.assertEqual(league_commands['team_tier'].aliases, [])
        self.assertNotIn('team_house', league_commands)

    async def test_prefix_tier_preserves_legacy_league_cog_scope(self):
        cog = league.league.__new__(league.league)
        outside_scope = SimpleNamespace(
            guild=SimpleNamespace(id=999),
            invoked_with='team_tier',
        )
        self.assertFalse(await cog.cog_check(outside_scope))
        in_scope = SimpleNamespace(
            guild=SimpleNamespace(
                id=int(league.settings.server_ids['polychampions'])
            ),
            invoked_with='team_tier',
        )
        self.assertTrue(await cog.cog_check(in_scope))

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
            if command.name == 'team_tier'
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
            member_ids=(101, 102),
        )
        result = SimpleNamespace(team_id=42)
        cog = league.league.__new__(league.league)
        with mock.patch.object(
            league.models.Team,
            'get_or_except',
            return_value=team,
        ) as direct_lookup, mock.patch.object(
            league.utilities,
            'guild_role_by_name',
            return_value=role,
        ) as direct_role_lookup, mock.patch.object(
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
            team_member_ids=(101, 102),
            native=False,
            invoked_with='team_tier',
            prefix='$',
        )
        direct_lookup.assert_not_called()
        direct_role_lookup.assert_not_called()
        reconcile.assert_awaited_once_with(ctx.guild, result)
        publish.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
