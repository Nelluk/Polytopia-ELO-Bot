"""One-time owner notices for the database-backed guild-settings rollout."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Awaitable, Callable, Mapping, Sequence

import discord

from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules import operator_guild_configuration_drafts as draft_service
from modules.guild_configuration_schema import document_digest


logger = logging.getLogger('polybot.' + __name__)
CAMPAIGN_ID = 'database-guild-settings-rollout-v1'
RECEIPT_DIRECTORY = 'guild-owner-notices'
RECEIPT_FILENAME = f'{CAMPAIGN_ID}.json'
MAX_MESSAGE_CHARACTERS = 1900
_receipt_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-owner-notice-receipts',
)
_delivery_active = False


class GuildOwnerNoticeError(RuntimeError):
    """A current, bounded owner-notice operation cannot be completed safely."""


@dataclass(frozen=True)
class GuildOwnerIdentity:
    guild_id: int
    guild_name: str
    owner_id: int
    owner_name: str


@dataclass(frozen=True)
class GuildReferenceIssue:
    field_key: str
    field_label: str
    kind: str
    object_ids: tuple[int, ...]
    owner_editable: bool


@dataclass(frozen=True)
class GuildNotice:
    guild_id: int
    guild_name: str
    revision: int
    generation: int
    document_digest: str
    issues: tuple[GuildReferenceIssue, ...]


@dataclass(frozen=True)
class OwnerNotice:
    owner_id: int
    owner_name: str
    guilds: tuple[GuildNotice, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class OwnerNoticePlan:
    campaign_id: str
    plan_digest: str
    guild_count: int
    issue_guild_count: int
    notices: tuple[OwnerNotice, ...]

    @property
    def recipient_count(self) -> int:
        return len(self.notices)

    @property
    def message_count(self) -> int:
        return sum(len(notice.messages) for notice in self.notices)


@dataclass(frozen=True)
class OwnerDeliveryStatus:
    owner_id: int
    state: str
    detail: str


@dataclass(frozen=True)
class OwnerNoticeDeliveryResult:
    statuses: tuple[OwnerDeliveryStatus, ...]

    @property
    def sent_count(self) -> int:
        return sum(value.state == 'sent' for value in self.statuses)

    @property
    def skipped_count(self) -> int:
        return sum(value.state == 'already_sent' for value in self.statuses)

    @property
    def failed_count(self) -> int:
        return sum(value.state == 'failed' for value in self.statuses)


_ROLE_FIELDS = (
    ('helper_roles', 'helper_role_ids'),
    ('mod_roles', 'mod_role_ids'),
    ('user_level_1_roles', 'user_role_ids_level_1'),
    ('user_level_2_roles', 'user_role_ids_level_2'),
    ('user_level_3_roles', 'user_role_ids_level_3'),
    ('user_level_4_roles', 'user_role_ids_level_4'),
    ('inactive_role', 'inactive_role_id'),
)
_CHANNEL_FIELDS = (
    ('bot_channels', 'bot_channel_ids'),
    ('strict_bot_channels', 'strict_bot_channel_ids'),
    ('private_bot_channels', 'private_bot_channel_ids'),
    ('newbie_channels', 'newbie_message_channel_ids'),
    ('challenge_channels', 'match_challenge_channel_ids'),
    ('ranked_game_channel', 'ranked_game_channel_id'),
    ('unranked_game_channel', 'unranked_game_channel_id'),
    ('steam_game_channel', 'steam_game_channel_id'),
    ('log_channel', 'log_channel_id'),
    ('game_announce_channel', 'game_announce_channel_id'),
    ('staff_help_channel', 'staff_help_channel_id'),
)


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise GuildOwnerNoticeError(f'{label} is invalid.')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise GuildOwnerNoticeError(f'{label} is invalid.') from exc
    if result <= 0:
        raise GuildOwnerNoticeError(f'{label} is invalid.')
    return result


def _safe_name(value: Any, fallback: str) -> str:
    rendered = str(value or '').strip() or fallback
    rendered = discord.utils.escape_mentions(
        discord.utils.escape_markdown(rendered)
    )
    return rendered[:100]


def capture_owner_identities(
    guilds: Sequence[Any],
    guild_ids: Sequence[int],
) -> tuple[GuildOwnerIdentity, ...]:
    """Freeze member-free guild ownership from the ready Discord cache."""

    expected = tuple(sorted(_positive_id(value, 'Guild ID') for value in guild_ids))
    if not expected or expected != tuple(sorted(set(expected))):
        raise GuildOwnerNoticeError('The active guild inventory is invalid.')
    by_id: dict[int, Any] = {}
    for guild in tuple(guilds):
        guild_id = _positive_id(getattr(guild, 'id', None), 'Discord guild ID')
        if guild_id not in expected:
            continue
        if guild_id in by_id:
            raise GuildOwnerNoticeError('The Discord guild inventory is duplicated.')
        by_id[guild_id] = guild
    if tuple(sorted(by_id)) != expected:
        raise GuildOwnerNoticeError(
            'The bot cannot see every active guild required by this notice.'
        )
    identities = []
    for guild_id in expected:
        guild = by_id[guild_id]
        owner_id = _positive_id(
            getattr(guild, 'owner_id', None),
            f'Owner for guild {guild_id}',
        )
        owner = getattr(guild, 'owner', None)
        owner_name = (
            getattr(owner, 'display_name', None)
            or getattr(owner, 'global_name', None)
            or getattr(owner, 'name', None)
            or f'Discord user {owner_id}'
        )
        identities.append(GuildOwnerIdentity(
            guild_id=guild_id,
            guild_name=_safe_name(getattr(guild, 'name', None), f'Guild {guild_id}'),
            owner_id=owner_id,
            owner_name=_safe_name(owner_name, f'Discord user {owner_id}'),
        ))
    return tuple(identities)


def _ids(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, int) and not isinstance(value, bool):
        return (value,)
    return tuple(int(item) for item in value)


def _issue(
    field_key: str,
    kind: str,
    object_ids: Sequence[int],
) -> GuildReferenceIssue:
    field = draft_service.FIELD_BY_KEY[field_key]
    return GuildReferenceIssue(
        field_key=field_key,
        field_label=field.label,
        kind=kind,
        object_ids=tuple(sorted(set(int(value) for value in object_ids))),
        owner_editable=field_key in draft_service.ORDINARY_FIELD_KEYS,
    )


def reference_issues(
    document: Any,
    snapshot: Mapping[str, Any],
) -> tuple[GuildReferenceIssue, ...]:
    """Return the exact current-reference failures accepted by validation."""

    role_rows = {int(row['id']): row for row in snapshot['roles']}
    channel_rows = {int(row['id']): row for row in snapshot['channels']}
    issues = []
    for field_key, attribute in _ROLE_FIELDS:
        values = _ids(getattr(document.permissions, attribute))
        missing = tuple(value for value in values if value not in role_rows)
        managed = tuple(
            value for value in values
            if value in role_rows and bool(role_rows[value]['managed'])
        )
        if missing:
            issues.append(_issue(field_key, 'missing_role', missing))
        if managed:
            issues.append(_issue(field_key, 'managed_role', managed))
    for field_key, attribute in _CHANNEL_FIELDS:
        values = _ids(getattr(document.channels, attribute))
        missing = tuple(value for value in values if value not in channel_rows)
        categories = tuple(
            value for value in values
            if value in channel_rows and channel_rows[value]['type'] == 'category'
        )
        if missing:
            issues.append(_issue(field_key, 'missing_channel', missing))
        if categories:
            issues.append(_issue(field_key, 'category_as_channel', categories))
    category_values = _ids(document.channels.game_category_ids)
    missing_categories = tuple(
        value for value in category_values
        if value not in channel_rows or channel_rows[value]['type'] != 'category'
    )
    if missing_categories:
        issues.append(_issue(
            'game_categories', 'missing_category', missing_categories,
        ))
    try:
        storage.validate_document_references(document, snapshot)
    except storage.GuildConfigurationStorageError as exc:
        if not issues:
            raise GuildOwnerNoticeError(
                'A live-reference validation failure could not be explained.'
            ) from exc
    else:
        if issues:
            raise GuildOwnerNoticeError(
                'The live-reference findings conflict with validation.'
            )
    return tuple(issues)


def _format_ids(values: Sequence[int]) -> str:
    shown = tuple(values[:8])
    text = ', '.join(f'`{value}`' for value in shown)
    if len(values) > len(shown):
        text += f' and {len(values) - len(shown)} more'
    return text


def _issue_line(issue: GuildReferenceIssue) -> str:
    nouns = {
        'missing_role': 'missing role',
        'managed_role': 'managed integration role',
        'missing_channel': 'missing channel',
        'category_as_channel': 'category used where a channel is required',
        'missing_category': 'missing or non-category channel',
    }
    noun = nouns[issue.kind]
    return f'- **{issue.field_label}:** {noun} {_format_ids(issue.object_ids)}.'


def _guild_block(guild: GuildNotice) -> str:
    lines = [f'**{guild.guild_name}** (`{guild.guild_id}`)']
    if not guild.issues:
        lines.append('✅ No configuration problems were detected for this server.')
        return '\n'.join(lines)
    ordinary = tuple(value for value in guild.issues if value.owner_editable)
    protected = tuple(value for value in guild.issues if not value.owner_editable)
    if ordinary:
        lines.append('Please review these items in `/guild settings`:')
        lines.extend(_issue_line(value) for value in ordinary)
    if protected:
        lines.append('Please ask Nelluk to correct these protected settings:')
        lines.extend(_issue_line(value) for value in protected)
    return '\n'.join(lines)


def _bounded_paragraphs(paragraphs: Sequence[str]) -> tuple[str, ...]:
    bounded = []
    for paragraph in paragraphs:
        if len(paragraph) <= MAX_MESSAGE_CHARACTERS:
            bounded.append(paragraph)
            continue
        current = ''
        for line in paragraph.splitlines():
            candidate = line if not current else f'{current}\n{line}'
            if len(candidate) <= MAX_MESSAGE_CHARACTERS:
                current = candidate
            else:
                if current:
                    bounded.append(current)
                if len(line) > MAX_MESSAGE_CHARACTERS:
                    raise GuildOwnerNoticeError(
                        'One owner-notice line exceeds the Discord message bound.'
                    )
                current = line
        if current:
            bounded.append(current)
    messages = []
    current = ''
    for paragraph in bounded:
        candidate = paragraph if not current else f'{current}\n\n{paragraph}'
        if len(candidate) <= MAX_MESSAGE_CHARACTERS:
            current = candidate
        else:
            messages.append(current)
            current = paragraph
    if current:
        messages.append(current)
    if not messages:
        raise GuildOwnerNoticeError('The owner notice is empty.')
    return tuple(messages)


def _owner_messages(guilds: Sequence[GuildNotice]) -> tuple[str, ...]:
    introduction = (
        'Hello! A quick PolyElo update:\n\n'
        'Slash commands are now the preferred way to use PolyElo. Type `/` '
        'in your server to browse the commands available there. Ping Nelluk '
        "if you notice a slash command that doesn't seem right.\n\n"
        'As the Discord server owner, you can now use `/guild settings` to '
        'review and update ordinary PolyElo settings. Settings that affect '
        'the wider bot network remain managed by Nelluk.\n\n'
        'If you would like another moderator or staff role to manage ordinary '
        'server settings, send Nelluk the server name and exact role name or '
        'role ID.'
    )
    conclusion = (
        'If everything looks correct, no action is required. This bot does '
        'not process replies to DMs; contact Nelluk directly if you need help.'
    )
    return _bounded_paragraphs((
        introduction,
        *(_guild_block(value) for value in guilds),
        conclusion,
    ))


def build_plan(
    *,
    profile: Any,
    runtime_records: Sequence[Any],
    discord_snapshot: Mapping[str, Any],
    owners: Sequence[GuildOwnerIdentity],
) -> OwnerNoticePlan:
    """Build one immutable campaign plan without database or Discord writes."""

    records = tuple(runtime_records)
    record_ids = tuple(sorted(_positive_id(value.guild_id, 'Runtime guild ID') for value in records))
    if not record_ids or record_ids != tuple(sorted(set(record_ids))):
        raise GuildOwnerNoticeError('The runtime guild inventory is invalid.')
    owners = tuple(owners)
    owner_by_guild = {value.guild_id: value for value in owners}
    if len(owner_by_guild) != len(owners) or tuple(sorted(owner_by_guild)) != record_ids:
        raise GuildOwnerNoticeError('The owner inventory differs from the runtime guilds.')
    try:
        target = shadow.target_from_profile(profile)
        snapshots = storage.validate_discord_snapshot(
            discord_snapshot,
            target=target,
            allowed_guild_ids=record_ids,
        )
    except (shadow.GuildConfigurationShadowError, storage.GuildConfigurationStorageError) as exc:
        raise GuildOwnerNoticeError('The current Discord snapshot is invalid.') from exc
    guild_notices = []
    for record in sorted(records, key=lambda value: int(value.guild_id)):
        guild_id = int(record.guild_id)
        identity = owner_by_guild[guild_id]
        stored_digest = str(record.document_digest)
        if (
                int(record.document.guild_id) != guild_id
                or len(stored_digest) != 64
                or any(character not in '0123456789abcdef' for character in stored_digest)
                or document_digest(record.document) != stored_digest
        ):
            raise GuildOwnerNoticeError(
                f'The runtime evidence for guild {guild_id} is invalid.'
            )
        guild_notices.append((identity.owner_id, identity.owner_name, GuildNotice(
            guild_id=guild_id,
            guild_name=identity.guild_name,
            revision=_positive_id(record.revision, 'Runtime revision'),
            generation=_positive_id(record.generation, 'Runtime generation'),
            document_digest=stored_digest,
            issues=reference_issues(record.document, snapshots[guild_id]),
        )))
    grouped: dict[int, dict[str, Any]] = {}
    for owner_id, owner_name, guild in guild_notices:
        value = grouped.setdefault(owner_id, {'name': owner_name, 'guilds': []})
        if value['name'] != owner_name:
            value['name'] = f'Discord user {owner_id}'
        value['guilds'].append(guild)
    notices = []
    for owner_id in sorted(grouped):
        guilds = tuple(sorted(grouped[owner_id]['guilds'], key=lambda value: value.guild_id))
        notices.append(OwnerNotice(
            owner_id=owner_id,
            owner_name=grouped[owner_id]['name'],
            guilds=guilds,
            messages=_owner_messages(guilds),
        ))
    digest_payload = {
        'campaign_id': CAMPAIGN_ID,
        'recipients': [{
            'owner_id': value.owner_id,
            'guilds': [{
                'guild_id': guild.guild_id,
                'revision': guild.revision,
                'generation': guild.generation,
                'document_digest': guild.document_digest,
                'issues': [issue.__dict__ for issue in guild.issues],
            } for guild in value.guilds],
            'messages': list(value.messages),
        } for value in notices],
    }
    digest = hashlib.sha256(json.dumps(
        digest_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode()).hexdigest()
    return OwnerNoticePlan(
        campaign_id=CAMPAIGN_ID,
        plan_digest=digest,
        guild_count=len(guild_notices),
        issue_guild_count=sum(bool(value.issues) for _, _, value in guild_notices),
        notices=tuple(notices),
    )


def receipt_path(log_root: Path) -> Path:
    root = Path(log_root).resolve()
    return root / RECEIPT_DIRECTORY / RECEIPT_FILENAME


def _empty_receipts() -> dict[str, Any]:
    return {
        'schema_version': 1,
        'campaign_id': CAMPAIGN_ID,
        'deliveries': {},
    }


def load_receipts(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return _empty_receipts()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuildOwnerNoticeError('The owner-notice receipt file is unreadable.') from exc
    if (
            not isinstance(value, dict)
            or value.get('schema_version') != 1
            or value.get('campaign_id') != CAMPAIGN_ID
            or not isinstance(value.get('deliveries'), dict)
    ):
        raise GuildOwnerNoticeError('The owner-notice receipt file is invalid.')
    for owner_id, delivery in value['deliveries'].items():
        try:
            _positive_id(owner_id, 'Receipt owner ID')
        except GuildOwnerNoticeError as exc:
            raise GuildOwnerNoticeError(
                'The owner-notice receipt file is invalid.'
            ) from exc
        if not isinstance(delivery, Mapping):
            raise GuildOwnerNoticeError('The owner-notice receipt file is invalid.')
        plan_digest = delivery.get('plan_digest')
        message_digests = delivery.get('message_digests')
        message_ids = delivery.get('message_ids')
        completed = delivery.get('completed')
        if (
                not isinstance(plan_digest, str)
                or len(plan_digest) != 64
                or any(value not in '0123456789abcdef' for value in plan_digest)
                or not isinstance(message_digests, list)
                or not isinstance(message_ids, list)
                or len(message_digests) != len(message_ids)
                or not isinstance(completed, bool)
                or (completed and not message_digests)
                or any(
                    not isinstance(item, str)
                    or len(item) != 64
                    or any(value not in '0123456789abcdef' for value in item)
                    for item in message_digests
                )
        ):
            raise GuildOwnerNoticeError('The owner-notice receipt file is invalid.')
        try:
            tuple(_positive_id(item, 'Receipt message ID') for item in message_ids)
        except GuildOwnerNoticeError as exc:
            raise GuildOwnerNoticeError(
                'The owner-notice receipt file is invalid.'
            ) from exc
    return value


def completed_owner_ids(receipts: Mapping[str, Any]) -> tuple[int, ...]:
    values = []
    for owner_id, delivery in receipts.get('deliveries', {}).items():
        if isinstance(delivery, Mapping) and delivery.get('completed') is True:
            values.append(_positive_id(owner_id, 'Receipt owner ID'))
    return tuple(sorted(values))


def _write_receipts(path: Path, receipts: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(receipts, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o640)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def record_delivery_part(
    path: Path,
    *,
    plan: OwnerNoticePlan,
    notice: OwnerNotice,
    message_index: int,
    message_id: int,
) -> None:
    if message_index < 0 or message_index >= len(notice.messages):
        raise GuildOwnerNoticeError('The owner-notice message index is invalid.')
    receipts = load_receipts(path)
    deliveries = receipts['deliveries']
    key = str(notice.owner_id)
    current = deliveries.get(key)
    if current is None:
        current = {
            'plan_digest': plan.plan_digest,
            'message_digests': [],
            'message_ids': [],
            'completed': False,
        }
    if current.get('plan_digest') != plan.plan_digest:
        raise GuildOwnerNoticeError(
            'A partial delivery exists for an older plan; review it manually.'
        )
    digests = list(current.get('message_digests', []))
    ids = list(current.get('message_ids', []))
    if message_index != len(digests) or message_index != len(ids):
        raise GuildOwnerNoticeError('The owner-notice receipt sequence is invalid.')
    digest = hashlib.sha256(notice.messages[message_index].encode()).hexdigest()
    digests.append(digest)
    ids.append(_positive_id(message_id, 'Discord message ID'))
    deliveries[key] = {
        'plan_digest': plan.plan_digest,
        'message_digests': digests,
        'message_ids': ids,
        'completed': len(digests) == len(notice.messages),
        'updated_at': datetime.now(UTC).isoformat(),
    }
    _write_receipts(path, receipts)


async def _drain_receipt_future(future: Future):
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            future.result()
        except BaseException:
            pass
        raise cancellation
    return future.result()


async def run_receipt_io(function: Callable[..., Any], *args, **kwargs):
    """Own and drain one bounded receipt operation off the event loop."""

    future = _receipt_executor.submit(function, *args, **kwargs)
    return await _drain_receipt_future(future)


async def deliver_plan(
    plan: OwnerNoticePlan,
    *,
    resolve_user: Callable[[int], Awaitable[Any]],
    receipts_path: Path,
) -> OwnerNoticeDeliveryResult:
    """Send one current plan sequentially and durably record each success."""

    global _delivery_active
    if _delivery_active:
        raise GuildOwnerNoticeError(
            'Another guild-owner delivery is already running.'
        )
    _delivery_active = True
    try:
        receipts = await run_receipt_io(load_receipts, receipts_path)
        complete = set(completed_owner_ids(receipts))
        statuses = []
        for notice in plan.notices:
            if notice.owner_id in complete:
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'already_sent',
                    'Already delivered in this campaign.',
                ))
                continue
            partial = receipts['deliveries'].get(str(notice.owner_id))
            if partial is not None and partial.get('plan_digest') != plan.plan_digest:
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'failed',
                    'A partial older delivery requires manual review.',
                ))
                continue
            start_index = len(partial.get('message_ids', ())) if partial else 0
            expected_prefix = [
                hashlib.sha256(message.encode()).hexdigest()
                for message in notice.messages[:start_index]
            ]
            if partial is not None and (
                    start_index >= len(notice.messages)
                    or partial.get('message_digests') != expected_prefix
            ):
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'failed',
                    'The partial delivery receipt requires manual review.',
                ))
                continue
            try:
                user = await resolve_user(notice.owner_id)
                if user is None:
                    raise GuildOwnerNoticeError(
                        'Discord user could not be resolved.'
                    )
                for index in range(start_index, len(notice.messages)):
                    sent = await user.send(
                        notice.messages[index],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    await run_receipt_io(
                        record_delivery_part,
                        receipts_path,
                        plan=plan,
                        notice=notice,
                        message_index=index,
                        message_id=int(sent.id),
                    )
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'sent', 'Delivered successfully.',
                ))
            except OSError as exc:
                logger.warning(
                    'Guild-owner notice receipt failed for owner %s after a '
                    'possible Discord send: %s', notice.owner_id, exc,
                )
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'failed',
                    'A DM may have been sent but its receipt failed. Do not '
                    'retry until the owner is checked manually.',
                ))
            except (discord.HTTPException, GuildOwnerNoticeError) as exc:
                logger.warning(
                    'Guild-owner notice delivery failed for owner %s: %s',
                    notice.owner_id, exc,
                )
                statuses.append(OwnerDeliveryStatus(
                    notice.owner_id, 'failed',
                    'Discord delivery failed; manual follow-up is required.',
                ))
        return OwnerNoticeDeliveryResult(tuple(statuses))
    finally:
        _delivery_active = False


__all__ = [
    'CAMPAIGN_ID',
    'GuildNotice',
    'GuildOwnerIdentity',
    'GuildOwnerNoticeError',
    'GuildReferenceIssue',
    'MAX_MESSAGE_CHARACTERS',
    'OwnerDeliveryStatus',
    'OwnerNotice',
    'OwnerNoticeDeliveryResult',
    'OwnerNoticePlan',
    'build_plan',
    'capture_owner_identities',
    'completed_owner_ids',
    'deliver_plan',
    'load_receipts',
    'receipt_path',
    'record_delivery_part',
    'reference_issues',
    'run_receipt_io',
]
