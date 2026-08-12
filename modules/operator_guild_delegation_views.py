"""Private Components v2 workspace for owner delegation policy changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import discord

from modules import components_v2
from modules import guild_configuration_delegation_storage as storage
from modules import operator_guild_delegation_workers as workers


Runner = Callable[..., Awaitable[workers.GuildDelegationResult]]


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class DelegationConfirmationModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildDelegationWorkspace'):
        self.workspace = workspace
        self.expected = workspace.confirmation
        super().__init__(title='Apply guild delegation', timeout=180.0)
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text='Type DELEGATE, guild ID, and the full plan digest',
            description='This replaces the complete manager policy.',
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value) != self.expected:
            return await _private(interaction, f'Type `{self.expected}` exactly.')
        if not await self.workspace.ready(interaction):
            return
        await self.workspace.apply(interaction, self.expected)


class GuildDelegationWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This delegation workspace expired. Run '
        '`/operator guild delegation` again.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildDelegationResult,
        runner: Runner,
        role_names: Mapping[int, str],
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.runner = runner
        self.role_names = dict(role_names)
        policy = result.policy
        self.manager_role_ids = (
            () if policy is None else policy.manager_role_ids
        )
        self.allow_activation = (
            False if policy is None else policy.allow_activation
        )
        self.busy = False
        self.status = 'Review the current owner-controlled policy.'
        self.rebuild()

    @property
    def expected_version(self) -> int | None:
        return (
            None if self.result.policy is None
            else self.result.policy.policy_version
        )

    @property
    def plan_digest(self) -> str:
        return storage.policy_digest(
            guild_id=self.result.guild_id,
            expected_version=self.expected_version,
            manager_role_ids=self.manager_role_ids,
            allow_activation=self.allow_activation,
        )

    @property
    def confirmation(self) -> str:
        return f'DELEGATE {self.result.guild_id} {self.plan_digest}'

    async def ready(self, interaction: Any) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished():
            await _private(interaction, self.expired_message)
            return False
        if self.busy:
            await _private(interaction, 'A delegation operation is already running.')
            return False
        return True

    async def _select_roles(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        selected = tuple(select.values)
        invalid = tuple(
            role for role in selected
            if bool(getattr(role, 'managed', False))
            or (
                callable(getattr(role, 'is_default', None))
                and role.is_default()
            )
        )
        if invalid:
            return await _private(
                interaction,
                'Choose assignable guild roles; `@everyone` and managed roles '
                'cannot receive configuration authority.',
            )
        self.manager_role_ids = tuple(sorted(int(role.id) for role in selected))
        if not self.manager_role_ids:
            self.allow_activation = False
        self.status = 'Staged role selection; no database change yet.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _toggle_activation(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if not self.manager_role_ids:
            return await _private(
                interaction, 'Select at least one manager role first.'
            )
        self.allow_activation = not self.allow_activation
        self.status = 'Staged activation permission; no database change yet.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _disable(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        self.manager_role_ids = ()
        self.allow_activation = False
        self.status = 'Delegation revocation staged; confirm Apply to commit it.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        current_roles = (
            () if self.result.policy is None else self.result.policy.manager_role_ids
        )
        current_activation = (
            False if self.result.policy is None
            else self.result.policy.allow_activation
        )
        if (
                self.manager_role_ids == current_roles
                and self.allow_activation == current_activation
        ):
            return await _private(interaction, 'The staged policy is unchanged.')
        await interaction.response.send_modal(DelegationConfirmationModal(self))

    async def apply(self, interaction: Any, confirmation: str) -> None:
        self.busy = True
        self.status = 'Applying delegation policy…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.runner(
                interaction,
                workers.APPLY,
                expected_policy_version=self.expected_version,
                manager_role_ids=self.manager_role_ids,
                allow_activation=self.allow_activation,
                expected_plan_digest=self.plan_digest,
                confirmation_text=confirmation,
            )
        except workers.OperatorGuildDelegationError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            self.busy = False
            self.status = (
                'The delegation operation stopped without a trustworthy result. '
                'Reopen the workspace before retrying.'
            )
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return await interaction.followup.send(self.status, ephemeral=True)
        self.result = result
        self.busy = False
        self.status = (
            f'Policy version {result.policy.policy_version} committed with '
            f'{len(result.policy.manager_role_ids)} manager role(s).'
        )
        self.rebuild()
        await interaction.edit_original_response(view=self)

    def rebuild(self) -> None:
        self.clear_items()
        roles = '\n'.join(
            f'- <@&{role_id}> `{role_id}` '
            f'({discord.utils.escape_markdown(self.role_names.get(role_id, "unresolved"))})'
            for role_id in self.manager_role_ids
        ) or '*None — delegation disabled*'
        children: list[Any] = [
            discord.ui.TextDisplay(
                '# Guild configuration delegation\n'
                f'**Guild:** `{self.result.guild_id}`\n'
                f'**Current policy version:** '
                f'`{self.expected_version or "none"}`\n'
                f'**Ordinary-setting activation:** '
                f'`{"allowed" if self.allow_activation else "owner only"}`\n\n'
                f'## Staged manager roles\n{roles}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        select = discord.ui.RoleSelect(
            placeholder='Replace manager roles (select zero to clear)',
            min_values=0,
            max_values=storage.MAX_MANAGER_ROLES,
            disabled=self.busy,
        )
        select.callback = lambda interaction: self._select_roles(interaction, select)
        children.append(discord.ui.ActionRow(select))
        activation = discord.ui.Button(
            label=(
                'Managers may activate' if self.allow_activation
                else 'Activation stays owner-only'
            ),
            style=(
                discord.ButtonStyle.success if self.allow_activation
                else discord.ButtonStyle.secondary
            ),
            disabled=self.busy or not self.manager_role_ids,
        )
        activation.callback = self._toggle_activation
        disable = discord.ui.Button(
            label='Revoke all', style=discord.ButtonStyle.danger,
            disabled=self.busy or not self.manager_role_ids,
        )
        disable.callback = self._disable
        apply = discord.ui.Button(
            label='Review and apply', style=discord.ButtonStyle.primary,
            disabled=self.busy,
        )
        apply.callback = self._confirm
        children.extend((
            discord.ui.ActionRow(activation, disable, apply),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {discord.utils.escape_markdown(self.status)}\n'
                '-# Managers can edit only ordinary settings in this guild. '
                'Roles, private/log/staff routes, global visibility, command '
                'capabilities, lifecycle, delegation, and cross-guild access '
                'remain owner-only.'
            ),
        ))
        self.add_item(discord.ui.Container(
            *children, accent_colour=components_v2.DEFAULT_ACCENT,
        ))


async def publish_private(interaction: Any, view: GuildDelegationWorkspace):
    message = await interaction.followup.send(
        view=view, ephemeral=True, wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


__all__ = [
    'DelegationConfirmationModal', 'GuildDelegationWorkspace', 'publish_private',
]
