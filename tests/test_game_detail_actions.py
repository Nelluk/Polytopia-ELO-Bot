"""Focused offline coverage for interactive pending-game cards."""

import asyncio
from dataclasses import replace
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
    completed: bool | None = None,
    reported: bool = False,
) -> workers.GameDetailSnapshot:
    if completed is None:
        # Keep the pre-P5.7 helper's non-pending default as a completed card;
        # tests that need the new action opt into an incomplete snapshot.
        completed = not pending
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

    if pending:
        status = 'Expired open game' if expired else (
            'Full — waiting to start' if full else 'Open'
        )
    elif completed or reported:
        status = 'Unconfirmed winner report' if reported else 'Completed'
    else:
        status = 'Incomplete'
    is_completed = bool(completed or reported)
    is_confirmed = bool(completed and not reported)
    winner_side_id = 101 if is_completed else None
    return workers.GameDetailSnapshot(
        game_id=77,
        guild_id=10,
        name='Pending card test',
        date='2026-08-01',
        completed_ts='2026-08-01' if is_completed else '',
        win_claimed_ts='2026-08-01' if is_completed else '',
        expiration='2099-01-01 00:00:00',
        is_pending=pending,
        is_completed=is_completed,
        is_confirmed=is_confirmed,
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
        winner_side_id=winner_side_id,
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
        self.reactions = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        return self

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class OrderedMessage(FakeMessage):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def edit(self, **kwargs):
        self.events.append('confirmation-edit')
        return await super().edit(**kwargs)


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
    on_delete_prepare=None,
    on_delete=None,
    on_winner=None,
    timeout=300,
):
    loader = loader or (lambda _interaction: _payload(snapshot))
    on_join = on_join or (lambda _interaction, _side: _success())
    on_leave = on_leave or (lambda _interaction: _success())
    on_start = on_start or (lambda _interaction, _name: _success())
    on_delete_prepare = on_delete_prepare or (lambda _interaction: _success())
    on_delete = on_delete or (lambda _interaction: _success())
    on_winner = on_winner or (
        lambda _interaction, _side_id, _winner_label: _success()
    )
    return actions.PendingGameCardView(
        snapshot=snapshot,
        load_card=loader,
        on_join=on_join,
        on_leave=on_leave,
        on_start=on_start,
        on_delete_prepare=on_delete_prepare,
        on_delete=on_delete,
        on_winner=on_winner,
        timeout=timeout,
    )


async def _success():
    return True


async def _payload(snapshot):
    return payload(snapshot)


class PendingGameCardStateTests(unittest.TestCase):
    def test_winner_options_cover_solo_team_uneven_and_multi_side_rosters(self):
        snapshot_value = card_snapshot(pending=False, completed=False)
        team_side = side(
            1,
            capacity=3,
            lineups=(lineup(101, 'Alpha'), lineup(102, 'Beta')),
            sidename='Home Team',
        )
        uneven_side = side(
            2,
            capacity=1,
            lineups=(lineup(202, 'Gamma'),),
            sidename='Away Team',
        )
        multi_side = side(
            3,
            capacity=1,
            lineups=(lineup(303, 'Delta'),),
            sidename='Third Side',
        )
        snapshot_value = replace(
            snapshot_value,
            sides=(team_side, uneven_side, multi_side),
        )
        options = actions._winner_side_options(snapshot_value)
        self.assertEqual([option.value for option in options], [
            '101', '102', '103',
        ])
        self.assertIn('Home Team', options[0].label)
        self.assertIn('Alpha', options[0].description)
        self.assertIn('2/3', options[0].description)
        self.assertIn('Away Team', options[1].label)
        self.assertIn('Third Side', options[2].label)

    def test_state_dependent_controls_are_exact_and_ordinary_views(self):
        cases = [
            (card_snapshot(), ['Join', 'Leave', 'Delete', 'Refresh']),
            (card_snapshot(full=True), ['Leave', 'Start', 'Delete', 'Refresh']),
            (card_snapshot(expired=True), ['Delete', 'Refresh']),
            (card_snapshot(pending=False, completed=False), [
                'Declare Winner', 'Refresh',
            ]),
            (card_snapshot(pending=False, reported=True), []),
            (card_snapshot(pending=False), []),
            (replace(
                card_snapshot(pending=False, completed=False),
                cross_guild=True,
            ), []),
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
    async def test_winner_selection_uses_stable_side_ids_and_requester_only_confirmation(self):
        events = []

        async def winner(_interaction, side_id, winner_label):
            events.append(('winner', side_id, winner_label))
            return True

        snapshot_value = card_snapshot(pending=False, completed=False)
        view = make_view(
            snapshot_value,
            on_winner=winner,
            loader=lambda _interaction: _payload(snapshot_value),
        )
        message = FakeMessage()
        view.message = message

        public_click = interaction(user_id=901, message=message)
        await view.declare_winner_button.callback(public_click)
        self.assertEqual(public_click.response.calls[0][0], 'defer')
        selector = public_click.followup.calls[0][1]['view']
        self.assertIsInstance(
            selector,
            actions.InProgressWinnerSideSelectView,
        )
        self.assertFalse(view._busy)
        self.assertEqual(
            [option.value for option in selector.side_select.options],
            ['101', '102'],
        )
        self.assertTrue(all(
            'Side ' in option.label
            for option in selector.side_select.options
        ))

        other_public_click = interaction(user_id=902, message=message)
        await view.declare_winner_button.callback(other_public_click)
        self.assertEqual(other_public_click.response.calls[0][0], 'defer')
        self.assertIsInstance(
            other_public_click.followup.calls[0][1]['view'],
            actions.InProgressWinnerSideSelectView,
        )

        other = interaction(user_id=902, message=message)
        self.assertFalse(await selector.interaction_check(other))
        self.assertIn('Only the member', other.response.calls[0][1])

        selector.side_select._values = ['102']
        selection = interaction(user_id=901, message=message)
        await selector.side_select.callback(selection)
        self.assertEqual(selection.response.calls[0][0], 'defer')
        confirmation = selection.followup.calls[0][1]['view']
        self.assertIsInstance(
            confirmation,
            actions.DeclareWinnerConfirmationView,
        )
        self.assertEqual(confirmation.winning_side_id, 102)

        other_confirmation = interaction(user_id=902, message=message)
        self.assertFalse(await confirmation.interaction_check(other_confirmation))
        self.assertIn('Only the member', other_confirmation.response.calls[0][1])

        submit = interaction(user_id=901, message=message)
        await confirmation.confirm_button.callback(submit)
        self.assertEqual(events[0][0], 'winner')
        self.assertEqual(events[0][1], 102)
        self.assertEqual(submit.response.calls[0][0], 'defer')
        self.assertFalse(view._busy)

    async def test_winner_confirmation_defers_before_edit_and_service(self):
        events = []

        async def winner(_interaction, _side_id, _winner_label):
            events.append('service')
            return True

        snapshot_value = card_snapshot(pending=False, completed=False)
        view = make_view(snapshot_value, on_winner=winner)
        source_message = FakeMessage()
        view.message = source_message
        click = interaction(message=source_message)
        await view.declare_winner_button.callback(click)
        selector = click.followup.calls[0][1]['view']
        selector.side_select._values = ['101']
        selection = interaction(message=source_message)
        await selector.side_select.callback(selection)
        confirmation = selection.followup.calls[0][1]['view']
        confirmation.message = OrderedMessage(events)

        submit = interaction(message=source_message)
        original_defer = submit.response.defer

        async def defer(**kwargs):
            events.append('defer')
            await original_defer(**kwargs)

        submit.response.defer = defer
        await confirmation.confirm_button.callback(submit)
        self.assertEqual(events[:3], [
            'defer',
            'confirmation-edit',
            'service',
        ])

    async def test_successful_first_claim_refresh_removes_dead_winner_control(self):
        events = []
        eligible = card_snapshot(pending=False, completed=False)
        reported = card_snapshot(pending=False, reported=True)
        snapshots = [eligible, eligible, reported]

        async def loader(_interaction):
            return payload(snapshots.pop(0))

        async def winner(_interaction, _side_id, _winner_label):
            events.append('service')
            return True

        view = make_view(eligible, loader=loader, on_winner=winner)
        source_message = FakeMessage()
        view.message = source_message
        click = interaction(message=source_message)
        await view.declare_winner_button.callback(click)
        selector = click.followup.calls[0][1]['view']
        selector.side_select._values = ['101']
        selection = interaction(message=source_message)
        await selector.side_select.callback(selection)
        confirmation = selection.followup.calls[0][1]['view']
        submit = interaction(message=source_message)
        await confirmation.confirm_button.callback(submit)

        self.assertEqual(events, ['service'])
        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])
        self.assertIsNone(source_message.edits[-1]['view'])

    async def test_stale_or_invalid_winner_claim_has_no_service_or_success_refresh(self):
        events = []

        async def winner(_interaction, _side_id, _winner_label):
            events.append('service')
            return True

        view = make_view(
            card_snapshot(pending=False, completed=False),
            loader=lambda _interaction: _payload(
                card_snapshot(pending=False, reported=True),
            ),
            on_winner=winner,
        )
        source_message = FakeMessage()
        view.message = source_message
        stale_click = interaction(message=source_message)
        await view.declare_winner_button.callback(stale_click)
        self.assertEqual(events, [])
        self.assertTrue(view.is_finished())
        self.assertIsNone(source_message.edits[-1]['view'])

        invalid_view = make_view(
            card_snapshot(pending=False, completed=False),
            on_winner=winner,
        )
        invalid_view.message = FakeMessage()
        invalid = interaction(message=invalid_view.message)
        await invalid_view.run_winner(
            invalid,
            winning_side_id=999,
            winner_label='not a side',
        )
        self.assertEqual(events, [])
        self.assertTrue(invalid.followup.calls)
        self.assertIn('no longer part', invalid.followup.calls[-1][0])

    async def test_timeout_during_winner_service_cannot_restore_dead_controls(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def winner(_interaction, _side_id, _winner_label):
            entered.set()
            await release.wait()
            return True

        snapshot_value = card_snapshot(pending=False, completed=False)
        view = make_view(snapshot_value, on_winner=winner)
        source_message = FakeMessage()
        view.message = source_message
        click = interaction(message=source_message)
        await view.declare_winner_button.callback(click)
        selector = click.followup.calls[0][1]['view']
        selector.side_select._values = ['101']
        selection = interaction(message=source_message)
        await selector.side_select.callback(selection)
        confirmation = selection.followup.calls[0][1]['view']
        submit = interaction(message=source_message)
        task = asyncio.create_task(confirmation.confirm_button.callback(submit))
        await entered.wait()

        await view.on_timeout()
        release.set()
        await task

        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])
        self.assertIsNone(source_message.edits[-1]['view'])

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
            ['Join', 'Leave', 'Delete', 'Refresh'],
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

    async def test_denied_start_does_not_refresh_or_mutate_card(self):
        async def deny_start(_interaction, _name):
            return False

        view = make_view(card_snapshot(full=True), on_start=deny_start)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.start_button.callback(click)
        modal = click.response.modal
        modal.game_name._value = 'Exact Polytopia Name'
        submission = interaction(message=message)
        await modal.on_submit(submission)

        self.assertEqual(message.edits, [])

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

    async def test_delete_click_requires_confirmation_and_confirm_removes_card_controls(self):
        events = []

        async def prepare(_interaction):
            events.append('prepare')
            return True

        async def delete(_interaction):
            events.append('delete')
            return True

        view = make_view(
            card_snapshot(),
            on_delete_prepare=prepare,
            on_delete=delete,
        )
        message = FakeMessage()
        view.message = message
        click = interaction(user_id=900, message=message)
        await view.delete_button.callback(click)

        self.assertEqual(events, ['prepare'])
        self.assertTrue(view._busy)
        self.assertEqual(click.response.calls[0][0], 'defer')
        self.assertEqual(len(click.followup.calls), 1)
        confirmation = click.followup.calls[0][1]['view']
        self.assertIsInstance(confirmation, actions.PendingGameDeleteConfirmationView)

        other = interaction(user_id=901, message=message)
        self.assertFalse(await confirmation.interaction_check(other))
        self.assertIn('Only the member', other.response.calls[0][1])
        self.assertEqual(events, ['prepare'])

        confirm = interaction(user_id=900, message=message)
        await confirmation.confirm_button.callback(confirm)
        self.assertEqual(events, ['prepare', 'delete'])
        self.assertFalse(view._busy)
        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])
        self.assertIsNone(message.edits[-1]['view'])

    async def test_delete_confirmation_defers_before_edit_and_deletion(self):
        events = []

        async def delete(_interaction):
            events.append('delete')
            return True

        view = make_view(card_snapshot(), on_delete=delete)
        message = FakeMessage()
        view.message = message
        click = interaction(user_id=900, message=message)
        await view.delete_button.callback(click)
        confirmation = click.followup.calls[0][1]['view']
        confirmation.message = OrderedMessage(events)

        confirm = interaction(user_id=900, message=message)
        original_defer = confirm.response.defer

        async def defer(**kwargs):
            events.append('defer')
            await original_defer(**kwargs)

        confirm.response.defer = defer
        await confirmation.confirm_button.callback(confirm)

        self.assertEqual(events[:3], [
            'defer',
            'confirmation-edit',
            'delete',
        ])
        self.assertFalse(view._busy)
        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])

    async def test_delete_cancellation_releases_claim_without_public_card_change(self):
        view = make_view(card_snapshot())
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.delete_button.callback(click)
        confirmation = click.followup.calls[0][1]['view']

        cancel = interaction(message=message)
        await confirmation.cancel_button.callback(cancel)
        self.assertFalse(view._busy)
        self.assertFalse(view.is_finished())
        self.assertEqual([child.label for child in view.children], [
            'Join', 'Leave', 'Delete', 'Refresh',
        ])
        self.assertEqual(cancel.followup.calls[-1][0], 'Game deletion cancelled.')

    async def test_delete_cancellation_defers_before_edit_and_releases_claim(self):
        events = []
        view = make_view(card_snapshot())
        message = FakeMessage()
        view.message = message
        click = interaction(user_id=900, message=message)
        await view.delete_button.callback(click)
        confirmation = click.followup.calls[0][1]['view']
        confirmation.message = OrderedMessage(events)

        cancel = interaction(user_id=900, message=message)
        original_defer = cancel.response.defer

        async def defer(**kwargs):
            events.append('defer')
            await original_defer(**kwargs)

        cancel.response.defer = defer
        original_send = cancel.followup.send

        async def send(content=None, **kwargs):
            events.append('cancel')
            return await original_send(content, **kwargs)

        cancel.followup.send = send
        await confirmation.cancel_button.callback(cancel)

        self.assertEqual(events, ['defer', 'confirmation-edit', 'cancel'])
        self.assertEqual(cancel.followup.calls[-1][0], 'Game deletion cancelled.')
        self.assertFalse(view._busy)
        self.assertFalse(view.is_finished())

    async def test_delete_stale_card_rejects_before_authorization_or_mutation(self):
        events = []

        async def prepare(_interaction):
            events.append('prepare')
            return True

        async def delete(_interaction):
            events.append('delete')
            return True

        view = make_view(
            card_snapshot(),
            loader=lambda _interaction: _payload(card_snapshot(pending=False)),
            on_delete_prepare=prepare,
            on_delete=delete,
        )
        click = interaction()
        await view.delete_button.callback(click)
        self.assertEqual(events, [])
        self.assertFalse(view._busy)
        self.assertIn('no longer pending', click.followup.calls[-1][0])

    async def test_delete_double_click_is_promptly_rejected_while_confirmation_loads(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def prepare(_interaction):
            entered.set()
            await release.wait()
            return True

        view = make_view(
            card_snapshot(),
            on_delete_prepare=prepare,
        )
        message = FakeMessage()
        view.message = message
        first = interaction(user_id=900, message=message)
        second = interaction(user_id=901, message=message)
        first_task = asyncio.create_task(view.delete_button.callback(first))
        await entered.wait()
        await view.delete_button.callback(second)
        self.assertEqual(second.response.calls[0][0], 'send_message')
        self.assertIn('already being processed', second.response.calls[0][1])
        release.set()
        await first_task
        self.assertTrue(first.followup.calls)

    async def test_delete_failure_keeps_card_and_has_no_success_effect(self):
        calls = []

        async def delete(_interaction):
            calls.append('delete')
            return False

        view = make_view(card_snapshot(), on_delete=delete)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.delete_button.callback(click)
        confirmation = click.followup.calls[0][1]['view']
        confirm = interaction(message=message)
        await confirmation.confirm_button.callback(confirm)

        self.assertEqual(calls, ['delete'])
        self.assertFalse(view._busy)
        self.assertFalse(view.is_finished())
        self.assertEqual(message.edits, [])
        self.assertTrue(view.children)

    async def test_delete_confirmation_timeout_releases_claim_and_parent_timeout_wins_race(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delete(_interaction):
            entered.set()
            await release.wait()
            return True

        view = make_view(card_snapshot(), on_delete=delete)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        await view.delete_button.callback(click)
        confirmation = click.followup.calls[0][1]['view']
        confirm = interaction(message=message)
        task = asyncio.create_task(confirmation.confirm_button.callback(confirm))
        await entered.wait()

        await view.on_timeout()
        self.assertTrue(view.is_finished())
        release.set()
        await task
        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])
        self.assertIsNone(message.edits[-1]['view'])

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

    async def test_timeout_during_action_keeps_final_card_free_of_controls(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def join(_interaction, _side):
            entered.set()
            await release.wait()
            return True

        view = make_view(card_snapshot(), on_join=join)
        message = FakeMessage()
        view.message = message
        click = interaction(message=message)
        action_task = asyncio.create_task(view.join_button.callback(click))
        await entered.wait()

        await view.on_timeout()
        self.assertTrue(view.is_finished())
        release.set()
        await action_task

        self.assertTrue(view.is_finished())
        self.assertEqual(view.children, [])
        self.assertIsNone(message.edits[-1]['view'])
        self.assertEqual(
            message.edits[-1]['content'],
            rendered(card_snapshot()).content,
        )

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
        self.assertFalse(view.is_finished())
        self.assertTrue(view.children)


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
        pending_message = FakeMessage()
        target = SimpleNamespace(
            send=mock.AsyncMock(return_value=FakeMessage()),
            edit_original_response=mock.AsyncMock(return_value=pending_message),
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
        self.assertEqual(
            pending_message.reactions,
            [games.settings.emoji_join_game],
        )

        completed_message = FakeMessage()
        target = SimpleNamespace(
            send=mock.AsyncMock(return_value=FakeMessage()),
            edit_original_response=mock.AsyncMock(return_value=completed_message),
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
        self.assertEqual(completed_message.reactions, [])

        in_progress_message = FakeMessage()
        target = SimpleNamespace(
            send=mock.AsyncMock(return_value=FakeMessage()),
            edit_original_response=mock.AsyncMock(
                return_value=in_progress_message,
            ),
        )
        cog._load_game_detail = mock.AsyncMock(
            return_value=card_snapshot(pending=False, completed=False),
        )
        self.assertTrue(await cog._send_game_detail(
            target,
            guild=guild,
            requester_id=900,
            channel_id=500,
            game_id=77,
            slash=True,
        ))
        self.assertEqual(
            [child.label for child in target.edit_original_response.await_args.kwargs['view'].children],
            ['Declare Winner', 'Refresh'],
        )

        prefix_message = FakeMessage()
        target = SimpleNamespace(send=mock.AsyncMock(return_value=prefix_message))
        cog._load_game_detail = mock.AsyncMock(
            return_value=card_snapshot(pending=False, completed=False),
        )
        self.assertTrue(await cog._send_game_detail(
            target,
            guild=guild,
            requester_id=900,
            channel_id=500,
            game_id=77,
        ))
        self.assertNotIn('view', target.send.await_args.kwargs)
        self.assertEqual(prefix_message.reactions, [])
        prefix_kwargs = target.send.await_args.kwargs
        in_progress_snapshot = card_snapshot(
            pending=False,
            completed=False,
        )
        prefix_render = views.render_classic_game_detail(
            views.resolve_display(
                in_progress_snapshot,
                guild=guild,
                prefix='!',
                join_emoji=getattr(games.settings, 'emoji_join_game', ''),
            )
        )
        self.assertEqual(prefix_kwargs['content'], prefix_render.content)
        self.assertEqual(prefix_kwargs['embed'].to_dict(), prefix_render.embed.to_dict())

    async def test_reaction_seeding_skips_ineligible_pending_states(self):
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            guilds=[],
            get_guild=lambda _guild_id: None,
            get_user=lambda _discord_id: None,
            get_channel=lambda _channel_id: None,
            get_cog=lambda _name: None,
        )
        guild = SimpleNamespace(
            id=10,
            get_member=lambda _member_id: None,
            get_role=lambda _role_id: None,
        )

        for snapshot in (
            card_snapshot(full=True),
            card_snapshot(expired=True),
            card_snapshot(pending=False),
        ):
            with self.subTest(status=snapshot.status_label):
                message = FakeMessage()
                target = SimpleNamespace(
                    edit_original_response=mock.AsyncMock(return_value=message),
                )
                cog._load_game_detail = mock.AsyncMock(return_value=snapshot)
                self.assertTrue(await cog._send_game_detail(
                    target,
                    guild=guild,
                    requester_id=900,
                    channel_id=500,
                    game_id=77,
                    slash=True,
                ))
                self.assertEqual(message.reactions, [])

    async def test_reaction_failure_keeps_native_card_successful(self):
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            guilds=[],
            get_guild=lambda _guild_id: None,
            get_user=lambda _discord_id: None,
            get_channel=lambda _channel_id: None,
            get_cog=lambda _name: None,
        )
        guild = SimpleNamespace(
            id=10,
            get_member=lambda _member_id: None,
            get_role=lambda _role_id: None,
        )
        message = FakeMessage()
        message.add_reaction = mock.AsyncMock(
            side_effect=RuntimeError('reaction unavailable'),
        )
        target = SimpleNamespace(
            edit_original_response=mock.AsyncMock(return_value=message),
        )
        cog._load_game_detail = mock.AsyncMock(return_value=card_snapshot())

        with self.assertLogs(games.logger.name, level='ERROR') as logs:
            self.assertTrue(await cog._send_game_detail(
                target,
                guild=guild,
                requester_id=900,
                channel_id=500,
                game_id=77,
                slash=True,
            ))

        self.assertIn('retain button and prefix fallback', '\n'.join(logs.output))
        self.assertIn('view', target.edit_original_response.await_args.kwargs)
        message.add_reaction.assert_awaited_once_with(
            games.settings.emoji_join_game,
        )

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
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=True,
        )
        result = await cog._pending_card_join(
            interaction_value,
            game_id=77,
            prefix='!',
            side_arg=None,
        )
        self.assertFalse(result)
        self.assertEqual(interaction_value.followup.calls[-1][0], 'not allowed')
        self.assertTrue(interaction_value.followup.calls[-1][1]['ephemeral'])

    async def test_card_mutations_apply_channel_policy_before_services(self):
        cog = games.polygames.__new__(games.polygames)
        matchmaking = SimpleNamespace(
            execute_join=mock.AsyncMock(),
            execute_leave=mock.AsyncMock(),
            execute_start=mock.AsyncMock(),
        )
        cog.bot = SimpleNamespace(get_cog=lambda _name: matchmaking)

        async def deny(interaction_value):
            interaction_value.guild = SimpleNamespace(id=10)
            interaction_value.channel_id = 999
            return interaction_value

        interactions = [await deny(interaction()) for _ in range(3)]
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda _guild_id, name: (
                [100] if name == 'bot_channels' else []
            ),
        ), mock.patch.object(games.settings, 'is_mod', return_value=False):
            self.assertFalse(await cog._pending_card_join(
                interactions[0],
                game_id=77,
                prefix='!',
                side_arg=None,
            ))
            self.assertFalse(await cog._pending_card_leave(
                interactions[1],
                game_id=77,
                prefix='!',
            ))
            self.assertFalse(await cog._pending_card_start(
                interactions[2],
                guild=SimpleNamespace(id=10),
                game_id=77,
                prefix='!',
                name='Exact Name',
            ))

        for interaction_value in interactions:
            self.assertTrue(interaction_value.followup.calls[-1][1]['ephemeral'])
            self.assertIn(
                'designated ELO bot channel',
                interaction_value.followup.calls[-1][0],
            )
        matchmaking.execute_join.assert_not_awaited()
        matchmaking.execute_leave.assert_not_awaited()
        matchmaking.execute_start.assert_not_awaited()

    async def test_card_mutation_adapters_call_existing_services_and_skip_second_card(self):
        cog = games.polygames.__new__(games.polygames)
        interaction_value = interaction()
        matchmaking = SimpleNamespace(
            execute_join=mock.AsyncMock(return_value=SimpleNamespace()),
            execute_leave=mock.AsyncMock(return_value=SimpleNamespace()),
            execute_start=mock.AsyncMock(return_value=SimpleNamespace()),
        )
        cog.bot = SimpleNamespace(get_cog=lambda name: matchmaking)
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=True,
        )
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
        self.assertEqual(cog._native_pending_game_channel_allowed.await_count, 3)
        cog._publish_native_join_result.assert_awaited_once_with(
            interaction_value,
            mock.ANY,
            member=interaction_value.user,
            prefix='!',
            publish_card=False,
        )

    async def test_winner_card_adapter_routes_to_shared_win_service(self):
        cog = games.polygames.__new__(games.polygames)
        interaction_value = interaction()
        interaction_value.guild = SimpleNamespace(id=10)
        interaction_value.channel = SimpleNamespace()
        cog._native_winner_game_channel_allowed = mock.AsyncMock(
            return_value=True,
        )

        request = object()
        with mock.patch.object(
            games.game_win,
            'build_request',
            return_value=request,
        ) as build_request, mock.patch.object(
            games.game_win,
            'run_win',
            new=mock.AsyncMock(
                return_value=games.game_win.WinApplicationOutcome(
                    result=object(),
                    public_effects_published=True,
                ),
            ),
        ) as run_win:
            result = await cog._pending_card_winner(
                interaction_value,
                game_id=77,
                prefix='!',
                winning_side_id=102,
                winner_label='Side 2 — Blue',
            )

        self.assertTrue(result)
        build_request.assert_called_once_with(
            game_id=77,
            member=interaction_value.user,
            guild_id=10,
            prefix='!',
            winner_text='Side 2 — Blue',
            winning_side_id=102,
            invoked_with='/game show Declare Winner',
        )
        run_win.assert_awaited_once()
        self.assertIs(run_win.await_args.args[0], request)
        self.assertTrue(run_win.await_args.kwargs['acknowledged'])

    async def test_winner_card_adapter_does_not_refresh_after_publish_reconciliation(self):
        cog = games.polygames.__new__(games.polygames)
        interaction_value = interaction()
        interaction_value.guild = SimpleNamespace(id=10)
        interaction_value.channel = SimpleNamespace()
        cog._native_winner_game_channel_allowed = mock.AsyncMock(
            return_value=True,
        )

        with (
            mock.patch.object(
                games.game_win,
                'build_request',
                return_value=object(),
            ),
            mock.patch.object(
                games.game_win,
                'run_win',
                new=mock.AsyncMock(
                    return_value=games.game_win.WinApplicationOutcome(
                        result=object(),
                        public_effects_published=False,
                    ),
                ),
            ) as run_win,
        ):
            result = await cog._pending_card_winner(
                interaction_value,
                game_id=77,
                prefix='!',
                winning_side_id=102,
                winner_label='Side 2 — Blue',
            )

        self.assertFalse(result)
        run_win.assert_awaited_once()
        self.assertFalse(any(
            'No public game change' in str(call[0])
            for call in interaction_value.followup.calls
        ))

    async def test_winner_card_uses_strict_channels_and_mod_bypass(self):
        cog = games.polygames.__new__(games.polygames)
        denied = interaction()
        denied.guild = SimpleNamespace(id=10)
        # This channel is accepted by general bot_channels but rejected by
        # the stricter policy used by the existing win commands.
        denied.channel_id = 100

        def guild_setting(_guild_id, name):
            return {
                'bot_channels': [100],
                'bot_channels_strict': [300],
                'bot_channels_private': [],
            }[name]

        with (
            mock.patch.object(
                games.settings,
                'guild_setting',
                side_effect=guild_setting,
            ),
            mock.patch.object(games.settings, 'is_mod', return_value=False),
        ):
            self.assertFalse(
                await cog._native_winner_game_channel_allowed(denied),
            )

        self.assertIn('<#300>', denied.followup.calls[-1][0])
        self.assertIn('bot spam channel', denied.followup.calls[-1][0])
        self.assertTrue(denied.followup.calls[-1][1]['ephemeral'])

        mod = interaction()
        mod.guild = SimpleNamespace(id=10)
        mod.channel_id = 100
        with (
            mock.patch.object(
                games.settings,
                'guild_setting',
                side_effect=guild_setting,
            ),
            mock.patch.object(games.settings, 'is_mod', return_value=True),
        ):
            self.assertTrue(
                await cog._native_winner_game_channel_allowed(mod),
            )
        self.assertEqual(mod.followup.calls, [])


if __name__ == '__main__':
    unittest.main()
