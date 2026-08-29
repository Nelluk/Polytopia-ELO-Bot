"""Focused offline coverage for the P8.3 team-image workflow."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

import discord
from PIL import Image
import peewee

from tests.test_image_storage import image_bytes
from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.team_image_workers')
service = import_offline_runtime('modules.team_image')
team_emoji_workers = import_offline_runtime('modules.team_emoji_workers')
administration = import_offline_runtime('modules.administration')
from modules import image_storage


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

    def is_null(self, value):
        return Condition(
            lambda record: (getattr(record, self.name) is None) == bool(value)
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

    def distinct(self):
        return self

    def __iter__(self):
        return iter(self.records)


class FakeDatabase:
    def __init__(self):
        self.team = None
        self.events = []
        self.logs = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.connection_threads = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_save = False
        self.fail_audit = False

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                database.connection_threads.append(threading.get_ident())
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
                self.old_image_url = database.team.image_url
                self.old_logs = list(database.logs)
                database.events.append('atomic-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.rollbacks += 1
                database.team.image_url = self.old_image_url
                database.logs = list(self.old_logs)
                database.events.append('rollback')
                return False

        return AtomicContext()


class TeamRecord:
    def __init__(
        self,
        database,
        *,
        team_id=42,
        name='Ronin',
        guild_id=300,
        image_url=None,
        is_hidden=False,
        is_archived=False,
    ):
        self.database = database
        self.id = team_id
        self.name = name
        self.guild_id = guild_id
        self.image_url = image_url
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
    responses = {}

    @classmethod
    def get_by_name(cls, team_name, guild_id, **kwargs):
        del guild_id, kwargs
        return cls.responses.get(team_name, (cls.record,))

    @classmethod
    def get_by_id(cls, team_id):
        if cls.record is None or int(cls.record.id) != int(team_id):
            raise peewee.DoesNotExist
        return cls.record

    @classmethod
    def select(cls, *fields):
        del fields
        return Query([cls.record] if cls.record is not None else [])


class FakeGameLog:
    database = None

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('audit failed')
        cls.database.logs.append(kwargs)


class FakeAttachment:
    def __init__(self, data, reported_size=None, error=None):
        self.data = data
        self.size = len(data) if reported_size is None else reported_size
        self.error = error
        self.read_calls = 0

    async def read(self):
        self.read_calls += 1
        if self.error:
            raise self.error
        return self.data


def read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        team_lookup='Ronin',
        requester_description='**Mod** (`100`)',
        invoked_with='/team image',
    )
    values.update(overrides)
    return workers.TeamImageReadRequest(**values)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        team_id=42,
        operation=workers.TEAM_IMAGE_LOCAL,
        image_url=None,
        staged_path=None,
        expected_image_url=None,
        expected_local_digest=None,
        requester_description='**Mod** (`100`)',
        ignored_url=False,
        native=True,
        invoked_with='/team image',
    )
    values.update(overrides)
    return workers.TeamImageMutationRequest(**values)


class TeamImageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = FakeDatabase()
        self.team = TeamRecord(self.database)
        self.database.team = self.team
        FakeTeamModel.record = self.team
        FakeTeamModel.responses = {}
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'Team', FakeTeamModel)
        )
        self.patches.enter_context(
            mock.patch.object(workers.models, 'GameLog', FakeGameLog)
        )
        self.patches.enter_context(
            mock.patch.object(image_storage, 'IMAGE_ROOT', Path(self.tempdir.name))
        )
        self.patches.enter_context(
            mock.patch.object(service.settings, 'guild_setting', return_value=True)
        )
        self.patches.enter_context(
            mock.patch.object(service.settings, 'is_mod', return_value=True)
        )
        image_storage.ensure_image_directories()
        self.addCleanup(self.patches.close)
        self.addCleanup(self.tempdir.cleanup)

    def stage(self, data=None, team_id=42):
        return image_storage.stage_normalised_image(
            data or image_bytes(),
            'team',
            team_id,
        )

    def test_pcplus_image_mutation_reloads_polychampions_team(self):
        pcplus = workers.team_record_scope.PCPLUS_GUILD_ID
        polychampions = workers.team_record_scope.POLYCHAMPIONS_GUILD_ID
        self.team.guild_id = polychampions

        loaded = workers._reload_team(mutation_request(guild_id=pcplus))

        self.assertIs(loaded, self.team)
        self.team.guild_id = pcplus
        with self.assertRaisesRegex(
            workers.TeamImageLookupError,
            'does not belong',
        ):
            workers._reload_team(mutation_request(guild_id=pcplus))

    def test_immutable_requests_worker_connection_and_effective_precedence(self):
        original = image_bytes('PNG', size=(21, 21))
        image_storage._normalise_image(
            original,
            image_storage.team_image_path(self.team.id),
        )
        visible_before = image_storage.team_image_path(self.team.id).read_bytes()
        self.team.image_url = 'https://example.com/team.png'
        request = read_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999

        result = asyncio.run(workers.run_team_image_read(request))

        self.assertEqual(result.effective_source, workers.TEAM_IMAGE_LOCAL)
        self.assertEqual(result.local_image_bytes, visible_before)
        self.assertEqual(
            result.local_digest,
            hashlib.sha256(
                image_storage.team_image_path(self.team.id).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertNotEqual(self.database.connection_threads[0], threading.get_ident())
        self.assertNotIn('TeamRecord', repr(request))

    def test_url_and_no_image_reads_are_effective_and_public(self):
        self.team.image_url = 'https://example.com/team.png'
        url_result = workers.read_team_image(read_request())
        self.assertEqual(url_result.effective_source, workers.TEAM_IMAGE_URL)

        self.team.image_url = None
        empty_result = workers.read_team_image(read_request())
        self.assertEqual(empty_result.effective_source, 'none')

        actor = service.TeamImageActor(100, '<@100>', '**Mod** (`100`)')
        sent = []

        async def send(content, **kwargs):
            sent.append((content, kwargs))

        asyncio.run(service.publish_read(url_result, send=send, actor=actor))
        asyncio.run(service.publish_read(empty_result, send=send, actor=actor))
        self.assertIn('<https://example.com/team.png>', sent[0][0])
        self.assertIn('Requested by <@100>', sent[0][0])
        self.assertIn('does not have an image set', sent[1][0])

    def test_mutation_is_atomic_audited_and_does_not_publish_filesystem_inside_transaction(self):
        original = image_bytes('PNG', size=(18, 18))
        destination = image_storage.team_image_path(self.team.id)
        image_storage._normalise_image(original, destination)
        visible_before = destination.read_bytes()
        self.team.image_url = 'https://example.com/old.png'
        staged = self.stage(image_bytes('JPEG', size=(12, 9), mode='RGB'))
        request = mutation_request(
            staged_path=staged.path,
            expected_image_url=self.team.image_url,
            expected_local_digest=hashlib.sha256(
                destination.read_bytes()
            ).hexdigest(),
        )

        result = workers.set_team_image(request)

        self.assertEqual(result.old_image_url, 'https://example.com/old.png')
        self.assertIsNone(result.image_url)
        self.assertIsNone(self.team.image_url)
        self.assertEqual(destination.read_bytes(), visible_before)
        self.assertTrue(Path(staged.path).exists())
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.database.logs[0]['guild_id'], 300)
        self.assertLess(
            self.database.events.index('commit'),
            self.database.events.index('connection-close'),
        )

    def test_db_failure_rolls_back_and_service_cleans_staged_file(self):
        original = image_bytes()
        destination = image_storage.team_image_path(self.team.id)
        image_storage._normalise_image(original, destination)
        visible_before = destination.read_bytes()
        staged = self.stage(image_bytes('JPEG', size=(15, 15), mode='RGB'))
        self.database.fail_save = True
        request = mutation_request(
            staged_path=staged.path,
            expected_local_digest=hashlib.sha256(
                destination.read_bytes()
            ).hexdigest(),
        )

        with self.assertRaises(peewee.PeeweeException):
            asyncio.run(service.run_mutation(request, staged=staged))

        self.assertEqual(destination.read_bytes(), visible_before)
        self.assertTrue(destination.exists())
        self.assertFalse(Path(staged.path).exists())
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.logs, [])

    def test_success_commits_before_publishing_and_replaces_visible_file(self):
        original = image_bytes()
        destination = image_storage.team_image_path(self.team.id)
        image_storage._normalise_image(original, destination)
        staged = self.stage(image_bytes('JPEG', size=(16, 8), mode='RGB'))
        request = mutation_request(
            staged_path=staged.path,
            expected_local_digest=hashlib.sha256(
                destination.read_bytes()
            ).hexdigest(),
        )
        original_publish = image_storage.publish_staged_image

        def publish(*args):
            self.database.events.append('publish')
            self.assertEqual(self.database.commits, 1)
            self.assertIsNone(self.team.image_url)
            return original_publish(*args)

        with mock.patch.object(
            image_storage,
            'publish_staged_image',
            side_effect=publish,
        ):
            result = asyncio.run(service.run_mutation(request, staged=staged))

        self.assertEqual(result.operation, workers.TEAM_IMAGE_LOCAL)
        self.assertEqual(destination.read_bytes(), staged.data)
        self.assertFalse(Path(staged.path).exists())
        self.assertLess(
            self.database.events.index('commit'),
            self.database.events.index('publish'),
        )

    def test_publication_failure_keeps_db_commit_old_file_and_recoverable_stage(self):
        original = image_bytes()
        destination = image_storage.team_image_path(self.team.id)
        image_storage._normalise_image(original, destination)
        visible_before = destination.read_bytes()
        staged = self.stage(image_bytes('JPEG', size=(17, 11), mode='RGB'))
        request = mutation_request(
            staged_path=staged.path,
            expected_local_digest=hashlib.sha256(
                destination.read_bytes()
            ).hexdigest(),
        )

        with mock.patch.object(
            image_storage,
            'publish_staged_image',
            side_effect=image_storage.ImageStorageError('disk full'),
        ), self.assertRaises(service.TeamImagePublicationError) as raised:
            asyncio.run(service.run_mutation(request, staged=staged))

        self.assertEqual(self.database.commits, 1)
        self.assertIsNone(self.team.image_url)
        self.assertEqual(destination.read_bytes(), visible_before)
        self.assertTrue(Path(staged.path).exists())
        self.assertIn('disk full', str(raised.exception))

    def test_url_and_clear_remove_every_local_override(self):
        original = image_bytes()
        destination = image_storage.team_image_path(self.team.id)
        image_storage._normalise_image(original, destination)
        self.team.image_url = 'https://example.com/old.png'
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()

        url_result = asyncio.run(
            service.run_mutation(
                mutation_request(
                    operation=workers.TEAM_IMAGE_URL,
                    image_url='https://example.com/new.png',
                    expected_image_url=self.team.image_url,
                    expected_local_digest=digest,
                )
            )
        )
        self.assertEqual(url_result.image_url, 'https://example.com/new.png')
        self.assertFalse(destination.exists())

        clear_result = asyncio.run(
            service.run_mutation(
                mutation_request(
                    operation=workers.TEAM_IMAGE_CLEAR,
                    expected_image_url='https://example.com/new.png',
                    expected_local_digest=None,
                )
            )
        )
        self.assertEqual(clear_result.operation, workers.TEAM_IMAGE_CLEAR)
        self.assertIsNone(self.team.image_url)
        self.assertFalse(destination.exists())

    def test_validation_attachment_rules_conflict_and_download_failure(self):
        oversized = FakeAttachment(
            image_bytes(),
            reported_size=image_storage.MAX_UPLOAD_BYTES + 1,
        )
        with self.assertRaises(image_storage.ImageStorageError):
            asyncio.run(service.stage_attachment(oversized, team_id=42))
        self.assertEqual(oversized.read_calls, 0)

        corrupt = FakeAttachment(b'not an image')
        with self.assertRaises(image_storage.ImageStorageError):
            asyncio.run(service.stage_attachment(corrupt, team_id=42))
        self.assertEqual(corrupt.read_calls, 1)

        failed_download = FakeAttachment(
            b'',
            error=RuntimeError('download failed'),
        )
        with self.assertRaises(service.TeamImageDownloadError):
            asyncio.run(service.stage_attachment(failed_download, team_id=42))

        with self.assertRaises(workers.TeamImageValidationError):
            workers.set_team_image(
                mutation_request(
                    operation=workers.TEAM_IMAGE_CLEAR,
                    staged_path='leftover.stage',
                )
            )

    def test_native_permission_conflict_is_private_before_defer_and_success_identifies_actor(self):
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('image')
        response = SimpleNamespace(send_message=mock.AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            response=response,
        )
        cog = SimpleNamespace()

        with mock.patch.object(
            administration.settings,
            'guild_setting',
            return_value=False,
        ):
            asyncio.run(command.callback(cog, interaction, 'Ronin', None, False))
        response.send_message.assert_awaited_once_with(
            'Teams are not enabled on this server.',
            ephemeral=True,
        )

        actor = service.TeamImageActor(100, '<@100>', '**Mod** (`100`)')
        result = workers.TeamImageMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            operation=workers.TEAM_IMAGE_CLEAR,
            old_image_url='https://example.com/old.png',
            image_url=None,
            old_local_digest=None,
            ignored_url=False,
            native=True,
        )
        sent = []

        async def send(content, **kwargs):
            sent.append((content, kwargs))

        asyncio.run(
            service.publish_mutation_result(
                result,
                send=send,
                actor=actor,
            )
        )
        self.assertIn('<@100>', sent[0][0])
        self.assertIn('cleared the image', sent[0][0])

    def test_native_read_uses_shared_target_and_publishes_public_actor_attributed_result(self):
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('image')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
            ),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = workers.TeamImageReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            image_url='https://example.com/team.png',
            effective_source=workers.TEAM_IMAGE_URL,
            local_image_bytes=None,
            local_digest=None,
        )

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(return_value=result),
        ) as run_read:
            returned = asyncio.run(
                command.callback(SimpleNamespace(), interaction, 'Ronin', None, False)
            )

        self.assertIs(returned, result)
        run_read.assert_awaited_once()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('<@100>', interaction.channel.send.await_args.args[0])
        self.assertIn('<https://example.com/team.png>', interaction.channel.send.await_args.args[0])

    def test_native_clear_publishes_only_after_committed_mutation(self):
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('image')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
            ),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        current = workers.TeamImageReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            image_url='https://example.com/team.png',
            effective_source=workers.TEAM_IMAGE_URL,
            local_image_bytes=None,
            local_digest=None,
        )
        cleared = workers.TeamImageMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            operation=workers.TEAM_IMAGE_CLEAR,
            old_image_url=current.image_url,
            image_url=None,
            old_local_digest=None,
            ignored_url=False,
            native=True,
        )

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            administration.team_image_service,
            'run_mutation',
            new=mock.AsyncMock(return_value=cleared),
        ) as run_mutation:
            returned = asyncio.run(
                command.callback(SimpleNamespace(), interaction, 'Ronin', None, True)
            )

        self.assertIs(returned, cleared)
        run_mutation.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('cleared the image', interaction.channel.send.await_args.args[0])
        self.assertIn('<@100>', interaction.channel.send.await_args.args[0])

    def test_native_publication_failure_warns_publicly_with_actor_attribution(self):
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('image')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        current = workers.TeamImageReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            image_url='https://example.com/team.png',
            effective_source=workers.TEAM_IMAGE_URL,
            local_image_bytes=None,
            local_digest=None,
        )
        committed = workers.TeamImageMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            operation=workers.TEAM_IMAGE_CLEAR,
            old_image_url=current.image_url,
            image_url=None,
            old_local_digest=None,
            ignored_url=False,
            native=True,
        )
        publication_error = service.TeamImagePublicationError(
            committed,
            detail='disk full',
        )

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            administration.team_image_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=publication_error),
        ):
            returned = asyncio.run(
                command.callback(SimpleNamespace(), interaction, 'Ronin', None, True)
            )

        self.assertIsNone(returned)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        warning = interaction.channel.send.await_args.args[0]
        self.assertIn('<@100>', warning)
        self.assertIn('**Mod** (`100`)', warning)
        self.assertIn('requires reconciliation', warning)
        interaction.followup.send.assert_not_awaited()

    def test_native_publication_warning_uses_ephemeral_fallback_when_public_delivery_fails(self):
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('image')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock(side_effect=RuntimeError('channel unavailable'))),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        current = workers.TeamImageReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            image_url='https://example.com/team.png',
            effective_source=workers.TEAM_IMAGE_URL,
            local_image_bytes=None,
            local_digest=None,
        )
        committed = workers.TeamImageMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            operation=workers.TEAM_IMAGE_CLEAR,
            old_image_url=current.image_url,
            image_url=None,
            old_local_digest=None,
            ignored_url=False,
            native=True,
        )
        publication_error = service.TeamImagePublicationError(
            committed,
            detail='disk full',
        )

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            administration.team_image_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=publication_error),
        ):
            asyncio.run(
                command.callback(SimpleNamespace(), interaction, 'Ronin', None, True)
            )

        warning = interaction.followup.send.await_args.args[0]
        self.assertIn('<@100>', warning)
        self.assertIn('requires reconciliation', warning)
        interaction.followup.send.assert_awaited_once_with(
            warning,
            ephemeral=True,
        )

    def test_prefix_direct_url_routes_through_shared_service_and_preserves_output(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_image'
        )
        send = mock.AsyncMock()
        ctx = SimpleNamespace(
            message=SimpleNamespace(attachments=[]),
            author=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            guild=SimpleNamespace(id=300),
            prefix='$',
            invoked_with='team_image',
            send=send,
        )
        real_run_read = administration.team_image_service.run_read
        real_run_mutation = administration.team_image_service.run_mutation
        url = 'https://example.com/new.png'

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(side_effect=real_run_read),
        ) as run_read, mock.patch.object(
            administration.team_image_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=real_run_mutation),
        ) as run_mutation:
            asyncio.run(command.callback(SimpleNamespace(), ctx, 'Ronin', url))

        run_read.assert_awaited_once()
        run_mutation.assert_awaited_once()
        request = run_mutation.await_args.args[0]
        self.assertEqual(request.operation, workers.TEAM_IMAGE_URL)
        self.assertEqual(request.image_url, url)
        self.assertFalse(request.native)
        self.assertEqual(self.team.image_url, url)
        self.assertEqual(
            send.await_args_list,
            [
                mock.call('Team **Ronin** updated with a direct image URL.'),
                mock.call(url),
            ],
        )

    def test_prefix_attachment_wins_over_url_and_routes_local_publication(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_image'
        )
        send = mock.AsyncMock()
        ctx = SimpleNamespace(
            message=SimpleNamespace(
                attachments=[FakeAttachment(image_bytes('JPEG', size=(13, 9), mode='RGB'))]
            ),
            author=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            guild=SimpleNamespace(id=300),
            prefix='$',
            invoked_with='team_image',
            send=send,
        )
        real_run_read = administration.team_image_service.run_read
        real_run_mutation = administration.team_image_service.run_mutation
        ignored_url = 'https://example.com/ignored.png'

        with mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(side_effect=real_run_read),
        ) as run_read, mock.patch.object(
            administration.team_image_service,
            'run_mutation',
            new=mock.AsyncMock(side_effect=real_run_mutation),
        ) as run_mutation:
            asyncio.run(
                command.callback(SimpleNamespace(), ctx, 'Ronin', ignored_url)
            )

        run_read.assert_awaited_once()
        run_mutation.assert_awaited_once()
        request = run_mutation.await_args.args[0]
        self.assertEqual(request.operation, workers.TEAM_IMAGE_LOCAL)
        self.assertIsNone(request.image_url)
        self.assertTrue(request.ignored_url)
        self.assertFalse(request.native)
        self.assertIsNone(self.team.image_url)
        self.assertTrue(image_storage.team_image_path(self.team.id).exists())
        self.assertEqual(send.await_count, 1)
        self.assertEqual(
            send.await_args.args[0],
            'Team **Ronin** updated with a local image. The supplied URL was ignored.',
        )
        self.assertIn('file', send.await_args.kwargs)

    def test_cancelled_success_drains_commit_and_publication_and_logs_outcome(self):
        staged = self.stage(image_bytes('JPEG', size=(14, 10), mode='RGB'))
        destination = image_storage.team_image_path(self.team.id)
        request = mutation_request(staged_path=staged.path)
        started = threading.Event()
        release = threading.Event()
        original_publish = image_storage.publish_staged_image

        def blocked_publish(*args):
            started.set()
            release.wait(timeout=2)
            self.database.events.append('publish')
            return original_publish(*args)

        async def exercise_cancel():
            with mock.patch.object(
                image_storage,
                'publish_staged_image',
                side_effect=blocked_publish,
            ):
                task = asyncio.create_task(
                    service.run_mutation(request, staged=staged)
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with self.assertLogs(service.logger, level='INFO') as logs:
            asyncio.run(exercise_cancel())

        self.assertEqual(self.database.commits, 1)
        self.assertLess(
            self.database.events.index('commit'),
            self.database.events.index('publish'),
        )
        self.assertEqual(destination.read_bytes(), staged.data)
        self.assertFalse(Path(staged.path).exists())
        self.assertIn(
            'committed and filesystem publication completed',
            '\n'.join(logs.output),
        )

    def test_event_loop_stays_responsive_during_blocking_stage_and_cancellation_cleans_stage(self):
        started = threading.Event()
        release = threading.Event()
        original_stage = image_storage.stage_normalised_image

        def blocked_stage(*args):
            started.set()
            release.wait(timeout=2)
            return original_stage(*args)

        async def exercise_stage():
            attachment = FakeAttachment(image_bytes())
            with mock.patch.object(
                image_storage,
                'stage_normalised_image',
                side_effect=blocked_stage,
            ):
                task = asyncio.create_task(
                    service.stage_attachment(attachment, team_id=42)
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                await asyncio.sleep(0)
                release.set()
                return await task

        staged = asyncio.run(exercise_stage())
        self.assertTrue(Path(staged.path).exists())
        image_storage.cleanup_staged_image(staged.path)

        started.clear()
        release.clear()

        async def cancel_stage():
            attachment = FakeAttachment(image_bytes())
            with mock.patch.object(
                image_storage,
                'stage_normalised_image',
                side_effect=blocked_stage,
            ):
                task = asyncio.create_task(
                    service.stage_attachment(attachment, team_id=42)
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(cancel_stage())
        self.assertEqual(
            list(image_storage.image_root().joinpath('teams').glob('*.stage')),
            [],
        )

        staged = self.stage()
        started.clear()
        release.clear()

        def blocked_failure(request):
            started.set()
            release.wait(timeout=2)
            raise RuntimeError('worker stopped')

        async def exercise_cancel():
            request = mutation_request(staged_path=staged.path)
            with mock.patch.object(workers, 'set_team_image', blocked_failure):
                task = asyncio.create_task(
                    service.run_mutation(request, staged=staged)
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(exercise_cancel())
        self.assertFalse(Path(staged.path).exists())


class TeamImagePrefixRegistrationTests(unittest.TestCase):
    def test_legacy_prefix_command_remains_registered_with_original_signature(self):
        commands_by_name = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertIn('team_image', commands_by_name)
        command = commands_by_name['team_image']
        self.assertEqual(
            list(command.clean_params),
            ['team_name', 'image_url'],
        )

    def test_more_than_one_attachment_is_rejected_before_any_worker(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_image'
        )
        send = mock.AsyncMock()
        ctx = SimpleNamespace(
            message=SimpleNamespace(attachments=[object(), object()]),
            send=send,
        )
        asyncio.run(command.callback(SimpleNamespace(), ctx, 'Ronin', None))
        send.assert_awaited_once_with('Please attach exactly one image.')

    def test_lookup_example_uses_team_image_command_name(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_image'
        )
        send = mock.AsyncMock()
        ctx = SimpleNamespace(
            message=SimpleNamespace(attachments=[]),
            author=SimpleNamespace(id=100, display_name='Mod', mention='<@100>'),
            guild=SimpleNamespace(id=300),
            prefix='$',
            send=send,
        )
        with mock.patch.object(
            administration.team_image_service,
            'build_read_request',
            return_value=read_request(),
        ), mock.patch.object(
            administration.team_image_service,
            'run_read',
            new=mock.AsyncMock(
                side_effect=workers.TeamImageLookupError('Team was not found.')
            ),
        ):
            asyncio.run(command.callback(SimpleNamespace(), ctx, 'Missing', None))

        message = send.await_args.args[0]
        self.assertIn('Example: `$team_image name http://url_to_image.png`', message)
        self.assertNotIn('team_emoji', message)


if __name__ == '__main__':
    unittest.main()
