"""Bounded read-only worker for league trade-price calculations."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from peewee import DoesNotExist, fn

from modules import models, utilities


class TradePriceError(RuntimeError):
    """Base user-facing trade-price failure."""


class TradePriceValidationError(TradePriceError):
    """The supplied price request is invalid."""


class TradePriceLookupError(TradePriceError):
    """The requested player or league season is unavailable."""


@dataclass(frozen=True)
class TradePriceRequest:
    guild_id: int
    player_discord_id: int
    player_display_name: str
    ending_season: int | None
    leadership_adjustment: bool


@dataclass(frozen=True)
class TradePriceSeason:
    season: int
    tier: int | None
    wins: int
    losses: int

    @property
    def games(self) -> int:
        return self.wins + self.losses


@dataclass(frozen=True)
class TradePriceResult:
    player_discord_id: int
    player_display_name: str
    ending_season: int
    inference: str
    current_season: int
    leadership_adjustment: bool
    seasons: tuple[TradePriceSeason, ...]
    price: int


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='league-trade-price')


def _load_player(request: TradePriceRequest):
    try:
        return (
            models.Player.select(models.Player, models.DiscordMember)
            .join(models.DiscordMember)
            .where(
                (models.Player.guild_id == int(request.guild_id))
                & (
                    models.DiscordMember.discord_id
                    == int(request.player_discord_id)
                )
            )
            .get()
        )
    except DoesNotExist as exc:
        raise TradePriceLookupError(
            f'{request.player_display_name} is not registered in this server.'
        ) from exc


def _current_season(guild_id: int) -> int:
    current = (
        models.Game.select(fn.MAX(models.Game.league_season))
        .where(
            (models.Game.guild_id == int(guild_id))
            & models.Game.league_season.is_null(False)
        )
        .scalar()
    )
    if current is None:
        raise TradePriceLookupError(
            'No league season data is available in this server.'
        )
    return int(current)


def _has_incomplete_current_game(player, guild_id: int, season: int) -> bool:
    return (
        models.Lineup.select(models.Lineup.id)
        .join(models.Game)
        .where(
            (models.Lineup.player == player.id)
            & (models.Game.guild_id == int(guild_id))
            & (models.Game.league_season == int(season))
            & (models.Game.is_confirmed == False)
        )
        .exists()
    )


def _calculate(request: TradePriceRequest) -> TradePriceResult:
    if int(request.guild_id) <= 0 or int(request.player_discord_id) <= 0:
        raise TradePriceValidationError('The server and player IDs must be valid.')
    if request.ending_season is not None and not 1 <= int(request.ending_season) <= 32767:
        raise TradePriceValidationError(
            'Season must be an integer between 1 and 32767.'
        )

    with models.db.connection_context():
        player = _load_player(request)
        if request.ending_season is not None:
            ending_season = int(request.ending_season)
            current_season = ending_season
            inference = 'explicit'
        else:
            current_season = _current_season(request.guild_id)
            if _has_incomplete_current_game(
                player, request.guild_id, current_season
            ):
                ending_season = current_season - 1
                inference = 'previous_due_to_incomplete'
            else:
                ending_season = current_season
                inference = 'current'

        rows = []
        formula_rows = []
        for season in range(ending_season - 2, ending_season + 1):
            tier = player.polychamps_season_tier(season)
            if tier:
                wins, losses = player.polychamps_season_record(season)
                wins, losses = int(wins), int(losses)
                if wins + losses:
                    row = TradePriceSeason(
                        season=season,
                        tier=int(tier),
                        wins=wins,
                        losses=losses,
                    )
                else:
                    row = TradePriceSeason(
                        season=season, tier=None, wins=0, losses=0
                    )
            else:
                row = TradePriceSeason(
                    season=season, tier=None, wins=0, losses=0
                )
            rows.append(row)
            formula_rows.append((row.tier, row.games, row.wins))

        if all(row.games == 0 for row in rows):
            raise TradePriceLookupError(
                f'{request.player_display_name} has not played in the '
                'three-season pricing window.'
            )
        price = utilities.trade_price_formula(
            formula_rows, bool(request.leadership_adjustment)
        )
        return TradePriceResult(
            player_discord_id=int(request.player_discord_id),
            player_display_name=str(request.player_display_name),
            ending_season=ending_season,
            inference=inference,
            current_season=current_season,
            leadership_adjustment=bool(request.leadership_adjustment),
            seasons=tuple(rows),
            price=int(price),
        )


async def run_trade_price(request: TradePriceRequest) -> TradePriceResult:
    """Run the fixed-size read off-loop and drain it before cancellation."""

    concurrent_future = _executor.submit(_calculate, request)
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return concurrent_future.result()
