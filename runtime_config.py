"""Central, side-effect-limited runtime profile selection for PolyBot."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import importlib.util
import os
from pathlib import Path
import re
from types import MappingProxyType, ModuleType
from typing import Mapping, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
SUPPORTED_ENVIRONMENTS = ('production', 'development')
SUPPORTED_GUILD_CONFIGURATION_SOURCES = ('static', 'database')
LEGACY_PRODUCTION_BOT_ID = 484067640302764042
DEVELOPMENT_PLACEHOLDER_ID = 123456789012345678
KNOWN_PRODUCTION_GUILD_IDS = frozenset({
    283436219780825088,
    447883341463814144,
})
_DEVELOPMENT_DATABASE_MARKER = re.compile(
    r'(^|[_-])(dev|development|test|testing|sandbox)([_-]|$)',
    re.IGNORECASE,
)


class RuntimeConfigurationError(RuntimeError):
    """Raised when a runtime profile is incomplete or unsafe."""


@dataclass(frozen=True)
class RuntimeProfile:
    """All environment-specific resources selected for one process."""

    environment: str
    project_root: Path
    config_path: Path
    discord_token: str = field(repr=False)
    expected_bot_id: int
    owner_id: int
    superuser_ids: Tuple[int, ...]
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    database_host: Optional[str]
    database_port: Optional[int]
    pastebin_key: Optional[str] = field(repr=False)
    server_settings_module: str
    server_settings: ModuleType
    guild_configuration_source: str
    allowed_guild_ids: Tuple[int, ...]
    shared_production_guild_ids: Tuple[int, ...]
    background_tasks_enabled: bool
    api_enabled: bool
    bullet_enabled: bool
    image_root: Path
    log_root: Path

    def validate_logged_in_bot(self, actual_bot_id: int) -> None:
        """Reject a Discord login that does not match the selected profile."""

        if int(actual_bot_id) != self.expected_bot_id:
            raise RuntimeConfigurationError(
                'Discord application mismatch: runtime profile expects bot ID '
                f'{self.expected_bot_id}, but Discord authenticated bot ID '
                f'{actual_bot_id}.'
            )


_PROFILE_LAYOUT = MappingProxyType({
    'production': MappingProxyType({
        'config_file': 'config.ini',
        'server_settings_module': 'server_settings',
        'image_root': 'data/images',
        'log_root': 'logs',
        'background_tasks_enabled': True,
        'api_enabled': True,
        'bullet_enabled': True,
    }),
    'development': MappingProxyType({
        'config_file': 'config.development.ini',
        'server_settings_module': 'server_settings_dev',
        'image_root': 'data/development/images',
        'log_root': 'logs/development',
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }),
})

_runtime_profile: Optional[RuntimeProfile] = None


def _required_value(
        parser: configparser.ConfigParser,
        key: str,
        config_path: Path) -> str:
    value = parser['DEFAULT'].get(key, '').strip()
    if not value:
        raise RuntimeConfigurationError(
            f'Missing required setting {key!r} in {config_path}.'
        )
    return value


def _positive_int(value: str, key: str, config_path: Path) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeConfigurationError(
            f'Setting {key!r} in {config_path} must be an integer.'
        ) from exc
    if parsed <= 0:
        raise RuntimeConfigurationError(
            f'Setting {key!r} in {config_path} must be positive.'
        )
    return parsed


def _positive_id_list(
        value: str,
        key: str,
        config_path: Path) -> Tuple[int, ...]:
    """Parse one optional comma-separated set of positive Discord IDs."""

    if not value.strip():
        return ()
    raw_values = tuple(part.strip() for part in value.split(','))
    if any(not part for part in raw_values):
        raise RuntimeConfigurationError(
            f'Setting {key!r} in {config_path} must be a comma-separated '
            'list of positive integer Discord IDs.'
        )
    parsed = tuple(
        _positive_int(part, key, config_path)
        for part in raw_values
    )
    if len(parsed) != len(set(parsed)):
        raise RuntimeConfigurationError(
            f'Setting {key!r} in {config_path} contains duplicate Discord IDs.'
        )
    return tuple(sorted(parsed))


def _optional_port(
        parser: configparser.ConfigParser,
        config_path: Path) -> Optional[int]:
    value = parser['DEFAULT'].get('psql_port', '').strip()
    if not value:
        return None
    port = _positive_int(value, 'psql_port', config_path)
    if port > 65535:
        raise RuntimeConfigurationError(
            f'Setting {"psql_port"!r} in {config_path} must be at most '
            '65535.'
        )
    return port


def _boolean_setting(
        parser: configparser.ConfigParser,
        key: str,
        default: bool,
        config_path: Path) -> bool:
    if key not in parser['DEFAULT']:
        return default
    try:
        return parser['DEFAULT'].getboolean(key)
    except ValueError as exc:
        raise RuntimeConfigurationError(
            f'Setting {key!r} in {config_path} must be true or false.'
        ) from exc


def _guild_configuration_source(
        parser: configparser.ConfigParser,
        environment: str,
        config_path: Path) -> str:
    """Select the explicit pre-start guild-policy authority for one process."""

    raw = parser['DEFAULT'].get('guild_configuration_source')
    if environment == 'production' and raw is None:
        return 'static'
    value = '' if raw is None else raw
    if value not in SUPPORTED_GUILD_CONFIGURATION_SOURCES:
        raise RuntimeConfigurationError(
            'Setting \'guild_configuration_source\' in '
            f'{config_path} must be exactly "static" or "database"; '
            f'received {value!r}.'
        )
    return value


def database_authentication_is_supported(
        *,
        environment: str,
        database_password: str,
        database_host: Optional[str]) -> bool:
    """Return whether one profile has an explicit supported DB auth mode."""

    if environment not in SUPPORTED_ENVIRONMENTS:
        return False
    if database_password:
        return True
    return environment == 'production' and database_host is None


def _read_config(config_path: Path) -> configparser.ConfigParser:
    if not config_path.is_file():
        raise RuntimeConfigurationError(
            f'Runtime configuration file does not exist: {config_path}'
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open(encoding='utf-8') as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error) as exc:
        raise RuntimeConfigurationError(
            f'Unable to read runtime configuration {config_path}: {exc}'
        ) from exc
    return parser


def _load_server_settings(project_root: Path, module_name: str) -> ModuleType:
    module_path = project_root / f'{module_name}.py'
    if not module_path.is_file():
        raise RuntimeConfigurationError(
            f'Server-settings file does not exist: {module_path}'
        )
    private_name = f'_polybot_{module_name}_{abs(hash(module_path))}'
    spec = importlib.util.spec_from_file_location(private_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeConfigurationError(
            f'Unable to load server-settings file: {module_path}'
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RuntimeConfigurationError(
            f'Unable to load server-settings file {module_path}: {exc}'
        ) from exc
    return module


def _allowed_guild_ids(
        server_settings: ModuleType,
        module_name: str) -> Tuple[int, ...]:
    server_list = getattr(server_settings, 'server_list', None)
    shortcuts = getattr(server_settings, 'server_shortcut_ids', None)
    if not isinstance(server_list, dict):
        raise RuntimeConfigurationError(
            f'{module_name}.server_list must be a dictionary.'
        )
    if not isinstance(shortcuts, dict):
        raise RuntimeConfigurationError(
            f'{module_name}.server_shortcut_ids must be a dictionary.'
        )

    guild_ids = []
    for guild_id in server_list:
        if guild_id == 'default':
            continue
        if not isinstance(guild_id, int) or guild_id <= 0:
            raise RuntimeConfigurationError(
                f'{module_name}.server_list contains an invalid guild ID: '
                f'{guild_id!r}.'
            )
        guild_ids.append(guild_id)
    if not guild_ids:
        raise RuntimeConfigurationError(
            f'{module_name}.server_list must contain at least one guild.'
        )
    return tuple(sorted(guild_ids))


def _resolve_runtime_path(
        project_root: Path,
        configured_value: str,
        default_value: str) -> Path:
    value = configured_value.strip() or default_value
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _production_comparison_values(
        project_root: Path) -> Tuple[Optional[str], Optional[str],
                                    Optional[int], frozenset]:
    database_name = None
    discord_token = None
    expected_bot_id = None
    guild_ids = set(KNOWN_PRODUCTION_GUILD_IDS)

    production_config_path = project_root / 'config.ini'
    if production_config_path.is_file():
        production_config = _read_config(production_config_path)
        defaults = production_config['DEFAULT']
        database_name = defaults.get('psql_db', '').strip() or None
        discord_token = defaults.get('discord_key', '').strip() or None
        bot_id_value = defaults.get('expected_bot_id', '').strip()
        if bot_id_value:
            expected_bot_id = _positive_int(
                bot_id_value, 'expected_bot_id', production_config_path
            )

    production_settings_path = project_root / 'server_settings.py'
    if production_settings_path.is_file():
        production_settings = _load_server_settings(
            project_root, 'server_settings'
        )
        production_server_list = getattr(
            production_settings, 'server_list', {}
        )
        if isinstance(production_server_list, dict):
            guild_ids.update(
                guild_id for guild_id in production_server_list
                if isinstance(guild_id, int)
            )

    return (
        database_name,
        discord_token,
        expected_bot_id,
        frozenset(guild_ids),
    )


def _validate_development_profile(
        profile: RuntimeProfile,
        parser: configparser.ConfigParser) -> None:
    if (
            profile.discord_token.upper().startswith('YOUR_')
            or profile.database_password.upper().startswith('YOUR_')):
        raise RuntimeConfigurationError(
            'Development token and database password placeholders must be '
            'replaced with separate development credentials.'
        )
    if (
            profile.expected_bot_id == DEVELOPMENT_PLACEHOLDER_ID
            or DEVELOPMENT_PLACEHOLDER_ID in profile.allowed_guild_ids):
        raise RuntimeConfigurationError(
            'Development bot and guild ID placeholders must be replaced.'
        )

    production_image_root = (profile.project_root / 'data/images').resolve()
    production_log_root = (profile.project_root / 'logs').resolve()
    if profile.image_root == production_image_root:
        raise RuntimeConfigurationError(
            'Development image root resolves to the production image path.'
        )
    if profile.log_root == production_log_root:
        raise RuntimeConfigurationError(
            'Development log root resolves to the production log path.'
        )
    for label, path in (
            ('image', profile.image_root), ('log', profile.log_root)):
        try:
            path.relative_to(profile.project_root)
        except ValueError as exc:
            raise RuntimeConfigurationError(
                f'Development {label} root must stay inside the development '
                f'checkout: {path}'
            ) from exc

    if not _DEVELOPMENT_DATABASE_MARKER.search(profile.database_name):
        raise RuntimeConfigurationError(
            'Development database name must include a clear dev, test, '
            'testing, development, or sandbox marker.'
        )

    declared_production_database = parser['DEFAULT'].get(
        'production_database_name', ''
    ).strip() or None
    if declared_production_database and declared_production_database.upper().startswith(
            ('REPLACE_', 'YOUR_')):
        raise RuntimeConfigurationError(
            'production_database_name must be blank or identify this '
            'installation\'s current production database.'
        )
    declared_production_bot_value = parser['DEFAULT'].get(
        'production_bot_id', ''
    ).strip()
    declared_production_bot_id = (
        _positive_int(
            declared_production_bot_value,
            'production_bot_id',
            profile.config_path,
        )
        if declared_production_bot_value
        else None
    )
    production_guild_value = parser['DEFAULT'].get(
        'production_guild_ids', ''
    ).strip()
    try:
        declared_production_guild_ids = {
            int(value.strip())
            for value in production_guild_value.split(',')
            if value.strip()
        }
    except ValueError as exc:
        raise RuntimeConfigurationError(
            'production_guild_ids must be a comma-separated list of integer '
            'Discord guild IDs.'
        ) from exc
    if any(guild_id <= 0 for guild_id in declared_production_guild_ids):
        raise RuntimeConfigurationError(
            'production_guild_ids may contain only positive Discord guild IDs.'
        )

    (production_database, production_token, production_bot_id,
     production_guild_ids) = _production_comparison_values(
         profile.project_root
     )
    production_database_names = {
        value for value in (
            production_database, declared_production_database
        ) if value
    }
    if profile.database_name in production_database_names:
        raise RuntimeConfigurationError(
            'Development database matches the configured production database.'
        )
    if production_token and profile.discord_token == production_token:
        raise RuntimeConfigurationError(
            'Development Discord token matches the configured production token.'
        )
    production_bot_ids = {
        value for value in (
            LEGACY_PRODUCTION_BOT_ID,
            production_bot_id,
            declared_production_bot_id,
        ) if value
    }
    if profile.expected_bot_id in production_bot_ids:
        raise RuntimeConfigurationError(
            'Development expected bot ID matches the production bot ID.'
        )
    all_production_guild_ids = (
        set(production_guild_ids) | declared_production_guild_ids
    )
    shared_guilds = (
        set(profile.allowed_guild_ids) & all_production_guild_ids
    )
    approved_shared_guilds = set(profile.shared_production_guild_ids)
    unapproved_shared_guilds = shared_guilds - approved_shared_guilds
    if unapproved_shared_guilds:
        raise RuntimeConfigurationError(
            'Development server settings contain production guild IDs: '
            + ', '.join(
                str(guild_id)
                for guild_id in sorted(unapproved_shared_guilds)
            )
        )
    if approved_shared_guilds - shared_guilds:
        raise RuntimeConfigurationError(
            'shared_production_guild_ids may contain only active development '
            'guilds that also appear in the production denylist.'
        )
    if approved_shared_guilds and not _boolean_setting(
            parser,
            'acknowledge_shared_production_guild_risk',
            False,
            profile.config_path):
        raise RuntimeConfigurationError(
            'Shared production guilds require '
            'acknowledge_shared_production_guild_risk=true.'
        )

    shortcuts = profile.server_settings.server_shortcut_ids
    unsafe_shortcuts = {
        guild_id for guild_id in shortcuts.values()
        if guild_id not in profile.allowed_guild_ids
    }
    if unsafe_shortcuts:
        raise RuntimeConfigurationError(
            'Development server shortcuts reference guilds outside the '
            'development server list: '
            + ', '.join(str(guild_id) for guild_id in sorted(unsafe_shortcuts))
        )

    if profile.background_tasks_enabled and not _boolean_setting(
            parser, 'allow_development_background_tasks', False,
            profile.config_path):
        raise RuntimeConfigurationError(
            'Development background tasks require the separate '
            'allow_development_background_tasks=true acknowledgement.'
        )
    if profile.api_enabled and not _boolean_setting(
            parser, 'allow_development_api', False, profile.config_path):
        raise RuntimeConfigurationError(
            'Development API enablement requires the separate '
            'allow_development_api=true acknowledgement.'
        )


def _create_development_directories(profile: RuntimeProfile) -> None:
    for path in (profile.image_root, profile.log_root):
        path.mkdir(mode=0o750, parents=True, exist_ok=True)
        os.chmod(path, 0o750)


def load_runtime_profile(
        *,
        project_root: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
        create_directories: bool = True) -> RuntimeProfile:
    """Load and validate one complete runtime profile.

    Optional arguments exist so offline tests and the inspection command can
    validate fixtures without importing application modules.
    """

    root = Path(project_root or PROJECT_ROOT).resolve()
    environment_values = os.environ if environ is None else environ
    raw_environment = environment_values.get('POLYBOT_ENV')
    environment = raw_environment if raw_environment is not None else ''
    if environment not in SUPPORTED_ENVIRONMENTS:
        raise RuntimeConfigurationError(
            'POLYBOT_ENV must be exactly "production" or "development"; '
            f'received {environment!r}.'
        )

    layout = _PROFILE_LAYOUT[environment]
    config_path = root / layout['config_file']
    parser = _read_config(config_path)
    expected_bot_id_value = parser['DEFAULT'].get(
        'expected_bot_id', ''
    ).strip()
    if not expected_bot_id_value:
        raise RuntimeConfigurationError(
            f'Missing required setting {"expected_bot_id"!r} in '
            f'{config_path}.'
        )

    database_password = parser['DEFAULT'].get('psql_password', '').strip()
    database_host = parser['DEFAULT'].get('psql_host', '').strip() or None
    if not database_authentication_is_supported(
            environment=environment,
            database_password=database_password,
            database_host=database_host):
        raise RuntimeConfigurationError(
            f'Missing required setting {"psql_password"!r} in {config_path}; '
            'passwordless authentication is permitted only for production '
            'over the default local PostgreSQL socket.'
        )

    if environment == 'development' and database_host is None:
        raise RuntimeConfigurationError(
            f'Missing required setting {"psql_host"!r} in {config_path}.'
        )

    module_name = layout['server_settings_module']
    server_settings = _load_server_settings(root, module_name)
    allowed_guild_ids = _allowed_guild_ids(server_settings, module_name)
    defaults = parser['DEFAULT']
    shared_guild_values = defaults.get(
        'shared_production_guild_ids', ''
    ).strip()
    try:
        shared_production_guild_ids = tuple(sorted({
            int(value.strip())
            for value in shared_guild_values.split(',')
            if value.strip()
        }))
    except ValueError as exc:
        raise RuntimeConfigurationError(
            'shared_production_guild_ids must be a comma-separated list of '
            'integer Discord guild IDs.'
        ) from exc
    if any(guild_id <= 0 for guild_id in shared_production_guild_ids):
        raise RuntimeConfigurationError(
            'shared_production_guild_ids may contain only positive Discord '
            'guild IDs.'
        )

    owner_id = _positive_int(
        _required_value(parser, 'owner_id', config_path),
        'owner_id',
        config_path,
    )
    configured_superuser_ids = _positive_id_list(
        defaults.get('superuser_ids', ''),
        'superuser_ids',
        config_path,
    )

    profile = RuntimeProfile(
        environment=environment,
        project_root=root,
        config_path=config_path.resolve(),
        discord_token=_required_value(parser, 'discord_key', config_path),
        expected_bot_id=_positive_int(
            expected_bot_id_value, 'expected_bot_id', config_path
        ),
        owner_id=owner_id,
        superuser_ids=tuple(sorted({owner_id, *configured_superuser_ids})),
        database_name=_required_value(parser, 'psql_db', config_path),
        database_user=_required_value(parser, 'psql_user', config_path),
        database_password=database_password,
        database_host=database_host,
        database_port=_optional_port(parser, config_path),
        pastebin_key=defaults.get('pastebin_key', '').strip() or None,
        server_settings_module=module_name,
        server_settings=server_settings,
        guild_configuration_source=_guild_configuration_source(
            parser,
            environment,
            config_path,
        ),
        allowed_guild_ids=allowed_guild_ids,
        shared_production_guild_ids=shared_production_guild_ids,
        background_tasks_enabled=_boolean_setting(
            parser,
            'background_tasks_enabled',
            layout['background_tasks_enabled'],
            config_path,
        ),
        api_enabled=_boolean_setting(
            parser, 'api_enabled', layout['api_enabled'], config_path
        ),
        bullet_enabled=_boolean_setting(
            parser, 'bullet_enabled', layout['bullet_enabled'], config_path
        ),
        image_root=_resolve_runtime_path(
            root, defaults.get('image_root', ''), layout['image_root']
        ),
        log_root=_resolve_runtime_path(
            root, defaults.get('log_root', ''), layout['log_root']
        ),
    )

    if environment == 'development':
        _validate_development_profile(profile, parser)
        if create_directories:
            _create_development_directories(profile)
    return profile


def get_runtime_profile() -> RuntimeProfile:
    """Return the process-wide immutable runtime profile."""

    global _runtime_profile
    if _runtime_profile is None:
        _runtime_profile = load_runtime_profile()
    return _runtime_profile


def format_runtime_profile(profile: RuntimeProfile) -> str:
    """Return a diagnostic summary that cannot expose stored secrets."""

    host = profile.database_host or '(default PostgreSQL socket)'
    port = str(profile.database_port) if profile.database_port else '(default)'
    database_authentication = (
        'local peer (no password configured)'
        if (
            profile.environment == 'production'
            and profile.database_host is None
            and not profile.database_password
        )
        else 'password configured (redacted)'
    )
    guilds = ', '.join(str(guild_id) for guild_id in profile.allowed_guild_ids)
    shared_guilds = (
        ', '.join(
            str(guild_id)
            for guild_id in profile.shared_production_guild_ids
        )
        or '(none)'
    )
    return '\n'.join((
        f'environment: {profile.environment}',
        f'expected bot ID: {profile.expected_bot_id}',
        f'authorized superuser identities: {len(profile.superuser_ids)}',
        f'database: {profile.database_name}',
        f'database host: {host}',
        f'database port: {port}',
        f'database authentication: {database_authentication}',
        f'server-settings module: {profile.server_settings_module}',
        f'guild configuration source: {profile.guild_configuration_source}',
        f'allowed guild IDs: {guilds}',
        f'acknowledged shared production guild IDs: {shared_guilds}',
        f'background tasks enabled: {profile.background_tasks_enabled}',
        f'HTTP API enabled: {profile.api_enabled}',
        f'Bullet spreadsheet enabled: {profile.bullet_enabled}',
        f'image root: {profile.image_root}',
        f'log root: {profile.log_root}',
    ))
