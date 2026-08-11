"""Discord modal for structured development beta feedback."""

from __future__ import annotations

import logging
from typing import Any

import discord

from modules import beta_feedback
from modules import staff_help
from runtime_config import get_runtime_profile


logger = logging.getLogger('polybot.' + __name__)


def _response_done(interaction: discord.Interaction) -> bool:
    response = getattr(interaction, 'response', None)
    is_done = getattr(response, 'is_done', None)
    return bool(is_done()) if callable(is_done) else False


async def _send_private(interaction: discord.Interaction, content: str) -> None:
    if _response_done(interaction):
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class StaffHelpModal(discord.ui.Modal):
    """Requester-bound modal with bounded native Components v2 fields."""

    category = discord.ui.Label(
        text='Category',
        description='What kind of report is this?',
        component=discord.ui.RadioGroup(
            custom_id='staffhelp-category',
            required=True,
            options=(
                discord.RadioGroupOption(label='Help', value='help'),
                discord.RadioGroupOption(label='Bug', value='bug'),
                discord.RadioGroupOption(label='Feature', value='feature'),
            ),
        ),
    )
    summary = discord.ui.Label(
        text='Short summary',
        description='Keep this concise so staff can triage it quickly.',
        component=discord.ui.TextInput(
            custom_id='staffhelp-summary',
            max_length=beta_feedback.MAX_SUMMARY_LENGTH,
            required=True,
            placeholder='Briefly describe the request',
        ),
    )
    details = discord.ui.Label(
        text='Detailed description',
        description='Include steps, expected behavior, and what happened.',
        component=discord.ui.TextInput(
            custom_id='staffhelp-details',
            style=discord.TextStyle.paragraph,
            max_length=beta_feedback.MAX_DETAILS_LENGTH,
            required=True,
            placeholder='Describe the help request, bug, or feature idea',
        ),
    )
    context = discord.ui.Label(
        text='Related command, game, or context (optional)',
        component=discord.ui.TextInput(
            custom_id='staffhelp-context',
            max_length=beta_feedback.MAX_CONTEXT_LENGTH,
            required=False,
            placeholder='For example: /game show 42500 or what you tried',
        ),
    )
    files = discord.ui.Label(
        text='Attachments (optional)',
        description='Up to 10 PNG/JPEG/WebP/GIF/PDF/Markdown/text files.',
        component=discord.ui.FileUpload(
            custom_id='staffhelp-attachments',
            required=False,
            max_values=beta_feedback.MAX_ATTACHMENTS,
        ),
    )

    def __init__(
            self,
            bot: Any,
            *,
            requester_id: int,
            guild_id: int,
            channel_id: int,
            profile: Any | None = None):
        self.profile = profile or get_runtime_profile()
        title = (
            'Staff help / beta feedback'
            if self.profile.environment == 'development'
            else 'Staff help'
        )
        super().__init__(title=title, timeout=300)
        self.bot = bot
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self._submitted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Keep a modal submission bound to its opener and original channel."""

        interaction_user_id = getattr(getattr(interaction, 'user', None), 'id', None)
        interaction_guild_id = getattr(interaction, 'guild_id', None)
        interaction_channel_id = getattr(interaction, 'channel_id', None)
        if interaction_user_id != self.requester_id:
            await _send_private(
                interaction,
                'Only the member who opened this staff-help form can submit it.',
            )
            return False
        if interaction_guild_id != self.guild_id or interaction_channel_id != self.channel_id:
            await _send_private(
                interaction,
                'This staff-help form is limited to its original server channel.',
            )
            return False
        if self._submitted:
            await _send_private(
                interaction,
                'This staff-help form was already submitted. Run `/staffhelp` again for a new form.',
            )
            return False
        return True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Modal dispatch normally calls interaction_check first; repeat the
        # checks here because tests and alternate dispatchers may call on_submit
        # directly.
        if not await self.interaction_check(interaction):
            return
        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            category = getattr(getattr(self.category, 'component', None), 'value', None)
            summary = getattr(getattr(self.summary, 'component', None), 'value', None)
            details = getattr(getattr(self.details, 'component', None), 'value', None)
            context = getattr(getattr(self.context, 'component', None), 'value', None)
            uploaded = getattr(getattr(self.files, 'component', None), 'values', ()) or ()
            captured_attachments = await beta_feedback.capture_attachments(tuple(uploaded))
            game_id, command_reference = beta_feedback._reference_fields(
                beta_feedback._optional_text(context, beta_feedback.MAX_CONTEXT_LENGTH)
            )
            draft = beta_feedback.build_report_draft(
                category=category,
                summary=summary,
                details=details,
                context=context,
                requester_id=interaction.user.id,
                requester_display_name=(
                    getattr(interaction.user, 'display_name', None)
                    or getattr(interaction.user, 'name', None)
                    or f'user-{interaction.user.id}'
                ),
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                source='slash',
                attachments=captured_attachments,
                game_id=game_id,
                command_reference=command_reference,
            )
            result = await staff_help.submit(
                self.bot,
                draft,
                profile=self.profile,
            )
        except beta_feedback.FeedbackValidationError as exc:
            noun = 'report was not stored' if (
                self.profile.environment == 'development'
            ) else 'message was not accepted'
            await interaction.followup.send(
                f'Your {noun}: {exc}',
                ephemeral=True,
            )
            return
        except beta_feedback.FeedbackStorageError:
            await interaction.followup.send(
                'Your report could not be recorded. No report ID was issued; please try again later.',
                ephemeral=True,
            )
            return
        except staff_help.StaffHelpConfigurationError:
            await interaction.followup.send(
                'Your message could not be sent because staff help is not '
                'configured for this server. Please ping a server staff member directly.',
                ephemeral=True,
            )
            return
        except staff_help.StaffHelpDeliveryError:
            await interaction.followup.send(
                'Your message could not be sent to server staff. '
                'Please try again later or ping a server staff member directly.',
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception(
                'Unexpected structured staffhelp failure (requester=%s guild=%s channel=%s).',
                self.requester_id,
                self.guild_id,
                self.channel_id,
            )
            await interaction.followup.send(
                'Your report could not be completed. No report ID was issued; please try again later.',
                ephemeral=True,
            )
            return

        if result.environment == 'production':
            message = (
                'Your message has been sent to server staff. '
                'Please wait patiently or submit another report with additional information.'
            )
        elif result.delivered:
            message = (
                f'Your report was recorded as `{result.report_id}`. '
                'Staff will review it.'
            )
        else:
            message = (
                f'Your report was recorded as `{result.report_id}`, '
                'but the staff relay is temporarily unavailable. Staff can reconcile it from the beta store.'
            )
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            # The authoritative append and any relay attempt already
            # completed.  Do not replace the real report ID with a false
            # "not stored" acknowledgement if Discord rejects the followup.
            if result.stored:
                logger.exception(
                    'Structured staffhelp acknowledgement failed after report commit '
                    '(report_id=%s requester=%s guild=%s channel=%s); '
                    'reconcile from the beta store.',
                    result.report_id,
                    self.requester_id,
                    self.guild_id,
                    self.channel_id,
                )
            else:
                logger.exception(
                    'Production staffhelp acknowledgement failed after Discord relay '
                    '(message=%s requester=%s guild=%s channel=%s); do not infer '
                    'that delivery failed.',
                    result.relay_message_id,
                    self.requester_id,
                    self.guild_id,
                    self.channel_id,
                )
