"""Focused H4 confirmation publication snapshot regressions."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, is_dataclass
import importlib
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase

with mock.patch.object(
    PostgresqlExtDatabase, 'connect', return_value=True
), mock.patch.object(
    PostgresqlExtDatabase, 'close', return_value=True
), mock.patch.object(
    PostgresqlExtDatabase, 'create_tables'
), mock.patch.object(
    SchemaManager, 'create_foreign_key'
):
    publication = importlib.import_module('modules.confirmation_publication')
    workers = importlib.import_module('modules.confirmation_publication_workers')
    game_detail_workers = importlib.import_module('modules.game_detail_workers')
    administration = importlib.import_module('modules.administration')
    elo_workers = importlib.import_module('modules.elo_workers')
from modules.elo_jobs import EloJobCoordinator


def confirmation_snapshot(
    *,
    winner_name: str = 'Alpha',
    side_targets=(),
    game_channel_id=None,
    experience_roles=(),
):
    alpha = game_detail_workers.GameDetailSide(
        side_id=11,
        position=1,
        name=winner_name,
        capacity=1,
        team_id=None,
        team_name='',
        team_emoji='',
        team_hidden=True,
        team_image_url='',
        team_elo_label='',
        squad_elo_label='',
        required_role_id=None,
        channel_id=None,
        external_guild_id=None,
        win_confirmed=True,
        lineups=(game_detail_workers.GameDetailLineup(
            player_id=21,
            discord_id=301,
            player_name='Alpha Player',
            tribe_name='',
            tribe_emoji='',
            elo_label='1210 +10',
        ),),
    )
    beta = game_detail_workers.GameDetailSide(
        side_id=12,
        position=2,
        name='Beta',
        capacity=1,
        team_id=None,
        team_name='',
        team_emoji='',
        team_hidden=True,
        team_image_url='',
        team_elo_label='',
        squad_elo_label='',
        required_role_id=None,
        channel_id=None,
        external_guild_id=None,
        win_confirmed=False,
        lineups=(game_detail_workers.GameDetailLineup(
            player_id=22,
            discord_id=302,
            player_name='Beta Player',
            tribe_name='',
            tribe_emoji='',
            elo_label='990 -10',
        ),),
    )
    game = game_detail_workers.GameDetailSnapshot(
        game_id=99,
        guild_id=100,
        name='Committed result',
        date='2026-08-10',
        completed_ts='2026-08-10 12:00:00',
        win_claimed_ts='2026-08-10 10:00:00',
        expiration='',
        is_pending=False,
        is_completed=True,
        is_confirmed=True,
        is_ranked=True,
        is_mobile=True,
        map_type='',
        notes='',
        league_season=None,
        league_tier=None,
        league_playoff=False,
        size=(1, 1),
        game_channel_id=game_channel_id,
        host_discord_id=301,
        host_name='Alpha Player',
        winner_side_id=11,
        status_label='Completed',
        result_label=f'Winner: {winner_name}',
        inferred_from_channel=False,
        cross_guild=False,
        sides=(alpha, beta),
    )
    return workers.ConfirmationPublicationSnapshot(
        game=game,
        winner_name=winner_name,
        roster_mentions=('<@301>', '<@302>'),
        side_channel_targets=tuple(side_targets),
        game_channel_id=game_channel_id,
        experience_roles=tuple(experience_roles),
        champion_roles=None,
        nova=None,
    )


class FakeGuild:
    def __init__(self, guild_id=100, *, channels=(), roles=(), members=()):
        self.id = guild_id
        self.name = f'Guild {guild_id}'
        self.roles = list(roles)
        self._channels = {item.id: item for item in channels}
        self._members = {item.id: item for item in members}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, discord_id):
        return self._members.get(discord_id)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)


class FakeBot:
    def __init__(self, guilds):
        self.guilds = list(guilds)

    def get_guild(self, guild_id):
        return next((guild for guild in self.guilds if guild.id == guild_id), None)

    def get_channel(self, channel_id):
        for guild in self.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None

    def get_user(self, _discord_id):
        return None


class ConfirmationPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_snapshot_loading_keeps_event_loop_responsive(self):
        coordinator = EloJobCoordinator()
        started = threading.Event()
        release = threading.Event()
        snapshot = confirmation_snapshot()

        def slow_confirmation(*args):
            self.assertIsInstance(
                args[3],
                workers.ConfirmationPublicationContext,
            )
            started.set()
            release.wait(timeout=2)
            return elo_workers.ConfirmedWinResult(99, 'Alpha', snapshot)

        cog = object.__new__(administration.administration)
        try:
            with mock.patch.object(
                administration.settings,
                'elo_job_coordinator',
                coordinator,
            ), mock.patch.object(
                administration.settings,
                'bot',
                FakeBot([]),
            ), mock.patch.object(
                administration.elo_workers,
                'confirm_game',
                new=slow_confirmation,
            ), mock.patch.object(
                administration.utilities,
                'lock_game',
            ), mock.patch.object(
                administration.utilities,
                'unlock_game',
            ):
                task = asyncio.create_task(cog._run_confirm_game_job(
                    game_id=99,
                    guild_id=100,
                    requester_id=300,
                    requester_name='Staff',
                    requester_description='Staff',
                ))
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(started.is_set())

                heartbeat = asyncio.Event()

                async def pulse():
                    await asyncio.sleep(0.01)
                    heartbeat.set()

                await asyncio.wait_for(pulse(), timeout=0.2)
                self.assertTrue(heartbeat.is_set())
                release.set()
                await asyncio.sleep(0.05)
                result = await task
            self.assertIs(result.publication, snapshot)
        finally:
            release.set()
            coordinator.shutdown()

    def test_snapshot_tree_is_frozen_and_primitive_only(self):
        snapshot = confirmation_snapshot(
            side_targets=(workers.ChannelPublicationTarget(100, 700),),
            experience_roles=(workers.ExperienceRoleEffect(
                discord_id=301,
                guild_ids=(100,),
                earned_role_name='ELO Rookie',
                removable_role_names=(),
            ),),
        )

        def assert_primitive_tree(value):
            if is_dataclass(value):
                if fields(value):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(value, fields(value)[0].name, None)
                for field in fields(value):
                    assert_primitive_tree(getattr(value, field.name))
                return
            if isinstance(value, tuple):
                for item in value:
                    assert_primitive_tree(item)
                return
            self.assertIsInstance(value, (str, int, bool, type(None)))

        assert_primitive_tree(snapshot)

    def test_nova_snapshot_uses_cached_role_candidates_in_committed_roster(self):
        snapshot = confirmation_snapshot().game
        eligible = workers.nova_graduation_workers.NovaParticipantSnapshot(
            discord_id=301,
            member_name='Cached Alpha',
            mention='<@301>',
            has_nova_role=True,
            has_grad_role=False,
        )
        unrelated = workers.nova_graduation_workers.NovaParticipantSnapshot(
            discord_id=999,
            member_name='Not In Game',
            mention='<@999>',
            has_nova_role=True,
            has_grad_role=False,
        )
        context = workers.ConfirmationPublicationContext(
            nova_guild_ids=(100,),
            nova_candidates=(eligible, unrelated),
        )
        result = SimpleNamespace(game_id=99)

        with mock.patch.object(
            workers.nova_graduation_workers,
            'load_nova_graduation',
            return_value=result,
        ) as loader:
            self.assertIs(workers._nova_snapshot(snapshot, context), result)

        request = loader.call_args.args[0]
        self.assertEqual(request.participants, (eligible,))

    async def test_publisher_uses_committed_snapshot_and_makes_no_orm_calls(self):
        snapshot = confirmation_snapshot(winner_name='Alpha')
        live_state = {'winner': 'Alpha'}
        live_state['winner'] = 'Beta'
        current = SimpleNamespace(send=mock.AsyncMock())
        guild = FakeGuild()
        bot = FakeBot([guild])

        with mock.patch.object(
            publication.settings,
            'guild_setting',
            return_value=None,
        ), mock.patch(
            'modules.models.Game.load_full_game',
            side_effect=AssertionError('publisher attempted ORM reload'),
        ), mock.patch(
            'modules.models.GameLog.write',
            side_effect=AssertionError('publisher attempted ORM write'),
        ), mock.patch(
            'modules.models.Player.select',
            side_effect=AssertionError('publisher attempted ORM read'),
        ), mock.patch(
            'modules.models.DiscordMember.select',
            side_effect=AssertionError('publisher attempted ORM read'),
        ):
            await publication.publish_confirmed_game(
                guild=guild,
                prefix='$',
                current_channel=current,
                snapshot=snapshot,
                bot=bot,
            )

        self.assertEqual(current.send.await_count, 2)
        first_message = current.send.await_args_list[0].args[0]
        self.assertIn('**Alpha**', first_message)
        self.assertNotIn('**Beta**', first_message)
        rendered_embed = current.send.await_args_list[1].kwargs['embed']
        self.assertIn('WINNER: Alpha', rendered_embed.title)

    async def test_channel_role_and_announcement_effects_use_snapshot(self):
        rookie = SimpleNamespace(id=1, name='ELO Rookie', members=[])
        member = SimpleNamespace(
            id=301,
            roles=[],
            remove_roles=mock.AsyncMock(),
            add_roles=mock.AsyncMock(),
        )
        announcement = SimpleNamespace(
            id=900,
            mention='<#900>',
            send=mock.AsyncMock(),
        )
        guild = FakeGuild(
            channels=(announcement,),
            roles=(rookie,),
            members=(member,),
        )
        bot = FakeBot([guild])
        current = SimpleNamespace(send=mock.AsyncMock())
        snapshot = confirmation_snapshot(
            side_targets=(workers.ChannelPublicationTarget(100, 701),),
            game_channel_id=702,
            experience_roles=(workers.ExperienceRoleEffect(
                discord_id=301,
                guild_ids=(100,),
                earned_role_name='ELO Rookie',
                removable_role_names=(),
            ),),
        )

        with mock.patch.object(
            publication.settings,
            'guild_setting',
            return_value=900,
        ), mock.patch.object(
            publication.channels,
            'send_message_to_channel',
            new=mock.AsyncMock(),
        ) as channel_effect:
            await publication.publish_confirmed_game(
                guild=guild,
                prefix='$',
                current_channel=current,
                snapshot=snapshot,
                bot=bot,
            )

        self.assertEqual(channel_effect.await_count, 2)
        self.assertEqual(
            [call.kwargs['channel_id'] for call in channel_effect.await_args_list],
            [701, 702],
        )
        member.add_roles.assert_awaited_once_with(rookie)
        self.assertEqual(announcement.send.await_count, 2)
        self.assertIn('**Alpha**', announcement.send.await_args_list[0].args[0])
        current.send.assert_awaited_once_with(
            'Game concluded! See <#900> for full details.'
        )

    async def test_champion_role_effect_is_model_free_and_deduplicated(self):
        role = SimpleNamespace(id=10, name='ELO Champion')
        old = SimpleNamespace(
            id=401,
            display_name='Old Champion',
            remove_roles=mock.AsyncMock(),
        )
        champion = SimpleNamespace(
            id=402,
            display_name='New Champion',
            add_roles=mock.AsyncMock(),
        )
        role.members = [old]
        guild = FakeGuild(100, roles=(role,), members=(old, champion))
        bot = FakeBot([guild])
        effect = workers.ChampionRoleEffect(
            global_champion_discord_id=402,
            guilds=(workers.ChampionGuildEffect(100, 402),),
        )

        with mock.patch.object(
            publication.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ) as log_effect, mock.patch(
            'modules.models.GameLog.write',
            side_effect=AssertionError('champion publisher attempted ORM'),
        ):
            await publication._publish_champion_roles(effect, bot)

        old.remove_roles.assert_awaited_once_with(
            role,
            reason='Recurring reset of champion list',
        )
        champion.add_roles.assert_awaited_once_with(
            role,
            reason='Local champion',
        )
        log_effect.assert_awaited_once()


class ConfirmationSnapshotTransactionTests(unittest.TestCase):
    def test_snapshot_failure_is_committed_reconciliation_before_discord(self):
        from tests.test_elo_jobs import FakeWinDatabase, FakeWinGame, fake_win_models

        game = FakeWinGame()
        game.is_completed = True
        game.winner = game.first_side
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)

        with mock.patch.object(
            elo_workers,
            'models',
            models,
        ), mock.patch.object(
            elo_workers.confirmation_publication_workers,
            'build_confirmation_publication_snapshot',
            side_effect=RuntimeError('snapshot load failed'),
        ), self.assertRaises(
            elo_workers.ConfirmedWinSnapshotError,
        ) as raised:
            elo_workers.confirm_game(84, 100, 'Staff')

        self.assertEqual(raised.exception.result.game_id, 84)
        self.assertTrue(game.is_confirmed)
        self.assertEqual(len(logs), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
