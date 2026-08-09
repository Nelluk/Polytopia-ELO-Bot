"""Offline coverage for the P5.4 pending-game start boundary."""

from contextlib import AbstractContextManager, ExitStack
import asyncio
from dataclasses import FrozenInstanceError
import datetime
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime("modules.game_start_workers")
adapter = import_offline_runtime("modules.game_start")
games = import_offline_runtime("modules.games")
matchmaking = import_offline_runtime("modules.matchmaking")


class FakeDatabase:
    def __init__(self, harness):
        self.harness = harness
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                return False

        return Context()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                database.harness.save_state()

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1
                    database.harness.restore_state()
                return False

        return Atomic()


class StartHarness:
    def __init__(self):
        self.guild_id = 300
        self.game_id = 322
        self.failure = None
        self.logs = []
        self.squads = []
        self.registered = {100, 200}
        self.team_one = SimpleNamespace(
            id=501, name="The Ronin", is_hidden=False, emoji=":ronin:"
        )
        self.team_two = SimpleNamespace(
            id=502, name="The Jets", is_hidden=False, emoji=":jets:"
        )
        self.host_member = SimpleNamespace(discord_id=100, name="host")
        self.target_member = SimpleNamespace(discord_id=200, name="target")
        self.host_player = self.player(11, "Host", self.host_member)
        self.target_player = self.player(22, "Target", self.target_member)
        self.side_one = self.side(1, self.host_player, self.team_one)
        self.side_two = self.side(2, self.target_player, self.team_two)
        self.game = self.make_game()
        self.database = FakeDatabase(self)

        harness = self

        class GameModel:
            @staticmethod
            def get_by_id(game_id):
                if int(game_id) != harness.game_id:
                    raise peewee.DoesNotExist()
                return harness.game

            @staticmethod
            def pregame_check(**kwargs):
                harness.pregame_groups = kwargs["discord_groups"]
                return (
                    [
                        [harness.team_one]
                        * len(harness.side_one._lineups),
                        [harness.team_two]
                        * len(harness.side_two._lineups),
                    ],
                    [harness.team_one, harness.team_two],
                )

        class DiscordMemberModel:
            @staticmethod
            def get_or_none(**kwargs):
                if kwargs["discord_id"] in harness.registered:
                    return SimpleNamespace(discord_id=kwargs["discord_id"])
                return None

        class GameLogModel:
            @staticmethod
            def write(**kwargs):
                harness.logs.append(kwargs)
                if harness.failure == "log":
                    raise peewee.OperationalError("log failure")

        class SquadModel:
            @staticmethod
            def upsert(*, player_list, guild_id):
                if harness.failure == "squad":
                    raise RuntimeError("squad failure")
                squad = SimpleNamespace(
                    id=len(harness.squads) + 1,
                    players=tuple(player_list),
                    guild_id=guild_id,
                )
                harness.squads.append(squad)
                return squad

        self.game_model = GameModel
        self.discord_member_model = DiscordMemberModel
        self.game_log_model = GameLogModel
        self.squad_model = SquadModel

    def player(self, player_id, name, discord_member):
        harness = self
        player = SimpleNamespace(
            id=player_id,
            name=name,
            discord_member=discord_member,
            team=None,
        )

        def save():
            if harness.failure == "player":
                raise RuntimeError("player failure")

        player.save = save
        return player

    def side(self, position, player, team):
        harness = self
        lineup = SimpleNamespace(id=position + 50, player=player)

        def save():
            if harness.failure == "side":
                raise RuntimeError("side failure")

        side = SimpleNamespace(
            position=position,
            size=1,
            team=team,
            squad=None,
            _lineups=[lineup],
            save=save,
        )
        side.ordered_player_list = lambda: list(side._lineups)
        return side

    def make_game(self):
        harness = self
        game = SimpleNamespace(
            id=self.game_id,
            guild_id=self.guild_id,
            is_pending=True,
            is_completed=False,
            is_confirmed=False,
            is_ranked=True,
            name=None,
            notes="notes",
            expiration=datetime.datetime(2026, 8, 2),
            size=[1, 1],
            host=self.host_player,
            league_season=None,
            league_tier=None,
            league_playoff=False,
            date=datetime.date(2026, 7, 30),
            broadcasts=(),
        )

        game.ordered_side_list = lambda: (harness.side_one, harness.side_two)
        game.capacity = lambda: (
            sum(
                len(side.ordered_player_list())
                for side in game.ordered_side_list()
            ),
            sum(side.size for side in game.ordered_side_list()),
        )
        game.is_hosted_by = lambda discord_id: (
            discord_id == 100,
            harness.host_player,
        )
        game.is_created_by = lambda discord_id: discord_id == 100
        game.creating_player = lambda: harness.host_player

        def save():
            if harness.failure == "game":
                raise RuntimeError("game failure")

        def update_league_fields():
            if harness.failure == "league":
                raise RuntimeError("league failure")
            return False

        game.save = save
        game.update_league_fields = update_league_fields
        return game

    def save_state(self):
        self.state = {
            "pending": self.game.is_pending,
            "name": self.game.name,
            "host_team": self.host_player.team,
            "target_team": self.target_player.team,
            "side_one_squad": self.side_one.squad,
            "side_two_squad": self.side_two.squad,
            "squads": list(self.squads),
            "logs": list(self.logs),
        }

    def restore_state(self):
        state = self.state
        self.game.is_pending = state["pending"]
        self.game.name = state["name"]
        self.host_player.team = state["host_team"]
        self.target_player.team = state["target_team"]
        self.side_one.squad = state["side_one_squad"]
        self.side_two.squad = state["side_two_squad"]
        self.squads[:] = state["squads"]
        self.logs[:] = state["logs"]

    def requester(self, *, staff=False, discord_id=100):
        return workers.StartMemberSnapshot(
            guild_id=self.guild_id,
            discord_id=discord_id,
            discord_name="requester",
            discord_nick=None,
            display_name="Requester",
            role_ids=(),
            role_names=(),
            level=5 if staff else 3,
            is_mod=False,
            is_staff=staff,
            description=f"Requester ({discord_id})",
            side_position=0,
            lineup_id=None,
            player_id=None,
            player_name="Requester",
        )

    def preflight_request(self, *, name="Fields of Fire", requester=None):
        return workers.StartPreflightRequest(
            game_id=self.game_id,
            guild_id=self.guild_id,
            name=name,
            prefix="$",
            requester=requester or self.requester(),
            require_teams=False,
            invoked_with="start",
        )

    def start_request(self, preflight, *, present=True):
        participants = []
        for identity in preflight.participants:
            participants.append(
                workers.StartMemberSnapshot(
                    guild_id=self.guild_id,
                    discord_id=identity.discord_id,
                    discord_name=identity.discord_name,
                    discord_nick=None,
                    display_name=identity.player_name,
                    role_ids=(),
                    role_names=(
                        ("The Ronin",)
                        if identity.discord_id == 100
                        else ("The Jets",)
                    ),
                    level=3,
                    is_mod=False,
                    is_staff=False,
                    description=f"{identity.player_name} ({identity.discord_id})",
                    side_position=identity.side_position,
                    lineup_id=identity.lineup_id,
                    player_id=identity.player_id,
                    player_name=identity.player_name,
                    member_present=present,
                )
            )
        return workers.StartRequest(
            game_id=self.game_id,
            guild_id=self.guild_id,
            name="Fields of Fire",
            prefix="$",
            requester=self.requester(),
            participants=tuple(participants),
            preflight=preflight,
            require_teams=False,
            invoked_with="start",
        )

    def patch(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(workers.models, "db", self.database))
        stack.enter_context(mock.patch.object(workers.models, "Game", self.game_model))
        stack.enter_context(mock.patch.object(
            workers.models, "DiscordMember", self.discord_member_model
        ))
        stack.enter_context(mock.patch.object(
            workers.models, "GameLog", self.game_log_model
        ))
        stack.enter_context(mock.patch.object(
            workers.models, "Squad", self.squad_model
        ))
        stack.enter_context(mock.patch.object(
            workers.settings,
            "guild_setting",
            side_effect=lambda guild_id, key: (
                ["Helper"] if key == "helper_roles" else False
            ),
        ))
        stack.enter_context(mock.patch.object(
            workers.utilities, "is_valid_poly_gamename", return_value=True
        ))
        return stack


class StartWorkerTests(unittest.TestCase):
    def test_success_uses_worker_connection_and_immutable_result(self):
        harness = StartHarness()
        harness.game.broadcasts = (
            SimpleNamespace(
                id=901,
                game=harness.game,
                channel_id=902,
                message_id=903,
            ),
        )
        with harness.patch():
            preflight = workers.preflight_start_game(harness.preflight_request())
            result = workers.start_game(harness.start_request(preflight))

        self.assertFalse(harness.game.is_pending)
        self.assertEqual(harness.game.name, "Fields of Fire")
        self.assertEqual(harness.host_player.team, harness.team_one)
        self.assertEqual(harness.target_player.team, harness.team_two)
        self.assertEqual(len(harness.logs), 1)
        self.assertEqual(harness.database.connection_opened, 2)
        self.assertEqual(harness.database.connection_closed, 2)
        self.assertEqual(harness.database.commits, 1)
        self.assertEqual(
            result.broadcast_targets,
            (
                workers.game_broadcast_workers.ExternalBroadcastTarget(
                    row_id=901,
                    game_id=harness.game_id,
                    guild_id=harness.guild_id,
                    channel_id=902,
                    message_id=903,
                ),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            result.name = "changed"

    def test_preflight_rejects_guild_nonpending_incomplete_and_missing_name(self):
        for scenario in ("guild", "nonpending", "incomplete", "name"):
            with self.subTest(scenario=scenario):
                harness = StartHarness()
                request = harness.preflight_request(
                    name=None if scenario == "name" else "Fields of Fire"
                )
                if scenario == "guild":
                    request = workers.StartPreflightRequest(
                        **{**request.__dict__, "guild_id": 301}
                    )
                if scenario == "nonpending":
                    harness.game.is_pending = False
                if scenario == "incomplete":
                    harness.side_two._lineups[:] = []
                with harness.patch(), self.assertRaises(
                    workers.GameStartValidationError
                ):
                    workers.preflight_start_game(request)

    def test_stale_lineup_fails_without_mutation(self):
        harness = StartHarness()
        with harness.patch():
            preflight = workers.preflight_start_game(harness.preflight_request())
            replacement = harness.player(
                99,
                "Replacement",
                SimpleNamespace(discord_id=999, name="replacement"),
            )
            harness.side_two._lineups[0].player = replacement
            with self.assertRaisesRegex(
                workers.GameStartValidationError,
                "changed while it was being prepared",
            ):
                workers.start_game(harness.start_request(preflight))
        self.assertTrue(harness.game.is_pending)
        self.assertEqual(harness.database.commits, 0)

    def test_missing_member_warning_is_primitive_and_team_required_can_fail(self):
        harness = StartHarness()
        with harness.patch():
            preflight = workers.preflight_start_game(harness.preflight_request())
            result = workers.start_game(
                harness.start_request(preflight, present=False)
            )
        self.assertEqual(len(result.missing_member_warnings), 2)
        self.assertTrue(all(item is None for item in harness.pregame_groups[0]))

        harness = StartHarness()
        with harness.patch():
            preflight = workers.preflight_start_game(harness.preflight_request())
            request = workers.StartRequest(
                **{**harness.start_request(preflight).__dict__, "require_teams": True}
            )
            with mock.patch.object(
                harness.game_model,
                "pregame_check",
                side_effect=workers.exceptions.CheckFailedError("team required"),
            ), self.assertRaisesRegex(
                workers.exceptions.CheckFailedError,
                "team required",
            ):
                workers.start_game(request)

    def test_staff_can_start_and_non_host_is_denied(self):
        harness = StartHarness()
        staff = harness.requester(staff=True, discord_id=300)
        harness.registered.add(300)
        with harness.patch():
            result = workers.preflight_start_game(
                harness.preflight_request(requester=staff)
            )
        self.assertEqual(result.game_id, harness.game_id)

        harness = StartHarness()
        non_host = harness.requester(discord_id=200)
        with harness.patch(), self.assertRaisesRegex(
            workers.GameStartValidationError,
            "Only the game host",
        ):
            workers.preflight_start_game(
                harness.preflight_request(requester=non_host)
            )

    def test_high_level_invalid_name_returns_warning_low_level_is_rejected(self):
        for level, expected_exception in ((3, True), (5, False)):
            harness = StartHarness()
            requester = harness.requester(staff=level > 3)
            with harness.patch(), mock.patch.object(
                workers.utilities,
                "is_valid_poly_gamename",
                return_value=False,
            ):
                if expected_exception:
                    with self.assertRaisesRegex(
                        workers.GameStartValidationError,
                        "names 322",
                    ) as raised:
                        workers.preflight_start_game(
                            harness.preflight_request(requester=requester)
                        )
                    self.assertNotIn("codes", str(raised.exception))
                else:
                    result = workers.preflight_start_game(
                        harness.preflight_request(requester=requester)
                    )
                    self.assertIn("override", result.name_warning)

    def test_registration_guidance_uses_one_polytopia_account_name(self):
        harness = StartHarness()
        harness.registered.remove(100)
        with harness.patch(), self.assertRaises(
            workers.GameStartValidationError
        ) as raised:
            workers.preflight_start_game(harness.preflight_request())

        message = str(raised.exception)
        self.assertIn("setname Your Polytopia Name", message)
        self.assertNotIn("Mobile", message)
        self.assertNotIn("Steam", message)
        self.assertNotIn("steamname", message)

    def test_mutation_and_audit_failures_roll_back(self):
        for failure in ("player", "squad", "side", "game", "league", "log"):
            with self.subTest(failure=failure):
                harness = StartHarness()
                with harness.patch():
                    if failure == "squad":
                        extra_player = harness.player(
                            33,
                            "Third",
                            SimpleNamespace(discord_id=300, name="third"),
                        )
                        harness.side_one.size = 2
                        harness.side_one._lineups.append(
                            SimpleNamespace(id=99, player=extra_player)
                        )
                    preflight = workers.preflight_start_game(
                        harness.preflight_request()
                    )
                    harness.failure = failure
                    with self.assertRaises(Exception):
                        workers.start_game(harness.start_request(preflight))
                self.assertTrue(harness.game.is_pending)
                self.assertIsNone(harness.game.name)
                self.assertIsNone(harness.host_player.team)
                self.assertIsNone(harness.target_player.team)
                self.assertEqual(harness.squads, [])
                self.assertEqual(harness.logs, [])
                self.assertEqual(harness.database.rollbacks, 1)


class StartExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_does_not_block_heartbeat(self):
        started = threading.Event()
        release = threading.Event()

        def slow_worker(_request):
            started.set()
            release.wait(timeout=2)
            return "done"

        with mock.patch.object(workers, "start_game", side_effect=slow_worker):
            task = asyncio.create_task(workers.run_start(mock.Mock()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await asyncio.wait_for(task, timeout=1), "done")


class StartAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_start_defers_and_publishes_public_success(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            channel_id=900,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = SimpleNamespace(game_id=322)
        handler = SimpleNamespace(
            execute_start=mock.AsyncMock(return_value=result),
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(get_cog=lambda name: handler)
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=True
        )

        command = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == "game"
        ).get_command("start")
        with mock.patch.object(
            games.settings, "guild_setting", return_value="$"
        ), mock.patch.object(
            games.settings, "bot", SimpleNamespace(guilds=())
        ), mock.patch.object(
            games.game_start, "publish_start_result", new=mock.AsyncMock()
        ):
            await command.callback(cog, interaction, 322, "Fields of Fire")

        interaction.response.defer.assert_awaited_once_with()
        handler.execute_start.assert_awaited_once_with(
            game_id=322,
            guild=interaction.guild,
            requester=interaction.user,
            name="Fields of Fire",
            prefix="$",
            invoked_with="/game start",
        )

    async def test_native_start_validation_failure_is_ephemeral(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            channel_id=900,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        handler = SimpleNamespace(
            execute_start=mock.AsyncMock(
                side_effect=workers.GameStartValidationError("not full")
            ),
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(get_cog=lambda name: handler)
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=True
        )
        command = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == "game"
        ).get_command("start")
        with mock.patch.object(
            games.settings, "guild_setting", return_value="$"
        ):
            await command.callback(cog, interaction, 322, "Fields of Fire")

        interaction.followup.send.assert_awaited_once_with(
            "not full",
            ephemeral=True,
        )

    async def test_prefix_start_uses_same_service_and_alias(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            prefix="$",
            invoked_with="startgame",
            send=mock.AsyncMock(),
        )
        result = SimpleNamespace(game_id=322)
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_start = mock.AsyncMock(return_value=result)
        command = next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == "start"
        )
        self.assertEqual(command.aliases, ["startgame"])
        self.assertEqual(command.usage, "game_id Name of Poly Game")
        with mock.patch.object(
            matchmaking.game_start,
            "publish_start_result",
            new=mock.AsyncMock(),
        ), mock.patch.object(
            matchmaking.settings, "bot", SimpleNamespace(guilds=())
        ):
            await command.callback(
                cog,
                ctx,
                "#322",
                name="Fields of Fire",
            )

        cog.execute_start.assert_awaited_once_with(
            game_id=322,
            guild=ctx.guild,
            requester=ctx.author,
            name="Fields of Fire",
            prefix="$",
            invoked_with="startgame",
        )

    async def test_adapter_passes_only_frozen_member_snapshots(self):
        role = SimpleNamespace(id=501, name="The Ronin")
        guild = SimpleNamespace(id=300)
        requester = SimpleNamespace(
            id=100,
            name="Requester",
            nick=None,
            display_name="Requester",
            roles=(role,),
            guild=guild,
        )
        guild.get_member = (
            lambda discord_id: requester if discord_id == 200 else None
        )
        identity = workers.StartParticipantIdentity(
            side_position=1,
            lineup_id=51,
            player_id=11,
            discord_id=200,
            player_name="Target",
            discord_name="Target Discord",
        )
        preflight = workers.StartPreflightResult(
            game_id=322,
            guild_id=300,
            participants=(identity,),
            side_sizes=(1,),
            host_id=100,
            creator_id=100,
            current_name=None,
            notes=None,
            expiration=None,
            is_ranked=True,
        )
        captured = {}

        async def run_start(request):
            captured["request"] = request
            return SimpleNamespace(game_id=322)

        with mock.patch.object(adapter.settings, "get_user_level", return_value=3), \
             mock.patch.object(adapter.settings, "is_mod", return_value=False), \
             mock.patch.object(adapter.settings, "is_staff", return_value=False), \
             mock.patch.object(
                 adapter.settings,
                 "guild_setting",
                 side_effect=lambda _guild, key: {
                     "command_prefix": "$",
                     "require_teams": False,
                 }[key],
             ), \
             mock.patch.object(
                 adapter.game_start_workers,
                 "run_start_preflight",
                 return_value=preflight,
             ), \
             mock.patch.object(
                 adapter.game_start_workers,
                 "run_start",
                 side_effect=run_start,
             ):
            result = await adapter.execute_start(
                game_id=322,
                guild=guild,
                requester=requester,
                name="Fields of Fire",
            )

        self.assertEqual(result.game_id, 322)
        self.assertIsInstance(
            captured["request"].participants[0],
            workers.StartMemberSnapshot,
        )
        self.assertEqual(
            captured["request"].participants[0].role_names,
            ("The Ronin",),
        )
        self.assertFalse(hasattr(captured["request"], "guild"))


class StartPostCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_postcommit_failure_reconciles_and_continues(self):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(content)

        game = SimpleNamespace(
            id=322,
            guild_id=300,
            is_ranked=True,
            is_mobile=True,
            gamesides=(SimpleNamespace(team=SimpleNamespace(is_hidden=False)),),
            embed=lambda **kwargs: (discord.Embed(title="started"), None),
            mentions=lambda: ["<@100>", "<@200>"],
            is_season_game=lambda: (),
            is_uncaught_season_game=lambda: False,
            smallest_team=lambda: 1,
            create_game_channels=mock.AsyncMock(),
        )
        result = workers.StartResult(
            game_id=322,
            guild_id=300,
            name="Fields of Fire",
            requester_id=100,
            mentions=("<@100>", "<@200>"),
            participant_ids=(100, 200),
            missing_member_warnings=(),
            name_warning=None,
            league_warning=None,
            creator_id=100,
            host_id=100,
            broadcast_targets=(
                workers.game_broadcast_workers.ExternalBroadcastTarget(
                    row_id=1,
                    game_id=322,
                    guild_id=300,
                    channel_id=600,
                    message_id=700,
                ),
            ),
        )
        guild = SimpleNamespace(id=300, get_channel=lambda _id: None)
        output = SimpleNamespace(send=send)

        with mock.patch.object(
            adapter.models.Game, "load_full_game", return_value=game
        ), mock.patch.object(
            adapter.settings,
            "guild_setting",
            side_effect=lambda _guild, key: {
                "game_announce_channel": None,
                "game_channel_categories": False,
            }[key],
        ), mock.patch.object(
            adapter.settings, "server_ids", {"polychampions": 999}
        ), mock.patch.object(
            adapter.image_storage,
            "send_game_embed",
            new=mock.AsyncMock(side_effect=RuntimeError("card failure")),
        ), mock.patch.object(
            adapter.league, "auto_grad_novas", new=mock.AsyncMock()
        ), mock.patch.object(
            adapter.game_broadcasts,
            "reconcile_started_broadcasts",
            new=mock.AsyncMock(return_value=(
                adapter.game_broadcasts.BroadcastReconciliationOutcome(
                    target=result.broadcast_targets[0],
                    status=adapter.game_broadcasts.RETAINED,
                    detail="broadcast failure",
                ),
            )),
        ):
            await adapter.publish_start_result(
                result,
                output_context=output,
                guild=guild,
                prefix="$",
                bot_guilds=(),
            )

        self.assertTrue(any("600/700" in str(item) for item in sent))
        self.assertTrue(any("game card" in str(item) for item in sent))
        self.assertTrue(any("now being tracked" in str(item) for item in sent))

    async def test_announcement_reference_uses_bounded_postcommit_worker(self):
        sent = []

        async def send(content=None, **kwargs):
            sent.append(content)

        channel = SimpleNamespace(
            id=700,
            mention="#announcements",
            send=mock.AsyncMock(),
        )
        announcement = SimpleNamespace(id=701, channel=channel)
        channel.send.return_value = announcement
        game = SimpleNamespace(
            id=322,
            guild_id=300,
            is_ranked=True,
            is_mobile=True,
            gamesides=(SimpleNamespace(team=SimpleNamespace(is_hidden=False)),),
            embed=lambda **kwargs: (discord.Embed(title="started"), None),
            mentions=lambda: ["<@100>"],
            is_season_game=lambda: (),
            is_uncaught_season_game=lambda: False,
            smallest_team=lambda: 1,
            create_game_channels=mock.AsyncMock(),
        )
        result = workers.StartResult(
            game_id=322,
            guild_id=300,
            name="Fields of Fire",
            requester_id=100,
            mentions=("<@100>",),
            participant_ids=(100,),
            missing_member_warnings=(),
            name_warning=None,
            league_warning=None,
            creator_id=100,
            host_id=100,
        )
        guild = SimpleNamespace(id=300, get_channel=lambda _id: channel)
        output = SimpleNamespace(send=send)
        persistence = mock.AsyncMock()

        with mock.patch.object(
            adapter.models.Game, "load_full_game", return_value=game
        ), mock.patch.object(
            adapter.settings,
            "guild_setting",
            side_effect=lambda _guild, key: (
                700 if key == "game_announce_channel" else False
            ),
        ), mock.patch.object(
            adapter.settings, "server_ids", {"polychampions": 999}
        ), mock.patch.object(
            adapter.image_storage,
            "send_game_embed",
            new=mock.AsyncMock(return_value=announcement),
        ), mock.patch.object(
            adapter.game_start_workers,
            "run_announcement_persistence",
            new=persistence,
        ), mock.patch.object(
            adapter.league, "auto_grad_novas", new=mock.AsyncMock()
        ):
            await adapter.publish_start_result(
                result,
                output_context=output,
                guild=guild,
                prefix="$",
                bot_guilds=(),
            )

        persistence.assert_awaited_once_with(
            workers.AnnouncementReferenceRequest(
                game_id=322,
                guild_id=300,
                channel_id=700,
                message_id=701,
            )
        )

        announcement_text = channel.send.await_args_list[0].args[0]
        self.assertNotIn("Steam", announcement_text)
        self.assertNotIn("Mobile", announcement_text)
