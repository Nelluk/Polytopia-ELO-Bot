"""Requester-bound compact dashboard for the development Beta Lab."""

from __future__ import annotations

import asyncio
import logging

import discord
import settings

from modules import (
    beta_feedback_views,
    beta_lab_catalog,
    beta_lab_personas,
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


async def _finish_started(task: asyncio.Task):
    """Drain a started session/persona transition despite caller cancellation."""

    current = asyncio.current_task()
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            if current is not None:
                while current.cancelling():
                    current.uncancel()


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def overview_markdown(status: beta_lab_workers.BetaLabStatus) -> str:
    lines = [
        '# 🧪 Beta Lab',
        '**Start here:** choose **Give me a 5-minute test** for a read-only '
        'task, or **Start guided session** for Team, House, and game-result tasks.',
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


def lane_markdown(
    session: beta_lab_sessions.BetaLabSessionSnapshot,
    task_key: str | None = None,
) -> str:
    scenario_by_name = {item.scenario: item for item in session.scenarios}
    if task_key is None:
        exercised = sum(item.status == 'exercised' for item in session.scenarios)
        return '\n'.join((
            '# 🎮 Guided test session',
            f'**Tester:** {_safe(session.requester_name)}',
            f'**Fixture opponent:** {_safe(session.opponent_name)}',
            f'**Session:** `{session.session_id}` • expires <t:{session.expires_epoch}:R>',
            '',
            '**Choose one task below. You do not have to complete all four.**',
            'Each task shows the exact slash-command fields and what should happen.',
            f'**Game progress:** {exercised} of {len(session.scenarios)} scenarios exercised.',
            '',
            'After running a command, return here and choose **Refresh results**. '
            'Use **Report problem** if the response is wrong or the instructions are unclear.',
            'When you are done, choose **Finish and clean up** to remove your test games and persona roles.',
        ))

    if task_key == 'team':
        return '\n'.join((
            '# 🏠 Team and House task',
            '**Goal:** verify that the bot recognizes your temporary league membership.',
            '',
            '1. Run `/team show` with `team: Beta Lab Team`.',
            f'   **Expected:** the roster includes {_safe(session.requester_name)} and the card shows `Beta Lab House`.',
            '2. Run `/house list`, select `Beta Lab House`, and open its Team.',
            '   **Expected:** `Beta Lab Team` appears under that House.',
            '3. Optionally run `/leaderboard teams` and look for `Beta Lab Team`.',
            '',
            'Return to this panel afterward. Choose another task, report a problem, or finish.',
        ))

    lines = [
        '# 🎮 Game result task',
    ]
    instructions = {
        'win': (
            'ready',
            'Test an ordinary win claim',
            ('Run `/game win` with:',
             None,
             f'`winner: {_safe(session.requester_name)}`'),
            'A public win claim should name you as winner and remain unconfirmed (1 of 2 sides confirmed).',
        ),
        'confirm': (
            'unconfirmed',
            'Test staff confirmation',
            ('Run `/game result confirm` with:', None),
            'The result should become confirmed publicly and ELO/result publication should run once.',
        ),
        'undo': (
            'completed',
            'Test result undo',
            ('Run `/game result undo` with:', None),
            'The completed result and its ELO change should be reversed once with a public reset notice.',
        ),
    }
    scenario, title, command_lines, expected = instructions[task_key]
    item = scenario_by_name[scenario]
    lines.extend((
        f'**Goal:** {_safe(title)}.',
        f'**Current state:** {_safe(item.status.title())}',
        '',
        command_lines[0],
        f'`game_id: {item.game_id}`',
    ))
    lines.extend(line for line in command_lines[2:] if line is not None)
    lines.extend((
        '',
        f'**Expected:** {_safe(expected)}',
        '',
        'Return here and choose **Refresh results**. Then choose another task, '
        'report a problem, or finish.',
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
        self.task_key: str | None = None
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
            body = lane_markdown(self.session, self.task_key)
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
            label='Session home' if self.session else 'Start guided session',
            style=discord.ButtonStyle.success,
            disabled=disabled or not self.lane_authorized,
        )
        lane.callback = self._lane
        refresh = discord.ui.Button(
            label='Refresh results',
            style=discord.ButtonStyle.secondary,
            disabled=disabled or self.session is None,
        )
        refresh.callback = self._refresh
        finished = discord.ui.Button(
            label='Finish and clean up',
            style=discord.ButtonStyle.success,
            disabled=disabled or self.session is None,
        )
        finished.callback = self._finished
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
            discord.ui.ActionRow(quick, lane, refresh, finished, report),
            *(
                (discord.ui.ActionRow(
                    self._task_button('Team & House', 'team', disabled),
                    self._task_button('Win claim', 'win', disabled),
                    self._task_button('Confirm result', 'confirm', disabled),
                    self._task_button('Undo result', 'undo', disabled),
                ),)
                if self.session is not None else ()
            ),
            discord.ui.ActionRow(selector),
            discord.ui.ActionRow(overview, previous, following),
            discord.ui.TextDisplay(f'-# {footer}'),
            accent_colour=discord.Colour.blurple(),
        ))

    def _task_button(self, label: str, key: str, disabled: bool):
        button = discord.ui.Button(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if self.task_key == key else discord.ButtonStyle.secondary
            ),
            disabled=disabled,
        )
        button.callback = {
            'team': self._team_task,
            'win': self._win_task,
            'confirm': self._confirm_task,
            'undo': self._undo_task,
        }[key]
        return button

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
            self.task_key = None
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
            await _finish_started(asyncio.create_task(
                self._activate_persona(interaction)
            ))
            self.mode = 'lane'
            self.task_key = None
            self.section_key = None
            self.notice = 'Your guided session is ready. Choose any task below.'
        except (
            beta_lab_sessions.BetaLabSessionError,
            beta_lab_personas.BetaLabPersonaError,
        ) as exc:
            if self.session is not None:
                try:
                    request = beta_lab_sessions.BetaLabSessionReleaseRequest(
                        guild_id=self.guild_id,
                        requester_id=self.requester_id,
                        requester_name=self.requester_name,
                        role_ids=self.role_ids,
                        session_id=self.session.session_id,
                        outcome='released',
                    )
                    await _finish_started(asyncio.create_task(
                        self._remove_persona_and_release(interaction, request)
                    ))
                    self.session = None
                except Exception:
                    logger.exception('Could not compensate failed persona assignment')
                    self.notice = (
                        f'{exc} The game session may still exist; do not retry '
                        'until an operator reconciles it.'
                    )
                else:
                    self.notice = f'{exc} No game session was retained.'
            else:
                self.notice = str(exc)
        except Exception:
            logger.exception('Unexpected Beta Lab lane claim failure')
            if self.session is not None:
                try:
                    request = beta_lab_sessions.BetaLabSessionReleaseRequest(
                        guild_id=self.guild_id,
                        requester_id=self.requester_id,
                        requester_name=self.requester_name,
                        role_ids=self.role_ids,
                        session_id=self.session.session_id,
                        outcome='released',
                    )
                    await _finish_started(asyncio.create_task(
                        self._remove_persona_and_release(interaction, request)
                    ))
                    self.session = None
                    self.notice = (
                        'The guided session could not be activated; no game '
                        'session or persona was retained.'
                    )
                except Exception:
                    logger.exception(
                        'Could not compensate unexpected guided-session activation failure'
                    )
                    self.notice = (
                        'The guided session could not be reconciled. Do not '
                        'retry until staff inspects it.'
                    )
            else:
                self.notice = (
                    'The lane could not be created. No retry is needed until '
                    'staff checks it.'
                )
        finally:
            self.busy = False
        await self._edit_after_defer(interaction)

    async def _activate_persona(self, interaction: discord.Interaction) -> None:
        active_owner_ids = await beta_lab_sessions.run_active_owner_ids(
            self.guild_id
        )
        await beta_lab_personas.reconcile_members(
            settings.runtime_profile,
            interaction.guild,
            active_owner_ids=active_owner_ids,
        )
        await beta_lab_personas.set_member_active(
            settings.runtime_profile,
            interaction.guild,
            interaction.user,
            active=True,
        )

    async def _remove_persona_and_release(
        self,
        interaction: discord.Interaction,
        request: beta_lab_sessions.BetaLabSessionReleaseRequest,
    ) -> beta_lab_sessions.BetaLabSessionReleaseResult:
        await beta_lab_personas.set_member_active(
            settings.runtime_profile,
            interaction.guild,
            interaction.user,
            active=False,
        )
        return await beta_lab_sessions.run_release_session(request)

    async def _choose_task(self, interaction: discord.Interaction, key: str) -> None:
        self.mode = 'lane'
        self.task_key = key
        self.section_key = None
        self.notice = None
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _team_task(self, interaction: discord.Interaction) -> None:
        await self._choose_task(interaction, 'team')

    async def _win_task(self, interaction: discord.Interaction) -> None:
        await self._choose_task(interaction, 'win')

    async def _confirm_task(self, interaction: discord.Interaction) -> None:
        await self._choose_task(interaction, 'confirm')

    async def _undo_task(self, interaction: discord.Interaction) -> None:
        await self._choose_task(interaction, 'undo')

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        self.busy = True
        try:
            self.session = await beta_lab_sessions.run_requester_session(self._request())
            if self.session is not None and self.session.state != 'expired':
                await _finish_started(asyncio.create_task(
                    self._activate_persona(interaction)
                ))
                self.notice = 'Progress refreshed from the live test games.'
            elif self.session is None:
                active_owner_ids = await beta_lab_sessions.run_active_owner_ids(
                    self.guild_id
                )
                await beta_lab_personas.reconcile_members(
                    settings.runtime_profile,
                    interaction.guild,
                    active_owner_ids=active_owner_ids,
                )
                self.mode = 'overview'
                self.task_key = None
                self.notice = 'This guided session no longer exists.'
            else:
                await beta_lab_personas.set_member_active(
                    settings.runtime_profile,
                    interaction.guild,
                    interaction.user,
                    active=False,
                )
                self.notice = 'This session expired. Finish cleanup before starting another.'
        except (beta_lab_sessions.BetaLabSessionError, beta_lab_personas.BetaLabPersonaError) as exc:
            self.notice = str(exc)
        except Exception:
            logger.exception('Unexpected guided-session refresh failure')
            self.notice = 'The session could not be refreshed; do not repeat a result mutation.'
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
            result = await _finish_started(asyncio.create_task(
                self._remove_persona_and_release(interaction, request)
            ))
            self.session = None
            self.mode = 'overview'
            self.task_key = None
            self.section_key = None
            self.notice = (
                'Thanks — your lane was removed and its test games were cleaned up.'
                if result.released else
                'That lane was already absent; no ordinary games were changed.'
            )
        except (beta_lab_sessions.BetaLabSessionError, beta_lab_personas.BetaLabPersonaError) as exc:
            self.notice = (
                f'{exc} Your temporary authority is removed; choose Refresh '
                'results to revalidate the lane before continuing.'
            )
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
        if self.session is not None:
            guild = self.bot.get_guild(self.guild_id)
            member = (
                guild.get_member(self.requester_id)
                if guild is not None else None
            )
            if member is not None:
                try:
                    await _finish_started(asyncio.create_task(
                        beta_lab_personas.set_member_active(
                            settings.runtime_profile,
                            guild,
                            member,
                            active=False,
                        )
                    ))
                except beta_lab_personas.BetaLabPersonaError:
                    logger.exception(
                        'Could not revoke expired Beta Lab panel persona for %s',
                        self.requester_id,
                    )
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
