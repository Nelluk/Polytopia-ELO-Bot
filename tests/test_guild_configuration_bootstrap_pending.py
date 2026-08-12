"""Focused offline coverage for the P11.5C bootstrap-pending latch."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import importlib
import os
import sys
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase

from modules import guild_configuration_bootstrap as bootstrap
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    document_digest,
    document_to_mapping,
    validate_document,
)
from tests import test_guild_configuration_storage as fixtures
import settings


def import_offline_runtime(module_name):
    with mock.patch.dict(
        os.environ, {'POLYBOT_ENV': 'development'}, clear=False,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'connect', return_value=True,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'close', return_value=True,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'create_tables',
    ), mock.patch.object(
        SchemaManager, 'create_foreign_key',
    ):
        return importlib.import_module(module_name)


bot_module = import_offline_runtime('bot')
GUILD_ID = storage.DEVELOPMENT_BETA_GUILD_ID


def bootstrap_plan():
    discord_snapshot = copy.deepcopy(fixtures.snapshot())
    discord_snapshot['guilds'][0]['guild_name'] = 'Fresh Development Guild'
    return bootstrap.build_first_guild_plan(
        target=fixtures.target(),
        allowed_guild_ids=(GUILD_ID,),
        discord_snapshot=discord_snapshot,
    )


def audit_summary(events, *, total_count, max_event_number):
    return {
        'total_count': total_count,
        'max_event_number': max_event_number,
        'relevant_count': len(events),
        'relevant': copy.deepcopy(events),
    }


def bootstrap_row():
    plan = bootstrap_plan()
    details = {
        'template': storage.FIRST_GUILD_BOOTSTRAP_TEMPLATE,
        'guild_name': plan.guild_name,
        'source_digest': plan.source_digest,
        'application_commands_synchronized': False,
    }
    return (
        GUILD_ID,
        storage.STORAGE_SCHEMA_VERSION,
        'active',
        1,
        1,
        1,
        plan.document.schema_version,
        document_to_mapping(plan.document),
        plan.document_digest,
        plan.source_digest,
        None,
        'owner_activation',
        storage.FIRST_GUILD_BOOTSTRAP_ACTOR,
        audit_summary(
            [[
                1,
                storage.FIRST_GUILD_BOOTSTRAP_EVENT_TYPE,
                1,
                1,
                plan.document_digest,
                storage.FIRST_GUILD_BOOTSTRAP_ACTOR,
                details,
            ]],
            total_count=1,
            max_event_number=1,
        ),
    )


def import_audit_summary(*, total_count=1, max_event_number=1):
    imported = fixtures.bundle().imports[0]
    return audit_summary(
        [[
            1,
            storage.IMPORT_EVENT_TYPE,
            1,
            1,
            imported.document_digest,
            storage.IMPORT_ACTOR,
            {'source_digest': imported.source_digest},
        ]],
        total_count=total_count,
        max_event_number=max_event_number,
    )


def nonpending_rows():
    imported = fixtures.bundle().imports[0]
    changed_mapping = copy.deepcopy(document_to_mapping(imported.document))
    changed_mapping['identity']['display_name'] = 'Activated Development Guild'
    changed = validate_document(changed_mapping)

    def row(*, state, revision, generation, document, parent, source_kind, actor,
            total_count, max_event_number, evidence=None):
        return (
            GUILD_ID,
            storage.STORAGE_SCHEMA_VERSION,
            state,
            revision,
            generation,
            revision,
            document.schema_version,
            document_to_mapping(document),
            document_digest(document),
            imported.source_digest,
            parent,
            source_kind,
            actor,
            import_audit_summary(
                total_count=total_count,
                max_event_number=max_event_number,
            ) if evidence is None else evidence,
        )

    return {
        'enrollment': row(
            state='active', revision=1, generation=1,
            document=imported.document, parent=None,
            source_kind=storage.IMPORT_SOURCE_KIND,
            actor=storage.IMPORT_ACTOR, total_count=1, max_event_number=1,
        ),
        'existing_activation': row(
            state='active', revision=2, generation=2,
            document=changed, parent=1, source_kind='owner_activation',
            actor='discord:owner', total_count=2, max_event_number=2,
        ),
        'rollback': row(
            state='active', revision=3, generation=3,
            document=imported.document, parent=2, source_kind='rollback',
            actor='discord:owner', total_count=3, max_event_number=3,
        ),
        'suspend': row(
            state='suspended', revision=1, generation=2,
            document=imported.document, parent=None,
            source_kind=storage.IMPORT_SOURCE_KIND,
            actor=storage.IMPORT_ACTOR, total_count=2, max_event_number=2,
        ),
        'resume': row(
            state='active', revision=1, generation=3,
            document=imported.document, parent=None,
            source_kind=storage.IMPORT_SOURCE_KIND,
            actor=storage.IMPORT_ACTOR, total_count=3, max_event_number=3,
        ),
    }


def runtime_from_stored(stored):
    return runtime.build_runtime_snapshot_from_stored(
        stored_configurations=(stored,),
        discord_snapshot=fixtures.snapshot(),
        allowed_guild_ids=(GUILD_ID,),
    )


def changed_runtime_snapshot(current):
    old = current.guilds[GUILD_ID]
    changed_mapping = copy.deepcopy(document_to_mapping(old.document))
    changed_mapping['identity']['display_name'] = 'Confirmed First Configuration'
    changed_document = validate_document(changed_mapping)
    candidate = replace(
        old,
        revision=old.revision + 1,
        generation=old.generation + 1,
        document=changed_document,
        document_digest=document_digest(changed_document),
        bootstrap_pending=False,
        parent_revision=old.revision,
        source_kind='owner_activation',
    )
    return runtime.GuildConfigurationRuntimeSnapshot(
        source='database',
        guilds=MappingProxyType({GUILD_ID: candidate}),
        legacy_config=current.legacy_config,
        command_policy=current.command_policy,
    )


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.one = None
        self.statement = ''

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _parameters=None):
        self.statement = statement
        if statement == 'SHOW transaction_read_only':
            self.one = ('on',)
        elif statement == 'SELECT current_database(), current_user':
            self.one = ('polytopia_dev', 'polybot_dev')

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.cursor_value = _Cursor(rows)
        self.closed = False

    def set_session(self, **_values):
        pass

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def exact_inventory():
    return storage.SchemaInventory(
        tuple(sorted(storage.STORAGE_TABLES)),
        storage.EXPECTED_COLUMNS,
        storage.EXPECTED_CONSTRAINTS,
    )


def database_profile():
    return SimpleNamespace(
        environment='development',
        database_name='polytopia_dev',
        database_user='polybot_dev',
        database_password='secret',
        database_host='localhost',
        database_port=5432,
        expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False,
        api_enabled=False,
        bullet_enabled=False,
        allowed_guild_ids=(GUILD_ID,),
        guild_configuration_source='database',
    )


class BootstrapPendingStoredGraphTests(unittest.TestCase):
    def test_stored_query_selects_revision_provenance_and_bounded_audit_evidence(self):
        cursor = _Cursor(())
        shadow._load_rows(cursor)
        statement = cursor.statement
        self.assertIn(
            f'FROM "{storage.AUDIT_TABLE}"',
            statement,
        )
        self.assertIn(
            'revision.parent_revision, revision.source_kind, revision.actor',
            statement,
        )
        self.assertIn('bootstrap_audit_evidence', statement)
        self.assertNotIn('{storage.', statement)

    def test_real_stored_loader_and_runtime_preserve_pending_latch(self):
        connection = _Connection((bootstrap_row(),))
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            stored = shadow.inspect_active_configuration(
                shadow.active_request_from_profile(database_profile())
            )
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0].bootstrap_pending)
        published = runtime_from_stored(stored[0])
        self.assertEqual(published.bootstrap_pending_guild_ids, (GUILD_ID,))
        self.assertTrue(published.guilds[GUILD_ID].bootstrap_pending)
        self.assertTrue(connection.closed)

    def test_malformed_or_conflicting_first_bootstrap_evidence_fails_closed(self):
        cases = {}
        missing = list(bootstrap_row())
        missing[13] = audit_summary([], total_count=0, max_event_number=0)
        cases['missing'] = tuple(missing)

        conflicting = list(bootstrap_row())
        conflicting[13] = audit_summary(
            conflicting[13]['relevant'], total_count=2, max_event_number=2,
        )
        cases['conflicting_count'] = tuple(conflicting)

        forged_actor = list(bootstrap_row())
        forged_actor[13]['relevant'][0][5] = 'discord:forged'
        cases['forged_actor'] = tuple(forged_actor)

        forged_details = list(bootstrap_row())
        forged_details[13]['relevant'][0][6]['template'] = 'not-operator-only'
        cases['forged_details'] = tuple(forged_details)

        for name, row in cases.items():
            with self.subTest(name=name), self.assertRaises(
                shadow.GuildConfigurationShadowMalformed,
            ):
                shadow._stored_values((row,))

    def test_import_enrollment_existing_activation_suspend_resume_and_rollback_are_not_pending(self):
        for name, row in nonpending_rows().items():
            with self.subTest(name=name):
                stored = shadow._stored_values((row,))[0]
                self.assertFalse(stored.bootstrap_pending)


class BootstrapPendingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings._database_guild_configuration_quarantine,
            settings.config,
            settings.application_command_policy,
        )
        settings.guild_configuration_source = 'database'
        settings._database_guild_configuration = None
        settings._database_guild_configuration_quarantine = frozenset()
        settings.config = MappingProxyType({})
        settings.application_command_policy = None

    def tearDown(self):
        (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings._database_guild_configuration_quarantine,
            settings.config,
            settings.application_command_policy,
        ) = self.original

    def current_snapshot(self):
        return runtime_from_stored(shadow._stored_values((bootstrap_row(),))[0])

    def test_initial_publication_keeps_owner_config_recovery_but_denies_dispatch(self):
        current = self.current_snapshot()
        settings.activate_database_guild_configuration(current)
        self.assertTrue(settings.guild_configuration_ready())
        self.assertTrue(
            settings.database_guild_configuration_bootstrap_pending(GUILD_ID)
        )
        self.assertFalse(settings.guild_configuration_allows_dispatch(GUILD_ID))

    def test_reconcile_alone_cannot_clear_but_changed_document_activation_can(self):
        current = self.current_snapshot()
        settings.activate_database_guild_configuration(current)
        old = current.guilds[GUILD_ID]
        same_document = runtime.GuildConfigurationRuntimeSnapshot(
            source='database',
            guilds=MappingProxyType({
                GUILD_ID: replace(old, bootstrap_pending=False),
            }),
            legacy_config=current.legacy_config,
            command_policy=current.command_policy,
        )
        expected_current = {
            GUILD_ID: (old.revision, old.generation, old.document_digest),
        }
        with self.assertRaisesRegex(RuntimeError, 'changed document'):
            settings.reconcile_database_guild_configuration(
                same_document,
                expected_current=expected_current,
                activated_guild_id=GUILD_ID,
                expected_activation=(old.revision, old.generation, old.document_digest),
            )
        self.assertTrue(
            settings.database_guild_configuration_bootstrap_pending(GUILD_ID)
        )

        changed = changed_runtime_snapshot(current)
        candidate = changed.guilds[GUILD_ID]
        settings.reconcile_database_guild_configuration(
            changed,
            expected_current=expected_current,
            activated_guild_id=GUILD_ID,
            expected_activation=(
                candidate.revision,
                candidate.generation,
                candidate.document_digest,
            ),
        )
        self.assertFalse(
            settings.database_guild_configuration_bootstrap_pending(GUILD_ID)
        )
        self.assertTrue(settings.guild_configuration_allows_dispatch(GUILD_ID))

    def test_failed_publication_quarantine_and_restart_remain_fail_closed(self):
        current = self.current_snapshot()
        settings.activate_database_guild_configuration(current)
        changed = changed_runtime_snapshot(current)
        old = current.guilds[GUILD_ID]
        candidate = changed.guilds[GUILD_ID]
        with self.assertRaisesRegex(RuntimeError, 'activation evidence'):
            settings.reconcile_database_guild_configuration(
                changed,
                expected_current={
                    GUILD_ID: (old.revision, old.generation, old.document_digest),
                },
                activated_guild_id=GUILD_ID,
                expected_activation=(
                    candidate.revision,
                    candidate.generation,
                    'f' * 64,
                ),
            )
        self.assertIs(settings.database_guild_configuration(GUILD_ID), old)
        self.assertFalse(settings.guild_configuration_allows_dispatch(GUILD_ID))

        settings.quarantine_database_guild_configuration(GUILD_ID)
        self.assertFalse(settings.guild_configuration_allows_dispatch(GUILD_ID))
        settings._database_guild_configuration = None
        settings.config = MappingProxyType({})
        self.assertFalse(settings.guild_configuration_ready())
        self.assertFalse(settings.guild_configuration_allows_dispatch(GUILD_ID))


def interaction(path, *, requester_id, autocomplete=False):
    leaf = {'name': path[-1], 'type': 4 if autocomplete else 1}
    for name in reversed(path[:-1]):
        leaf = {'name': name, 'type': 2, 'options': [leaf]}
    response = SimpleNamespace(
        is_done=lambda: False,
        send_message=mock.AsyncMock(),
        autocomplete=mock.AsyncMock(),
    )
    return SimpleNamespace(
        guild_id=GUILD_ID,
        user=SimpleNamespace(id=requester_id),
        data=leaf,
        type=(
            bot_module.discord.InteractionType.autocomplete
            if autocomplete else None
        ),
        response=response,
        followup=SimpleNamespace(send=mock.AsyncMock()),
    )


class BootstrapPendingDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings._database_guild_configuration_quarantine,
            settings.config,
            settings.application_command_policy,
        )
        settings.guild_configuration_source = 'database'
        settings._database_guild_configuration = None
        settings._database_guild_configuration_quarantine = frozenset()
        current = runtime_from_stored(shadow._stored_values((bootstrap_row(),))[0])
        settings.activate_database_guild_configuration(current)

    def tearDown(self):
        (
            settings.guild_configuration_source,
            settings._database_guild_configuration,
            settings._database_guild_configuration_quarantine,
            settings.config,
            settings.application_command_policy,
        ) = self.original

    async def test_exact_owner_allowlist_and_nonowner_or_unrelated_denial(self):
        owner_id = int(settings.owner_id)
        for path in bot_module.BOOTSTRAP_PENDING_INTERACTION_PATHS:
            with self.subTest(path=path):
                allowed = interaction(path, requester_id=owner_id)
                self.assertTrue(
                    await bot_module.PolyBotCommandTree.interaction_check(
                        None, allowed,
                    )
                )

        for requester_id, path in (
            (owner_id + 1, ('operator', 'guild', 'edit')),
            (owner_id, ('operator', 'guild', 'rollback')),
            (owner_id, ('operator', 'database', 'backup')),
        ):
            with self.subTest(requester_id=requester_id, path=path):
                denied = interaction(path, requester_id=requester_id)
                self.assertFalse(
                    await bot_module.PolyBotCommandTree.interaction_check(
                        None, denied,
                    )
                )
                denied.response.send_message.assert_awaited_once()

        autocomplete = interaction(
            ('operator', 'guild', 'rollback'),
            requester_id=owner_id + 1,
            autocomplete=True,
        )
        self.assertFalse(
            await bot_module.PolyBotCommandTree.interaction_check(None, autocomplete)
        )
        autocomplete.response.autocomplete.assert_awaited_once_with([])

    async def test_prefix_messages_preinvoke_and_listener_events_are_inert(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(id=GUILD_ID, name='Fresh Development Guild'),
            author=SimpleNamespace(name='non-owner'),
        )
        for content in ('$setname New Name', '$opengame'):
            message.content = content
            self.assertEqual(bot_module.get_prefix(None, message), 'fakeprefix')

        class Loop:
            def create_task(self, coroutine):
                coroutine.close()
                return mock.Mock()

        instance = bot_module.init_bot(loop=Loop(), args=[])
        try:
            instance.process_commands = mock.AsyncMock()
            await instance.on_message(message)
            instance.process_commands.assert_not_awaited()

            with mock.patch.object(
                bot_module.importlib, 'import_module',
            ) as import_module, self.assertRaises(bot_module.commands.CheckFailure):
                await instance._before_invoke(
                    SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID))
                )
            import_module.assert_not_called()

            with mock.patch.object(
                bot_module.settings, 'guild_configuration_allows_dispatch',
                return_value=False,
            ), mock.patch.object(
                bot_module.commands.Bot, 'dispatch', autospec=True,
            ) as parent_dispatch:
                instance.dispatch('message', message)
                instance.dispatch(
                    'raw_reaction_add', SimpleNamespace(guild_id=GUILD_ID)
                )
            parent_dispatch.assert_not_called()
        finally:
            await instance.close()

    async def test_pending_startup_persona_mutation_is_suppressed(self):
        instance = bot_module.MyBot()
        persona_module = SimpleNamespace(
            manifest=lambda: SimpleNamespace(guild_id=GUILD_ID),
            revoke_members_on_startup=mock.AsyncMock(return_value=1),
        )
        instance.get_guild = mock.Mock()
        try:
            with mock.patch.object(
                bot_module.settings,
                'runtime_profile',
                SimpleNamespace(environment='development'),
            ), mock.patch.dict(
                sys.modules,
                {'modules.beta_lab_personas': persona_module},
            ):
                self.assertEqual(await instance._revoke_beta_lab_personas(), 0)
        finally:
            await instance.close()
        instance.get_guild.assert_not_called()
        persona_module.revoke_members_on_startup.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
