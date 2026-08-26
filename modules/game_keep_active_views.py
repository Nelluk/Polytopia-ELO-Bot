"""Persistent/dynamic Keep active button presentation."""

from __future__ import annotations

import datetime
import re

import discord


CUSTOM_ID_TEMPLATE = r'keep-active:(?P<game_id>[0-9]+):(?P<deadline>[0-9]{4}-[0-9]{2}-[0-9]{2})'


def custom_id(game_id: int, protected_through: datetime.date) -> str:
    return f'keep-active:{int(game_id)}:{protected_through.isoformat()}'


class KeepActiveButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_TEMPLATE):
    def __init__(self, item: discord.ui.Button):
        super().__init__(item)

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(discord.ui.Button(
            label='Keep active for 30 days',
            style=discord.ButtonStyle.secondary,
            custom_id=match.group(0),
        ))

    async def callback(self, interaction: discord.Interaction):
        from modules import game_keep_active
        match = self.template.fullmatch(self.custom_id)
        await game_keep_active.run_button(
            interaction,
            game_id=int(match.group('game_id')),
            protected_through=datetime.date.fromisoformat(
                match.group('deadline')
            ),
        )


class KeepActiveView(discord.ui.View):
    def __init__(self, game_id: int, protected_through: datetime.date):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label='Keep active for 30 days',
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id(game_id, protected_through),
        ))


def register_dynamic_item(bot) -> None:
    bot.add_dynamic_items(KeepActiveButton)
