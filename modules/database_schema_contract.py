"""Model-free contract for the schema required by an ordinary bot start."""

from __future__ import annotations


REQUIRED_TABLES = (
    'apiapplication',
    'auction',
    'bid',
    'configuration',
    'discordmember',
    'game',
    'gamelog',
    'gameside',
    'house',
    'lineup',
    'player',
    'playerhousepreference',
    'squad',
    'squadmember',
    'team',
    'team_server_broadcast_message',
    'tribe',
)

WINNER_FOREIGN_KEY_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM pg_constraint AS constraint_record
    JOIN pg_class AS source_table
      ON source_table.oid = constraint_record.conrelid
    JOIN pg_namespace AS source_namespace
      ON source_namespace.oid = source_table.relnamespace
    JOIN pg_class AS target_table
      ON target_table.oid = constraint_record.confrelid
    JOIN pg_namespace AS target_namespace
      ON target_namespace.oid = target_table.relnamespace
    JOIN pg_attribute AS source_column
      ON source_column.attrelid = source_table.oid
     AND source_column.attnum = constraint_record.conkey[1]
    JOIN pg_attribute AS target_column
      ON target_column.attrelid = target_table.oid
     AND target_column.attnum = constraint_record.confkey[1]
    WHERE constraint_record.contype = 'f'
      AND array_length(constraint_record.conkey, 1) = 1
      AND array_length(constraint_record.confkey, 1) = 1
      AND source_namespace.nspname = current_schema()
      AND target_namespace.nspname = current_schema()
      AND source_table.relname = 'game'
      AND source_column.attname = 'winner_id'
      AND target_table.relname = 'gameside'
      AND target_column.attname = 'id'
)
"""
