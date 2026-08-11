from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import modules.image_storage as image_storage
import modules.channels as channels
import settings
import logging
import peewee
import modules.exceptions as exceptions
import datetime
import asyncio
import discord
import re
import functools
from pathlib import Path
from modules.games import PolyGame
from modules import auto_confirmation_workers
from modules import confirmation_publication, confirmation_publication_workers
from modules import elo_workers, game_correction_publication, game_workers
from modules import nova_graduation_workers
from modules.elo_jobs import EloJobConflict
from modules import team_emoji as team_emoji_service
from modules import team_emoji_workers
from modules import team_creation as team_creation_service
from modules import team_creation_workers
from modules import team_archive as team_archive_service
from modules import team_archive_workers
from modules import team_attributes as team_attributes_service
from modules import team_attributes_workers
from modules import team_image as team_image_service
from modules import team_image_workers
from modules import team_show as team_show_service
from modules import team_show_workers
from modules import incomplete_game_purge
from modules import operator_tribe as operator_tribe_service
from modules import operator_tribe_workers
from modules import operator_player_migration as operator_player_migration_service
from modules import operator_player_migration_views
from modules import operator_player_migration_workers
from modules import operator_player_deletion as operator_player_deletion_service
from modules import operator_player_deletion_views
from modules import operator_player_deletion_workers
from modules import operator_backup
from modules import operator_backup_views
from modules import operator_channel_purge as operator_channel_purge_service
from modules import operator_channel_purge_views
from modules import operator_channel_purge_workers
from modules import operator_restart as operator_restart_service
from modules import operator_restart_views
from modules import operator_beta_fixtures as operator_beta_fixtures_service
from modules import operator_beta_fixtures_views
from modules import operator_beta_fixtures_workers
from modules import game_open_workers
from modules import interaction_lifecycle

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfirmedWinPublicationError(RuntimeError):
    """The confirmation committed, but its Discord effects did not finish."""

    def __init__(self, result: elo_workers.ConfirmedWinResult):
        self.result = result
        super().__init__(
            f'Committed game {result.game_id} confirmation requires '
            'Discord reconciliation.'
        )


def format_confirmed_win_reconciliation(
    result: elo_workers.ConfirmedWinResult,
) -> str:
    return (
        f'Game {result.game_id} was confirmed as **{result.winner_name}** '
        'and its ELO changes committed, but one or more Discord updates '
        'failed. Do not confirm it again; staff should reconcile its '
        'channels, roles, and announcement.'
    )


def load_unconfirmed_game_summaries(guild_id: int):
    """Load display-only unconfirmed game data on a local connection."""

    with models.db.connection_context():
        game_query = models.Game.search(
            status_filter=5,
            guild_id=guild_id,
        ).order_by(models.Game.win_claimed_ts)
        return utilities.summarize_game_list(game_query)


def format_elo_job_status(active_job, now=None):
    """Render coordinator state without performing database work."""

    if active_job is None:
        return 'No ELO mutation job is currently running.'

    now = now or discord.utils.utcnow()
    elapsed_seconds = max(
        0,
        int((now - active_job.started_at).total_seconds()),
    )
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed_parts = []
    if hours:
        elapsed_parts.append(f'{hours}h')
    if minutes or hours:
        elapsed_parts.append(f'{minutes}m')
    elapsed_parts.append(f'{seconds}s')

    requester = active_job.requester_name
    if active_job.requester_id is not None:
        requester += f' (`{active_job.requester_id}`)'
    game = (
        active_job.game_id
        if active_job.game_id is not None
        else 'all games'
    )
    started_timestamp = int(active_job.started_at.timestamp())
    return (
        f'Active ELO job: `{active_job.operation}`\n'
        f'Game: `{game}`\n'
        f'Requester: {requester}\n'
        f'Started: <t:{started_timestamp}:F> (<t:{started_timestamp}:R>)\n'
        f'Elapsed: {" ".join(elapsed_parts)}'
    )


def current_restart_activity() -> operator_restart_service.RestartActivitySnapshot:
    """Capture primitive in-process work that a normal restart must respect."""

    descriptions = []
    active_elo = settings.elo_job_coordinator.active_job
    if active_elo is not None:
        target = (
            f'game {active_elo.game_id}'
            if active_elo.game_id is not None else 'all games'
        )
        descriptions.append(f'ELO job `{active_elo.operation}` for {target}')
    pending_count = game_open_workers.pending_game_coordinator.active_count
    if pending_count:
        descriptions.append(f'{pending_count} pending-game worker(s)')
    if operator_backup.backup_coordinator.active is not None:
        descriptions.append('manual database backup')
    purge_count = len(
        operator_channel_purge_service.manual_purge_coordinator.active_guilds
    )
    if purge_count:
        descriptions.append(
            f'manual channel purge in {purge_count} guild(s)'
        )
    return operator_restart_service.RestartActivitySnapshot(tuple(descriptions))


class administration(commands.Cog):
    elo_group = discord.app_commands.Group(
        name='elo',
        description='Inspect and maintain ELO calculations.',
        guild_only=True,
    )
    team_group = discord.app_commands.Group(
        name='team',
        description='View and manage competitive team attributes.',
        guild_only=True,
    )
    operator_group = discord.app_commands.Group(
        name='operator',
        description='Run restricted bot-wide operator workflows.',
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )
    operator_tribe_group = discord.app_commands.Group(
        name='tribe',
        description='Inspect and manage global Tribe metadata.',
        parent=operator_group,
        guild_only=True,
    )
    operator_player_group = discord.app_commands.Group(
        name='player',
        description='Run restricted player identity workflows.',
        parent=operator_group,
        guild_only=True,
    )
    operator_database_group = discord.app_commands.Group(
        name='database',
        description='Run restricted database operations.',
        parent=operator_group,
        guild_only=True,
    )
    operator_channels_group = discord.app_commands.Group(
        name='channels',
        description='Review restricted channel-maintenance operations.',
        parent=operator_group,
        guild_only=True,
    )
    operator_bot_group = discord.app_commands.Group(
        name='bot',
        description='Run restricted bot lifecycle operations.',
        parent=operator_group,
        guild_only=True,
    )
    operator_beta_group = discord.app_commands.Group(
        name='beta',
        description='Prepare restricted development-beta test scenarios.',
        parent=operator_group,
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_confirm_auto())
            self.bg_task2 = asyncio.create_task(self.task_purge_incomplete())

    async def cog_check(self, ctx):

        if settings.is_staff(ctx.author):
            return True
        else:
            if ctx.invoked_with == 'help' and ctx.command.name != 'help':
                return False
            else:
                await ctx.send('You do not have permission to use this command.')
                return False

    async def _run_confirm_game_job(
        self,
        *,
        game_id: int,
        guild_id: int,
        requester_id: int | None,
        requester_name: str,
        requester_description: str,
        auto_policy: (
            auto_confirmation_workers.AutoConfirmationPolicy | None
        ) = None,
    ):
        nova_guild_ids = tuple(dict.fromkeys(
            int(settings.server_ids[key])
            for key in ('polychampions', 'test')
            if settings.server_ids.get(key)
        ))
        nova_candidates = ()
        runtime_guild = settings.bot.get_guild(guild_id)
        if runtime_guild is not None and guild_id in nova_guild_ids:
            nova_role = discord.utils.get(runtime_guild.roles, name='The Novas')
            grad_role = discord.utils.get(runtime_guild.roles, name='Nova Grad')
            if nova_role is not None and grad_role is not None:
                nova_candidates = tuple(
                    nova_graduation_workers.NovaParticipantSnapshot(
                        discord_id=int(member.id),
                        member_name=str(member.name),
                        mention=str(member.mention),
                        has_nova_role=True,
                        has_grad_role=False,
                    )
                    for member in tuple(getattr(nova_role, 'members', ()) or ())
                    if grad_role not in tuple(getattr(member, 'roles', ()) or ())
                )
        publication_context = (
            confirmation_publication_workers.ConfirmationPublicationContext(
                bot_guild_ids=tuple(
                    int(candidate.id)
                    for candidate in getattr(settings.bot, 'guilds', ())
                ),
                nova_guild_ids=nova_guild_ids,
                nova_candidates=nova_candidates,
            )
        )
        utilities.lock_game(game_id)
        try:
            return await settings.elo_job_coordinator.run(
                operation='confirm_game',
                game_id=game_id,
                requester_id=requester_id,
                requester_name=requester_name,
                worker=elo_workers.confirm_game,
                worker_args=(
                    game_id,
                    guild_id,
                    requester_description,
                    publication_context,
                    auto_policy,
                ),
            )
        finally:
            utilities.unlock_game(game_id)

    async def _confirm_game_and_post(
        self,
        *,
        game_id: int,
        guild,
        prefix: str,
        channel,
        requester,
    ):
        try:
            result = await self._run_confirm_game_job(
                game_id=game_id,
                guild_id=guild.id,
                requester_id=requester.id,
                requester_name=requester.display_name,
                requester_description=models.GameLog.member_string(requester),
            )
        except elo_workers.ConfirmedWinSnapshotError as exc:
            raise ConfirmedWinPublicationError(exc.result) from exc
        await self._publish_confirmed_game(
            result=result,
            guild=guild,
            prefix=prefix,
            channel=channel,
        )
        return result

    async def _publish_confirmed_game(
        self,
        *,
        result: elo_workers.ConfirmedWinResult,
        guild,
        prefix: str,
        channel,
    ) -> None:
        try:
            if result.publication is None:
                raise RuntimeError('Committed confirmation has no publication snapshot.')
            await confirmation_publication.publish_confirmed_game(
                guild=guild,
                prefix=prefix,
                current_channel=channel,
                snapshot=result.publication,
                bot=settings.bot,
            )
        except Exception as exc:
            logger.exception(
                'Game %s confirmation committed but Discord publication '
                'failed',
                result.game_id,
            )
            raise ConfirmedWinPublicationError(result) from exc

    async def _run_recalculation_job(
        self,
        *,
        game_id: int,
        requester_id: int,
        requester_name: str,
    ):
        return await settings.elo_job_coordinator.run(
            operation='recalc_games_from',
            game_id=game_id,
            requester_id=requester_id,
            requester_name=requester_name,
            worker=elo_workers.recalculate_games_from,
            worker_args=(game_id,),
        )

    async def _set_ranked_state_and_post(
        self,
        *,
        game_id: int,
        guild,
        is_ranked: bool,
        requester,
    ):
        utilities.lock_game(game_id)
        try:
            try:
                result = await game_workers.run_ranked_state_correction(
                    game_id,
                    guild.id,
                    is_ranked,
                    models.GameLog.member_string(requester),
                )
            except game_workers.RankedStateSnapshotError as exc:
                logger.exception(
                    'Game %s ranked-state correction committed but its '
                    'publication snapshot failed',
                    game_id,
                )
                result = exc.result
                state = 'ranked' if result.is_ranked else 'unranked'
                return (
                    f'Game {result.game_id} is now marked as {state}, but '
                    'its committed Discord publication snapshot could not be '
                    'loaded. Do not run the correction again; staff should '
                    'reconcile its game-channel notice.'
                )
            state = 'ranked' if result.is_ranked else 'unranked'
            if result.publication is None:
                raise RuntimeError(
                    'Committed ranked-state correction has no publication '
                    'snapshot.'
                )
            try:
                await game_correction_publication.publish_ranked_state(
                    result.publication,
                    requester_display_name=requester.display_name,
                    bot=settings.bot,
                )
            except game_correction_publication.GameCorrectionPublicationError:
                logger.exception(
                    'Game %s ranked-state correction committed but Discord '
                    'publication failed',
                    result.game_id,
                )
                return (
                    f'Game {result.game_id} is now marked as {state}, but '
                    'its game-channel notice failed. Do not run the '
                    'correction again; staff should reconcile the notice.'
                )
            return (
                f'Game {result.game_id} is now marked as {state}.\n'
                'Notifying players: '
                f'{" ".join(result.publication.roster_mentions)}'
            )
        finally:
            utilities.unlock_game(game_id)

    async def _extend_pending_game(
        self,
        *,
        game_id: int,
        guild_id: int,
        requester,
    ):
        utilities.lock_game(game_id)
        try:
            return await game_workers.run_pending_game_extension(
                game_id,
                guild_id,
                models.GameLog.member_string(requester),
            )
        finally:
            utilities.unlock_game(game_id)

    async def _unstart_game_and_post(
        self,
        *,
        game_id: int,
        guild,
        prefix: str,
        requester,
        invocation_channel_id: int | None,
        invoked_with: str | None = None,
    ):
        utilities.lock_game(game_id)
        try:
            warnings = []
            try:
                result = await game_workers.run_game_unstart(
                    game_id,
                    guild.id,
                    models.GameLog.member_string(requester),
                    invoked_with or f'{prefix}unstart',
                    invocation_channel_id,
                )
            except game_workers.GameUnstartSnapshotError as exc:
                logger.exception(
                    'Game %s unstart committed but its publication snapshot '
                    'failed',
                    game_id,
                )
                result = exc.result
                warnings.append(
                    'the committed announcement snapshot needs reconciliation'
                )
            if (
                result.announcement_channel_id is not None
                and result.announcement_message_id is not None
                and result.publication is not None
            ):
                try:
                    await (
                        game_correction_publication
                        .publish_cancelled_unstart_announcement(
                            result.publication,
                            game_name=result.game_name,
                            announcement_channel_id=(
                                result.announcement_channel_id
                            ),
                            announcement_message_id=(
                                result.announcement_message_id
                            ),
                            guild=guild,
                            prefix=prefix,
                            bot=self.bot,
                        )
                    )
                except (
                    game_correction_publication
                    .GameCorrectionPublicationError
                ):
                    logger.exception(
                        'Could not publish the cancelled announcement for '
                        'unstarted game %s',
                        result.game_id,
                    )
                    warnings.append('the game announcement was not updated')

            deleted_targets = []
            for target in result.channel_targets:
                target_guild = discord.utils.get(
                    self.bot.guilds,
                    id=target.guild_id,
                )
                if target_guild is None:
                    logger.warning(
                        'Could not find guild %s for game %s channel %s',
                        target.guild_id,
                        result.game_id,
                        target.channel_id,
                    )
                    continue
                if await channels.delete_game_channel(
                    target_guild,
                    target.channel_id,
                ):
                    deleted_targets.append(target)

            if deleted_targets:
                try:
                    await game_workers.run_deleted_channel_reconciliation(
                        result.game_id,
                        guild.id,
                        tuple(deleted_targets),
                    )
                except peewee.PeeweeException:
                    logger.exception(
                        'Could not reconcile deleted channels for unstarted '
                        'game %s',
                        result.game_id,
                    )
                    warnings.append(
                        'deleted channel references need reconciliation'
                    )

            if len(deleted_targets) != len(result.channel_targets):
                warnings.append('one or more game channels were not deleted')

            message = (
                f'Game {result.game_id} is now an open game and no longer in '
                f'progress.\nNotifying players: {" ".join(result.mentions)}'
            )
            if warnings:
                message += f'\n:warning: {"; ".join(warnings)}.'
            return message
        finally:
            utilities.unlock_game(game_id)

    @commands.command(aliases=['confirmgame'], usage='game_id')
    # async def confirm(self, ctx, winning_game: PolyGame = None):
    async def confirm(self, ctx, *, arg: str = None):
        """ *Staff*: List unconfirmed games, or let staff confirm winners
         **Examples**
        `[p]confirm` - List unconfirmed games
        `[p]confirm 5` - Confirms the winner of game 5 and performs ELO changes
        """

        if arg is None:
            # display list of unconfirmed games
            game_list = await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    load_unconfirmed_game_summaries,
                    ctx.guild.id,
                ),
            )
            if len(game_list) == 0:
                return await ctx.send('No unconfirmed games found.')
            await utilities.paginate(self.bot, ctx, title=f'{len(game_list)} unconfirmed games', message_list=game_list, page_start=0, page_end=15, page_size=15)
            return

        if settings.elo_job_coordinator.is_active:
            logger.info('Skipping command due to active ELO job')
            return await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. No new game results can be logged. Please try again in a few minutes.')

        if arg.lower() == 'auto':
            (unconfirmed_count, games_confirmed) = await self.confirm_auto(ctx.guild, ctx.prefix, ctx.channel)
            return await ctx.send(f'Autoconfirm process complete. {games_confirmed} games auto-confirmed. {unconfirmed_count - games_confirmed} games left unconfirmed.')

        # else confirming a specific game ie. $confirm 1234
        game_converter = PolyGame()
        winning_game_id = await game_converter.convert(ctx, arg)

        try:
            async with ctx.typing():
                result = await self._confirm_game_and_post(
                    game_id=winning_game_id,
                    guild=ctx.guild,
                    prefix=ctx.prefix,
                    channel=ctx.channel,
                    requester=ctx.author,
                )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.WinValidationError as exc:
            return await ctx.send(str(exc))
        except exceptions.CheckFailedError as exc:
            return await ctx.send(f'*Error*: {exc}')
        except ConfirmedWinPublicationError as exc:
            return await ctx.send(
                format_confirmed_win_reconciliation(exc.result)
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure confirming game %s', winning_game_id
            )
            return await ctx.send(
                'Game confirmation failed and rolled back. No Discord '
                'channel updates were made.'
            )

        await ctx.send(
            f'**Game {result.game_id}** winner has been confirmed as '
            f'**{result.winner_name}**'
        )

    async def confirm_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        try:
            result = await self._confirm_game_and_post(
                game_id=game_id,
                guild=interaction.guild,
                prefix=prefix,
                channel=interaction.channel,
                requester=interaction.user,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await interaction.followup.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.WinValidationError as exc:
            return await interaction.followup.send(str(exc))
        except exceptions.CheckFailedError as exc:
            return await interaction.followup.send(f'*Error*: {exc}')
        except ConfirmedWinPublicationError as exc:
            return await interaction.followup.send(
                format_confirmed_win_reconciliation(exc.result)
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure confirming game %s from slash command',
                game_id,
            )
            return await interaction.followup.send(
                'Game confirmation failed and rolled back. No Discord '
                'channel updates were made.'
            )

        await interaction.followup.send(
            f'**Game {result.game_id}** winner has been confirmed as '
            f'**{result.winner_name}**'
        )

    async def confirm_auto(self, guild, prefix, current_channel):
        logger.info(f'in confirm_auto with guild {guild} prefix {prefix} current_channel {current_channel}')

        if settings.elo_job_coordinator.is_active:
            logger.info('Skipping confirm_auto due to active ELO job')
            return (0, 0)

        policy = auto_confirmation_workers.AutoConfirmationPolicy(
            as_of=datetime.datetime.now(),
        )
        batch = await (
            auto_confirmation_workers.run_discover_auto_confirmations(
                auto_confirmation_workers.AutoConfirmationDiscoveryRequest(
                    guild_id=guild.id,
                    policy=policy,
                )
            )
        )
        games_confirmed = 0
        unconfirmed_count = batch.unconfirmed_count
        if batch.truncated:
            logger.warning(
                'Automatic-confirmation discovery for guild %s reached its '
                'per-cycle limit; remaining records are deferred',
                guild.id,
            )

        for candidate in batch.candidates:
            game_id = candidate.game_id
            logger.debug(f'auto_confirm checking game {game_id}')

            try:
                result = await self._run_confirm_game_job(
                    game_id=game_id,
                    guild_id=guild.id,
                    requester_id=None,
                    requester_name='automatic confirmation task',
                    requester_description='Automatic confirmation task',
                    auto_policy=batch.policy,
                )
            except elo_workers.ConfirmedWinSnapshotError as exc:
                games_confirmed += 1
                try:
                    await current_channel.send(
                        format_confirmed_win_reconciliation(exc.result)
                    )
                except Exception:
                    logger.exception(
                        'Could not report snapshot reconciliation warning '
                        'for auto-confirmed game %s',
                        exc.result.game_id,
                    )
                continue
            except exceptions.RecordLocked:
                logger.info(
                    'Cannot auto-confirm game %s - it is locked', game_id
                )
                continue
            except elo_workers.AutoConfirmationIneligible:
                logger.info(
                    'Skipping stale automatic-confirmation candidate %s',
                    game_id,
                )
                continue
            except EloJobConflict:
                logger.info(
                    'Stopping auto-confirm because another ELO job started'
                )
                break
            except (
                elo_workers.WinValidationError,
                exceptions.CheckFailedError,
                peewee.PeeweeException,
            ):
                logger.exception('Could not auto-confirm game %s', game_id)
                continue

            games_confirmed += 1
            try:
                await self._publish_confirmed_game(
                    result=result,
                    guild=guild,
                    prefix=prefix,
                    channel=current_channel,
                )
            except ConfirmedWinPublicationError as exc:
                try:
                    await current_channel.send(
                        format_confirmed_win_reconciliation(exc.result)
                    )
                except Exception:
                    logger.exception(
                        'Could not report reconciliation warning for '
                        'auto-confirmed game %s',
                        result.game_id,
                    )
                continue

            try:
                evidence = (
                    result.auto_confirmation
                    or candidate.discovered_evidence
                )
                await current_channel.send(
                    f'Game {game_id} auto-confirmed. '
                    f'{evidence.reason} {evidence.confirmed_count} of '
                    f'{evidence.side_count} sides had confirmed.'
                )
            except Exception:
                logger.exception(
                    'Game %s auto-confirmed but its completion summary '
                    'could not be sent',
                    result.game_id,
                )

        logger.debug(f'confirm_auto processed {unconfirmed_count} and confirmed {games_confirmed} games.')
        return (unconfirmed_count, games_confirmed)

    async def run_confirm_auto_cycle(self):
        """Run one isolated recurring cycle without retaining database state."""

        if settings.elo_job_coordinator.is_active:
            logger.debug(
                'Skipping task_confirm_auto while an ELO job is active.'
            )
            return
        for guild in self.bot.guilds:
            try:
                staff_output_channel = guild.get_channel(
                    settings.guild_setting(guild.id, 'log_channel')
                )
                if not staff_output_channel:
                    logger.debug(
                        'Could not load log_channel for server %s - skipping',
                        guild.id,
                    )
                    continue
                logger.debug('Loaded log_channel for server %s', guild.id)
                prefix = settings.guild_setting(
                    guild.id,
                    'command_prefix',
                )
                unconfirmed_count, games_confirmed = await self.confirm_auto(
                    guild,
                    prefix,
                    staff_output_channel,
                )
                if games_confirmed:
                    message = (
                        'Autoconfirm process complete. '
                        f'{games_confirmed} games auto-confirmed. '
                        f'{unconfirmed_count - games_confirmed} games left '
                        'unconfirmed.'
                    )
                    await staff_output_channel.send(message)
                    logger.debug(message)
                else:
                    logger.debug(
                        'No games_confirmed for guild %s', guild.id
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    'Automatic-confirmation cycle failed for guild %s; '
                    'later guilds and cycles remain available',
                    guild.id,
                )

    async def task_confirm_auto(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 0.5)  # half hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(8)
            logger.debug('Task running: task_confirm_auto')

            await self.run_confirm_auto_cycle()

            await asyncio.sleep(sleep_cycle)

    async def task_purge_incomplete(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 5)  # 5 hour cycle

        while not self.bot.is_closed():
            await asyncio.sleep(900)
            logger.debug('Task running: task_purge_incomplete')
            as_of = datetime.date.today()
            for guild in self.bot.guilds:
                try:
                    await (
                        incomplete_game_purge
                        .purge_incomplete_games_for_guild(
                            bot=self.bot,
                            guild=guild,
                            as_of=as_of,
                        )
                    )
                except Exception:
                    logger.exception(
                        'Unhandled incomplete-game purge failure for guild %s',
                        guild.id,
                    )

            await asyncio.sleep(sleep_cycle)

    @commands.command(usage='game_id')
    async def rankset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as ranked
        Turns an incomplete unranked game into a ranked game
         **Examples**
        `[p]rankset 50`
        """
        if game is None:
            return await ctx.send('No matching game was found.')

        try:
            message = await self._set_ranked_state_and_post(
                game_id=game,
                guild=ctx.guild,
                is_ranked=True,
                requester=ctx.author,
            )
        except game_workers.RankedStateValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(message)

    @commands.command(usage='game_id')
    async def rankunset(self, ctx, game: PolyGame = None):
        """ *Staff*: Marks an incomplete game as unranked
        Turns an incomplete ranked game into an unranked game
         **Examples**
        `[p]rankunset 50`
        """
        if game is None:
            return await ctx.send('No matching game was found.')

        try:
            message = await self._set_ranked_state_and_post(
                game_id=game,
                guild=ctx.guild,
                is_ranked=False,
                requester=ctx.author,
            )
        except game_workers.RankedStateValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(message)

    async def set_ranked_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        ranked: bool,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        # Successful competitive-state corrections are intentionally public
        # so server members can see the same audit-facing result as the
        # preserved prefix commands.
        await interaction.response.defer()
        try:
            message = await self._set_ranked_state_and_post(
                game_id=game_id,
                guild=interaction.guild,
                is_ranked=ranked,
                requester=interaction.user,
            )
        except game_workers.RankedStateValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except (peewee.PeeweeException, exceptions.RecordLocked):
            logger.exception('Failed ranked-state correction for %s', game_id)
            return await interaction.followup.send(
                'Ranked-state correction failed; no Discord update was made.',
                ephemeral=True,
            )
        await interaction.followup.send(message)

    @settings.in_bot_channel()
    @commands.command(usage='game_id')
    async def unstart(self, ctx, game: PolyGame = None):
        """ *Staff*: Resets an in progress game to a pending matchmaking sesson

         **Examples**
        `[p]unstart 1234`
        """

        if game is None:
            return await ctx.send('No matching game was found.')

        try:
            message = await self._unstart_game_and_post(
                game_id=game,
                guild=ctx.guild,
                prefix=ctx.prefix,
                requester=ctx.author,
                invocation_channel_id=ctx.channel.id,
            )
        except game_workers.GameUnstartValidationError as exc:
            return await ctx.send(str(exc))
        await ctx.send(message)

    async def unstart_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        try:
            message = await self._unstart_game_and_post(
                game_id=game_id,
                guild=interaction.guild,
                prefix=prefix,
                requester=interaction.user,
                invocation_channel_id=interaction.channel_id,
                invoked_with='/game manage unstart',
            )
        except game_workers.GameUnstartValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Failed to unstart game %s', game_id)
            return await interaction.followup.send(
                'Game restoration failed and rolled back. No Discord cleanup '
                'was performed.',
                ephemeral=True,
            )
        await interaction.followup.send(message)

    @commands.command(usage='game_id')
    async def extend(self, ctx, game: PolyGame = None):
        """ *Staff*: Extends the timer of an open game by 24 hours

         **Examples**
        `[p]extend 1234`
        """

        if game is None:
            return await ctx.send('No game ID provided.')

        try:
            result = await self._extend_pending_game(
                game_id=game,
                guild_id=ctx.guild.id,
                requester=ctx.author,
            )
        except game_workers.GameExtensionValidationError as exc:
            return await ctx.send(str(exc))
        return await ctx.send(
            f'Game {result.game_id}\'s deadline has been extended to '
            f'**{result.new_expiration}**. Previous expiration was '
            f'**{result.old_expiration}**.'
        )

    async def extend_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.defer()
        try:
            result = await self._extend_pending_game(
                game_id=game_id,
                guild_id=interaction.guild.id,
                requester=interaction.user,
            )
        except game_workers.GameExtensionValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Failed pending-game extension for %s', game_id)
            return await interaction.followup.send(
                'Game extension failed and rolled back.',
                ephemeral=True,
            )
        await interaction.followup.send(
            f'Game {result.game_id}\'s deadline has been extended to '
            f'**{result.new_expiration}**. Previous expiration was '
            f'**{result.old_expiration}**.'
        )

    @operator_channels_group.command(
        name='purge',
        description='Privately preview and purge exact game channels.',
    )
    @discord.app_commands.choices(mode=[
        discord.app_commands.Choice(
            name='Stale tracked channels',
            value=operator_channel_purge_workers.STALE,
        ),
        discord.app_commands.Choice(
            name='Capacity relief',
            value=operator_channel_purge_workers.CAPACITY,
        ),
        discord.app_commands.Choice(
            name='Untracked category channels',
            value=operator_channel_purge_workers.ORPHAN,
        ),
        discord.app_commands.Choice(
            name='Missing tracked references',
            value=operator_channel_purge_workers.MISSING,
        ),
    ])
    @discord.app_commands.describe(
        mode='Candidate policy to review; no channel is selected automatically.',
    )
    async def operator_channels_purge_slash(
        self,
        interaction: discord.Interaction,
        mode: discord.app_commands.Choice[str],
    ):
        """Owner-only exact-selection channel cleanup."""

        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        if int(interaction.user.id) != int(settings.owner_id):
            return await interaction.response.send_message(
                'Only the configured bot owner can purge game channels.',
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            preview = await operator_channel_purge_service.load_preview(
                interaction,
                str(mode.value),
            )
        except operator_channel_purge_workers.ManualChannelPurgeError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Could not load manual channel-purge preview')
            return await interaction.followup.send(
                'Could not load the channel-purge preview.', ephemeral=True
            )

        view = operator_channel_purge_views.ManualChannelPurgeWorkspace(
            requester_id=int(interaction.user.id),
            preview=preview,
            refresher=operator_channel_purge_service.load_preview,
            confirmer=operator_channel_purge_service.confirm_purge,
        )
        await operator_channel_purge_views.publish_private(interaction, view)

    @operator_tribe_group.command(
        name='emoji',
        description='View or update one global Tribe emoji.',
    )
    @discord.app_commands.autocomplete(
        tribe=operator_tribe_service.autocomplete_tribes,
    )
    @discord.app_commands.describe(
        tribe='Tribe name.',
        emoji='Unicode or custom emoji to set; omit this to view it.',
    )
    async def operator_tribe_emoji_slash(
        self,
        interaction: discord.Interaction,
        tribe: str,
        emoji: str | None = None,
    ):
        """Owner-only atomic global Tribe emoji read/edit."""

        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        if int(interaction.user.id) != int(settings.owner_id):
            return await interaction.response.send_message(
                'Only the configured bot owner can manage Tribe emojis.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            if emoji is None:
                result = await operator_tribe_workers.run_read(
                    operator_tribe_service.read_request(interaction, tribe)
                )
            else:
                result = await operator_tribe_workers.run_mutation(
                    operator_tribe_service.mutation_request(
                        interaction,
                        tribe,
                        emoji,
                    )
                )
            await operator_tribe_service.publish_result(interaction, result)
            return result
        except operator_tribe_workers.OperatorTribeError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except operator_tribe_service.OperatorTribePublicationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Operator Tribe emoji database operation failed')
            return await interaction.followup.send(
                'The Tribe emoji operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected operator Tribe emoji failure')
            return await interaction.followup.send(
                'The Tribe emoji operation failed. Please try again.',
                ephemeral=True,
            )

    @operator_player_group.command(
        name='migrate',
        description='Preview and migrate one stored player identity.',
    )
    @discord.app_commands.describe(
        source_id='Old Discord user ID or mention stored by the bot.',
        destination='Current member whose Discord account will replace it.',
    )
    async def operator_player_migrate_slash(
        self,
        interaction: discord.Interaction,
        source_id: str,
        destination: discord.Member,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        if not settings.is_superuser(interaction.user):
            return await interaction.response.send_message(
                'Only a configured bot superuser can migrate players.',
                ephemeral=True,
            )
        parsed_source_id = utilities.string_to_user_id(source_id)
        if not parsed_source_id:
            return await interaction.response.send_message(
                'Enter a valid source Discord user ID or mention.',
                ephemeral=True,
            )
        if int(parsed_source_id) == int(destination.id):
            return await interaction.response.send_message(
                'Source and destination must be different Discord accounts.',
                ephemeral=True,
            )
        if destination.bot:
            return await interaction.response.send_message(
                'A bot account cannot be the migration destination.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            preview = await operator_player_migration_workers.run_preview(
                operator_player_migration_service.preview_request(
                    interaction,
                    source_id=int(parsed_source_id),
                    destination=destination,
                )
            )
        except operator_player_migration_workers.PlayerMigrationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Could not load player migration preview')
            return await interaction.followup.send(
                'Could not load the migration preview.', ephemeral=True
            )

        async def confirm(component_interaction, accepted_preview):
            try:
                result = await operator_player_migration_workers.run_commit(
                    operator_player_migration_service.commit_request(
                        component_interaction,
                        accepted_preview,
                    )
                )
            except operator_player_migration_workers.PlayerMigrationError:
                raise
            except peewee.PeeweeException as exc:
                logger.exception('Player migration rolled back')
                raise operator_player_migration_workers.PlayerMigrationError(
                    'Player migration failed and rolled back.'
                ) from exc
            try:
                await operator_player_migration_service.publish_result(
                    component_interaction,
                    result,
                )
            except operator_player_migration_service.PlayerMigrationPublicationError as exc:
                await component_interaction.followup.send(str(exc), ephemeral=True)

        view = operator_player_migration_views.PlayerMigrationPreviewView(
            requester_id=int(interaction.user.id),
            preview=preview,
            confirmer=confirm,
        )
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    async def _open_beta_fixture_preview(
        self,
        interaction: discord.Interaction,
        *,
        operation: str,
        user_ids: tuple[int, ...] = (),
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        if int(interaction.user.id) != int(settings.owner_id):
            return await interaction.response.send_message(
                'Only the configured bot owner can prepare or reset beta '
                'fixtures.',
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        try:
            preview = await operator_beta_fixtures_workers.run_preview(
                operator_beta_fixtures_service.preview_request(
                    interaction,
                    operation=operation,
                    user_ids=user_ids,
                )
            )
        except operator_beta_fixtures_workers.BetaFixtureError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected beta fixture preview failure')
            return await interaction.followup.send(
                'The beta fixture preview could not be loaded. No database '
                'changes were made.',
                ephemeral=True,
            )

        async def confirm(component_interaction, accepted_preview):
            return await operator_beta_fixtures_workers.run_commit(
                operator_beta_fixtures_service.commit_request(
                    component_interaction,
                    accepted_preview,
                )
            )

        view = operator_beta_fixtures_views.BetaFixturePreviewView(
            requester_id=int(interaction.user.id),
            preview=preview,
            confirmer=confirm,
        )
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass
        return preview

    @operator_beta_group.command(
        name='prepare',
        description='Preview a fixed beta scenario bundle for two members.',
    )
    @discord.app_commands.describe(
        participant_one='First registered development-guild participant.',
        participant_two='Second registered development-guild participant.',
    )
    async def operator_beta_prepare_slash(
        self,
        interaction: discord.Interaction,
        participant_one: discord.Member,
        participant_two: discord.Member,
    ):
        return await self._open_beta_fixture_preview(
            interaction,
            operation=operator_beta_fixtures_workers.PREPARE,
            user_ids=(int(participant_one.id), int(participant_two.id)),
        )

    @operator_beta_group.command(
        name='reset',
        description='Preview restoration of the exact owned beta scenarios.',
    )
    async def operator_beta_reset_slash(
        self,
        interaction: discord.Interaction,
    ):
        return await self._open_beta_fixture_preview(
            interaction,
            operation=operator_beta_fixtures_workers.RESET,
        )

    @operator_bot_group.command(
        name='restart',
        description='Restart the bot through its reviewed process supervisor.',
    )
    @discord.app_commands.describe(
        force=(
            'Owner only: bypass known active-work checks after exact '
            'confirmation.'
        ),
    )
    async def operator_bot_restart_slash(
        self,
        interaction: discord.Interaction,
        force: bool = False,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        requester_id = int(interaction.user.id)
        requester_name = str(
            getattr(interaction.user, 'display_name', None)
            or getattr(interaction.user, 'name', None)
            or f'user-{requester_id}'
        )

        def restart_request(
            confirmation_text: str | None = None,
        ) -> operator_restart_service.RestartRequest:
            return operator_restart_service.RestartRequest(
                requester_id=requester_id,
                requester_name=requester_name,
                is_superuser=bool(settings.is_superuser(interaction.user)),
                is_owner=requester_id == int(settings.owner_id),
                force=bool(force),
                confirmation_text=confirmation_text,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            preview = await operator_restart_service.restart_coordinator.preview(
                restart_request(),
                project_root=PROJECT_ROOT,
                activity_loader=current_restart_activity,
            )
        except operator_restart_service.RestartError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected supervised restart preflight failure')
            return await interaction.followup.send(
                'Restart preflight failed without stopping the bot. Inspect '
                'the bot logs before retrying.',
                ephemeral=True,
            )

        async def run_restart(component_interaction, confirmation_text):
            component_user_id = int(component_interaction.user.id)
            request = operator_restart_service.RestartRequest(
                requester_id=component_user_id,
                requester_name=str(
                    getattr(component_interaction.user, 'display_name', None)
                    or getattr(component_interaction.user, 'name', None)
                    or f'user-{component_user_id}'
                ),
                is_superuser=bool(
                    settings.is_superuser(component_interaction.user)
                ),
                is_owner=component_user_id == int(settings.owner_id),
                force=bool(force),
                confirmation_text=confirmation_text,
            )

            async def shutdown(requester_id, force_restart):
                async def acknowledge():
                    view.mark_accepted()
                    await component_interaction.edit_original_response(
                        view=view,
                    )

                await self.bot.request_supervised_restart(
                    requester_id,
                    force_restart,
                    before_close=acknowledge,
                )

            return await operator_restart_service.restart_coordinator.run(
                request,
                project_root=PROJECT_ROOT,
                activity_loader=current_restart_activity,
                shutdown=shutdown,
            )

        view = operator_restart_views.RestartConfirmationView(
            preview=preview,
            runner=run_restart,
        )
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @operator_database_group.command(
        name='backup',
        description='Confirm an exceptional production recovery backup.',
    )
    async def operator_database_backup_slash(
        self,
        interaction: discord.Interaction,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        if int(interaction.user.id) != int(settings.owner_id):
            return await interaction.response.send_message(
                'Only the configured bot owner can run a production backup.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            await operator_backup.validate_runtime(int(interaction.user.id))
        except operator_backup.BackupError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected operator backup preflight failure')
            return await interaction.followup.send(
                'The production backup preflight failed. No backup was '
                'started.',
                ephemeral=True,
            )

        def request_for(component_interaction):
            actor_name = str(
                getattr(component_interaction.user, 'display_name', None)
                or getattr(component_interaction.user, 'name', None)
                or f'user-{component_interaction.user.id}'
            )
            return operator_backup.BackupRequest(
                guild_id=int(component_interaction.guild_id),
                channel_id=int(component_interaction.channel_id or 0),
                requester_id=int(component_interaction.user.id),
                requester_description=actor_name,
            )

        async def run_backup(component_interaction):
            return await operator_backup.backup_coordinator.run(
                request_for(component_interaction)
            )

        view = operator_backup_views.BackupConfirmationView(
            requester_id=int(interaction.user.id),
            runner=run_backup,
        )
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @operator_player_group.command(
        name='delete',
        description='Preview and delete one orphan stored player identity.',
    )
    @discord.app_commands.describe(
        player_id='Stored Discord user ID or mention to delete.',
    )
    async def operator_player_delete_slash(
        self,
        interaction: discord.Interaction,
        player_id: str,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.', ephemeral=True
            )
        if int(interaction.user.id) != int(settings.owner_id):
            return await interaction.response.send_message(
                'Only the configured bot owner can delete stored player '
                'identities.',
                ephemeral=True,
            )
        parsed_player_id = utilities.string_to_user_id(player_id)
        if not parsed_player_id:
            return await interaction.response.send_message(
                'Enter a valid stored Discord user ID or mention.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            preview = await operator_player_deletion_workers.run_preview(
                operator_player_deletion_service.preview_request(
                    interaction,
                    target_id=int(parsed_player_id),
                )
            )
        except operator_player_deletion_workers.PlayerDeletionError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Could not load player deletion preview')
            return await interaction.followup.send(
                'Could not load the player deletion preview.', ephemeral=True
            )
        except Exception:
            logger.exception('Unexpected player deletion preview failure')
            return await interaction.followup.send(
                'Could not load the player deletion preview.', ephemeral=True
            )

        async def confirm(
            component_interaction,
            accepted_preview,
            confirmation_text,
        ):
            try:
                result = await operator_player_deletion_workers.run_commit(
                    operator_player_deletion_service.commit_request(
                        component_interaction,
                        accepted_preview,
                        confirmation_text=confirmation_text,
                    )
                )
            except operator_player_deletion_workers.PlayerDeletionError:
                raise
            except peewee.PeeweeException as exc:
                logger.exception('Player deletion rolled back')
                raise operator_player_deletion_workers.PlayerDeletionError(
                    'Player deletion failed and rolled back.'
                ) from exc
            try:
                await operator_player_deletion_service.publish_result(
                    component_interaction,
                    result,
                )
            except operator_player_deletion_service.PlayerDeletionPublicationError as exc:
                try:
                    await component_interaction.followup.send(
                        str(exc), ephemeral=True
                    )
                except Exception:
                    logger.exception(
                        'Could not report committed player deletion '
                        'publication failure'
                    )

        view = operator_player_deletion_views.PlayerDeletionPreviewView(
            requester_id=int(interaction.user.id),
            preview=preview,
            confirmer=confirm,
        )
        await interaction.edit_original_response(content=None, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @team_group.command(
        name='create',
        description='Create a competitive team.',
    )
    @discord.app_commands.describe(
        name='Team name; this becomes the exact Discord membership role name.',
    )
    async def team_create_slash(
        self,
        interaction: discord.Interaction,
        name: str,
    ):
        """Create one team and its actor-attributed audit entry after commit."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_creation_service.native_access_error(
            interaction.user,
            guild_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_creation_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            request = team_creation_service.build_request(
                member=interaction.user,
                guild_id=guild_id,
                name=name,
                native=True,
                invoked_with='/team create',
            )
            result = await team_creation_service.run_create(request)
            await team_creation_service.publish_success(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_creation_workers.TeamCreationValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team create command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team creation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team create failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team creation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='archive',
        description='Permanently archive an inactive competitive team.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_house_teams,
    )
    @discord.app_commands.describe(
        team='Active team to archive.',
        confirm='Must be true after checking the team and its open games.',
    )
    async def team_archive_slash(
        self,
        interaction: discord.Interaction,
        team: str,
        confirm: bool,
    ):
        """Archive one eligible Team through the bounded atomic worker."""

        guild_id = getattr(getattr(interaction, 'guild', None), 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_archive_service.native_access_error(
            interaction.user,
            guild_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if not confirm:
            return await interaction.response.send_message(
                'Team archival was not started. Set `confirm` to true only '
                'after checking the selected Team.',
                ephemeral=True,
            )

        actor = team_archive_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            preflight = await team_archive_service.run_preflight(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
            )
            result = await team_archive_service.run_archive(
                team_archive_service.build_request(
                    member=interaction.user,
                    guild_id=guild_id,
                    preflight=preflight,
                    confirmed=confirm,
                )
            )
        except (
            team_archive_workers.TeamArchiveError,
            team_attributes_workers.TeamAttributeValidationError,
        ) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure archiving team in guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team archival failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected team archival failure in guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team archival failed and rolled back.',
                ephemeral=True,
            )

        try:
            await team_emoji_service.public_interaction_sender(interaction)(
                team_archive_service.success_message(result, actor=actor)
            )
        except Exception:
            logger.exception(
                'Committed team archival %s could not publish in guild %s',
                result.team_id,
                guild_id,
            )
            await interaction.followup.send(
                f':warning: Team **{team_archive_service.display_team_name(result.team_name)}** '
                'was archived, but the '
                'public confirmation could not be sent. Do not retry the '
                'archive; an operator should reconcile the announcement.',
                ephemeral=True,
            )
        return result

    @team_group.command(
        name='show',
        description='View a competitive team card and roster history.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
    )
    async def team_show_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
    ):
        """Publish the shared dense team card from a bounded read worker."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        channel_id = getattr(interaction, 'channel_id', None)
        if channel_id is None:
            channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
        access_error = team_show_service.native_access_error(
            interaction.user,
            int(guild_id),
            channel_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            request = team_show_service.build_request(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                activity_mode=team_show_workers.TEAM_ACTIVITY_RECENT,
                native=True,
                invoked_with='/team show',
                prefix=str(
                    settings.guild_setting(int(guild_id), 'command_prefix')
                ),
                channel_id=channel_id,
            )
            result = await team_show_service.run(request)
            return await team_show_service.publish_native(interaction, result)
        except team_show_workers.TeamShowLookupError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except team_show_workers.TeamShowPermissionError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except interaction_lifecycle.PublicInteractionDestinationError:
            logger.exception(
                'No public destination for native team show in guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'The team card was not published publicly because its '
                'channel could not be resolved. Please try again.',
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team show command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'The team card could not be loaded because the database read '
                'failed.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team show failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'The team card could not be loaded. Please try again.',
                ephemeral=True,
            )

    @commands.command(usage='team_name new_emoji')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_emoji(self, ctx, team_name: str, emoji: str = None):
        """*Mod*: Assign an emoji to a team
        **Example:**
        `[p]team_emoji Ronin :my_fancy_emoji:` - Set new emoji. Currently requires a custom emoji.
        `[p]team_emoji Ronin` - Display currently saved emoji
        """

        if emoji is not None and not team_emoji_workers.is_valid_emoji(emoji):
            return await ctx.send(f'Valid emoji not detected. Example: `{ctx.prefix}team_emoji name :my_custom_emoji:`')

        try:
            if emoji is None:
                result = await team_emoji_service.run_read(
                    team_emoji_service.build_read_request(
                        member=ctx.author,
                        guild_id=ctx.guild.id,
                        team_lookup=team_name,
                        invoked_with=ctx.invoked_with,
                    )
                )
                return await ctx.send(
                    team_emoji_service.legacy_read_message(result),
                )

            request = team_emoji_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                team_lookup=team_name,
                emoji=emoji,
                native=False,
                invoked_with=ctx.invoked_with,
            )

            async def publish(result):
                await team_emoji_service.publish_mutation_result(
                    result,
                    send=ctx.send,
                )

            await team_emoji_service.run_mutation(
                request,
                after_commit=publish,
            )
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')
        except team_emoji_workers.TeamEmojiLookupError as ex:
            return await ctx.send(f'{ex}\nExample: `{ctx.prefix}team_emoji name :my_custom_emoji:`')
        except team_emoji_workers.TeamEmojiValidationError as ex:
            return await ctx.send(str(ex))
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team emoji for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team emoji operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team emoji failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team emoji operation failed and rolled back.')

    @team_group.command(
        name='emoji',
        description='View or update a team emoji.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        emoji='Unicode or custom emoji to set; omit this to view it.',
        clear='Explicitly remove the configured team emoji.',
    )
    async def team_emoji_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        emoji: str | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team emoji with post-commit public output."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        try:
            teams_enabled = bool(
                settings.guild_setting(guild_id, 'allow_teams')
            )
            requester_is_mod = bool(settings.is_mod(interaction.user))
        except (AttributeError, TypeError, exceptions.CheckFailedError):
            return await interaction.response.send_message(
                'This command is not available on this server.',
                ephemeral=True,
            )
        if not teams_enabled:
            return await interaction.response.send_message(
                'Teams are not enabled on this server.',
                ephemeral=True,
            )
        if not requester_is_mod:
            return await interaction.response.send_message(
                'You do not have permission to manage team emojis.',
                ephemeral=True,
            )
        if clear and emoji is not None:
            return await interaction.response.send_message(
                'Choose either an emoji or `clear`, not both.',
                ephemeral=True,
            )
        if emoji is not None and not team_emoji_workers.is_valid_emoji(emoji):
            return await interaction.response.send_message(
                'Valid Unicode or custom emoji syntax was not detected.',
                ephemeral=True,
            )

        actor = team_emoji_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if emoji is None and not clear:
                result = await team_emoji_service.run_read(
                    team_emoji_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        team_lookup=team,
                        invoked_with='/team emoji',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_emoji_service.read_message(result, actor=actor)
                )
                return result

            request = team_emoji_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                team_lookup=team,
                emoji=emoji,
                clear=clear,
                native=True,
                invoked_with='/team emoji',
            )
            result = await team_emoji_service.run_mutation(request)
            await team_emoji_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(
                    interaction
                ),
                actor=actor,
            )
            return result
        except team_emoji_workers.TeamEmojiValidationError as ex:
            return await interaction.followup.send(str(ex), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team emoji command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team emoji operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team emoji failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team emoji operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='name',
        description='View or update a team name.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        name='Replacement name; omit this to view the current name.',
    )
    async def team_name_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        name: str | None = None,
    ):
        """Read or mod-edit one team name with public committed output."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_NAME,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if name is None:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                        team_lookup=team,
                        invoked_with='/team name',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                team_lookup=team,
                name=name,
                native=True,
                invoked_with='/team name',
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team name command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team name operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team name failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team name operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='server',
        description='View or update a team external server ID.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        server_id='Raw external Discord server ID; omit this to view it.',
        clear='Explicitly clear the nullable external server ID.',
    )
    async def team_server_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        server_id: int | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team external server ID."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and server_id is not None:
            return await interaction.response.send_message(
                'Choose either a server ID or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if server_id is None and not clear:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                        team_lookup=team,
                        invoked_with='/team server',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup=team,
                server_id=server_id,
                clear=clear,
                native=True,
                invoked_with='/team server',
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team server command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team server operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team server failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team server operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='tier',
        description='View or update a team tier.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.choices(
        tier=team_attributes_service.TEAM_TIER_CHOICES,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        tier='Configured tier number; omit this to view the current tier.',
    )
    async def team_tier_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        tier: str | None = None,
    ):
        """Read or mod-edit a team tier with post-commit role refresh."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_TIER,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if tier is None:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
                        team_lookup=team,
                        invoked_with='/team tier',
                    )
                )
                await team_emoji_service.public_interaction_sender(
                    interaction
                )(
                    team_attributes_service.read_message(
                        result,
                        actor=actor,
                    )
                )
                return result

            preflight = await team_attributes_service.run_tier_preflight(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                invoked_with='/team tier',
            )

            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
                team_lookup=team,
                tier=tier,
                expected_team_id=preflight.current.team_id,
                expected_value=preflight.current.value,
                expected_value_present=True,
                team_role_id=preflight.team_role_id,
                team_role_name=preflight.team_role_name,
                team_member_ids=preflight.member_ids,
                native=True,
                invoked_with='/team tier',
            )
            result = await team_attributes_service.run_mutation(request)
            try:
                reconciliation = await team_attributes_service.reconcile_tier_roles(
                    interaction.guild,
                    result,
                )
            except Exception:
                logger.exception(
                    'Committed team tier %s could not reconcile roles',
                    result.team_id,
                )
                reconciliation = team_attributes_service.TierRoleReconciliation(
                    team_id=result.team_id,
                    attempted=0,
                    updated=0,
                    team_role_missing=True,
                )
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
                reconciliation=reconciliation,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team tier command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team tier operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team tier failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team tier operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='house',
        description='View or update a team House affiliation.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_house_teams,
        house=team_attributes_service.autocomplete_houses,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        house='House name to assign; omit this to view the current House.',
        clear='Explicitly remove the team House affiliation.',
    )
    async def team_house_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        house: str | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team House affiliation after commit."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        mutation = house is not None or clear
        access_error = team_attributes_service.native_access_error(
            interaction.user,
            guild_id,
            team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
            mutation=mutation,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and house is not None:
            return await interaction.response.send_message(
                'Choose either a House or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_attributes_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            if not mutation:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=interaction.user,
                        guild_id=guild_id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                        team_lookup=team,
                        invoked_with='/team house',
                    )
                )
                await team_emoji_service.public_interaction_sender(interaction)(
                    team_attributes_service.read_message(result, actor=actor)
                )
                return result

            preflight = await team_attributes_service.run_house_preflight(
                member=interaction.user,
                guild=interaction.guild,
                team_lookup=team,
                invoked_with='/team house',
            )
            request = team_attributes_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                team_lookup=team,
                house=house,
                clear=clear,
                expected_team_id=preflight.current.team_id,
                expected_value=preflight.current.value,
                expected_value_present=True,
                team_role_id=preflight.team_role_id,
                team_role_name=preflight.team_role_name,
                team_member_ids=preflight.member_ids,
                native=True,
                invoked_with='/team house',
            )
            result = await team_attributes_service.run_mutation(request)
            try:
                reconciliation = await team_attributes_service.reconcile_tier_roles(
                    interaction.guild,
                    result,
                )
            except Exception:
                logger.exception(
                    'Committed team house %s could not reconcile roles',
                    result.team_id,
                )
                reconciliation = team_attributes_service.TierRoleReconciliation(
                    team_id=result.team_id,
                    attempted=0,
                    updated=0,
                    team_role_missing=True,
                    attribute=team_attributes_workers.TEAM_ATTRIBUTE_HOUSE,
                )
            await team_attributes_service.publish_mutation_result(
                result,
                send=team_emoji_service.public_interaction_sender(interaction),
                actor=actor,
                reconciliation=reconciliation,
            )
            return result
        except team_attributes_workers.TeamAttributeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team house command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team House operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team house failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team House operation failed and rolled back.',
                ephemeral=True,
            )

    @team_group.command(
        name='image',
        description='View or update a team image.',
    )
    @discord.app_commands.autocomplete(
        team=team_attributes_service.autocomplete_teams,
    )
    @discord.app_commands.describe(
        team='Team name; omit this to infer your only team.',
        image='PNG, JPEG, or WebP attachment; omit this to view the image.',
        clear='Explicitly clear the team image.',
    )
    async def team_image_slash(
        self,
        interaction: discord.Interaction,
        team: str | None = None,
        image: discord.Attachment | None = None,
        clear: bool = False,
    ):
        """Read or mod-edit one team image with staged publication."""

        guild_id = getattr(interaction.guild, 'id', None)
        if guild_id is None:
            return await interaction.response.send_message(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = team_image_service.native_access_error(
            interaction.user,
            guild_id,
        )
        if access_error:
            return await interaction.response.send_message(
                access_error,
                ephemeral=True,
            )
        if clear and image is not None:
            return await interaction.response.send_message(
                'Choose either an image replacement or `clear`, not both.',
                ephemeral=True,
            )

        actor = team_image_service.capture_actor(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            current = await team_image_service.run_read(
                team_image_service.build_read_request(
                    member=interaction.user,
                    guild_id=guild_id,
                    team_lookup=team,
                    invoked_with='/team image',
                )
            )
            if image is None and not clear:
                await team_image_service.publish_read(
                    current,
                    send=team_image_service.public_interaction_sender(
                        interaction,
                    ),
                    actor=actor,
                )
                return current

            staged = None
            operation = team_image_workers.TEAM_IMAGE_CLEAR
            if image is not None:
                staged = await team_image_service.stage_attachment(
                    image,
                    team_id=current.team_id,
                )
                operation = team_image_workers.TEAM_IMAGE_LOCAL
            request = team_image_service.build_mutation_request(
                member=interaction.user,
                guild_id=guild_id,
                team_id=current.team_id,
                operation=operation,
                staged_path=(staged.path if staged is not None else None),
                expected_image_url=current.image_url,
                expected_local_digest=current.local_digest,
                native=True,
                invoked_with='/team image',
            )
            result = await team_image_service.run_mutation(
                request,
                staged=staged,
            )
            await team_image_service.publish_mutation_result(
                result,
                send=team_image_service.public_interaction_sender(
                    interaction,
                ),
                actor=actor,
            )
            return result
        except team_image_service.TeamImagePublicationError as exc:
            logger.exception(
                'Committed native team image %s requires publication '
                'reconciliation for guild %s',
                exc.result.team_id,
                guild_id,
            )
            warning = team_image_service.publication_failure_message(
                exc,
                actor=actor,
            )
            try:
                await team_image_service.public_interaction_sender(
                    interaction,
                )(warning)
            except Exception:
                logger.exception(
                    'Committed native team image %s could not publish its '
                    'public reconciliation warning; using an ephemeral '
                    'fallback for guild %s',
                    exc.result.team_id,
                    guild_id,
                )
                return await interaction.followup.send(
                    warning,
                    ephemeral=True,
                )
            return None
        except team_image_workers.TeamImageValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except (team_image_service.TeamImageDownloadError, image_storage.ImageStorageError) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in native team image command for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team image operation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected native team image failure for guild %s',
                guild_id,
            )
            return await interaction.followup.send(
                'Team image operation failed and rolled back.',
                ephemeral=True,
            )
    @commands.command(usage='team_name [image_url or attachment]')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_image(self, ctx, team_name: str, image_url: str = None):
        """*Mod*: Set a team's logo image

        **Example:**
        `[p]team_image Ronin http://www.path.to/image.png`
        `[p]team_image Ronin` with an attached PNG, JPEG, or WebP image
        `[p]team_image Ronin` - Display currently saved image
        """
        attachments = ctx.message.attachments
        if len(attachments) > 1:
            return await ctx.send('Please attach exactly one image.')
        try:
            current = await team_image_service.run_read(
                team_image_service.build_read_request(
                    member=ctx.author,
                    guild_id=ctx.guild.id,
                    team_lookup=team_name,
                    invoked_with=getattr(ctx, 'invoked_with', 'team_image'),
                )
            )
            if not attachments and not image_url:
                return await team_image_service.publish_legacy_read(
                    current,
                    send=ctx.send,
                )

            staged = None
            operation = team_image_workers.TEAM_IMAGE_URL
            if attachments:
                staged = await team_image_service.stage_attachment(
                    attachments[0],
                    team_id=current.team_id,
                )
                operation = team_image_workers.TEAM_IMAGE_LOCAL
            request = team_image_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                team_id=current.team_id,
                operation=operation,
                image_url=(image_url if operation == team_image_workers.TEAM_IMAGE_URL else None),
                staged_path=(staged.path if staged is not None else None),
                expected_image_url=current.image_url,
                expected_local_digest=current.local_digest,
                ignored_url=bool(attachments and image_url),
                native=False,
                invoked_with=getattr(ctx, 'invoked_with', 'team_image'),
            )
            result = await team_image_service.run_mutation(
                request,
                staged=staged,
            )
            return await team_image_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_image_service.TeamImagePublicationError as exc:
            logger.exception(
                'Committed prefix team image %s requires publication '
                'reconciliation for guild %s',
                exc.result.team_id,
                ctx.guild.id,
            )
            return await ctx.send(
                team_image_service.publication_failure_message(
                    exc,
                    actor=team_image_service.capture_actor(ctx.author),
                )
            )
        except team_image_workers.TeamImageLookupError as exc:
            return await ctx.send(
                f'{exc}\nExample: `{ctx.prefix}team_image name '
                'http://url_to_image.png`'
            )
        except team_image_workers.TeamImageValidationError as exc:
            return await ctx.send(str(exc))
        except (team_image_service.TeamImageDownloadError, image_storage.ImageStorageError) as exc:
            if image_url and not attachments:
                return await ctx.send(
                    f'{exc} Example: `{ctx.prefix}team_image name '
                    'http://url_to_image.png`'
                )
            return await ctx.send(f'Unable to save team image: {exc}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team image for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team image operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team image failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team image operation failed and rolled back.')

    @commands.command(usage='old_name new_name')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_name(self, ctx, old_team_name: str, new_team_name: str):
        """*Mod*: Change a team's name
        The team should have a Role with an identical name.
        Old name doesn't need to be precise, but new name does. Include quotes if it's more than one word.
        **Example:**
        `[p]team_name Amazeballs "The Wowbaggers"`
        """

        try:
            request = team_attributes_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_NAME,
                team_lookup=old_team_name,
                name=new_team_name,
                native=False,
                invoked_with=ctx.invoked_with,
                prefix=ctx.prefix,
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_attributes_workers.TeamAttributeValidationError as ex:
            return await ctx.send(
                f'{ex}\nExample: `{ctx.prefix}team_name "Current name" '
                '"New Team Name"`'
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure reading or updating team name for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team name operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team name failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team name operation failed and rolled back.')

    @commands.command(usage='team_name server_id')
    @settings.is_mod_check()
    @settings.guild_has_setting(setting_name='allow_teams')
    async def team_server(self, ctx, team_name: str = None, team_server_id: str = None):
        """*Mod*: Change a team's external server

        **Example:**
        `[p]team_server Ronin` Check existing server setting
        `[p]team_server Ronin 572885616656908288` Update the server setting
        """
        # TODO: better input handling (display server_id if new ID not provided)
        example = (
            f'`{ctx.prefix}team_server "Team Name" '
            '447883341463814144` (Use the raw numeric ID of the team\'s server)'
        )
        if not team_name:
            return await ctx.send(f'Example: {example}')

        if not team_server_id:
            try:
                result = await team_attributes_service.run_read(
                    team_attributes_service.build_read_request(
                        member=ctx.author,
                        guild_id=ctx.guild.id,
                        attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                        team_lookup=team_name,
                        invoked_with=ctx.invoked_with,
                    )
                )
            except team_attributes_workers.TeamAttributeValidationError as ex:
                return await ctx.send(f'{ex}\nExample: {example}')
            except peewee.PeeweeException:
                logger.exception(
                    'Database failure reading team server for guild %s',
                    ctx.guild.id,
                )
                return await ctx.send(
                    'Team server operation failed and rolled back.'
                )
            return await ctx.send(
                team_attributes_service.legacy_server_read_message(result)
            )

        try:
            server_id = int(team_server_id)
        except (TypeError, ValueError):
            return await ctx.send(
                f'Server ID was invalid.\nExample: {example}'
            )

        try:
            request = team_attributes_service.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                attribute=team_attributes_workers.TEAM_ATTRIBUTE_SERVER,
                team_lookup=team_name,
                server_id=server_id,
                native=False,
                invoked_with=ctx.invoked_with,
                prefix=ctx.prefix,
            )
            result = await team_attributes_service.run_mutation(request)
            await team_attributes_service.publish_mutation_result(
                result,
                send=ctx.send,
            )
        except team_attributes_workers.TeamAttributeValidationError as ex:
            return await ctx.send(f'{ex}\nExample: {example}')
        except peewee.PeeweeException:
            logger.exception(
                'Database failure updating team server for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team server operation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected team server failure for guild %s',
                ctx.guild.id,
            )
            return await ctx.send('Team server operation failed and rolled back.')

    @commands.command(hidden=True)
    @commands.is_owner()
    async def recalc_games_from(self, ctx, *, arg: str = None):
        """*Owner*: Recalculate games from a specific timestamp

        Give a game ID, and the bot will *recalculate_elo_since* all games completed after that game was completed.
        """

        try:
            game_id = int(arg)
        except (TypeError, ValueError):
            return await ctx.send(f'no game found for id {arg}')

        await ctx.send('This may take a while...')
        try:
            async with ctx.typing():
                timestamp = await self._run_recalculation_job(
                    game_id=game_id,
                    requester_id=ctx.author.id,
                    requester_name=ctx.author.display_name,
                )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except elo_workers.RecalculationValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception(
                'Database failure recalculating games from %s', game_id
            )
            return await ctx.send('Database recalculation failed and rolled back.')
        except Exception:
            logger.exception(
                'Unexpected failure recalculating games from %s', game_id
            )
            return await ctx.send('Database recalculation failed and rolled back.')

        await ctx.send(f'DB has been refreshed from {timestamp} onward')

    @elo_group.command(
        name='recalculate',
        description='Recalculate ELO from a completed game onward.',
    )
    @discord.app_commands.describe(
        game_id='Completed game that establishes the recalculation timestamp.',
        confirm='Must be true to start this destructive maintenance job.',
    )
    async def recalc_games_from_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        confirm: bool,
    ):
        if interaction.user.id != settings.owner_id:
            return await interaction.response.send_message(
                'Only the bot owner can use this command.',
                ephemeral=True,
            )
        if not confirm:
            return await interaction.response.send_message(
                'Recalculation was not started. Set `confirm` to true after '
                'checking the game ID.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            timestamp = await self._run_recalculation_job(
                game_id=game_id,
                requester_id=interaction.user.id,
                requester_name=interaction.user.display_name,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await interaction.followup.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.',
                ephemeral=True,
            )
        except elo_workers.RecalculationValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure recalculating games from %s from slash',
                game_id,
            )
            return await interaction.followup.send(
                'Database recalculation failed and rolled back.',
                ephemeral=True,
            )
        except Exception:
            logger.exception(
                'Unexpected failure recalculating games from %s from slash',
                game_id,
            )
            return await interaction.followup.send(
                'Database recalculation failed and rolled back.',
                ephemeral=True,
            )

        await interaction.followup.send(
            f'DB has been refreshed from {timestamp} onward.',
            ephemeral=True,
        )

    @elo_group.command(
        name='status',
        description='Show the currently running ELO mutation job.',
    )
    async def elo_job_status_slash(
        self,
        interaction: discord.Interaction,
    ):
        if not settings.is_staff(interaction.user):
            return await interaction.response.send_message(
                'You do not have permission to use this command.',
                ephemeral=True,
            )
        await interaction.response.send_message(
            format_elo_job_status(settings.elo_job_coordinator.active_job),
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(administration(bot))
