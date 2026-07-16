import io
import re
import asyncio
import logging
import datetime
from collections import defaultdict, deque
from dataclasses import dataclass

import discord
from discord.ext import commands
from PIL import Image

import settings
from modules import utilities

logger = logging.getLogger('polybot.' + __name__)

# ---------------------------------------------------------------------------
# Cross-channel scam/spam detection.
#
# Derived from no-scams by seriaati (https://github.com/seriaati/no-scams),
# licensed GPL-3.0, based on commit 203c4eee158fb6807e862633f96d949b096aab79.
# Modified 2026-06-24 by the PolyChampions maintainers: reimplemented the image
# hash on Pillow (dropping the imagehash dependency), gated the same-content
# rule on a URL/invite being present, added per-user locking, attachment size/
# concurrency limits, off-loop image decoding, and key TTL eviction.
# This file, like the upstream project, is distributed under the GNU GPL v3.
# ---------------------------------------------------------------------------

MAX_MESSAGE_NUM = 3                 # consecutive messages needed to trigger
CONSECUTIVE_WINDOW_MINUTES = 2      # ...within this many minutes
TIMEOUT_MINUTES = 15               # how long to time the scammer out
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # skip hashing attachments larger than this (compressed)
MAX_IMAGE_PIXELS = 24_000_000      # skip decoding images whose decoded size exceeds this (bomb guard)
MAX_IMAGES_PER_MESSAGE = 4         # cap attachments hashed per message
HASH_CONCURRENCY = 4               # cap simultaneous decode/hash jobs cog-wide
EVICT_EVERY = 500                  # sweep stale tracking keys every N messages

DISCORD_INVITE = re.compile(r'(?:https?://)?(?:www\.)?(?:discord(?:\.app|app)?\.com/invite|discord\.gg|discord\.com/events)/\S+', re.IGNORECASE)
URL_RE = re.compile(r'https?://\S+')


def _contains_url(content: str) -> bool:
    return bool(URL_RE.search(content) or DISCORD_INVITE.search(content))


def _average_hash(data: bytes) -> str:
    # Matches imagehash.average_hash: 8x8 greyscale, bit set when pixel > mean.
    img = Image.open(io.BytesIO(data))
    # Reject decompression bombs by header dimensions before forcing a full decode.
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise ValueError(f'image too large to hash: {img.width}x{img.height}')
    img = img.convert('L').resize((8, 8), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    return ''.join('1' if p > avg else '0' for p in pixels)


@dataclass
class TrackedMessage:
    id: int
    channel_id: int
    content: str
    image_hashes: frozenset
    created_at: datetime.datetime


def _all_same(lst) -> bool:
    if len(lst) <= 1 or not lst[0]:
        return False
    return all(x == lst[0] for x in lst)


def _all_different(lst) -> bool:
    if len(lst) <= 1:
        return True
    return len(set(lst)) == len(lst)


class AntiScam(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._messages = defaultdict(lambda: deque(maxlen=MAX_MESSAGE_NUM))  # (guild_id, author_id) -> messages
        self._locks = defaultdict(asyncio.Lock)  # (guild_id, author_id) -> lock
        self._actioned = {}  # (guild_id, author_id) -> suppression expiry datetime
        self._hash_sem = asyncio.Semaphore(HASH_CONCURRENCY)
        self._msg_count = 0

    async def _build_tracked(self, message: discord.Message) -> TrackedMessage:
        hashes = []
        attempted = 0
        for attachment in message.attachments:
            if attempted >= MAX_IMAGES_PER_MESSAGE:
                break
            ctype = attachment.content_type or ''
            if not ctype.startswith('image/'):
                continue
            if not attachment.filename.lower().endswith(IMAGE_EXTENSIONS):
                continue
            if attachment.size and attachment.size > MAX_IMAGE_BYTES:
                continue
            attempted += 1  # count every image we try, so corrupt files can't bypass the cap
            try:
                # Hold the semaphore across the download too, so we never buffer more
                # than HASH_CONCURRENCY attachments in memory at once.
                async with self._hash_sem:
                    data = await attachment.read()
                    hashes.append(await asyncio.to_thread(_average_hash, data))
            except Exception:
                logger.warning(f'AntiScam: could not hash attachment {attachment.filename}')
        return TrackedMessage(
            id=message.id,
            channel_id=message.channel.id,
            content=message.content,
            image_hashes=frozenset(hashes),
            created_at=message.created_at,
        )

    def _is_scam(self, messages) -> bool:
        if len(messages) < MAX_MESSAGE_NUM:
            return False

        window = max(m.created_at for m in messages) - min(m.created_at for m in messages)
        if window > datetime.timedelta(minutes=CONSECUTIVE_WINDOW_MINUTES):
            return False
        if not _all_different([m.channel_id for m in messages]):
            return False

        # Stricter than upstream: a repeated text message must carry a link/invite
        # to count, so ordinary chatter ("gg" x3) does not trip the filter.
        same_content = _all_same([m.content for m in messages]) and all(_contains_url(m.content) for m in messages)
        same_images = _all_same([m.image_hashes for m in messages])
        all_have_images = all(m.image_hashes for m in messages)
        all_no_text = all(not m.content.strip() for m in messages)

        return same_content or same_images or (all_have_images and all_no_text)

    def _maybe_evict(self, now: datetime.datetime) -> None:
        self._msg_count += 1
        if self._msg_count % EVICT_EVERY != 0:
            return
        cutoff = now - datetime.timedelta(minutes=CONSECUTIVE_WINDOW_MINUTES)
        stale = [
            key for key, dq in self._messages.items()
            if not dq or dq[-1].created_at < cutoff
        ]
        for key in stale:
            del self._messages[key]
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                del self._locks[key]
        for key in [k for k, expiry in self._actioned.items() if expiry < now]:
            del self._actioned[key]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.webhook_id is not None:
            return
        if message.guild.id not in settings.config:
            return
        # NOTE: staff are exempt; a compromised staff account would bypass this filter.
        if isinstance(message.author, discord.Member) and settings.is_staff(message.author):
            return

        key = (message.guild.id, message.author.id)

        # Per-user lock serializes tracking + enforcement so concurrent messages
        # (e.g. while attachments download) cannot race past each other.
        async with self._locks[key]:
            # Suppression window: messages already in flight when the user was timed
            # out still arrive as events. Delete them and skip tracking so the burst
            # neither survives nor re-triggers another timeout.
            expiry = self._actioned.get(key)
            if expiry is not None:
                if message.created_at < expiry:
                    await self._delete(message.channel, message.id, message.guild)
                    return
                del self._actioned[key]

            self._messages[key].append(await self._build_tracked(message))
            self._maybe_evict(message.created_at)

            if not self._is_scam(self._messages[key]):
                return

            logger.info(f'AntiScam: scam detected from {message.author} ({message.author.id}) in {message.guild.name}')
            self._actioned[key] = message.created_at + datetime.timedelta(minutes=TIMEOUT_MINUTES)
            try:
                for tracked in list(self._messages[key]):
                    channel = message.guild.get_channel_or_thread(tracked.channel_id)
                    if channel is not None:
                        await self._delete(channel, tracked.id, message.guild)
                await self._punish(message)
            finally:
                self._messages[key].clear()

    async def _delete(self, channel, message_id: int, guild) -> None:
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass  # already gone
        except discord.Forbidden:
            logger.warning(f'AntiScam: no permission to delete message {message_id} in {guild.name}')
        except discord.HTTPException as e:
            logger.warning(f'AntiScam: failed to delete message {message_id} in {guild.name}: {e}')

    async def _punish(self, message: discord.Message) -> None:
        if not isinstance(message.author, discord.Member):
            return
        try:
            await message.author.timeout(
                datetime.timedelta(minutes=TIMEOUT_MINUTES), reason='Sending scam messages')
        except discord.Forbidden:
            logger.warning(f'AntiScam: no permission to timeout {message.author}')
            return
        except discord.HTTPException as e:
            logger.warning(f'AntiScam: failed to timeout {message.author}: {e}')
            return

        logger.info(f'AntiScam: timed out {message.author} for {TIMEOUT_MINUTES}m')
        try:
            await utilities.send_to_log_channel(
                message.guild,
                f'Anti-scam: timed out {message.author.mention} ({message.author}) for {TIMEOUT_MINUTES} '
                f'minutes for cross-posting scam messages in {message.channel.mention}.')
        except discord.HTTPException as e:
            logger.warning(f'AntiScam: could not post timeout notice for {message.guild.name}: {e}')


async def setup(bot):
    await bot.add_cog(AntiScam(bot))
