"""Model-free Discord publishers for committed game corrections."""

from __future__ import annotations

from dataclasses import replace

import settings
from modules import confirmation_publication, game_detail_views
from modules.game_result_publication_workers import GameResultPublicationSnapshot

class GameCorrectionPublicationError(RuntimeError):
    """A frozen correction could not complete its Discord publication."""


async def publish_ranked_state(
    snapshot: GameResultPublicationSnapshot,
    *,
    requester_display_name: str,
    bot=None,
) -> None:
    """Notify game channels from one committed ranked-state snapshot."""

    state = 'ranked' if snapshot.game.is_ranked else 'unranked'
    try:
        await confirmation_publication.publish_game_channels(
            snapshot,
            bot=bot or settings.bot,
            message=(
                f'Staff member **{requester_display_name}** has set this '
                f'game to be *{state}*.'
            ),
        )
    except Exception as exc:
        raise GameCorrectionPublicationError(
            f'Could not publish ranked state for game {snapshot.game.game_id}.'
        ) from exc


async def publish_cancelled_unstart_announcement(
    snapshot: GameResultPublicationSnapshot,
    *,
    game_name: str,
    announcement_channel_id: int | None,
    announcement_message_id: int | None,
    guild,
    prefix: str,
    bot=None,
) -> None:
    """Repaint the legacy cancelled card without a live game model."""

    if announcement_channel_id is None or announcement_message_id is None:
        return
    channel = guild.get_channel(announcement_channel_id)
    if channel is None:
        raise GameCorrectionPublicationError(
            f'Could not find announcement channel {announcement_channel_id}.'
        )
    try:
        message = await channel.fetch_message(announcement_message_id)
        cancelled = replace(
            snapshot.game,
            name=f'~~{game_name}~~ GAME CANCELLED',
            is_pending=False,
            pending_join_available=False,
            pending_full=False,
            pending_draft_order=(),
        )
        display = game_detail_views.resolve_display(
            cancelled,
            guild=guild,
            bot=bot or settings.bot,
            prefix=prefix,
            presentation='prefix',
        )
        rendered = game_detail_views.render_classic_game_detail(display)
        kwargs = game_detail_views.classic_edit_kwargs(message, rendered)
        # The legacy updater did not alter an existing view.
        kwargs.pop('view', None)
        await message.edit(**kwargs)
    except Exception as exc:
        raise GameCorrectionPublicationError(
            f'Could not update the announcement for game '
            f'{snapshot.game.game_id}.'
        ) from exc


__all__ = [
    'GameCorrectionPublicationError',
    'publish_cancelled_unstart_announcement',
    'publish_ranked_state',
]
