"""First trusted-guild bootstrap for a fresh development database.

The ordinary Discord enrollment flow requires an already-active guild.  This
module owns the one day-zero exception for the container operator interface:
one exact, Discord-observed guild may be activated in an otherwise empty
development schema.  It never synchronizes Discord application commands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from modules import (
    guild_configuration_delegation_storage as delegation,
    guild_configuration_draft_storage as drafts,
    guild_configuration_storage as storage,
)
from modules.database_schema_contract import REQUIRED_TABLES, WINNER_FOREIGN_KEY_SQL
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    document_digest,
    document_to_mapping,
    validate_document,
)


BOOTSTRAP_SCHEMA_VERSION = 1
BOOTSTRAP_ACTOR = storage.FIRST_GUILD_BOOTSTRAP_ACTOR
BOOTSTRAP_EVENT_TYPE = storage.FIRST_GUILD_BOOTSTRAP_EVENT_TYPE
BOOTSTRAP_TEMPLATE = storage.FIRST_GUILD_BOOTSTRAP_TEMPLATE
BOOTSTRAP_ADVISORY_LOCK_KEY = 0x50313135
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class FirstGuildBootstrapError(RuntimeError):
    """The first-guild plan, target, schema, or transaction is unsafe."""


@dataclass(frozen=True)
class FirstGuildBootstrapPlan:
    schema_version: int
    guild_id: int
    guild_name: str
    document_digest: str
    source_digest: str
    base_schema_digest: str
    draft_schema_digest: str
    delegation_schema_digest: str
    plan_digest: str
    document: GuildConfigurationDocument = field(repr=False)

    @property
    def confirmation(self) -> str:
        return f'P11.5B APPLY {self.guild_id} {self.plan_digest}'


@dataclass(frozen=True)
class FirstGuildBootstrapResult:
    guild_id: int
    revision: int
    generation: int
    document_digest: str
    base_schema_created: bool
    draft_schema_created: bool
    delegation_schema_created: bool
    application_commands_synchronized: bool = False


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _operator_only_document(
    *, guild_id: int, guild_name: str,
) -> GuildConfigurationDocument:
    """Return the reviewed least-authority day-zero configuration."""

    return validate_document({
        'schema_version': 1,
        'guild_id': guild_id,
        'identity': {
            'display_name': guild_name,
            'command_prefix': '$',
        },
        'permissions': {
            'helper_role_ids': [],
            'mod_role_ids': [],
            'user_role_ids_level_1': [],
            'user_role_ids_level_2': [guild_id],
            'user_role_ids_level_3': [],
            'user_role_ids_level_4': [],
            'inactive_role_id': None,
        },
        'teams': {
            'require_teams': False,
            'allow_teams': False,
            'allow_uneven_teams': False,
            'max_team_size': 2,
        },
        'visibility': {
            'include_in_global_leaderboard': False,
        },
        'channels': {
            'bot_channel_ids': None,
            'strict_bot_channel_ids': None,
            'private_bot_channel_ids': [],
            'newbie_message_channel_ids': [],
            'match_challenge_channel_ids': [],
            'ranked_game_channel_id': None,
            'unranked_game_channel_id': None,
            'steam_game_channel_id': None,
            'log_channel_id': None,
            'game_announce_channel_id': None,
            'staff_help_channel_id': None,
            'game_category_ids': [],
        },
        # The owner receives only the roots needed to finish configuration.
        # Guild-only command registration remains a separate explicit apply.
        'command_capabilities': ['guild_admin', 'operator'],
    })


def build_first_guild_plan(
    *,
    target: storage.StorageTarget,
    allowed_guild_ids: Sequence[int],
    discord_snapshot: Mapping[str, Any],
) -> FirstGuildBootstrapPlan:
    """Build one deterministic, database-free bootstrap plan."""

    storage.validate_target(target)
    allowed = tuple(allowed_guild_ids)
    if (
        len(allowed) != 1
        or isinstance(allowed[0], bool)
        or not isinstance(allowed[0], int)
        or allowed[0] <= 0
    ):
        raise FirstGuildBootstrapError(
            'First-guild bootstrap requires exactly one configured guild ID.'
        )
    guild_id = allowed[0]
    try:
        snapshots = storage.validate_discord_snapshot(
            discord_snapshot,
            target=target,
            allowed_guild_ids=allowed,
        )
        guild_name = snapshots[guild_id]['guild_name']
        document = _operator_only_document(
            guild_id=guild_id,
            guild_name=guild_name,
        )
        storage.validate_document_references(document, snapshots[guild_id])
    except (KeyError, storage.GuildConfigurationStorageError, ValueError) as exc:
        raise FirstGuildBootstrapError(
            'The configured application could not verify the exact first guild.'
        ) from exc

    document_value_digest = document_digest(document)
    source_digest = _canonical_digest({
        'schema_version': BOOTSTRAP_SCHEMA_VERSION,
        'template': BOOTSTRAP_TEMPLATE,
        'environment': target.environment,
        'application_id': target.expected_application_id,
        'guild_id': guild_id,
        'guild_name': guild_name,
        'document_digest': document_value_digest,
    })
    base_schema_digest = _canonical_digest({
        'storage_schema_version': storage.STORAGE_SCHEMA_VERSION,
        'statements': storage.CREATE_SCHEMA_STATEMENTS,
    })
    draft_plan = drafts.draft_schema_plan(target)
    delegation_plan = delegation.delegation_schema_plan(target)
    plan_payload = {
        'schema_version': BOOTSTRAP_SCHEMA_VERSION,
        'guild_id': guild_id,
        'document_digest': document_value_digest,
        'source_digest': source_digest,
        'base_schema_digest': base_schema_digest,
        'draft_schema_digest': draft_plan.statement_digest,
        'delegation_schema_digest': delegation_plan.statement_digest,
    }
    return FirstGuildBootstrapPlan(
        schema_version=BOOTSTRAP_SCHEMA_VERSION,
        guild_id=guild_id,
        guild_name=guild_name,
        document_digest=document_value_digest,
        source_digest=source_digest,
        base_schema_digest=base_schema_digest,
        draft_schema_digest=draft_plan.statement_digest,
        delegation_schema_digest=delegation_plan.statement_digest,
        plan_digest=_canonical_digest(plan_payload),
        document=document,
    )


def plan_to_mapping(plan: FirstGuildBootstrapPlan) -> dict[str, Any]:
    _validate_plan(plan)
    return {
        'schema_version': plan.schema_version,
        'operation': 'first_trusted_guild_bootstrap',
        'guild_id': plan.guild_id,
        'guild_name': plan.guild_name,
        'template': BOOTSTRAP_TEMPLATE,
        'command_capabilities': list(plan.document.command_capabilities),
        'document_digest': plan.document_digest,
        'source_digest': plan.source_digest,
        'base_schema_digest': plan.base_schema_digest,
        'draft_schema_digest': plan.draft_schema_digest,
        'delegation_schema_digest': plan.delegation_schema_digest,
        'plan_digest': plan.plan_digest,
        'confirmation': plan.confirmation,
        'application_commands_synchronized': False,
    }


def _validate_plan(plan: FirstGuildBootstrapPlan) -> None:
    if not isinstance(plan, FirstGuildBootstrapPlan):
        raise FirstGuildBootstrapError('A frozen first-guild plan is required.')
    if plan.schema_version != BOOTSTRAP_SCHEMA_VERSION:
        raise FirstGuildBootstrapError('First-guild plan version is invalid.')
    if plan.guild_id != plan.document.guild_id:
        raise FirstGuildBootstrapError('First-guild plan identity is inconsistent.')
    digests = (
        plan.document_digest,
        plan.source_digest,
        plan.base_schema_digest,
        plan.draft_schema_digest,
        plan.delegation_schema_digest,
        plan.plan_digest,
    )
    if any(not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value)
           for value in digests):
        raise FirstGuildBootstrapError('First-guild plan digest is invalid.')
    if document_digest(plan.document) != plan.document_digest:
        raise FirstGuildBootstrapError('First-guild document changed after planning.')
    try:
        expected_document = _operator_only_document(
            guild_id=plan.guild_id,
            guild_name=plan.guild_name,
        )
    except ValueError as exc:
        raise FirstGuildBootstrapError(
            'First-guild identity or template is invalid.'
        ) from exc
    if plan.document != expected_document:
        raise FirstGuildBootstrapError('First-guild template scope is invalid.')
    expected_plan_digest = _canonical_digest({
        'schema_version': plan.schema_version,
        'guild_id': plan.guild_id,
        'document_digest': plan.document_digest,
        'source_digest': plan.source_digest,
        'base_schema_digest': plan.base_schema_digest,
        'draft_schema_digest': plan.draft_schema_digest,
        'delegation_schema_digest': plan.delegation_schema_digest,
    })
    if expected_plan_digest != plan.plan_digest:
        raise FirstGuildBootstrapError('First-guild plan evidence changed.')


def _validate_application_schema_is_empty(cursor: Any) -> None:
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s) '
        'ORDER BY table_name',
        (list(REQUIRED_TABLES),),
    )
    actual = tuple(row[0] for row in cursor.fetchall())
    if actual != REQUIRED_TABLES:
        raise FirstGuildBootstrapError(
            'The complete application schema must exist before first-guild bootstrap.'
        )
    cursor.execute(WINNER_FOREIGN_KEY_SQL)
    if not bool(cursor.fetchone()[0]):
        raise FirstGuildBootstrapError(
            'The required game winner foreign key is absent.'
        )
    for table_name in REQUIRED_TABLES:
        cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table_name}" LIMIT 1)')
        if bool(cursor.fetchone()[0]):
            raise FirstGuildBootstrapError(
                'First-guild bootstrap requires an empty application database; '
                f'table {table_name!r} already contains data.'
            )


def _table_is_empty(cursor: Any, table_name: str) -> None:
    cursor.execute(f'SELECT EXISTS (SELECT 1 FROM "{table_name}" LIMIT 1)')
    if bool(cursor.fetchone()[0]):
        raise FirstGuildBootstrapError(
            f'First-guild bootstrap requires empty table {table_name!r}.'
        )


def _prepare_configuration_schemas(
    cursor: Any,
    *,
    target: storage.StorageTarget,
) -> tuple[bool, bool, bool]:
    base_inventory = storage.inspect_schema_inventory(cursor)
    base_present = storage.validate_schema_inventory(base_inventory)
    draft_inventory = drafts.inspect_draft_schema(cursor)
    draft_present = drafts.validate_draft_schema(draft_inventory)
    delegation_inventory = delegation.inspect_delegation_schema(cursor)
    delegation_present = delegation.validate_delegation_schema(
        delegation_inventory
    )
    if not base_present and (draft_present or delegation_present):
        raise FirstGuildBootstrapError(
            'Dependent guild-configuration storage exists without its base schema.'
        )
    if base_present:
        for table_name in storage.STORAGE_TABLES:
            _table_is_empty(cursor, table_name)
    if draft_present:
        _table_is_empty(cursor, drafts.DRAFT_TABLE)
    if delegation_present:
        _table_is_empty(cursor, delegation.DELEGATION_TABLE)

    if not base_present:
        for statement in storage.CREATE_SCHEMA_STATEMENTS:
            cursor.execute(statement)
    if not draft_present:
        for statement in drafts.CREATE_DRAFT_SCHEMA_STATEMENTS:
            cursor.execute(statement)
    if not delegation_present:
        for statement in delegation.CREATE_DELEGATION_SCHEMA_STATEMENTS:
            cursor.execute(statement)

    if not storage.validate_schema_inventory(
        storage.inspect_schema_inventory(cursor)
    ):
        raise FirstGuildBootstrapError('Base configuration schema was not created.')
    if not drafts.validate_draft_schema(drafts.inspect_draft_schema(cursor)):
        raise FirstGuildBootstrapError('Draft configuration schema was not created.')
    if not delegation.validate_delegation_schema(
        delegation.inspect_delegation_schema(cursor)
    ):
        raise FirstGuildBootstrapError(
            'Delegation configuration schema was not created.'
        )
    return not base_present, not draft_present, not delegation_present


def _insert_first_guild(cursor: Any, plan: FirstGuildBootstrapPlan) -> None:
    document_json = json.dumps(
        document_to_mapping(plan.document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'INSERT INTO "{storage.REGISTRY_TABLE}" '
        '(guild_id, storage_schema_version, enrollment_state, active_revision, '
        'generation, created_at, updated_at) '
        "VALUES (%s, %s, 'active', NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (plan.guild_id, storage.STORAGE_SCHEMA_VERSION),
    )
    cursor.execute(
        f'INSERT INTO "{storage.REVISION_TABLE}" '
        '(guild_id, revision_number, schema_version, document, document_digest, '
        'source_digest, parent_revision, source_kind, actor, created_at) '
        'VALUES (%s, 1, %s, CAST(%s AS JSONB), %s, %s, NULL, %s, %s, '
        'CURRENT_TIMESTAMP)',
        (
            plan.guild_id,
            plan.document.schema_version,
            document_json,
            plan.document_digest,
            plan.source_digest,
            drafts.ACTIVATION_SOURCE_KIND,
            BOOTSTRAP_ACTOR,
        ),
    )
    cursor.execute(
        f'UPDATE "{storage.REGISTRY_TABLE}" SET active_revision = 1, '
        'generation = 1, updated_at = CURRENT_TIMESTAMP '
        'WHERE guild_id = %s AND active_revision IS NULL AND generation = 0',
        (plan.guild_id,),
    )
    if cursor.rowcount != 1:
        raise FirstGuildBootstrapError(
            'The first-guild registry changed during activation.'
        )
    details = json.dumps({
        'template': BOOTSTRAP_TEMPLATE,
        'guild_name': plan.guild_name,
        'source_digest': plan.source_digest,
        'application_commands_synchronized': False,
    }, sort_keys=True)
    cursor.execute(
        f'INSERT INTO "{storage.AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) '
        'VALUES (%s, 1, %s, 1, 1, %s, %s, CAST(%s AS JSONB), CURRENT_TIMESTAMP)',
        (
            plan.guild_id,
            BOOTSTRAP_EVENT_TYPE,
            plan.document_digest,
            BOOTSTRAP_ACTOR,
            details,
        ),
    )


def _verify_first_guild(cursor: Any, plan: FirstGuildBootstrapPlan) -> None:
    cursor.execute(
        f'SELECT registry.enrollment_state, registry.active_revision, '
        'registry.generation, revision.schema_version, revision.document, '
        'revision.document_digest, revision.source_digest, '
        'revision.parent_revision, revision.source_kind, revision.actor '
        f'FROM "{storage.REGISTRY_TABLE}" AS registry '
        f'JOIN "{storage.REVISION_TABLE}" AS revision '
        'ON revision.guild_id = registry.guild_id '
        'AND revision.revision_number = registry.active_revision '
        'WHERE registry.guild_id = %s',
        (plan.guild_id,),
    )
    row = cursor.fetchone()
    expected = (
        'active', 1, 1, plan.document.schema_version,
        document_to_mapping(plan.document), plan.document_digest,
        plan.source_digest, None, drafts.ACTIVATION_SOURCE_KIND,
        BOOTSTRAP_ACTOR,
    )
    if row is None or tuple(row) != expected:
        raise FirstGuildBootstrapError(
            'The committed first-guild configuration failed exact verification.'
        )
    cursor.execute(
        f'SELECT event_number, event_type, revision_number, generation, '
        f'document_digest, actor FROM "{storage.AUDIT_TABLE}" '
        'WHERE guild_id = %s',
        (plan.guild_id,),
    )
    if tuple(cursor.fetchone() or ()) != (
        1, BOOTSTRAP_EVENT_TYPE, 1, 1, plan.document_digest, BOOTSTRAP_ACTOR,
    ):
        raise FirstGuildBootstrapError(
            'The committed first-guild audit failed exact verification.'
        )
    _table_is_empty(cursor, drafts.DRAFT_TABLE)
    _table_is_empty(cursor, delegation.DELEGATION_TABLE)


def apply_first_guild_bootstrap(
    connection: Any,
    *,
    target: storage.StorageTarget,
    plan: FirstGuildBootstrapPlan,
    confirmation: str,
) -> FirstGuildBootstrapResult:
    """Atomically create configuration storage and activate the first guild."""

    storage.validate_target(target)
    _validate_plan(plan)
    expected_source_digest = _canonical_digest({
        'schema_version': BOOTSTRAP_SCHEMA_VERSION,
        'template': BOOTSTRAP_TEMPLATE,
        'environment': target.environment,
        'application_id': target.expected_application_id,
        'guild_id': plan.guild_id,
        'guild_name': plan.guild_name,
        'document_digest': plan.document_digest,
    })
    expected_base_schema_digest = _canonical_digest({
        'storage_schema_version': storage.STORAGE_SCHEMA_VERSION,
        'statements': storage.CREATE_SCHEMA_STATEMENTS,
    })
    if (
        plan.source_digest != expected_source_digest
        or plan.base_schema_digest != expected_base_schema_digest
        or plan.draft_schema_digest
        != drafts.draft_schema_plan(target).statement_digest
        or plan.delegation_schema_digest
        != delegation.delegation_schema_plan(target).statement_digest
    ):
        raise FirstGuildBootstrapError(
            'Bootstrap plan does not match the current target and schema contract.'
        )
    if confirmation != plan.confirmation:
        raise FirstGuildBootstrapError(
            f'Bootstrap requires exact confirmation {plan.confirmation!r}.'
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), current_user')
            actual_database, actual_user = cursor.fetchone()
            storage.validate_live_identity(
                target,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() not in {'off', 'false'}:
                raise FirstGuildBootstrapError(
                    'First-guild bootstrap requires a read-write transaction.'
                )
            cursor.execute(
                'SELECT pg_advisory_xact_lock(%s)',
                (BOOTSTRAP_ADVISORY_LOCK_KEY,),
            )
            _validate_application_schema_is_empty(cursor)
            created = _prepare_configuration_schemas(cursor, target=target)
            _insert_first_guild(cursor, plan)
            _verify_first_guild(cursor, plan)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return FirstGuildBootstrapResult(
        guild_id=plan.guild_id,
        revision=1,
        generation=1,
        document_digest=plan.document_digest,
        base_schema_created=created[0],
        draft_schema_created=created[1],
        delegation_schema_created=created[2],
    )


__all__ = [
    'BOOTSTRAP_ACTOR',
    'BOOTSTRAP_ADVISORY_LOCK_KEY',
    'BOOTSTRAP_EVENT_TYPE',
    'BOOTSTRAP_TEMPLATE',
    'FirstGuildBootstrapError',
    'FirstGuildBootstrapPlan',
    'FirstGuildBootstrapResult',
    'apply_first_guild_bootstrap',
    'build_first_guild_plan',
    'plan_to_mapping',
]
