"""Focused coverage for the targetable owner guild console."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from modules import guild_configuration_storage as storage
from modules import operator_guild_configuration_workers as workers
from modules import operator_guild_console_views as views
from tests import test_guild_configuration_storage as fixtures


OWNER_ID = 101
GUILD_ID = fixtures.GUILD_ID


def record(index: int, *, state: str = 'active'):
    document = fixtures.bundle().imports[0].document
    return workers.GuildConfigurationRecord(
        guild_id=GUILD_ID + index,
        storage_schema_version=storage.STORAGE_SCHEMA_VERSION,
        enrollment_state=state,
        active_revision=3,
        generation=4,
        updated_at='2026-08-28T12:00:00+00:00',
        document_digest='a' * 64,
        source_digest='b' * 64,
        document=document,
    )


def registry(records):
    return workers.GuildConfigurationReadResult(
        operation=workers.LIST,
        guild_id=GUILD_ID,
        records=tuple(records),
    )


class GuildRegistryConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_42_guilds_are_paginated_below_component_limits(self):
        async def runner(*_args):
            return None

        workspace = views.GuildRegistryConsole(
            requester_id=OWNER_ID,
            result=registry(record(index) for index in range(42)),
            runner=runner,
        )
        self.assertEqual(workspace.page_count, 3)
        selects = [
            item for item in workspace.walk_children()
            if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual(len(selects[0].options), views.PAGE_SIZE)
        self.assertLessEqual(workspace.total_children_count, 40)

    async def test_selected_guild_drives_five_contextual_actions(self):
        called = []

        async def runner(_interaction, action, guild_id):
            called.append((action, guild_id))

        target = record(1)
        workspace = views.GuildRegistryConsole(
            requester_id=OWNER_ID,
            result=registry((target,)),
            runner=runner,
        )
        workspace.selected_guild_id = target.guild_id
        workspace.rebuild()
        buttons = {
            item.label: item
            for item in workspace.walk_children()
            if isinstance(item, discord.ui.Button) and item.label
        }
        for label in (
            'Validate', 'History', 'Suspend', 'Managers', 'Repair commands',
        ):
            self.assertIn(label, buttons)
            self.assertFalse(buttons[label].disabled)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID),
            guild_id=GUILD_ID,
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        await workspace._action(interaction, views.HISTORY)
        self.assertEqual(called, [(views.HISTORY, target.guild_id)])

    async def test_suspended_guild_offers_resume_and_history_only(self):
        async def runner(*_args):
            return None

        target = record(1, state='suspended')
        workspace = views.GuildRegistryConsole(
            requester_id=OWNER_ID,
            result=registry((target,)),
            runner=runner,
        )
        workspace.selected_guild_id = target.guild_id
        workspace.rebuild()
        buttons = {
            item.label: item
            for item in workspace.walk_children()
            if isinstance(item, discord.ui.Button) and item.label
        }
        self.assertFalse(buttons['Resume'].disabled)
        self.assertFalse(buttons['History'].disabled)
        self.assertTrue(buttons['Validate'].disabled)
        self.assertTrue(buttons['Managers'].disabled)
        self.assertTrue(buttons['Repair commands'].disabled)


class GuildHistoryWorkspaceTests(unittest.TestCase):
    def test_history_exposes_earlier_revision_restore_in_same_workflow(self):
        selected = record(1)
        revisions = tuple(
            workers.GuildConfigurationRevisionSummary(
                revision_number=value,
                parent_revision=value - 1 if value > 1 else None,
                document_digest=str(value) * 64,
                source_kind='owner_activation',
                actor='discord:101',
                created_at=f'2026-08-2{value}T12:00:00+00:00',
            )
            for value in (3, 2, 1)
        )
        result = workers.GuildConfigurationReadResult(
            operation=workers.HISTORY,
            guild_id=selected.guild_id,
            records=(selected,),
            selected=selected,
            revisions=revisions,
        )

        async def rollback(*_args):
            return None

        workspace = views.GuildHistoryWorkspace(
            requester_id=OWNER_ID,
            result=result,
            rollback_runner=rollback,
        )
        select = next(
            item for item in workspace.walk_children()
            if isinstance(item, discord.ui.Select)
        )
        self.assertEqual([option.value for option in select.options], ['2', '1'])
        self.assertLessEqual(workspace.total_children_count, 40)


if __name__ == '__main__':
    unittest.main()
