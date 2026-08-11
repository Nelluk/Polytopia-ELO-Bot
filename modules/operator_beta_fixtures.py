"""Discord adapters for the owner-only development fixture workflow."""

from __future__ import annotations

import discord

from modules import operator_beta_fixtures_workers as workers


def _safe_name(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


def actor_description(member) -> str:
    name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{member.id}'
    )
    safe = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
    return f'**{safe}** (`{int(member.id)}`)'


def preview_request(
    interaction,
    *,
    operation: str,
    user_ids: tuple[int, ...] = (),
) -> workers.BetaFixturePreviewRequest:
    return workers.BetaFixturePreviewRequest(
        operation=str(operation),
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        user_ids=tuple(int(value) for value in user_ids),
    )


def commit_request(
    interaction,
    preview: workers.BetaFixturePreview,
) -> workers.BetaFixtureCommitRequest:
    return workers.BetaFixtureCommitRequest(
        operation=preview.operation,
        guild_id=int(interaction.guild_id),
        requester_id=int(interaction.user.id),
        requester_description=actor_description(interaction.user),
        user_ids=preview.user_ids,
        expected_game_ids=preview.snapshot.game_ids,
        expected_fingerprint=preview.snapshot.fingerprint,
    )


def readiness_markdown(snapshot: workers.BetaFixtureSnapshot) -> str:
    lines = [
        '## Fixture readiness',
        f'**Result scenarios:** {snapshot.readiness.title()}',
        snapshot.detail,
    ]
    if snapshot.participants:
        lines.append(
            '**Participants:** '
            + ', '.join(
                f'**{_safe_name(item.display_name)}** '
                f'(`{item.user_id}`)'
                for item in snapshot.participants
            )
        )
    elif snapshot.user_ids:
        lines.append(
            '**Participants:** '
            + ', '.join(f'`{value}`' for value in snapshot.user_ids)
        )
    if snapshot.scenarios:
        lines.extend(
            f'- **{item.scenario.title()}** — game `{item.game_id}` '
            f'({item.status})'
            for item in snapshot.scenarios
        )
    else:
        lines.append(
            '- An owner can create the fixed bundle with '
            '`/operator beta prepare`.'
        )
    return '\n'.join(lines)


def completion_markdown(
    result: workers.BetaFixtureResult,
    *,
    participants: tuple[workers.BetaFixtureParticipant, ...] = (),
) -> str:
    verb = 'prepared' if result.operation == workers.PREPARE else 'reset'
    lines = [
        f'Beta result fixtures were **{verb}** successfully.',
        'Participants: '
        + (
            ', '.join(
                f'**{_safe_name(item.display_name)}** '
                f'(`{item.user_id}`)'
                for item in participants
            )
            if participants else
            ', '.join(f'`{value}`' for value in result.user_ids)
        ),
    ]
    lines.extend(
        f'- **{item.scenario.title()}** — game `{item.game_id}`'
        for item in result.scenarios
    )
    if result.old_game_ids:
        lines.append(
            'Replaced owned game IDs: '
            + ', '.join(f'`{value}`' for value in result.old_game_ids)
        )
    return '\n'.join(lines)
