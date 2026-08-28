import argparse
from contextlib import nullcontext
import asyncio
import importlib
import logging
import os
import secrets
import sys
from timeit import default_timer as timer
from typing import Awaitable, Callable, List

import discord
from discord.ext import commands

import logging_config
import modules.exceptions as exceptions
import settings
from modules import image_storage, operator_restart

logger = logging.getLogger('polybot.' + __name__)
# https://discord.com/channels/336642139381301249/1042604006226280468/1042645381143613532


BOOTSTRAP_PENDING_INTERACTION_PATHS = frozenset({
    ('guild', 'edit'),
    ('operator', 'guild', 'list'),
    ('operator', 'guild', 'settings'),
    ('operator', 'guild', 'validate'),
    ('operator', 'guild', 'history'),
    ('operator', 'guild', 'edit'),
    ('operator', 'guild', 'sync'),
    ('operator', 'bot', 'restart'),
})


def prefix_error_reference() -> str:
    """Return a short, non-secret reference for one unexpected failure."""

    return secrets.token_hex(4).upper()


async def handle_prefix_command_error(ctx, exc) -> None:
    """Handle retained-prefix failures without exposing exception details."""

    if hasattr(ctx.command, 'on_error'):
        return
    ignored = (
        commands.CommandNotFound,
        commands.UserInputError,
        commands.CheckFailure,
    )

    if (
        isinstance(exc, commands.CommandNotFound)
        and str(ctx.invoked_with or '')[:4] == 'join'
    ):
        await ctx.send(
            'Cannot understand command. Make sure to include a space and a '
            f'numeric game ID.\n*Example:* `{ctx.prefix}join 11234`'
        )

    if isinstance(exc, ignored):
        logger.warning(
            'Exception on ignored list raised in %s: %s',
            ctx.command,
            exc,
        )
        return
    if isinstance(exc, commands.CommandOnCooldown):
        logger.info('Cooldown triggered: %s', exc)
        await ctx.send(
            'This command is on a cooldown period. Try again in '
            f'{exc.retry_after:.0f} seconds.'
        )
        return
    if isinstance(exc, exceptions.RecordLocked):
        await ctx.send(f':warning: {exc}')
        return

    error = getattr(exc, 'original', exc)
    reference = prefix_error_reference()
    logger.critical(
        'Unexpected prefix command failure reference=%s command=%s',
        reference,
        ctx.command,
        exc_info=(type(error), error, error.__traceback__),
    )
    await ctx.send(
        'Something went wrong while running that command. '
        f'Error reference: `{reference}`. Please give this reference to a '
        'bot administrator.'
    )


def configure_runtime_arguments(args: List[str] = None):
    """Parse command arguments and apply process runtime policy overrides."""

    parser = argparse.ArgumentParser()
    parser.add_argument('--add_default_data', action='store_true')
    parser.add_argument('--recalc_elo', action='store_true')
    parser.add_argument('--game_export', action='store_true')
    parser.add_argument('--skip_tasks', action='store_true')
    # Ignore extra args from uvicorn.
    args, _unknown = parser.parse_known_args(args)
    settings.run_tasks = (
        settings.runtime_profile.background_tasks_enabled
        and not args.skip_tasks
    )
    return args


def main(args: List[str] = None):
    args = configure_runtime_arguments(args)
    mutation_lock = nullcontext()
    if (
        getattr(settings.runtime_profile, 'environment', None) == 'development'
        and (args.add_default_data or args.recalc_elo)
    ):
        from modules.beta_database_writer_lock import BetaDatabaseWriterLock

        mutation_lock = BetaDatabaseWriterLock(settings.runtime_profile)
    if args.add_default_data:
        with mutation_lock:
            initialize_data = importlib.import_module('modules.initialize_data')

            initialize_data.initialize_data()
        exit(0)
    if args.recalc_elo:
        with mutation_lock:
            models = importlib.import_module('modules.models')

            print('Recalculating all ELO')
            start = timer()
            # This is a standalone synchronous operator path, not a Discord
            # worker. Own its Peewee connection explicitly so success, failure,
            # and process exit all have a deterministic connection lifecycle.
            with models.db.connection_context():
                models.Game.recalculate_all_elo()
            end = timer()
            print(f'Recalculation complete - took {end - start} seconds.')
        exit(0)
    if args.game_export:
        utilities = importlib.import_module('modules.utilities')

        print('Exporting game data to file')
        start = timer()
        utilities.export_game_data()
        print(f'Recalculation complete - took {timer() - start} seconds.')
        exit(0)


def _interaction_command_path(interaction: discord.Interaction) -> tuple[str, ...]:
    """Return only the root/group/subcommand path from Discord interaction data."""

    data = getattr(interaction, 'data', None)
    if not isinstance(data, dict):
        return ()
    path = []
    current = data
    while isinstance(current, dict):
        name = current.get('name')
        if not isinstance(name, str):
            break
        path.append(name)
        nested = current.get('options')
        current = next(
            (
                option
                for option in nested
                if isinstance(option, dict) and option.get('type') in (1, 2)
            ),
            None,
        ) if isinstance(nested, list) else None
    return tuple(path)


def _is_owner_restart_recovery(interaction: discord.Interaction) -> bool:
    """Keep one exact owner recovery action available during quarantine."""

    try:
        requester_id = int(interaction.user.id)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        requester_id == int(settings.owner_id)
        and _interaction_command_path(interaction) == ('operator', 'bot', 'restart')
    )


def _is_bootstrap_pending_owner_path(interaction: discord.Interaction) -> bool:
    """Allow only configured-owner first-configuration/recovery commands."""

    try:
        guild_id = int(interaction.guild_id)
        requester_id = int(interaction.user.id)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        requester_id == int(settings.owner_id)
        and settings.database_guild_configuration_bootstrap_pending(guild_id)
        and _interaction_command_path(interaction)
        in BOOTSTRAP_PENDING_INTERACTION_PATHS
    )


class PolyBotCommandTree(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        async def deny(message: str) -> bool:
            if getattr(interaction, 'type', None) is discord.InteractionType.autocomplete:
                if not interaction.response.is_done():
                    await interaction.response.autocomplete([])
                return False
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
            return False

        if not getattr(settings, 'guild_configuration_ready', lambda: True)():
            return await deny(
                'The bot is still validating its server configuration. '
                'Try the command again in a moment.'
            )
        guild_id = getattr(interaction, 'guild_id', None)
        quarantined = (
            guild_id is not None
            and settings.database_guild_configuration_quarantined(guild_id)
        )
        owner_restart_recovery = (
            _is_owner_restart_recovery(interaction)
            and guild_id is not None
            and (int(guild_id) in settings.config or quarantined)
        )
        if quarantined and not owner_restart_recovery:
            return await deny(
                'This server is temporarily quarantined because a committed '
                'configuration change could not be published safely. No command '
                'was run; the owner can use `/operator bot restart` to reconcile.'
            )
        if (
                guild_id is not None
                and settings.database_guild_configuration_bootstrap_pending(guild_id)
        ):
            if not _is_bootstrap_pending_owner_path(interaction):
                return await deny(
                    'This server is awaiting first configuration. Only the '
                    'configured owner may use the approved configuration or '
                    'recovery paths.'
                )
            return True
        if (
                guild_id is not None
                and int(guild_id) not in settings.config
                and not owner_restart_recovery
        ):
            return await deny(
                'This server is not active in the bot configuration. It may '
                'be quarantined or suspended; no command was run.'
            )
        if guild_id is not None and not owner_restart_recovery:
            data = getattr(interaction, 'data', None) or {}
            root = data.get('name') if isinstance(data, dict) else None
            allowed_roots = settings.application_command_policy.roots_for_guild(
                int(guild_id)
            )
            if not isinstance(root, str) or root not in allowed_roots:
                return await deny(
                    'This application command is not enabled for this server. '
                    'No command was run.'
                )
        if owner_restart_recovery:
            return True
        if not settings.maintenance_mode:
            return True
        return await deny('The bot is restarting. Try the command again in a moment.')


class MyBot(commands.Bot):
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    intents.typing = False
    intents.presences = False
    def __init__(self):
        super().__init__(command_prefix=get_prefix,
                         owner_id=settings.owner_id,
                         allowed_mentions=discord.AllowedMentions(everyone=False),
                         intents=self.intents,
                         tree_cls=PolyBotCommandTree,
                         activity=discord.Activity(name='$guide', type=discord.ActivityType.playing))
        settings.bot = self
        # Auto-deleting task messages cleaned up before a planned restart.
        # Each item is a (guild_id, channel_id, message_id) tuple.
        self.purgable_messages = []
        self.locked_game_records = set()  # Games which cannot be written to since another command is working on them right now. Ugly hack to do what should be done at the DB level
        self._startup_identity_validated = False
        self._startup_schema_preflight_complete = False
        self._startup_schema_preflight_lock = asyncio.Lock()
        self._startup_bans_reconciled = False
        self._startup_ban_lock = asyncio.Lock()
        self._guild_configuration_shadow_complete = False
        self._guild_configuration_shadow_lock = asyncio.Lock()
        self.guild_configuration_shadow_result = None
        self._restart_exit_status = None
        self._database_watchdog = None
        self._close_lock = asyncio.Lock()
        self._close_complete = False
        # Startup never deploys commands. Source-shape deployments remain in
        # the explicit external tool; P10.6c may separately apply one
        # owner-confirmed database capability plan to one exact guild. Keep
        # dispatch failures observable and acknowledge delivered interactions.
        self.tree.on_error = self._on_application_command_error

    @staticmethod
    def _dispatched_guild_id(args) -> int | None:
        if not args:
            return None
        subject = args[0]
        guild = getattr(subject, 'guild', None)
        value = (
            getattr(guild, 'id', None)
            if guild is not None
            else getattr(subject, 'guild_id', None)
        )
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    def dispatch(self, event_name: str, /, *args, **kwargs) -> None:
        """Keep every unknown-guild event inert, not only command dispatch."""

        lifecycle_events = {
            'connect', 'disconnect', 'guild_join', 'interaction', 'ready',
            'resumed',
        }
        guild_id = self._dispatched_guild_id(args)
        if guild_id is not None and event_name not in lifecycle_events:
            if (
                not settings.guild_configuration_allows_dispatch(guild_id)
            ):
                logger.debug(
                    'Dropping unpublished or quarantined guild event %s for guild %s.',
                    event_name,
                    guild_id,
                )
                return
        super().dispatch(event_name, *args, **kwargs)

    async def on_interaction(self, interaction: discord.Interaction):
        """Log the safe routing envelope for application-command delivery."""

        if interaction.type in (
            discord.InteractionType.application_command,
            discord.InteractionType.autocomplete,
        ):
            data = interaction.data or {}
            logger.info(
                'Application interaction received: interaction=%s '
                'application=%s type=%s guild=%s channel=%s user=%s '
                'command=%s command_id=%s',
                interaction.id,
                interaction.application_id,
                interaction.type.name,
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                data.get('name'),
                data.get('id'),
            )

    async def _on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        """Log and acknowledge command-tree failures at the dispatch edge."""

        logger.error(
            'Application command dispatch failed: interaction=%s guild=%s '
            'channel=%s user=%s command=%s error=%r',
            interaction.id,
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
            (interaction.data or {}).get('name'),
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if isinstance(error, discord.app_commands.CommandOnCooldown):
            message = (
                'That command is on cooldown. Try again in '
                f'{error.retry_after:.0f} seconds.'
            )
        else:
            message = (
                'Discord delivered the command, but the beta could not route '
                'it. The failure has been logged for review.'
            )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception(
                'Could not acknowledge failed application interaction %s',
                interaction.id,
            )

    def _validate_startup_identity(self) -> None:
        if self.user is None:
            raise RuntimeError(
                'Discord authentication must complete before startup effects.'
            )
        settings.runtime_profile.validate_logged_in_bot(int(self.user.id))
        self._startup_identity_validated = True

    async def _preflight_startup_schema(self):
        if not self._startup_identity_validated:
            raise RuntimeError(
                'Startup schema preflight requires validated bot identity.'
            )
        if self._startup_schema_preflight_complete:
            return None
        async with self._startup_schema_preflight_lock:
            if self._startup_schema_preflight_complete:
                return None
            schema_preflight = importlib.import_module(
                'modules.startup_schema_preflight'
            )
            profile = settings.runtime_profile
            request = schema_preflight.StartupSchemaPreflightRequest(
                environment=profile.environment,
                database_name=profile.database_name,
                database_user=profile.database_user,
                database_password=profile.database_password,
                database_host=profile.database_host,
                database_port=profile.database_port,
            )
            result = await schema_preflight.run_startup_schema_preflight(
                request
            )
            self._startup_schema_preflight_complete = True
            logger.info(
                'Startup schema preflight verified database=%s role=%s '
                'tables=%s winner_foreign_key=%s',
                result.database_name,
                result.database_user,
                len(result.verified_tables),
                result.winner_foreign_key_verified,
            )
            return result

    async def _reconcile_startup_bans(self):
        if not self._startup_identity_validated:
            raise RuntimeError(
                'Startup ban reconciliation requires validated bot identity.'
            )
        if self._startup_bans_reconciled:
            return None
        async with self._startup_ban_lock:
            if self._startup_bans_reconciled:
                return None
            startup_ban_workers = importlib.import_module(
                'modules.startup_ban_workers'
            )

            request = startup_ban_workers.StartupBanReconciliationRequest(
                discord_ids=tuple(dict.fromkeys(
                    int(value) for value in getattr(
                        settings, 'discord_id_ban_list', ()
                    )
                )),
                polytopia_ids=tuple(dict.fromkeys(
                    str(value) for value in getattr(
                        settings, 'poly_id_ban_list', ()
                    )
                )),
            )
            result = await startup_ban_workers.run_startup_ban_reconciliation(
                request
            )
            self._startup_bans_reconciled = True
            logger.info(
                'Startup ban snapshot reconciled reset_rows=%s '
                'discord_rows=%s polytopia_rows=%s',
                result.reset_rows,
                result.discord_rows,
                result.polytopia_rows,
            )
            return result

    async def _run_guild_configuration_authority(self):
        """Compare once and publish only an explicitly selected DB authority."""

        profile = settings.runtime_profile
        database_selected = (
            getattr(profile, 'guild_configuration_source', 'static')
            == 'database'
        )
        if profile.environment == 'production' and not database_selected:
            return None
        if not self._startup_identity_validated or not self._startup_schema_preflight_complete:
            raise RuntimeError(
                'Guild configuration shadow reads require validated identity '
                'and startup schema preflight.'
            )
        if self._guild_configuration_shadow_complete:
            return self.guild_configuration_shadow_result
        async with self._guild_configuration_shadow_lock:
            if self._guild_configuration_shadow_complete:
                return self.guild_configuration_shadow_result
            shadow = importlib.import_module(
                'modules.guild_configuration_shadow'
            )
            try:
                if database_selected:
                    stored = await shadow.run_active_configuration(
                        shadow.active_request_from_profile(profile)
                    )
                    guild_ids = tuple(value.guild_id for value in stored)
                    discord_snapshot = shadow.capture_discord_snapshot(
                        profile=profile,
                        guilds=tuple(self.guilds),
                        guild_ids=guild_ids,
                    )
                    result = shadow.GuildConfigurationShadowResult(
                        status=shadow.STATUS_MATCHED,
                        expected_guild_ids=guild_ids,
                        stored_guild_ids=guild_ids,
                        matched_guild_ids=guild_ids,
                        stored_configurations=stored,
                    )
                else:
                    discord_snapshot = shadow.capture_discord_snapshot(
                        profile=profile,
                        guilds=tuple(self.guilds),
                    )
                    bundle = shadow.expected_bundle_from_snapshot(
                        profile=profile,
                        discord_snapshot=discord_snapshot,
                    )
                    request = shadow.request_from_profile(
                        profile=profile,
                        expected_bundle=bundle,
                    )
                    result = await shadow.run_shadow_comparison(request)
            except shadow.GuildConfigurationShadowUnavailable as exc:
                result = shadow.failure_result(
                    shadow.STATUS_UNAVAILABLE,
                    str(exc),
                )
            except shadow.GuildConfigurationShadowMalformed as exc:
                result = shadow.failure_result(
                    shadow.STATUS_MALFORMED,
                    str(exc),
                )
            except Exception:
                logger.exception(
                    'Unexpected guild configuration shadow failure; static '
                    'settings remain authoritative.'
                )
                result = shadow.failure_result(
                    shadow.STATUS_MALFORMED,
                    'shadow_runtime_failure',
                )
            self.guild_configuration_shadow_result = result
            if database_selected and result.promotion_ready:
                runtime = importlib.import_module(
                    'modules.guild_configuration_runtime'
                )
                try:
                    runtime_snapshot = runtime.build_runtime_snapshot_from_stored(
                        stored_configurations=result.stored_configurations,
                        discord_snapshot=discord_snapshot,
                        allowed_guild_ids=result.matched_guild_ids,
                        target=shadow.target_from_profile(profile),
                    )
                    settings.activate_database_guild_configuration(
                        runtime_snapshot
                    )
                except Exception as exc:
                    logger.critical(
                        'Database guild configuration publication failed; '
                        'automatic static fallback is disabled.',
                        exc_info=True,
                    )
                    raise RuntimeError(
                        'Database guild configuration publication failed.'
                    ) from exc
                logger.info(
                    'Guild configuration source=database status=active-loaded '
                    'guilds=%s generations=%s; immutable runtime snapshot '
                    'published.',
                    result.matched_guild_ids,
                    tuple(
                        (value.guild_id, value.generation)
                        for value in result.stored_configurations
                    ),
                )
                self._guild_configuration_shadow_complete = True
            elif database_selected:
                logger.critical(
                    'Guild configuration source=database status=%s '
                    'promotion_ready=false reason=%s; automatic static '
                    'fallback is disabled.',
                    result.status,
                    result.safe_reason,
                )
                raise RuntimeError(
                    'Selected database guild configuration is unavailable.'
                )
            elif result.promotion_ready:
                self._guild_configuration_shadow_complete = True
                logger.info(
                    'Guild configuration shadow status=%s promotion_ready=true '
                    'guilds=%s; static settings remain authoritative.',
                    result.status,
                    result.matched_guild_ids,
                )
            else:
                self._guild_configuration_shadow_complete = True
                logger.error(
                    'Guild configuration shadow status=%s promotion_ready=false '
                    'expected_guilds=%s stored_guilds=%s mismatches=%s reason=%s; '
                    'static settings remain authoritative.',
                    result.status,
                    result.expected_guild_ids,
                    result.stored_guild_ids,
                    tuple((value.guild_id, value.paths) for value in result.mismatches),
                    result.safe_reason,
                )
            return result

    async def setup_hook(self):
        try:
            self._validate_startup_identity()
        except Exception:
            logger.critical(
                'Authenticated Discord bot does not match the runtime profile; '
                'startup effects were not enabled.',
                exc_info=True,
            )
            raise

        await self._preflight_startup_schema()
        await self._reconcile_startup_bans()
        utilities = importlib.import_module('modules.utilities')

        utilities.connect()
        image_storage.ensure_image_directories()
        initial_extensions = [
            'modules.games', 'modules.customhelp', 'modules.matchmaking',
            'modules.administration', 'modules.misc', 'modules.league',
            'modules.api_cog', 'modules.antiscam'
        ]
        if settings.runtime_profile.bullet_enabled:
            initial_extensions.append('modules.bullet')
        else:
            logger.info(
                'Skipping the Bullet extension because it is disabled in '
                'the runtime profile.'
            )
        for extension in initial_extensions:
            await self.load_extension(extension)
        database_health = importlib.import_module('modules.database_health')
        self._database_watchdog = database_health.DatabaseWatchdog(self)
        self._database_watchdog.start()
    async def close(self):
        async with self._close_lock:
            if self._close_complete:
                return
            if self._database_watchdog is not None:
                await self._database_watchdog.stop()
                self._database_watchdog = None
            await super().close()
            self._close_complete = True

    @property
    def restart_exit_status(self) -> int | None:
        return self._restart_exit_status

    async def _cleanup_restart_messages(self) -> None:
        """Remove task-owned temporary notices before a planned restart."""

        if not settings.run_tasks or not self.purgable_messages:
            return
        logger.debug('Purging restart message list %s', self.purgable_messages)
        for guild_id, channel_id, message_id in reversed(self.purgable_messages):
            guild = self.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild is not None else None
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.DiscordException:
                logger.warning(
                    'Could not remove temporary restart message guild=%s '
                    'channel=%s message=%s',
                    guild_id,
                    channel_id,
                    message_id,
                )
        await asyncio.sleep(3)

    async def request_supervised_restart(
        self,
        requester_id: int,
        force: bool,
        *,
        before_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Close cleanly, then let ``init_bot`` return a restart exit status."""

        operator_restart.assert_supervised()
        if self._restart_exit_status is not None:
            raise operator_restart.RestartConflictError(
                'Another accepted restart is already shutting down the bot.'
            )
        self._restart_exit_status = operator_restart.RESTART_EXIT_STATUS
        settings.maintenance_mode = True
        logger.warning(
            'Supervised restart accepted requester=%s force=%s exit_status=%s',
            int(requester_id),
            bool(force),
            self._restart_exit_status,
        )
        try:
            if before_close is not None:
                await before_close()
            await self._cleanup_restart_messages()
            await self.close()
        except BaseException:
            self._restart_exit_status = None
            settings.maintenance_mode = False
            raise


def get_prefix(bot, message):
    # Guild-specific command prefixes
    if not getattr(settings, 'guild_configuration_ready', lambda: True)():
        return 'fakeprefix'
    if (
            message.guild
            and settings.guild_configuration_allows_dispatch(message.guild.id)
    ):
        # Current guild is allowed
        set_prefix = settings.guild_setting(message.guild.id, "command_prefix")
        if not set_prefix:
            logger.error(f'No prefix found in settings! Guild: {message.guild.id} {message.guild.name}')
            return 'fakeprefix'

        # temp debug log to try to fix NoneType errors related to prefixes
        # logger.debug(f'Found prefix setting {settings.guild_setting(message.guild.id, "command_prefix")} for guild {message.guild.id}')
        return commands.when_mentioned_or(settings.guild_setting(message.guild.id, 'command_prefix'))(bot, message)
    else:
        if message.guild:
            logger.error(f'Message received not from allowed guild. ID {message.guild.id }')
        # probably a PM
        logger.warning(f'returning None prefix for received PM. Author: {message.author.name}')
        return 'fakeprefix'


def init_bot(loop: asyncio.AbstractEventLoop = None, args: List[str] = None):
    main(args)
    bot = MyBot()

    cooldown = commands.CooldownMapping.from_cooldown(6, 30.0, commands.BucketType.user)

    @bot.check
    async def globally_block_dms(ctx):
        # Should prevent bot from being able to be controlled via DM
        return ctx.guild is not None

    @bot.check
    async def restrict_banned_users(ctx):
        if ctx.author.id in settings.discord_id_ban_list or discord.utils.get(ctx.author.roles, name='ELO Banned'):
            await ctx.send('You are banned from using this bot. :kissing_heart:')
            return False
        return True

    @bot.check
    async def cooldown_check(ctx):
        if ctx.invoked_with == 'help' and ctx.command.name != 'help':
            # otherwise check will run once for every command in the bot when someone invokes $help
            return True
        if ctx.author.id == settings.owner_id:
            return True
        bucket = cooldown.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            await ctx.send('You\'re on cooldown. Slow down those commands!')
            logger.warning(f'Cooldown limit reached for user {ctx.author.id}')
            return False

        # not on cooldown
        return True

    @bot.event
    async def on_command_error(ctx, exc):
        await handle_prefix_command_error(ctx, exc)

    check_once = getattr(bot, 'check_once', bot.check)

    @check_once
    async def database_health_check(_ctx):
        database_health = importlib.import_module('modules.database_health')
        try:
            database_health.ensure_connection()
        except database_health.CONNECTION_ERRORS as exc:
            # Bot.can_run only routes CommandError subclasses to the prefix
            # error event.  Preserve the original Peewee failure for the
            # existing bounded/non-secret error handler.
            raise commands.CommandInvokeError(exc) from exc
        return True

    @bot.before_invoke
    async def pre_invoke_setup(ctx):
        if (
                ctx.guild is not None
                and not settings.guild_configuration_allows_dispatch(ctx.guild.id)
        ):
            raise commands.CheckFailure(
                'Guild configuration is not published for command dispatch.'
            )
        logger.debug(
            f'Command invoked: {ctx.invoked_with}. '
            f'By {ctx.author.id} {ctx.author.name} in '
            f'{ctx.channel.id} {ctx.channel.name} on {ctx.guild.name}'
        )

    @bot.event
    async def on_message(message):
        if not getattr(settings, 'guild_configuration_ready', lambda: True)():
            logger.debug(
                'Ignoring messages while guild configuration is not ready.'
            )
        elif (
                message.guild is not None
                and not settings.guild_configuration_allows_dispatch(message.guild.id)
        ):
            logger.debug(
                'Ignoring message while guild %s configuration is quarantined.',
                message.guild.id,
            )
        elif settings.maintenance_mode:
            if message.content and message.content.startswith(tuple(get_prefix(bot, message))):
                logger.debug('Ignoring messages while settings.maintenance_mode is set to True')
        else:
            # it is possible to modify the content of a message here before processing, ie replace curly quotes in message.content with straight quotes
            await bot.process_commands(message)

    @bot.event
    async def on_guild_join(guild):
        if guild.id in settings.config:
            logger.info('Connected to enrolled guild %s %s.', guild.id, guild.name)
            return
        logger.warning(
            'New guild %s %s is quarantined. No configuration row was '
            'created and command dispatch remains disabled until owner '
            'enrollment.',
            guild.id,
            guild.name,
        )

    @bot.event
    async def on_ready():
        """http://discordpy.readthedocs.io/en/rewrite/api.html#discord.on_ready"""

        try:
            bot._validate_startup_identity()
            if not bot._startup_bans_reconciled:
                raise RuntimeError(
                    'Startup ban reconciliation did not complete before ready.'
                )
        except Exception:
            logger.critical(
                'Authenticated Discord bot does not match the runtime profile.',
                exc_info=True,
            )
            await bot.close()
            raise

        try:
            await bot._run_guild_configuration_authority()
        except Exception:
            logger.critical(
                'Guild configuration startup failed; closing before command '
                'service.',
                exc_info=True,
            )
            await bot.close()
            raise

        pending_guild_ids = tuple(
            getattr(
                bot.guild_configuration_shadow_result,
                'bootstrap_pending_guild_ids',
                (),
            )
        )
        if pending_guild_ids:
            logger.warning(
                'Bootstrap-pending guilds=%s remain inert for ordinary dispatch; '
                'only the configured owner allowlist is available.',
                pending_guild_ids,
            )

        print(f'\n\nv2 Logged in as: {bot.user.name} - {bot.user.id}\nVersion: {discord.__version__}\n')
        print('Successfully logged in and booted...!')

        for g in bot.guilds:
            if g.id in settings.config:
                logger.debug(f'Loaded in guild {g.id} {g.name}')
            else:
                logger.warning(
                    'Quarantined guild %s %s is not enrolled; retaining an '
                    'inert connection for owner review.',
                    g.id,
                    g.name,
                )

        logger.info(
            'Automatic application-command synchronization is disabled. '
            'Use scripts/manage_application_commands.py with an explicit '
            'guild-scoped plan/apply workflow.'
        )

    if loop:
        loop.create_task(bot.start(settings.runtime_profile.discord_token))
    else:
        bot.run(settings.runtime_profile.discord_token)
        if bot.restart_exit_status is not None:
            raise SystemExit(bot.restart_exit_status)
    return bot


if __name__ == '__main__':
    init_bot()
