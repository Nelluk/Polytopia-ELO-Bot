"""Focused coverage for the owner-driven production operator bootstrap."""

from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


administration = import_offline_runtime('modules.administration')
capabilities = import_offline_runtime(
    'modules.operator_guild_command_capabilities'
)


PC_GUILD_ID = 447883341463814144
PLAN_DIGEST = 'a' * 64


def plan(*, creates=('guild', 'operator'), updates=(), removals=()):
    return SimpleNamespace(
        creates=tuple(creates),
        updates=tuple(updates),
        removals=tuple(removals),
        plan_digest=PLAN_DIGEST,
    )


class OperatorCommandBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.cog.bot = SimpleNamespace()
        self.command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'syncpcoperator'
        )

    @staticmethod
    def context(*, guild_id=PC_GUILD_ID):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id),
            prefix='$',
            send=mock.AsyncMock(),
        )

    async def test_static_plan_reuses_guarded_reconciliation_service(self):
        policy = SimpleNamespace(
            capabilities_for_guild=lambda guild_id: (
                ('core_user', 'operator') if guild_id == PC_GUILD_ID else ()
            )
        )
        frozen_plan = plan()
        with mock.patch.object(
            administration.settings,
            'application_command_policy',
            policy,
        ), mock.patch.object(
            capabilities,
            'inspect_command_plan',
            new=mock.AsyncMock(return_value=frozen_plan),
        ) as inspect_plan:
            result = await self.cog._static_operator_command_plan(PC_GUILD_ID)

        self.assertIs(result, frozen_plan)
        arguments = inspect_plan.await_args.kwargs
        self.assertIs(arguments['bot'], self.cog.bot)
        self.assertIs(arguments['policy'], policy)
        self.assertEqual(arguments['guild_id'], PC_GUILD_ID)
        self.assertEqual(arguments['mode'], capabilities.RECONCILE)
        self.assertEqual(arguments['current_capabilities'], ('core_user', 'operator'))
        self.assertEqual(arguments['desired_capabilities'], ('core_user', 'operator'))
        self.assertRegex(arguments['active_document_digest'], r'^[0-9a-f]{64}$')

    async def test_command_is_hidden_and_pc_only(self):
        self.assertTrue(self.command.hidden)
        ctx = self.context(guild_id=1)
        with mock.patch.object(
            administration.settings,
            'runtime_profile',
            SimpleNamespace(environment='production'),
        ), mock.patch.object(
            administration.settings,
            'server_ids',
            {'polychampions': PC_GUILD_ID},
        ), mock.patch.object(
            self.cog,
            '_static_operator_command_plan',
            new=mock.AsyncMock(),
        ) as inspect_plan:
            await self.command.callback(self.cog, ctx, confirmation=None)

        inspect_plan.assert_not_awaited()
        self.assertIn('PolyChampions', ctx.send.await_args.args[0])

    async def test_preview_binds_the_exact_remote_plan_digest(self):
        ctx = self.context()
        frozen_plan = plan()
        with mock.patch.object(
            administration.settings,
            'runtime_profile',
            SimpleNamespace(environment='production'),
        ), mock.patch.object(
            administration.settings,
            'server_ids',
            {'polychampions': PC_GUILD_ID},
        ), mock.patch.object(
            self.cog,
            '_static_operator_command_plan',
            new=mock.AsyncMock(return_value=frozen_plan),
        ), mock.patch.object(
            capabilities,
            'apply_command_plan',
            new=mock.AsyncMock(),
        ) as apply:
            await self.command.callback(self.cog, ctx, confirmation=None)

        apply.assert_not_awaited()
        message = ctx.send.await_args.args[0]
        self.assertIn('Create: guild, operator', message)
        self.assertIn(f'SYNC PC OPERATOR {PLAN_DIGEST}', message)

    async def test_exact_confirmation_applies_and_reports_verified_roots(self):
        ctx = self.context()
        frozen_plan = plan()
        applied = capabilities.GuildCommandCapabilityApplyResult(
            guild_id=PC_GUILD_ID,
            roots=('game', 'guild', 'operator'),
            synced_count=3,
        )
        with mock.patch.object(
            administration.settings,
            'runtime_profile',
            SimpleNamespace(environment='production'),
        ), mock.patch.object(
            administration.settings,
            'server_ids',
            {'polychampions': PC_GUILD_ID},
        ), mock.patch.object(
            self.cog,
            '_static_operator_command_plan',
            new=mock.AsyncMock(return_value=frozen_plan),
        ), mock.patch.object(
            capabilities,
            'apply_command_plan',
            new=mock.AsyncMock(return_value=applied),
        ) as apply:
            await self.command.callback(
                self.cog,
                ctx,
                confirmation=f'SYNC PC OPERATOR {PLAN_DIGEST}',
            )

        apply.assert_awaited_once_with(
            bot=self.cog.bot,
            policy=administration.settings.application_command_policy,
            plan=frozen_plan,
        )
        self.assertIn('/operator', ctx.send.await_args.args[0])

    async def test_noop_reports_already_synchronized_without_apply(self):
        ctx = self.context()
        with mock.patch.object(
            administration.settings,
            'runtime_profile',
            SimpleNamespace(environment='production'),
        ), mock.patch.object(
            administration.settings,
            'server_ids',
            {'polychampions': PC_GUILD_ID},
        ), mock.patch.object(
            self.cog,
            '_static_operator_command_plan',
            new=mock.AsyncMock(return_value=plan(creates=())),
        ), mock.patch.object(
            capabilities,
            'apply_command_plan',
            new=mock.AsyncMock(),
        ) as apply:
            await self.command.callback(self.cog, ctx, confirmation=None)

        apply.assert_not_awaited()
        self.assertIn('already synchronized', ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
