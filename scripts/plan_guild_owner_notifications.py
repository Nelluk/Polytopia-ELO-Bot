#!/usr/bin/env python3
"""Build a private, offline guild-owner notification plan; never send it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import guild_configuration_storage as storage  # noqa: E402
from runtime_config import load_runtime_profile  # noqa: E402
from scripts import manage_guild_configuration_storage as manager  # noqa: E402


DEFAULT_IMPORT_PLAN = (
    'logs/production/guild-configuration/import-plan.json'
)
DEFAULT_OUTPUT = (
    'logs/production/guild-configuration/owner-notification-plan.json'
)
SCOPES = ('all', 'access', 'routing', 'review')
MAX_MESSAGE_CHARACTERS = 1900


class OwnerNotificationPlanError(RuntimeError):
    """The private import plan cannot produce a safe notification plan."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Plan guild-owner migration notices without sending messages.'
    )
    parser.add_argument('operation', choices=('plan',))
    parser.add_argument('--import-plan', default=DEFAULT_IMPORT_PLAN)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--scope', choices=SCOPES, default='review')
    parser.add_argument(
        '--guild-ids',
        default='all',
        help='all or a comma-separated subset of imported guild IDs',
    )
    return parser


def _target(profile: Any) -> storage.StorageTarget:
    return storage.StorageTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        expected_application_id=profile.expected_bot_id,
        background_tasks_enabled=profile.background_tasks_enabled,
        api_enabled=profile.api_enabled,
        bullet_enabled=profile.bullet_enabled,
    )


def _selected_guild_ids(raw: str, allowed: Sequence[int]) -> tuple[int, ...]:
    allowed_ids = tuple(sorted(set(int(value) for value in allowed)))
    if raw == 'all':
        return allowed_ids
    try:
        selected = tuple(sorted({
            int(value) for value in raw.split(',') if value
        }))
    except ValueError as exc:
        raise OwnerNotificationPlanError(
            'Guild IDs must be all or a comma-separated integer list.'
        ) from exc
    if not selected or any(value <= 0 for value in selected):
        raise OwnerNotificationPlanError('At least one positive guild ID is required.')
    unknown = sorted(set(selected) - set(allowed_ids))
    if unknown:
        raise OwnerNotificationPlanError(
            'Notification targets are outside the import plan: '
            + ', '.join(str(value) for value in unknown)
        )
    return selected


def _cleanup_guilds(
    value: Mapping[str, Any],
    *,
    imported_guild_ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    report = value.get('production_cleanup_report')
    if not isinstance(report, Mapping) or report.get('schema_version') != 1:
        raise OwnerNotificationPlanError(
            'Import plan has no supported production cleanup report.'
        )
    rows = report.get('guilds')
    if not isinstance(rows, list):
        raise OwnerNotificationPlanError('Cleanup report guilds are invalid.')
    by_guild = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise OwnerNotificationPlanError('Cleanup report row is invalid.')
        guild_id = raw.get('guild_id')
        owner = raw.get('owner')
        if (
                isinstance(guild_id, bool) or not isinstance(guild_id, int)
                or guild_id <= 0 or guild_id in by_guild
                or not isinstance(raw.get('guild_name'), str)
                or not raw['guild_name']
                or raw.get('severity') not in {
                    'none', 'informational', 'partial_cleanup',
                    'review_before_cutover',
                }
                or not isinstance(raw.get('issues'), list)
                or not isinstance(owner, Mapping)
                or isinstance(owner.get('owner_id'), bool)
                or not isinstance(owner.get('owner_id'), int)
                or owner['owner_id'] <= 0
                or not isinstance(owner.get('owner_name'), str)
                or not owner['owner_name']
        ):
            raise OwnerNotificationPlanError('Cleanup report owner row is invalid.')
        by_guild[guild_id] = dict(raw)
    if tuple(sorted(by_guild)) != tuple(sorted(imported_guild_ids)):
        raise OwnerNotificationPlanError(
            'Cleanup report guilds differ from the digest-bound import plan.'
        )
    return by_guild


def _matches_scope(guild: Mapping[str, Any], scope: str) -> bool:
    if scope == 'all':
        return True
    if scope == 'review':
        return guild['severity'] == 'review_before_cutover'
    categories = {
        issue.get('category')
        for issue in guild['issues']
        if isinstance(issue, Mapping)
    }
    if scope == 'access':
        return 'guild_administration_access' in categories
    return bool(categories.intersection({
        'bot_channel_routing',
        'operational_destination',
        'game_channel_categories',
    }))


def _issue_text(issue: Mapping[str, Any]) -> str:
    field = str(issue.get('field', 'setting')).replace('_', ' ')
    configured = issue.get('configured_value')
    kind = issue.get('kind')
    if kind == 'ambiguous_role_name':
        return (
            f'{field}: {configured!r} matches multiple roles; all exact '
            'matches will be preserved.'
        )
    if kind == 'case_only_role':
        candidates = ', '.join(issue.get('case_only_candidates') or ())
        return (
            f'{field}: {configured!r} has only a case-different match '
            f'({candidates}) and will not be granted automatically.'
        )
    if kind == 'managed_role':
        return f'{field}: managed role {configured!r} cannot grant bot access.'
    if kind == 'singleton_channel_list':
        return (
            f'{field}: its sole configured channel ID '
            f'{issue.get("resolved_channel_id")} will be preserved.'
        )
    if kind == 'duplicate_channel_id':
        return (
            f'{field}: duplicate channel ID {configured} will be reduced '
            'to one preserved destination.'
        )
    if 'channel' in str(kind):
        return f'{field}: channel ID {configured} no longer resolves and will be cleared.'
    return f'{field}: {configured!r} no longer resolves and will be cleared.'


def _guild_notice(guild: Mapping[str, Any]) -> str:
    lines = [
        f'**{guild["guild_name"]}** (`{guild["guild_id"]}`)',
        f'Cleanup status: {str(guild["severity"]).replace("_", " ")}.',
    ]
    issues = [
        issue for issue in guild['issues'] if isinstance(issue, Mapping)
    ]
    if issues:
        lines.extend(f'- {_issue_text(issue)}' for issue in issues)
    else:
        lines.append('- No stale role or channel references were detected.')
    return '\n'.join(lines)


def _message_parts(
    owner_name: str,
    guilds: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    introduction = (
        f'Hello {owner_name},\n\n'
        'PolyELO is preparing to move this server from legacy static settings '
        'to guild-managed configuration. Squad play remains available '
        'independently; persistent Team and league commands will appear only '
        'in designated Team/League venues.\n\n'
        'As the Discord server owner, you will retain access to ordinary '
        '`/guild settings` management after rollout. Protected bot-owner '
        'settings remain centrally managed.'
    )
    conclusion = (
        'After rollout, use `/guild settings` to review the live role and '
        'channel mappings. This is an advance notice; no reply is required '
        'unless you want a missing role or destination restored.'
    )
    paragraphs = [introduction]
    for guild in guilds:
        notice = _guild_notice(guild)
        current = ''
        for line in notice.splitlines():
            candidate = line if not current else f'{current}\n{line}'
            if len(candidate) <= MAX_MESSAGE_CHARACTERS:
                current = candidate
            else:
                if current:
                    paragraphs.append(current)
                current = line
        if current:
            paragraphs.append(current)
    paragraphs.append(conclusion)

    messages = []
    current = ''
    for paragraph in paragraphs:
        if len(paragraph) > MAX_MESSAGE_CHARACTERS:
            raise OwnerNotificationPlanError(
                'One notification paragraph exceeds the Discord message bound.'
            )
        candidate = paragraph if not current else f'{current}\n\n{paragraph}'
        if len(candidate) <= MAX_MESSAGE_CHARACTERS:
            current = candidate
        else:
            messages.append(current)
            current = paragraph
    if current:
        messages.append(current)
    if not messages or any(len(message) > MAX_MESSAGE_CHARACTERS for message in messages):
        raise OwnerNotificationPlanError(
            'Notification messages could not be bounded safely.'
        )
    return tuple(messages)


def build_plan(
    import_plan: Mapping[str, Any],
    *,
    profile: Any,
    scope: str,
    guild_ids: str,
) -> dict[str, Any]:
    target = _target(profile)
    storage.validate_target(target)
    if profile.environment != storage.PRODUCTION_ENVIRONMENT:
        raise OwnerNotificationPlanError(
            'Guild-owner notification planning is production-only.'
        )
    bundle = storage.bundle_from_mapping(import_plan, target=target)
    imported_ids = tuple(item.guild_id for item in bundle.imports)
    selected_ids = _selected_guild_ids(guild_ids, imported_ids)
    cleanup = _cleanup_guilds(
        import_plan,
        imported_guild_ids=imported_ids,
    )
    selected = [
        cleanup[guild_id]
        for guild_id in selected_ids
        if _matches_scope(cleanup[guild_id], scope)
    ]
    by_owner: dict[int, dict[str, Any]] = {}
    for guild in selected:
        owner = guild['owner']
        recipient = by_owner.setdefault(owner['owner_id'], {
            'owner_id': owner['owner_id'],
            'owner_name': owner['owner_name'],
            'guilds': [],
        })
        if recipient['owner_name'] != owner['owner_name']:
            raise OwnerNotificationPlanError(
                'One owner ID has conflicting names in the cleanup report.'
            )
        recipient['guilds'].append(guild)
    recipients = []
    for owner_id in sorted(by_owner):
        recipient = by_owner[owner_id]
        guilds = sorted(recipient['guilds'], key=lambda value: value['guild_id'])
        recipients.append({
            'owner_id': owner_id,
            'owner_name': recipient['owner_name'],
            'guild_ids': [guild['guild_id'] for guild in guilds],
            'messages': list(_message_parts(recipient['owner_name'], guilds)),
        })
    digest_payload = {
        'schema_version': 1,
        'import_bundle_digest': bundle.bundle_digest,
        'scope': scope,
        'recipients': recipients,
    }
    digest = hashlib.sha256(json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return {
        **digest_payload,
        'kind': 'guild_owner_notification_plan',
        'notification_plan_digest': digest,
        'selected_guild_ids': [guild['guild_id'] for guild in selected],
        'recipient_count': len(recipients),
        'guild_count': len(selected),
        'message_count': sum(
            len(recipient['messages']) for recipient in recipients
        ),
        'messages_sent': 0,
        'discord_connected': False,
        'database_connected': False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.environ.get('POLYBOT_ENV') != storage.PRODUCTION_ENVIRONMENT:
            raise OwnerNotificationPlanError(
                'Set exact POLYBOT_ENV=production for owner notification planning.'
            )
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        import_plan = manager._load_snapshot(
            args.import_plan,
            environment=storage.PRODUCTION_ENVIRONMENT,
        )
        plan = build_plan(
            import_plan,
            profile=profile,
            scope=args.scope,
            guild_ids=args.guild_ids,
        )
        path = manager._write_snapshot(
            args.output,
            plan,
            environment=storage.PRODUCTION_ENVIRONMENT,
        )
        print(json.dumps({
            'status': 'planned',
            'path': str(path.relative_to(PROJECT_ROOT)),
            'notification_plan_digest': plan['notification_plan_digest'],
            'recipient_count': plan['recipient_count'],
            'guild_count': plan['guild_count'],
            'message_count': plan['message_count'],
            'messages_sent': 0,
            'discord_connected': False,
            'database_connected': False,
        }, sort_keys=True, indent=2))
        return 0
    except (
        OwnerNotificationPlanError,
        storage.GuildConfigurationStorageError,
    ) as exc:
        print(f'Guild-owner notification plan refused: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
