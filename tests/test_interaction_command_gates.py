"""Offline regression coverage for shared slash moderation and rate limits."""

import ast
from contextlib import ExitStack
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest import mock

import discord
from discord.ext import commands

from tests.test_newgame_worker import import_offline_runtime

bot_module = import_offline_runtime('bot')
views = import_offline_runtime('modules.game_ping_views')
games = import_offline_runtime('modules.games')
gates = bot_module.interaction_bans


def interaction(*, root='game', roles=(), autocomplete=False):
    return SimpleNamespace(
        user=SimpleNamespace(id=10, roles=roles),
        guild_id=1,
        channel_id=100,
        data={'name': root},
        type=(discord.InteractionType.autocomplete if autocomplete
              else discord.InteractionType.application_command),
        extras={},
        response=SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
            autocomplete=mock.AsyncMock(),
            defer=mock.AsyncMock(),
        ),
        followup=SimpleNamespace(send=mock.AsyncMock()),
    )


class InteractionCommandGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in (
            ('config', {1: {}}), ('maintenance_mode', False),
            ('owner_id', 999), ('discord_id_ban_list', []),
            ('application_command_policy', SimpleNamespace(
                roots_for_guild=lambda _: {'game', 'player', 'staffhelp'},
            )),
        ):
            self.patches.enter_context(mock.patch.object(bot_module.settings, name, value))
        for name, value in (
            ('guild_configuration_ready', True),
            ('database_guild_configuration_quarantined', False),
            ('database_guild_configuration_bootstrap_pending', False),
        ):
            self.patches.enter_context(mock.patch.object(
                bot_module.settings, name, return_value=value,
            ))
        self.bot = SimpleNamespace(_command_cooldown=commands.CooldownMapping.from_cooldown(
            6, 30.0, commands.BucketType.user,
        ))
        self.bot.command_retry_after = MethodType(bot_module.MyBot.command_retry_after, self.bot)
        self.tree = SimpleNamespace(client=self.bot)

    async def check(self, value):
        return await bot_module.PolyBotCommandTree.interaction_check(self.tree, value)

    async def test_bans_block_all_roots_before_rate_limit_or_callback(self):
        for root in ('game', 'player', 'staffhelp'):
            for role_ban in (False, True):
                with self.subTest(root=root, role_ban=role_ban):
                    bot_module.settings.discord_id_ban_list = [] if role_ban else [10]
                    value = interaction(root=root, roles=(
                        SimpleNamespace(name='ELO Banned'),
                    ) if role_ban else ())
                    with mock.patch.object(self.bot, 'command_retry_after') as rate:
                        self.assertFalse(await self.check(value))
                    rate.assert_not_called()
                    value.response.send_message.assert_awaited_once_with(
                        gates.ELO_BAN_DENIAL_MESSAGE, ephemeral=True,
                    )

    async def test_autocomplete_ban_is_empty_and_other_autocomplete_is_free(self):
        value = interaction(autocomplete=True)
        with mock.patch.object(bot_module.settings, 'discord_id_ban_list', [10]):
            self.assertFalse(await self.check(value))
        value.response.autocomplete.assert_awaited_once_with([])
        value.response.send_message.assert_not_awaited()
        with mock.patch.object(self.bot, 'command_retry_after') as rate:
            self.assertTrue(await self.check(interaction(autocomplete=True)))
        rate.assert_not_called()

    async def test_wrapped_slash_commands_consume_only_one_shared_slot(self):
        # Exercise the actual prefix global check without starting init_bot.
        source = ast.parse(Path(bot_module.__file__).read_text())
        init = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'init_bot')
        check = next(node for node in init.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'cooldown_check')
        check.decorator_list = []
        namespace = {**vars(bot_module), 'bot': self.bot}
        exec(compile(ast.Module(body=[check], type_ignores=[]), bot_module.__file__, 'exec'), namespace)
        for _ in range(6):
            value = interaction()
            self.assertTrue(await self.check(value))
            ctx = SimpleNamespace(
                author=value.user, interaction=value, invoked_with='player',
                command=SimpleNamespace(name='player'), send=mock.AsyncMock(),
            )
            self.assertTrue(await namespace['cooldown_check'](ctx))
        denied = interaction()
        self.assertFalse(await self.check(denied))
        denied.response.send_message.assert_awaited_once_with(
            "You're on cooldown. Slow down those commands!", ephemeral=True,
        )
        # Prefix usage shares the same exhausted bucket.
        self.assertFalse(await namespace['cooldown_check'](
            SimpleNamespace(**{**vars(ctx), 'interaction': None}),
        ))
        self.assertIsNone(self.bot.command_retry_after(SimpleNamespace(id=999)))

    async def test_wrapped_prefix_denial_returns_without_worker_or_tree_error(self):
        value = interaction()
        value.guild = SimpleNamespace(id=1)
        ctx = SimpleNamespace(send=mock.AsyncMock())

        async def deny(_ctx):
            await ctx.send('Access denied', ephemeral=True)
            raise commands.CheckFailure('expected denial')

        cog = SimpleNamespace(lb=SimpleNamespace(can_run=deny))
        with mock.patch.object(
            commands.Context, 'from_interaction', new=mock.AsyncMock(return_value=ctx),
        ), mock.patch.object(bot_module.settings, 'guild_setting', return_value='$'), mock.patch.object(
            games.leaderboard_workers, 'run_player_leaderboard', new=mock.AsyncMock(),
        ) as worker:
            await games.polygames.player_leaderboard_slash.callback(cog, value)
        ctx.send.assert_awaited_once_with('Access denied', ephemeral=True)
        worker.assert_not_awaited()

    async def test_real_prefix_global_check_denial_is_handled(self):
        async def callback(ctx):
            pass

        ctx = SimpleNamespace(
            command=None,
            bot=SimpleNamespace(can_run=mock.AsyncMock(return_value=False)),
        )
        command = commands.Command(callback)
        self.assertFalse(await gates.prefix_command_can_run(command, ctx))
        self.assertIsNone(ctx.command)

    async def test_unexpected_permission_failure_still_propagates(self):
        command = SimpleNamespace(can_run=mock.AsyncMock(
            side_effect=RuntimeError('unexpected failure'),
        ))
        with self.assertRaisesRegex(RuntimeError, 'unexpected failure'):
            await gates.prefix_command_can_run(command, SimpleNamespace())

    def test_shared_rate_limit_recovers_after_window(self):
        member = SimpleNamespace(id=10)
        for _ in range(6):
            self.assertIsNone(self.bot.command_retry_after(member, current=100))
        self.assertEqual(self.bot.command_retry_after(member, current=100), 30)
        self.assertIsNone(self.bot.command_retry_after(member, current=131))

    async def test_open_ping_modal_and_draft_recheck_current_ban(self):
        value = interaction(roles=(SimpleNamespace(name='ELO Banned'),))
        submitter = mock.AsyncMock()
        modal = views.GamePingStartModal(
            requester_id=10, guild_id=1, channel_id=100, submitter=submitter,
        )
        await modal.on_submit(value)
        submitter.assert_not_awaited()
        value.response.defer.assert_not_awaited()
        value.response.send_message.assert_awaited_once_with(
            gates.ELO_BAN_DENIAL_MESSAGE, ephemeral=True,
        )
        value = interaction(roles=(SimpleNamespace(name='ELO Banned'),))
        confirmer = mock.AsyncMock()
        view = SimpleNamespace(requester_id=10, confirmer=confirmer)
        view.authorize = MethodType(views.GamePingComposerView.authorize, view)
        await views.GamePingComposerView._confirm_clicked(view, value)
        confirmer.assert_not_awaited()
        value.response.send_message.assert_awaited_once_with(
            gates.ELO_BAN_DENIAL_MESSAGE, ephemeral=True,
        )
