"""Shared service and public presentation helpers for ``/squad name``."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import exceptions, squad_identity_workers, squad_show_workers


logger = logging.getLogger('polybot.' + __name__)

MAX_SQUAD_NAME_LENGTH = squad_identity_workers.MAX_SQUAD_NAME_LENGTH


@dataclass(frozen=True)
class SquadIdentityActor:
    """Safe event-loop-captured actor identity for public output and audit."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> SquadIdentityActor:
    """Capture only primitive identity values before worker submission."""

    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name),
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return SquadIdentityActor(
        discord_id=discord_id,
        mention=str(mention or f'<@{discord_id}>'),
        identity=f'**{safe_name}** (`{discord_id}`)',
    )


def _requester_is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _requester_role_names(member) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(role.name)
            for role in (getattr(member, 'roles', None) or ())
            if getattr(role, 'name', None) is not None
        )
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    squad_id: int,
    name: str | None = None,
    clear: bool = False,
    expected_name: str | None = None,
    captured_can_edit: bool = False,
    invoked_with: str = '/squad name',
) -> squad_identity_workers.SquadNameMutationRequest:
    """Freeze Discord/config values into the write-worker request."""

    actor = capture_actor(member)
    return squad_identity_workers.SquadNameMutationRequest(
        guild_id=int(guild_id),
        squad_id=int(squad_id),
        requester_id=actor.discord_id,
        requester_is_staff=_requester_is_staff(member),
        requester_description=actor.identity,
        requester_role_names=_requester_role_names(member),
        name=(str(name) if name is not None else None),
        clear=bool(clear),
        expected_name=(
            str(expected_name) if expected_name is not None else None
        ),
        check_expected_name=expected_name is not None,
        captured_can_edit=bool(captured_can_edit),
        invoked_with=str(invoked_with),
    )


def validate_input(name: str | None, clear: bool) -> None:
    """Perform quick modal validation while retaining worker authority."""

    if clear and name is not None:
        raise squad_identity_workers.SquadNameValidationError(
            'Choose either a squad name or clear, not both.'
        )
    if not clear and name is None:
        raise squad_identity_workers.SquadNameValidationError(
            'Enter a squad name or explicitly select clear.'
        )
    if not clear:
        squad_identity_workers.normalize_squad_name(name)


async def run_mutation(
    request: squad_identity_workers.SquadNameMutationRequest,
) -> squad_identity_workers.SquadNameMutationResult:
    """Use the one shared bounded worker path for slash and modal writes."""

    return await squad_identity_workers.run_squad_name_mutation(request)


def _display(value: str | None) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def read_message(
    card: squad_show_workers.SquadShowCard,
    *,
    actor: SquadIdentityActor | None = None,
) -> str:
    """Render a public, safely escaped current-name read."""

    message = (
        f'Current squad name for squad {int(card.squad_id)}: '
        f'**{_display(card.squad_name)}**'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def mutation_message(
    result: squad_identity_workers.SquadNameMutationResult,
    *,
    actor: SquadIdentityActor | None = None,
) -> str:
    """Render the post-commit public actor/target/result announcement."""

    actor_label = actor.label if actor is not None else result.requester_description
    if result.cleared:
        message = (
            f'{actor_label} cleared the name for squad {result.squad_id}. '
            f'Resulting name: **None**.'
        )
    else:
        message = (
            f'{actor_label} set the name for squad {result.squad_id} to '
            f'**{_display(result.name)}**.'
        )
    if result.truncated:
        message += (
            f'\n:information_source: The submitted value was normalized and '
            f'truncated to {MAX_SQUAD_NAME_LENGTH} characters.'
        )
    return message


def committed_refresh_warning(result) -> str:
    """Explain a committed write whose dense-card refresh did not complete."""

    return (
        f':warning: Squad {int(result.squad_id)} name data was committed, '
        'but the public squad card could not be refreshed. Run `/squad show '
        f'{int(result.squad_id)}` to reconcile the card.'
    )


def committed_public_warning(result) -> str:
    """Private fallback when a post-commit public announcement cannot send."""

    return (
        f':warning: Squad {int(result.squad_id)} name data was committed, '
        'but the public success announcement could not be sent. Run `/squad '
        f'show {int(result.squad_id)}` to reconcile the public state.'
    )
