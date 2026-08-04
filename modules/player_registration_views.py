"""Modal presentation for account-wide canonical player registration."""

from __future__ import annotations

import logging

import discord
import peewee

from modules import player_registration, player_registration_workers as workers


logger = logging.getLogger('polybot.' + __name__)


async def _send_private(interaction, content: str) -> None:
    response = getattr(interaction, 'response', None)
    is_done = getattr(response, 'is_done', None)
    if callable(is_done) and is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await response.send_message(content, ephemeral=True)


class PlayerRegistrationModal(
    discord.ui.Modal,
    title='Register account-wide Polytopia name',
):
    """Collect exactly one canonical Polytopia name field."""

    canonical_name = discord.ui.TextInput(
        label='Canonical Polytopia name (account-wide)',
        placeholder='The name shown in your Polytopia profile',
        min_length=1,
        max_length=workers.MAX_NAME_LENGTH,
        required=True,
    )

    def __init__(
        self,
        *,
        guild_id: int,
        requester_id: int,
        target_snapshot: workers.MemberSnapshot,
    ):
        super().__init__()
        self.guild_id = int(guild_id)
        self.requester_id = int(requester_id)
        self.target_snapshot = target_snapshot
        # This is presentation only. Submission still re-resolves the
        # captured primitive ID and rebuilds the worker request from the
        # current guild member; display text is never an authority source.
        selected_name = discord.utils.escape_markdown(
            discord.utils.escape_mentions(target_snapshot.display_name),
            as_needed=True,
        )
        self.selected_target_text = (
            f'**Selected member:** {selected_name}\n'
            'This is the member whose account-wide name will be updated.'
        )
        self.add_item(discord.ui.TextDisplay(self.selected_target_text))
        self._submitted = False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _send_private(
                interaction,
                'This registration modal was already submitted. Run '
                '`/player register` again for a fresh form.',
            )
            return
        if int(interaction.user.id) != self.requester_id:
            await _send_private(
                interaction,
                'Only the member who opened this registration form can submit it.',
            )
            return

        self._submitted = True
        try:
            target = self.target_snapshot
            guild = getattr(interaction, 'guild', None)
            get_member = getattr(guild, 'get_member', None)
            if callable(get_member):
                current_target = get_member(target.discord_id)
                if current_target is not None:
                    target = current_target

            request = player_registration.build_request(
                actor=interaction.user,
                guild_id=self.guild_id,
                canonical_name=str(self.canonical_name.value or ''),
                target=target if not isinstance(
                    target, workers.MemberSnapshot
                ) else None,
                target_snapshot=(
                    target
                    if isinstance(target, workers.MemberSnapshot)
                    else None
                ),
                invoked_with='player register',
            )
        except (
            workers.PlayerRegistrationValidationError,
            workers.PlayerRegistrationPermissionError,
            ValueError,
        ) as exc:
            await _send_private(interaction, str(exc))
            return

        # Keep the response private while the bounded ordinary-write worker
        # runs. A successful committed result is published below.
        await interaction.response.defer(ephemeral=True)
        try:
            result = await workers.run_player_registration(request)
        except (
            workers.PlayerRegistrationValidationError,
            workers.PlayerRegistrationPermissionError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.warning('Player registration failed after modal defer: %s', exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception('Unexpected player registration failure')
            await interaction.followup.send(
                'Registration failed before it could be confirmed. No public '
                'success message was sent.',
                ephemeral=True,
            )
            return

        try:
            await player_registration.public_interaction_sender(interaction)(
                player_registration.success_message(request, result),
            )
        except Exception:
            logger.exception(
                'Canonical player registration committed but public output '
                'failed'
            )
            try:
                await interaction.followup.send(
                    'Registration was saved, but the public confirmation could '
                    'not be posted. An operator can verify the audit entry.',
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Could not send registration reconciliation')
