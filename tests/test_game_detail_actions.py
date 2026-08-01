"""Focused offline coverage for interactive pending-game cards."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_detail_workers')
views = import_offline_runtime('modules.game_detail_views')
actions = import_offline_runtime('modules.game_detail_actions')
games = import_offline_runtime('modules.games')


def lineup(discord_id: int, name: str) -> workers.GameDetailLineup:
    return workers.GameDetailLineup(
        player_id=discord_id + 1000,
        discord_id=discord_id,
        player_name=name,
        tribe_name='',
        tribe_emoji='',
        elo_label='1000',
        platform_name='Poly name',
    )


def side(
    position: int,
    *,
    capacity: int = 1,
    lineups=(),
    sidename: str = '',
    required_role_id: int | None = None,
) -> workers.GameDetailSide:
    return workers.GameDetailSide(
        side_id=position + 100,
        position=position,
        name=sidename or f'Side {position}',
        capacity=capacity,
        team_id=None,
        team_name='',
        team_emoji='',
        team_hidden=False,
        team_image_url='',
        team_elo_label='',
        squad_elo_label='',
        required_role_id=required_role_id,
        channel_id=None,
        external_guild_id=None,
        win_confirmed=False,
        lineups=tuple(lineups),
        sidename=sidename,
    )


def card_snapshot(
    *,
    pending: bool = True,
    full: bool = False,
    expired: bool = False,
    ambiguous: bool = False,
) -> workers.GameDetailSnapshot:
    if full:
        sides = (
            side(1, lineups=(lineup(101, 'Alpha'),), sidename='Red'),
            side(2, lineups=(lineup(202, 'Beta'),), sidename='Blue'),
        )
    elif ambiguous:
        sides = (
            side(1, capacity=2, lineups=(lineup(101, 'Alpha'),), sidename='Red'),
            side(2, capacity=2, lineups=(lineup(202, 'Beta'),), sidename='Blue'),
        )
    else:
        sides = (
            side(1, lineups=(lineup(101, 'Alpha'),), sidename='Red'),
            side(2, lineups=(), sidename='Blue'),
        )

    if not pending:
        status = 'Incomplete'
    elif expired:
        status = 'Expired open game'
    elif full:
        status = 'Full — waiting to start'
    else:
        status = 'Open'
    return workers.GameDetailSnapshot(
        game_id=77,
        guild_id=10,
        name='Pending card test',
        date='2026-08-01',
        completed_ts='',
        win_claimed_ts='',
        expiration='2099-01-01 00:00:00',
        is_pending=pending,
        is_completed=False,
        is_confirmed=False,
        is_ranked=True,
        is_mobile=True,
        map_type='',
        notes='card notes',
        league_season=None,
        league_tier=None,
        league_playoff=False,
        size=tuple(item.capacity for item in sides),
        game_channel_id=500,
        host_discord_id=101,
        host_name='Alpha',
        winner_side_id=None,
        status_label=status,
        result_label='',
        inferred_from_channel=False,
        cross_guild=False,
        sides=sides,
        pending_join_available=bool(pending and not full and not expired),
        pending_full=bool(pending and full),
        pending_creator_name='Alpha',
        pending_creator_discord_id=101,
    )


class FakeMessage:
    def __init__(self):
        self.attachments = []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self


class FakeResponse:
    def __init__(self):
        self.done = False
        self.calls = []
        self.modal = None

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.calls.append(('defer', kwargs))

    async def send_message(self, content, **kwargs):
        self.done = True
        self.calls.append(('send_message', content, kwargs))

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal
        self.calls.append(('send_modal', modal))


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, content=None, **kwargs):
        self.calls.append((content, kwargs))
        return FakeMessage()


def interaction(user_id: int = 900, *, message=None) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=FakeResponse(),
        followup=FakeFollowup(),
        message=message or FakeMessage(),
    )


def rendered(snapshot):
    guild = SimpleNamespace(
        id=10,
        name='Test Guild',
        get_member=lambda member_id: None,
        get_role=lambda role_id: None,
    )
    display = views.resolve_display(snapshot, guild=guild, prefix='!')
    return views.render_classic_game_detail(display)


def payload(snapshot):
    return actions.PendingGameCardPayload(snapshot, rendered(snapshot))


def make_view(
    snapshot,
    *,
    loader=None,
    on_join=None,
    on_leave=None,
    on_start=None,
    timeout=300,
):
    loader = loader or (lambda _interaction: _payload(snapshot))
    on_join = on_join or (lambda _interaction, _side: _success())
    on_leave = on_leave or (lambda _interaction: _success())
    on_start = on_start or (lambda _interaction, _name: _success())
    return actions.PendingGameCardView(
        snapshot=snapshot,
        load_card=loader,
        on_join=on_join,
        on_leave=on_leave,
        on_start=on_start,
        timeout=timeout,
    )


async def _success():
    return True


async def _payload(snapshot):
    return payload(snapshot)


class PendingGameCardStateTests(unittest.TestCase):
    def test_state_dependent_controls_are_exact_and_ordinary_views(self):
        cases = [
            (card_snapshot(), ['Join', 'Leave', 'Refresh']),
            (card_snapshot(full=True), ['Leave', 'Start', 'Refresh']),
            (card_snapshot(expired=True), ['Refresh']),
            (card_snapshot(pending=False), []),
        ]
        for snapshot_value, expected in cases:
            with self.subTest(status=snapshot_value.status_label):
                view = make_view(snapshot_value)
                self.assertIsInstance(view, discord.ui.View)
                self.assertNotIsInstance(view, discord.ui.LayoutView)
                self.assertEqual(
                    [child.label for child in view.children],
                    expected,
                )

    def test_classic_card_payload_preserves_renderer_and_only_adds_pending_view(self):
        pending_render = rendered(card_snapshot())
        completed_render = rendered(card_snapshot(pending=False))
        self.assertEqual(pending_render.content, 'Join game 77 with `!join 77`')
        self.assertEqual(completed_render.embed.title, rendered(card_snapshot(pending=False)).embed.title)
        pending_view = make_view(card_snapshot())
        self.assertEqual(
            pending_view.snapshot.name,
            'Pending card test',
        )

    def test_classic_edit_preserves_content_embed_and_unmanaged_attachments(self):
        render = rendered(card_snapshot())
        message = SimpleNamespace(attachments=[
            SimpleNamespace(filename='operator-note.txt'),
            SimpleNamespace(filename='team-logo-old.png'),
        ])
        edit_kwargs = views.classic_edit_kwargs(
            message,
            render,
            view=make_view(card_snapshot()),
        )
        self.assertEqual(edit_kwargs['content'], render.content)
        self.assertEqual(edit_kwargs['embed'].to_dict(), render.embed.to_dict())
        self.assertEqual(
            [attachment.filename for attachment in edit_kwargs['attachments']],
            ['operator-note.txt'],
        )
        self.assertIsNotNone(edit_kwargs['view'])


class PendingGameCardInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_card_can_be_used_by_a_different_member(self):
        snapshot_value = card_snapshot()
        events = []

        async def join(_interaction, side_arg):
            events.append(side_arg)
            return True

        view = make_view(snapshot_value, on_join=join)
        message = FakeMessage()
        view.message = message
        click = interaction(user_id=123, message=message)
        await view.join_button.callback(click)

        self.assertEqual(events, [None])
        self.assertEqual(click.response.calls[0][0], 'defer')
        self.assertEqual(len(message.edits), 1)
        self.assertEqual(
            [child.label for child in view.children],
            ['Join', 'Leave', 'Refresh'],
        )

    async def test_every_mutation_click_reloads_before_service_and_refreshes_after_commit(self):
        snapshots = [card_snapshot(), card_snapshot()]
        events = []

        async def loader(_interaction):
            events.append('load')
            return payload(snapshots.pop(0))

        async def join(_interaction, _side):
            events.append('service')
            return True

        async def refresh_loader(_interaction):
            events.append('refresh-load')
            return payload(card_snapshot())

        view = make_view(
            card_snapshot(),
            loader=loader,
            on_join=join,
        )
        view.load_card = loader
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)

        async def combined_loader(_interaction):
            if 'service' not in events:
                return await loader(_interaction)
            return await refresh_loader(_interaction)

        view.load_card = combined_loader
        await view.join_button.callback(click)

        self.assertEqual(events, ['load', 'service', 'refresh-load'])
        self.assertEqual(len(message.edits), 1)

    async def test_ambiguous_join_opens_ephemeral_side_selector_then_revalidates(self):
        events = []
        view = make_view(
            card_snapshot(ambiguous=True),
            loader=lambda _interaction: _payload(card_snapshot(ambiguous=True)),
            on_join=lambda _interaction, side_arg: _record_success(events, side_arg),
        )
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.join_button.callback(click)

        self.assertEqual(click.response.calls[0][0], 'defer')
        self.assertEqual(len(click.followup.calls), 1)
        selector = click.followup.calls[0][1]['view']
        self.assertIsInstance(selector, discord.ui.View)
        self.assertEqual(
            [option.value for option in selector.side_select.options],
            ['1', '2'],
        )
        selector.message = click.followup.calls[0][1].get('message')
        selector.side_select._values = ['2']
        selection = interaction(user_id=900, message=message)
        await selector.side_select.callback(selection)
        self.assertEqual(events, ['2'])
        self.assertEqual(selection.response.calls[0][0], 'defer')

    async def test_start_button_opens_modal_and_submission_defer_precedes_service(self):
        events = []

        async def start(_interaction, name):
            events.append(('service', name))
            return True

        view = make_view(card_snapshot(full=True), on_start=start)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.start_button.callback(click)
        self.assertEqual(click.response.calls[0][0], 'send_modal')
        modal = click.response.modal
        modal.game_name._value = 'Exact Polytopia Name'
        submission = interaction(message=message)
        await modal.on_submit(submission)
        self.assertEqual(events, [('service', 'Exact Polytopia Name')])
        self.assertEqual(submission.response.calls[0][0], 'defer')
        self.assertEqual(len(message.edits), 1)

    async def test_leave_and_start_are_routed_to_callbacks_and_public_then_refresh(self):
        events = []

        async def leave(_interaction):
            events.append('leave-service')
            return True

        async def start(_interaction, _name):
            events.append('start-service')
            return True

        full = card_snapshot(full=True)
        view = make_view(full, on_leave=leave, on_start=start)
        message = FakeMessage()
        view.message = message
        leave_click = interaction(message=message)
        await view.leave_button.callback(leave_click)
        self.assertEqual(events, ['leave-service'])
        self.assertEqual(len(message.edits), 1)

        view = make_view(full, on_leave=leave, on_start=start)
        view.message = message
        start_click = interaction(message=message)
        await view.start_button.callback(start_click)
        modal = start_click.response.modal
        modal.game_name._value = 'New Name'
        start_submission = interaction(message=message)
        await modal.on_submit(start_submission)
        self.assertEqual(events[-1], 'start-service')

    async def test_stale_state_rejects_without_mutation(self):
        events = []
        view = make_view(
            card_snapshot(),
            loader=lambda _interaction: _payload(card_snapshot(expired=True)),
            on_join=lambda _interaction, _side: _record_success(events, None),
        )
        click = interaction()
        await view.join_button.callback(click)
        self.assertEqual(events, [])
        self.assertTrue(click.followup.calls[-1][1]['ephemeral'])
        self.assertIn('no longer open', click.followup.calls[-1][0])

    async def test_service_failure_is_ephemeral_and_does_not_refresh(self):
        events = []

        async def join(_interaction, _side):
            events.append('service')
            return False

        view = make_view(card_snapshot(), on_join=join)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.join_button.callback(click)
        self.assertEqual(events, ['service'])
        self.assertEqual(message.edits, [])

    async def test_double_click_conflict_is_ephemeral_without_second_service_call(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def join(_interaction, _side):
            calls.append('service')
            entered.set()
            await release.wait()
            return True

        view = make_view(card_snapshot(), on_join=join)
        message = FakeMessage()
        view.message = message
        first = interaction(message=message)
        second = interaction(user_id=901, message=message)
        first_task = asyncio.create_task(view.join_button.callback(first))
        await entered.wait()
        await view.join_button.callback(second)
        self.assertEqual(calls, ['service'])
        self.assertTrue(second.response.calls)
        self.assertIn('already being processed', second.response.calls[0][1])
        release.set()
        await first_task

    async def test_timeout_disables_controls_and_points_to_rerun(self):
        view = make_view(card_snapshot(), timeout=1)
        message = FakeMessage()
        view.message = message
        await view.on_timeout()
        self.assertTrue(view.is_finished())
        self.assertTrue(all(child.disabled for child in view.children))
        self.assertEqual(len(message.edits), 1)
        click = interaction(message=message)
        await view.interaction_check(click)
        self.assertIn('expired', click.response.calls[0][1].lower())

    async def test_refresh_failure_is_ephemeral_after_acknowledgement(self):
        async def loader(_interaction):
            raise RuntimeError('read failed')

        view = make_view(card_snapshot(), loader=loader)
        click = interaction()
        await view.refresh_button.callback(click)
        self.assertEqual(click.response.calls[0][0], 'defer')
        self.assertTrue(click.followup.calls)
        self.assertTrue(click.followup.calls[-1][1]['ephemeral'])


async def _record_success(events, value):
    events.append(value)
    return True


class PendingGameCardAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_sender_attaches_view_only_to_pending_cards(self):
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            guilds=[],
            get_guild=lambda _guild_id: None,
            get_user=lambda _discord_id: None,
            get_channel=lambda _channel_id: None,
            get_cog=lambda _name: None,
        )
        cog._load_game_detail = mock.AsyncMock(return_value=card_snapshot())
        cog._game_detail_prefix = mock.Mock(return_value='!')
        guild = SimpleNamespace(
            id=10,
            get_member=lambda _member_id: None,
            get_role=lambda _role_id: None,
        )
        target = SimpleNamespace(
            send=mock.AsyncMock(return_value=FakeMessage()),
            edit_original_response=mock.AsyncMock(return_value=FakeMessage()),
        )
        self.assertTrue(await cog._send_game_detail(
            target,
            guild=guild,
            requester_id=900,
            channel_id=500,
            game_id=77,
            slash=True,
        ))
        pending_kwargs = target.edit_original_response.await_args.kwargs
        self.assertIsInstance(pending_kwargs['view'], discord.ui.View)
        self.assertNotIsInstance(pending_kwargs['view'], discord.ui.LayoutView)

        target = SimpleNamespace(
            send=mock.AsyncMock(return_value=FakeMessage()),
            edit_original_response=mock.AsyncMock(return_value=FakeMessage()),
        )
        cog._load_game_detail = mock.AsyncMock(
            return_value=card_snapshot(pending=False),
        )
        self.assertTrue(await cog._send_game_detail(
            target,
            guild=guild,
            requester_id=900,
            channel_id=500,
            game_id=77,
            slash=True,
        ))
        self.assertNotIn('view', target.edit_original_response.await_args.kwargs)

        target = SimpleNamespace(send=mock.AsyncMock(return_value=FakeMessage()))
        cog._load_game_detail = mock.AsyncMock(return_value=card_snapshot())
        self.assertTrue(await cog._send_game_detail(
            target,
            guild=guild,
            requester_id=900,
            channel_id=500,
            game_id=77,
        ))
        self.assertNotIn('view', target.send.await_args.kwargs)
        prefix_kwargs = target.send.await_args.kwargs
        prefix_render = views.render_classic_game_detail(
            views.resolve_display(
                card_snapshot(),
                guild=guild,
                prefix='!',
                join_emoji=getattr(games.settings, 'emoji_join_game', ''),
            )
        )
        self.assertEqual(prefix_kwargs['content'], prefix_render.content)
        self.assertEqual(prefix_kwargs['embed'].to_dict(), prefix_render.embed.to_dict())

    async def test_adapter_validation_failure_is_ephemeral(self):
        cog = games.polygames.__new__(games.polygames)
        interaction_value = interaction()
        matchmaking = SimpleNamespace(
            execute_join=mock.AsyncMock(
                side_effect=games.game_join_workers.PendingGameJoinValidationError(
                    'not allowed',
                ),
            ),
        )
        cog.bot = SimpleNamespace(get_cog=lambda _name: matchmaking)
        result = await cog._pending_card_join(
            interaction_value,
            game_id=77,
            prefix='!',
            side_arg=None,
        )
        self.assertFalse(result)
        self.assertEqual(interaction_value.followup.calls[-1][0], 'not allowed')
        self.assertTrue(interaction_value.followup.calls[-1][1]['ephemeral'])

    async def test_card_mutation_adapters_call_existing_services_and_skip_second_card(self):
        cog = games.polygames.__new__(games.polygames)
        interaction_value = interaction()
        matchmaking = SimpleNamespace(
            execute_join=mock.AsyncMock(return_value=SimpleNamespace()),
            execute_leave=mock.AsyncMock(return_value=SimpleNamespace()),
            execute_start=mock.AsyncMock(return_value=SimpleNamespace()),
        )
        cog.bot = SimpleNamespace(get_cog=lambda name: matchmaking)
        cog._publish_native_join_result = mock.AsyncMock()
        cog._publish_native_leave_result = mock.AsyncMock()
        with (
            mock.patch.object(
                games.game_start,
                'publish_start_result',
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                games.game_start,
                'native_output_context',
                return_value=object(),
            ),
        ):
            self.assertTrue(await cog._pending_card_join(
                interaction_value,
                game_id=77,
                prefix='!',
                side_arg='2',
            ))
            self.assertTrue(await cog._pending_card_leave(
                interaction_value,
                game_id=77,
                prefix='!',
            ))
            self.assertTrue(await cog._pending_card_start(
                interaction_value,
                guild=SimpleNamespace(id=10),
                game_id=77,
                prefix='!',
                name='Exact Name',
            ))
        matchmaking.execute_join.assert_awaited_once()
        self.assertEqual(matchmaking.execute_join.await_args.kwargs['side_arg'], '2')
        matchmaking.execute_leave.assert_awaited_once()
        matchmaking.execute_start.assert_awaited_once()
        cog._publish_native_join_result.assert_awaited_once_with(
            interaction_value,
            mock.ANY,
            member=interaction_value.user,
            prefix='!',
            publish_card=False,
        )


if __name__ == '__main__':
    unittest.main()
