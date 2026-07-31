"""Small database-agnostic primitives for public Components v2 workspaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
import math
from typing import Any

import discord


DEFAULT_ACCENT = discord.Colour.from_rgb(83, 126, 231)


def page_count(rows: Sequence[Any], page_size: int) -> int:
    return max(1, math.ceil(len(rows) / page_size))


def page_slice(
    rows: Sequence[Any],
    page_index: int,
    page_size: int,
) -> tuple[Sequence[Any], int, int]:
    start = page_index * page_size
    page_rows = rows[start:start + page_size]
    return page_rows, start + 1 if page_rows else 0, start + len(page_rows)


def disable_controls(view: discord.ui.LayoutView) -> None:
    """Recursively disable every interactive child in a layout."""

    for item in view.walk_children():
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            item.disabled = True


class PageJumpModal(discord.ui.Modal):
    """Database-free page jump for a requester-controlled layout."""

    def __init__(self, view: 'RequesterLayoutView'):
        super().__init__(title='Jump to page', timeout=60.0)
        self.target_view = view
        self.page_number = discord.ui.TextInput(
            placeholder=str(view.page_index + 1),
            default=str(view.page_index + 1),
            min_length=1,
            max_length=max(1, len(str(view.page_count))),
        )
        self.add_item(discord.ui.Label(
            text=f'Page number (1–{view.page_count})',
            description='The public result will move to this page.',
            component=self.page_number,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self.target_view
        if not await view.authorize(interaction):
            return
        if view.is_finished():
            await interaction.response.send_message(
                view.expired_message,
                ephemeral=True,
            )
            return
        try:
            page = int(self.page_number.value.strip())
        except ValueError:
            page = 0
        if page < 1 or page > view.page_count:
            await interaction.response.send_message(
                f'Enter a page number from 1 to {view.page_count}.',
                ephemeral=True,
            )
            return
        view.page_index = page - 1
        view.rebuild()
        await interaction.response.edit_message(view=view)


class RequesterLayoutView(discord.ui.LayoutView):
    """Public result whose controls belong to the invoking user."""

    unauthorized_message = 'Only the requester can control this result.'
    expired_message = (
        'This interaction has expired. Run the command again for a fresh '
        'result.'
    )

    def __init__(self, *, requester_id: int, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.page_index = 0
        self.message: discord.Message | None = None

    @property
    def page_count(self) -> int:
        raise NotImplementedError

    def rebuild(self) -> None:
        raise NotImplementedError

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            self.unauthorized_message,
            ephemeral=True,
        )
        return False

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        return await self.authorize(interaction)

    async def open_page_modal(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(PageJumpModal(self))

    async def show_previous(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.page_index -= 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def show_next(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.page_index += 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        disable_controls(self)
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class CachedRequesterLayoutView(RequesterLayoutView):
    """Requester layout with lazy, immutable snapshot caching."""

    def __init__(
        self,
        *,
        requester_id: int,
        initial_key: Any,
        initial_result: Any,
        loader: Callable[[Any], Awaitable[Any]],
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.loader = loader
        self.result = initial_result
        self._cache = {initial_key: initial_result}

    async def load_key(
        self,
        interaction: discord.Interaction,
        key: Any,
    ) -> bool:
        cached = self._cache.get(key)
        if cached is not None:
            self.result = cached
            return True
        await interaction.response.defer()
        try:
            result = await self.loader(key)
        except Exception as exc:
            await interaction.followup.send(
                f'Could not load that view: {exc}',
                ephemeral=True,
            )
            return False
        self._cache[key] = result
        self.result = result
        return True
