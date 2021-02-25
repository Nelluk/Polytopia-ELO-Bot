"""Read-only HTTP API for bot data."""
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .models import ApiApplication, DiscordMember, Game


server = FastAPI()
security = HTTPBasic()


async def get_scopes(
        credentials: HTTPBasicCredentials = Depends(security)) -> List[str]:
    """Get the scopes the requester has access to."""
    app = ApiApplication.authenticate(
        credentials.username, credentials.password
    )
    if app:
        return app.scopes.split()
    raise HTTPException(
        status_code=401,
        detail='Incorrect app ID or token.',
        headers={'WWW-Authenticate': 'Basic'}
    )


@server.get('/users/{discord_id}')
async def get_user(
        discord_id: int, response: Response,
        scopes: List[str] = Depends(get_scopes)) -> dict:
    """Get a user by discord ID."""
    if 'users:read' not in scopes:
        raise HTTPException(
            status_code=403, detail='Not authorised for scope users:read.'
        )
    user = DiscordMember.get_or_none(DiscordMember.discord_id == discord_id)
    if user:
        return user.as_json(include_games='games:read' in scopes)
    raise HTTPException(status_code=404, detail='User not found.')


@server.get('/games/{game_id}')
async def get_game(
        game_id: int, response: Response,
        scopes: List[str] = Depends(get_scopes)) -> dict:
    """Get a game by ID."""
    if 'games:read' not in scopes:
        raise HTTPException(
            status_code=403, detail='Not authorised for scope games:read.'
        )
    game = Game.get_or_none(Game.id == game_id)
    if game:
        return game.as_json()
    raise HTTPException(status_code=404, detail='Game not found.')
