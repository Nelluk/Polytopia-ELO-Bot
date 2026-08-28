"""Private Components v2 workspace for owner delegation policy changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import discord

from modules import components_v2
from modules import guild_configuration_delegation_storage as storage
from modules import operator_guild_console_views as console
from modules import operator_guild_delegation_workers as workers


Runner = Callable[..., Awaitable[workers.GuildDelegationResult]]


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class DelegationRoleModal(discord.ui.Modal):
    def __init__(
        self,
        workspace: 'GuildDelegationWorkspace',
        *,
        remove: bool,
    ):
        self.workspace = workspace
        self.remove = remove
        super().__init__(
            title='Remove manager role' if remove else 'Add manager role',
            timeout=180.0,
        )
        self.role = discord.ui.TextInput(
            placeholder='Exact role name or numeric role ID',
            min_length=1,
            max_length=100,
        )
        self.add_item(discord.ui.Label(
            text='Role in the selected server',
            description=(
                'Duplicate role names are refused; use the numeric role ID '
                'to disambiguate.'
            ),
            component=self.role,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.workspace.stage_role(
            interaction,
            str(self.role.value),
            remove=self.remove,
        )


class GuildDelegationWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This manager workspace expired. Reopen it from '
        '`/operator guild list`.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildDelegationResult,
        runner: Runner,
        role_names: Mapping[int, str],
        back_runner: console.BackRunner | None = None,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.runner = runner
        self.back_runner = back_runner
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

    def resolve_role(self, value: str, *, remove: bool) -> int:
        raw = value.strip()
        if raw.isdigit():
            role_id = int(raw)
            if remove and role_id in self.manager_role_ids:
                return role_id
            if role_id in self.role_names:
                return role_id
            raise ValueError('That role ID is not assignable in the selected server.')
        matches = tuple(sorted(
            role_id for role_id, name in self.role_names.items()
            if name.casefold() == raw.casefold()
            and (not remove or role_id in self.manager_role_ids)
        ))
        if not matches:
            raise ValueError('No assignable role has that exact name.')
        if len(matches) != 1:
            raise ValueError(
                'More than one role has that name; enter the numeric role ID.'
            )
        return matches[0]

    async def stage_role(
        self,
        interaction: Any,
        value: str,
        *,
        remove: bool,
    ) -> None:
        if not await self.ready(interaction):
            return
        try:
            role_id = self.resolve_role(value, remove=remove)
        except ValueError as exc:
            return await _private(interaction, str(exc))
        roles = set(self.manager_role_ids)
        if remove:
            if role_id not in roles:
                return await _private(interaction, 'That role is not a manager.')
            roles.remove(role_id)
        else:
            if role_id in roles:
                return await _private(interaction, 'That role is already a manager.')
            if len(roles) >= storage.MAX_MANAGER_ROLES:
                return await _private(
                    interaction,
                    f'At most {storage.MAX_MANAGER_ROLES} manager roles are allowed.',
                )
            roles.add(role_id)
        self.manager_role_ids = tuple(sorted(roles))
        if not self.manager_role_ids:
            self.allow_activation = False
        self.status = (
            f'Staged removal of role `{role_id}`; no database change yet.'
            if remove else
            f'Staged manager role `{role_id}`; no database change yet.'
        )
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _add_role(self, interaction: Any) -> None:
        if await self.ready(interaction):
            await interaction.response.send_modal(
                DelegationRoleModal(self, remove=False)
            )

    async def _remove_role(self, interaction: Any) -> None:
        if await self.ready(interaction):
            if not self.manager_role_ids:
                return await _private(interaction, 'No manager roles are staged.')
            await interaction.response.send_modal(
                DelegationRoleModal(self, remove=True)
            )

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
        await self.apply(interaction, self.confirmation)

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
                target_guild_id=self.result.guild_id,
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
        activation_description = (
            'managers allowed'
            if self.allow_activation else 'guild/bot owner only'
        )
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
                f'`{activation_description}`\n\n'
                f'## Staged manager roles\n{roles}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        add = discord.ui.Button(
            label='Add role',
            style=discord.ButtonStyle.primary,
            disabled=self.busy,
        )
        add.callback = self._add_role
        remove = discord.ui.Button(
            label='Remove role',
            disabled=self.busy or not self.manager_role_ids,
        )
        remove.callback = self._remove_role
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
        children.append(
            discord.ui.ActionRow(add, remove, activation, disable, apply)
        )
        if self.back_runner is not None:
            children.append(discord.ui.ActionRow(
                console.guild_list_back_button(
                    self, self.back_runner, disabled=self.busy,
                )
            ))
        children.extend((
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {discord.utils.escape_markdown(self.status)}\n'
                '-# Managers can edit only ordinary settings in this guild. '
                'Roles, private/log/staff routes, global visibility, command '
                'capabilities, lifecycle, delegation, and cross-guild access '
                'remain bot-owner-only. The Discord guild owner always has '
                'ordinary-setting edit and activation access, even when no '
                'manager roles are configured.'
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
    'DelegationRoleModal',
    'GuildDelegationWorkspace', 'publish_private',
]
