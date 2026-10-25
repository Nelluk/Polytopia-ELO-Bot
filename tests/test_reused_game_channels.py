"""Restarted games retain their history while reconciling roster and access."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock
import asyncio

import discord

from tests.test_game_start_channels import (
    creation, frozen_plan, game_graph, workers,
)


def retained_plan():
    plan = frozen_plan()
    target = replace(
        plan.side_targets[0], channel_id=801, channel_guild_id=700,
    )
    return replace(plan, side_targets=(), reused_targets=(target,))


class ReusedChannelPlanTests(unittest.TestCase):
    def graph(self):
        game = game_graph(game_channel=900)
        game.is_pending = game.is_completed = game.is_confirmed = False
        side = game.ordered_side_list()[0]
        side.team_chan = 801
        side.team_chan_external_server = 700
        return game

    def test_retained_side_and_central_keep_actual_routes_and_current_roster(self):
        game = self.graph()
        # New team routing must not move an existing channel to another server.
        for lineup in game.ordered_side_list()[0].ordered_player_list():
            lineup.player.team.external_server = 701
        plan = workers.freeze_started_channel_plan(game)
        self.assertEqual([t.side_id for t in plan.side_targets], [12])
        side, central = plan.reused_targets
        self.assertEqual((side.channel_id, side.channel_guild_id), (801, 700))
        self.assertEqual(side.preferred_guild_id, 701)
        self.assertEqual((central.channel_id, central.channel_guild_id), (900, 300))
        self.assertEqual([p.discord_member.discord_id for p in central.players],
                         [1001, 1002, 1003, 1004])
        self.assertIsNone(plan.central_target)

    def test_revalidation_rejects_changed_roster_reference_or_game_state(self):
        for change in ('none', 'roster', 'channel', 'guild', 'pending', 'completed', 'confirmed'):
            with self.subTest(change=change):
                game = self.graph()
                plan = workers.freeze_started_channel_plan(game)
                target = plan.reused_targets[0]
                if change == 'roster':
                    game.ordered_side_list()[0].ordered_player_list()[1].player.discord_member.discord_id = 1999
                elif change == 'channel':
                    game.ordered_side_list()[0].team_chan = 802
                elif change == 'guild':
                    game.ordered_side_list()[0].team_chan_external_server = 701
                elif change != 'none':
                    setattr(game, 'is_' + change, True)
                with (
                    mock.patch.object(workers.models.db, 'connection_context', return_value=nullcontext()) as connection,
                    mock.patch.object(workers.models.db, 'atomic', return_value=nullcontext()),
                    mock.patch.object(workers.models.Game, 'load_full_game', return_value=game),
                    mock.patch.object(workers.models.GameSide, 'update') as update,
                ):
                    if change == 'none':
                        workers.validate_reused_channel(plan, target)
                    else:
                        with self.assertRaises(workers.StartedChannelConflictError):
                            workers.validate_reused_channel(plan, target)
                connection.assert_called_once()
                update.assert_not_called()


class ReusedChannelDiscordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = retained_plan()
        self.members = {i: SimpleNamespace(id=i) for i in (1001, 1002, 1999)}
        self.guild = SimpleNamespace(
            id=700, get_member=self.members.get, fetch_member=mock.AsyncMock(),
            fetch_channel=mock.AsyncMock(),
        )
        self.permissions = discord.Permissions.none()
        self.permissions.update(view_channel=True, send_messages=True, read_message_history=True)
        self.overwrite = discord.PermissionOverwrite()
        self.channel = SimpleNamespace(
            id=801, guild=self.guild, type=discord.ChannelType.text,
            name='archive-custom-moderator-name',
            topic='This is the **allied team channel** for game **Old Name**, ID 42.\nOld roster',
            permissions_for=mock.Mock(side_effect=lambda member: self.permissions),
            overwrites_for=mock.Mock(side_effect=lambda member: self.overwrite),
            edit=mock.AsyncMock(), set_permissions=mock.AsyncMock(),
            delete=mock.AsyncMock(), send=mock.AsyncMock(),
        )
        self.guild.fetch_channel.return_value = self.channel
        self.source = SimpleNamespace(id=300, text_channels=tuple(range(490)))
        self.validate = mock.patch.object(workers, 'run_validate_reused_channel', new=mock.AsyncMock())
        self.validator = self.validate.start()
        self.addCleanup(self.validate.stop)
        self.create = mock.patch.object(creation.channels, 'create_game_channel', new=mock.AsyncMock())
        self.creator = self.create.start()
        self.addCleanup(self.create.stop)

    async def run_reuse(self):
        result = await creation.create_started_game_channels(
            plan=self.plan, source_guild=self.source,
            bot_guilds=(self.source, self.guild),
        )
        self.creator.assert_not_awaited()
        self.channel.delete.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        return result

    async def test_reuse_refreshes_topic_without_renaming_recreating_or_resending(self):
        self.plan = replace(self.plan, game=replace(self.plan.game, league_season=23))
        result = await self.run_reuse()
        self.assertEqual(result.warnings, ())
        self.channel.set_permissions.assert_not_awaited()
        kwargs = self.channel.edit.await_args.kwargs
        self.assertEqual(set(kwargs), {'topic', 'reason'})
        self.assertIn('Fields of Fire', kwargs['topic'])
        self.assertIn('One, Two', kwargs['topic'])
        self.assertNotIn('Old roster', kwargs['topic'])
        self.assertIn('retained after', kwargs['topic'])
        self.assertNotIn('self-destruct', kwargs['topic'])
        self.assertLessEqual(len(kwargs['topic']), 1024)
        self.guild.fetch_channel.assert_awaited_once_with(801)

    async def test_minimal_grant_preserves_unrelated_overwrites_and_old_members(self):
        denied = discord.Permissions.none()
        self.channel.permissions_for.side_effect = lambda m: denied if m.id == 1002 else self.permissions
        self.overwrite.update(manage_messages=False, embed_links=False, attach_files=True)
        result = await self.run_reuse()
        self.assertEqual(result.warnings, ())
        self.channel.set_permissions.assert_awaited_once()
        args = self.channel.set_permissions.await_args
        self.assertEqual(args.args[0].id, 1002)
        actual = args.kwargs['overwrite']
        self.assertTrue(actual.view_channel)
        self.assertTrue(actual.send_messages)
        self.assertTrue(actual.read_message_history)
        self.assertFalse(actual.manage_messages)
        self.assertFalse(actual.embed_links)
        self.assertTrue(actual.attach_files)

    async def test_explicit_member_denial_is_preserved_and_reported(self):
        self.permissions.send_messages = False
        self.overwrite.send_messages = False
        result = await self.run_reuse()
        self.channel.set_permissions.assert_not_awaited()
        self.assertEqual(len(result.warnings), 2)
        self.assertIn('explicit permission denial was preserved', result.warnings[0])
        self.channel.edit.assert_awaited_once()

    async def test_role_denials_and_timeouts_are_not_bypassed(self):
        self.permissions.send_messages = False
        self.members[1001].roles = (SimpleNamespace(id=701),)
        role_deny = discord.PermissionOverwrite(send_messages=False)
        self.channel.overwrites_for.side_effect = (
            lambda subject: role_deny if subject.id == 701 else self.overwrite
        )
        self.members[1002].is_timed_out = lambda: True
        result = await self.run_reuse()
        self.channel.set_permissions.assert_not_awaited()
        self.assertEqual(len(result.warnings), 2)

    async def test_custom_and_other_game_topics_are_preserved(self):
        for topic in ('Moderator notes: keep these',
                      'This is the **allied team channel** for game **Other**, ID 43.\n'):
            with self.subTest(topic=topic):
                self.channel.topic = topic
                result = await self.run_reuse()
                self.assertEqual(result.warnings, ())
                self.channel.edit.assert_not_awaited()

    async def test_current_topic_needs_no_edit(self):
        self.channel.topic = creation.channels.game_channel_greeting(
            self.plan.roster_names, self.plan.game, self.plan.reused_targets[0].players,
        )[:1024]
        await self.run_reuse()
        self.channel.edit.assert_not_awaited()

    async def test_existing_central_channel_is_refreshed_for_all_players(self):
        self.guild.id = 300
        self.source = self.guild
        target = replace(
            self.plan.reused_targets[0], kind='central', side_id=None,
            channel_guild_id=300,
        )
        self.plan = replace(self.plan, reused_targets=(target,))
        await self.run_reuse()
        topic = self.channel.edit.await_args.kwargs['topic']
        self.assertIn('**full game channel**', topic)
        self.assertIn('Participants in this game are <@1001> / <@1002>', topic)

    async def test_cancellation_finishes_permission_and_topic_updates(self):
        self.permissions.send_messages = False
        started, release = asyncio.Event(), asyncio.Event()

        async def grant(*_args, **_kwargs):
            started.set()
            await release.wait()

        self.channel.set_permissions.side_effect = grant
        task = asyncio.create_task(self.run_reuse())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.005)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.channel.edit.assert_awaited_once()
        self.channel.delete.assert_not_awaited()

    async def test_missing_member_is_fetched_and_failures_do_not_block_other_work(self):
        self.members.pop(1002)
        self.guild.fetch_member.side_effect = RuntimeError('member absent')
        self.channel.edit.side_effect = RuntimeError('topic denied')
        result = await self.run_reuse()
        self.guild.fetch_member.assert_awaited_once_with(1002)
        self.assertEqual(len(result.warnings), 2)
        self.assertIn('access for player `1002`', result.warnings[0])
        self.assertIn('topic could not be updated', result.warnings[1])

    async def test_missing_channel_does_not_create_or_clear_reference(self):
        self.guild.fetch_channel.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason='Not Found'), 'Unknown channel',
        )
        result = await self.run_reuse()
        self.assertIn('could not be verified', result.warnings[0])
        self.assertIn('do not start it again', result.warnings[0])
        self.channel.edit.assert_not_awaited()
        self.channel.set_permissions.assert_not_awaited()

    async def test_changed_database_state_prevents_all_discord_edits(self):
        self.validator.side_effect = workers.StartedChannelConflictError('roster changed')
        result = await self.run_reuse()
        self.assertTrue(result.warnings)
        self.channel.edit.assert_not_awaited()
        self.channel.set_permissions.assert_not_awaited()

    async def test_wrong_discord_identity_prevents_all_edits(self):
        self.channel.guild = self.source
        result = await self.run_reuse()
        self.assertTrue(result.warnings)
        self.validator.assert_not_awaited()
        self.channel.edit.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
