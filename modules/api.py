"""Read-only HTTP API for bot data."""
import fastapi

from .models import DiscordMember, Game


server = fastapi.FastAPI()


@server.get('/users/{discord_id}')
async def get_user(discord_id: int, response: fastapi.Response) -> dict:
    """Get a user by discord ID."""
    user = DiscordMember.get_or_none(DiscordMember.discord_id == discord_id)
    if user:
        return user.as_json()
    response.status_code = 404
    return {'error': 'User not found.'}


@server.get('/games/{game_id}')
async def get_game(game_id: int, response: fastapi.Response) -> dict:
    """Get a game by ID."""
    game = Game.get_or_none(Game.id == game_id)
    if game:
        return game.as_json()
    response.status_code = 404
    return {'error': 'Game not found.'}
