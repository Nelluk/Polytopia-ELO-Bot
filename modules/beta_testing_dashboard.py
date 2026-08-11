"""Requester-bound compact dashboard for the development Beta Lab."""

from __future__ import annotations

import logging

import discord

from modules import (
    beta_feedback_views,
    beta_lab_catalog,
    beta_lab_sessions,
    beta_lab_workers,
    beta_testing_guide,
)


logger = logging.getLogger('polybot.' + __name__)

_STATE_ICON = {
    'ready': '✅',
    'refreshable': '🔄',
    'missing': '⚠️',
    'blocked': '⛔',
}


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def overview_markdown(status: beta_lab_workers.BetaLabStatus) -> str:
    lines = [
        '# 🧪 Beta Lab',
        '**Start here:** choose **Give me a 5-minute test**, or create a '
        'private game lane for result-changing commands.',
        '',
        f'**Lab health:** {status.overall.title()}',
    ]
    for pack in status.packs:
        lines.append(
            f'{_STATE_ICON.get(pack.state, "•")} {_safe(pack.title)} '
            f'— {_safe(pack.state.title())}'
        )
    snapshot = status.result_snapshot
    if snapshot is not None and snapshot.scenarios:
        lines.extend(('', '**Shared operator scenarios**'))
        lines.extend(
            f'- {item.scenario.title()}: game `{item.game_id}` ({_safe(item.status)})'
            for item in snapshot.scenarios
        )
        if snapshot.participants:
            lines.append(
                '-# Participants: '
                + ', '.join(
                    f'{_safe(item.display_name)} (`{item.user_id}`)'
                    for item in snapshot.participants
                )
            )
    lines.extend((
        '',
        '-# The area menu keeps the full reference checklist available without '
        'posting it into the channel. Report anything confusing directly from '
        'this panel.',
    ))
    return '\n'.join(lines)


def quick_test_markdown(test: beta_lab_catalog.QuickTest) -> str:
    lines = [
        f'# ⚡ {_safe(test.title)}',
        f'**{_safe(test.duration)}**',
        '',
    ]
    lines.extend(
        f'{number}. {_safe(step)}'
        for number, step in enumerate(test.steps, start=1)
    )
    lines.extend((
        '',
        '-# Click the same button for another test. Use Report problem without '
        'leaving this panel.',
    ))
    return '\n'.join(lines)


def lane_markdown(session: beta_lab_sessions.BetaLabSessionSnapshot) -> str:
    scenario_by_name = {item.scenario: item for item in session.scenarios}
    lines = [
        '# 🎮 My game lane',
        f'**Tester:** {_safe(session.requester_name)}',
        f'**Fixture opponent:** {_safe(session.opponent_name)}',
        f'**Lane:** `{session.session_id}` • {session.state} • expires '
        f'<t:{session.expires_epoch}:R>',
        '',
        'Each game starts in a different result state:',
    ]
    instructions = {
        'ready': 'Run `/game win GAME_ID` and follow the result flow.',
        'unconfirmed': 'Run `/game result confirm GAME_ID`.',
        'completed': 'Run `/game result undo GAME_ID`.',
    }
    for scenario in beta_lab_sessions.SCENARIOS:
        item = scenario_by_name.get(scenario)
        if item is None:
            continue
        lines.append(
            f'- **{scenario.title()}** — game `{item.game_id}` '
            f'({_safe(item.status)}): {instructions[scenario].replace("GAME_ID", str(item.game_id))}'
        )
    lines.extend((
        '',
        'When done, choose **Finished**. Choose **Release lane** if you are '
        'stopping early. Both safely remove only your three owned test games.',
        '-# Do not edit the game name or notes; those fields carry the lane’s '
        'safety markers.',
    ))
    return '\n'.join(lines)


class BetaTestingDashboard(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        bot,
        requester_id: int,
        requester_name: str,
        guild_id: int,
        channel_id: int,
        role_ids: tuple[int, ...],
        lane_authorized: bool,
        session: beta_lab_sessions.BetaLabSessionSnapshot | None,
        status: beta_lab_workers.BetaLabStatus,
        guide: beta_testing_guide.ChecklistGuide,
        timeout: float = 600.0,
    ):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.requester_id = int(requester_id)
        self.requester_name = str(requester_name)
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.role_ids = tuple(int(value) for value in role_ids)
        self.lane_authorized = bool(lane_authorized)
        self.session = session
        self.status = status
        self.guide = guide
        self.section_key: str | None = None
        self.page = 0
        self.mode = 'overview'
        self.quick_index = self.requester_id % len(beta_lab_catalog.QUICK_TESTS)
        self.notice: str | None = None
        self.busy = False
        self.message = None
        self.expired = False
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Open `/whattotest` for your own private Beta Lab dashboard.',
            ephemeral=True,
        )
        return False

    def _section(self):
        return next(
            (
                section for section in self.guide.sections
                if section.key == self.section_key
            ),
            None,
        )

    def _body(self) -> str:
        section = self._section()
        if section is not None:
            pages = beta_testing_guide.item_pages(section)
            self.page = min(self.page, len(pages) - 1)
            lines = [
                f'# {_safe(section.title)}',
                f'**Page {self.page + 1} of {len(pages)}**',
                '',
            ]
            first_item_number = sum(len(page) for page in pages[:self.page]) + 1
            lines.extend(
                f'{index}. {_safe(item)}'
                for index, item in enumerate(
                    pages[self.page],
                    start=first_item_number,
                )
            )
            lines.extend(('', '-# Use the menu to switch areas or Overview to return.'))
            body = '\n'.join(lines)
        elif self.mode == 'quick':
            body = quick_test_markdown(beta_lab_catalog.QUICK_TESTS[self.quick_index])
        elif self.mode == 'lane' and self.session is not None:
            body = lane_markdown(self.session)
        else:
            body = overview_markdown(self.status)
        if self.notice:
            body += f'\n\n**Update:** {_safe(self.notice)}'
        return body

    def rebuild(self) -> None:
        self.clear_items()
        section = self._section()
        pages = beta_testing_guide.item_pages(section) if section else ((),)
        disabled = self.expired or self.busy
        selector = discord.ui.Select(
            placeholder='Browse the full testing reference',
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=item.title[:100],
                    value=item.key,
                    default=item.key == self.section_key,
                )
                for item in self.guide.sections[:25]
            ],
            disabled=disabled,
        )
        selector.callback = self._select
        overview = discord.ui.Button(
            label='Overview',
            style=discord.ButtonStyle.secondary,
            disabled=disabled or (section is None and self.mode == 'overview'),
        )
        overview.callback = self._overview
        previous = discord.ui.Button(
            label='Previous',
            style=discord.ButtonStyle.secondary,
            disabled=disabled or section is None or self.page <= 0,
        )
        previous.callback = self._previous
        following = discord.ui.Button(
            label='Next',
            style=discord.ButtonStyle.secondary,
            disabled=(disabled or section is None or self.page >= len(pages) - 1),
        )
        following.callback = self._next

        quick = discord.ui.Button(
            label='Give me a 5-minute test',
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        quick.callback = self._quick
        lane = discord.ui.Button(
            label='My game lane' if self.session else 'Create my game lane',
            style=discord.ButtonStyle.success,
            disabled=disabled or not self.lane_authorized,
        )
        lane.callback = self._lane
        finished = discord.ui.Button(
            label='Finished',
            style=discord.ButtonStyle.success,
            disabled=disabled or self.session is None,
        )
        finished.callback = self._finished
        release = discord.ui.Button(
            label='Release lane',
            style=discord.ButtonStyle.danger,
            disabled=disabled or self.session is None,
        )
        release.callback = self._release
        report = discord.ui.Button(
            label='Report problem',
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        report.callback = self._report
        footer = (
            'This private dashboard expired; rerun `/whattotest`.'
            if self.expired else
            'Private tester workspace • no channel spam'
        )
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(self._body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(quick, lane, finished, release, report),
            discord.ui.ActionRow(selector),
            discord.ui.ActionRow(overview, previous, following),
            discord.ui.TextDisplay(f'-# {footer}'),
            accent_colour=discord.Colour.blurple(),
        ))

    async def _edit_after_defer(self, interaction: discord.Interaction) -> None:
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _select(self, interaction: discord.Interaction) -> None:
        selector = next(
            item for item in self.walk_children()
            if isinstance(item, discord.ui.Select)
        )
        self.section_key = selector.values[0]
        self.page = 0
        self.notice = None
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _overview(self, interaction: discord.Interaction) -> None:
        self.section_key = None
        self.mode = 'overview'
        self.page = 0
        self.notice = None
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _quick(self, interaction: discord.Interaction) -> None:
        if self.mode == 'quick' and self.section_key is None:
            self.quick_index = (self.quick_index + 1) % len(beta_lab_catalog.QUICK_TESTS)
        self.mode = 'quick'
        self.section_key = None
        self.notice = None
        self.rebuild()
        await interaction.response.edit_message(view=self)

    def _request(self) -> beta_lab_sessions.BetaLabSessionRequest:
        return beta_lab_sessions.BetaLabSessionRequest(
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            requester_name=self.requester_name,
            role_ids=self.role_ids,
        )

    async def _lane(self, interaction: discord.Interaction) -> None:
        if self.session is not None:
            self.mode = 'lane'
            self.section_key = None
            self.notice = None
            self.rebuild()
            return await interaction.response.edit_message(view=self)
        await interaction.response.defer(ephemeral=True)
        self.role_ids = tuple(
            int(role.id) for role in getattr(interaction.user, 'roles', ())
        )
        self.busy = True
        try:
            self.session = await beta_lab_sessions.run_claim_session(self._request())
            self.mode = 'lane'
            self.section_key = None
            self.notice = 'Your lane is ready.'
        except beta_lab_sessions.BetaLabSessionError as exc:
            self.notice = str(exc)
        except Exception:
            logger.exception('Unexpected Beta Lab lane claim failure')
            self.notice = 'The lane could not be created. No retry is needed until staff checks it.'
        finally:
            self.busy = False
        await self._edit_after_defer(interaction)

    async def _finish(self, interaction: discord.Interaction, outcome: str) -> None:
        if self.session is None:
            return await interaction.response.send_message(
                'This panel no longer has an active lane.',
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        self.busy = True
        session_id = self.session.session_id
        request = beta_lab_sessions.BetaLabSessionReleaseRequest(
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            requester_name=self.requester_name,
            role_ids=self.role_ids,
            session_id=session_id,
            outcome=outcome,
        )
        try:
            result = await beta_lab_sessions.run_release_session(request)
            self.session = None
            self.mode = 'overview'
            self.section_key = None
            self.notice = (
                'Thanks — your lane was removed and its test games were cleaned up.'
                if result.released else
                'That lane was already absent; no ordinary games were changed.'
            )
        except beta_lab_sessions.BetaLabSessionError as exc:
            self.notice = str(exc)
        except Exception:
            logger.exception('Unexpected Beta Lab lane release failure')
            self.notice = (
                'The lane release could not be reconciled. Do not retry; staff '
                f'should inspect lane `{session_id}`.'
            )
        finally:
            self.busy = False
        await self._edit_after_defer(interaction)

    async def _finished(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, 'finished')

    async def _release(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, 'released')

    async def _report(self, interaction: discord.Interaction) -> None:
        context = 'Beta Lab `/whattotest` dashboard'
        if self.session is not None:
            game_ids = ', '.join(str(value) for value in self.session.game_ids)
            context += f'; lane {self.session.session_id}; games {game_ids}'
        await interaction.response.send_modal(beta_feedback_views.StaffHelpModal(
            self.bot,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            context_default=context,
        ))

    async def on_timeout(self) -> None:
        self.expired = True
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
