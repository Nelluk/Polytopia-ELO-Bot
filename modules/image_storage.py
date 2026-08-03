"""Local storage and Discord attachment helpers for team and house images."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
import os
from pathlib import Path
import tempfile
from typing import Optional, Union
from urllib.parse import urlparse
import warnings

import discord
from PIL import Image, ImageOps, UnidentifiedImageError


logger = logging.getLogger('polybot.' + __name__)


# Tests may replace this override. Runtime code otherwise resolves the path
# lazily from the central profile, so importing this helper has no config or
# filesystem side effects.
IMAGE_ROOT = None
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_DIMENSION = 1024
ALLOWED_FORMATS = {'PNG', 'JPEG', 'WEBP'}
MANAGED_ATTACHMENT_PREFIX = 'team-logo-'

_update_locks = {}


def image_root() -> Path:
    if IMAGE_ROOT is not None:
        return Path(IMAGE_ROOT)
    from runtime_config import get_runtime_profile
    return get_runtime_profile().image_root


class ImageStorageError(ValueError):
    """Raised when an uploaded or configured image cannot be accepted."""


@dataclass(frozen=True)
class LocalImageAttachment:
    """A local image and the filename used to reference it from an embed."""

    path: Path
    filename: str

    @property
    def embed_url(self) -> str:
        return f'attachment://{self.filename}'

    def to_discord_file(self) -> discord.File:
        return discord.File(self.path, filename=self.filename)


def ensure_image_directories() -> None:
    """Create the runtime image directories if needed."""

    root = image_root()
    for directory in (root / 'teams', root / 'houses'):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)


def team_image_path(team_id: int) -> Path:
    return image_root() / 'teams' / f'{int(team_id)}.png'


def house_image_path(house_id: int) -> Path:
    return image_root() / 'houses' / f'{int(house_id)}.png'


def entity_image_path(kind: str, entity_id: int) -> Path:
    if kind == 'team':
        return team_image_path(entity_id)
    if kind == 'house':
        return house_image_path(entity_id)
    raise ValueError(f'Unsupported image entity kind: {kind}')


def update_lock(kind: str, entity_id: int) -> asyncio.Lock:
    """Return an in-process lock for updates to one entity image."""

    key = (kind, int(entity_id))
    lock = _update_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _update_locks[key] = lock
    return lock


def validate_http_url(url: str) -> str:
    """Validate and return a direct HTTP(S) URL."""

    url = (url or '').strip()
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ImageStorageError('Image URL must be a complete http:// or https:// URL.')
    return url


def resolve_image(kind: str, entity) -> Optional[Union[Path, str]]:
    """Return the preferred image source: local file, legacy URL, or None."""

    local_path = entity_image_path(kind, entity.id)
    if local_path.is_file():
        return local_path
    return entity.image_url or None


def local_attachment(kind: str, entity) -> Optional[LocalImageAttachment]:
    """Return a Discord attachment descriptor when an entity has a local image."""

    path = entity_image_path(kind, entity.id)
    if not path.is_file():
        return None
    return LocalImageAttachment(path=path, filename=f'{kind}-logo-{entity.id}.png')


def set_entity_thumbnail(embed: discord.Embed, kind: str, entity) -> Optional[LocalImageAttachment]:
    """Set an entity thumbnail and return the local file descriptor if required."""

    attachment = local_attachment(kind, entity)
    if attachment:
        embed.set_thumbnail(url=attachment.embed_url)
        return attachment
    if entity.image_url:
        embed.set_thumbnail(url=entity.image_url)
    return None


def game_local_attachment(game) -> Optional[LocalImageAttachment]:
    """Return the local winning-team logo used by a completed game embed."""

    if game.is_completed != 1:
        return None
    winner = game.winner
    if not winner or not winner.team:
        return None
    if len(winner.lineup) == 1:
        return None
    return local_attachment('team', winner.team)


async def send_game_embed(destination, game, *, embed, content=None, **kwargs):
    """Send a game embed, attaching its local thumbnail when necessary."""

    attachment = game_local_attachment(game)
    if attachment:
        kwargs['file'] = attachment.to_discord_file()
    return await destination.send(embed=embed, content=content, **kwargs)


async def edit_game_embed(message, game, *, embed, content=None):
    """Edit a game embed while replacing only bot-managed logo attachments."""

    retained = [
        attachment for attachment in message.attachments
        if not attachment.filename.startswith(MANAGED_ATTACHMENT_PREFIX)
    ]
    attachment = game_local_attachment(game)
    if attachment:
        retained.append(attachment.to_discord_file())
    return await message.edit(embed=embed, content=content, attachments=retained)


def _normalise_image(data: bytes, destination: Path) -> None:
    """Validate and write canonical PNG image data atomically."""

    logger.debug(
        'Validating image upload: destination=%s bytes=%d',
        destination,
        len(data),
    )
    if not data:
        raise ImageStorageError('The attached image is empty.')
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageStorageError('The attached image is larger than 5 MiB.')

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                image_format = (probe.format or '').upper()
                width, height = probe.size
                is_animated = getattr(probe, 'is_animated', False) or getattr(probe, 'n_frames', 1) > 1
                probe.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageStorageError('The attached image is too large to decode safely.')
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        logger.warning(
            'Image validation failed: destination=%s bytes=%d error=%s',
            destination,
            len(data),
            exc,
        )
        raise ImageStorageError('The attachment is not a valid supported image.') from exc

    pixel_count = width * height
    logger.debug(
        'Detected image upload: destination=%s format=%s dimensions=%dx%d '
        'pixels=%d animated=%s bytes=%d',
        destination,
        image_format,
        width,
        height,
        pixel_count,
        is_animated,
        len(data),
    )
    if image_format not in ALLOWED_FORMATS:
        raise ImageStorageError('Image must be a static PNG, JPEG, or WebP file.')
    if width <= 0 or height <= 0 or pixel_count > MAX_IMAGE_PIXELS:
        logger.warning(
            'Image rejected for dimensions: destination=%s format=%s '
            'dimensions=%dx%d pixels=%d limit=%d bytes=%d',
            destination,
            image_format,
            width,
            height,
            pixel_count,
            MAX_IMAGE_PIXELS,
            len(data),
        )
        raise ImageStorageError(
            f'Image dimensions {width}x{height} ({pixel_count:,} pixels) '
            f'exceed the {MAX_IMAGE_PIXELS:,}-pixel limit.'
        )
    if is_animated:
        raise ImageStorageError('Animated images are not supported.')

    try:
        with Image.open(BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
            image = image.convert('RGBA')
            image.thumbnail(
                (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                Image.Resampling.LANCZOS,
            )

            destination.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f'.{destination.stem}-',
                suffix='.tmp',
                dir=str(destination.parent),
            )
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                image.save(temp_path, format='PNG', optimize=True)
                if temp_path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise ImageStorageError('The normalised image is larger than 5 MiB.')
                logger.debug(
                    'Normalised image upload: destination=%s dimensions=%dx%d '
                    'bytes=%d',
                    destination,
                    image.width,
                    image.height,
                    temp_path.stat().st_size,
                )
                os.chmod(temp_path, 0o640)
                os.replace(temp_path, destination)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
    except ImageStorageError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageStorageError('The image could not be normalised and stored.') from exc


async def save_attachment(attachment: discord.Attachment, kind: str, entity_id: int) -> Path:
    """Download, validate, and atomically store a Discord image attachment."""

    logger.debug(
        'Reading image attachment: kind=%s entity_id=%d filename=%r '
        'reported_size=%s content_type=%r attachment_id=%s',
        kind,
        entity_id,
        getattr(attachment, 'filename', None),
        getattr(attachment, 'size', None),
        getattr(attachment, 'content_type', None),
        getattr(attachment, 'id', None),
    )
    if attachment.size and attachment.size > MAX_UPLOAD_BYTES:
        logger.warning(
            'Image attachment rejected before download: kind=%s entity_id=%d '
            'filename=%r reported_size=%d byte_limit=%d content_type=%r',
            kind,
            entity_id,
            getattr(attachment, 'filename', None),
            attachment.size,
            MAX_UPLOAD_BYTES,
            getattr(attachment, 'content_type', None),
        )
        raise ImageStorageError('The attached image is larger than 5 MiB.')
    try:
        data = await attachment.read()
    except discord.HTTPException:
        logger.exception(
            'Image attachment download failed: kind=%s entity_id=%d '
            'filename=%r reported_size=%s content_type=%r attachment_id=%s',
            kind,
            entity_id,
            getattr(attachment, 'filename', None),
            getattr(attachment, 'size', None),
            getattr(attachment, 'content_type', None),
            getattr(attachment, 'id', None),
        )
        raise
    destination = entity_image_path(kind, entity_id)
    try:
        await asyncio.to_thread(_normalise_image, data, destination)
    except ImageStorageError as exc:
        logger.warning(
            'Image attachment rejected: kind=%s entity_id=%d filename=%r '
            'reported_size=%s downloaded_size=%d content_type=%r error=%s',
            kind,
            entity_id,
            getattr(attachment, 'filename', None),
            getattr(attachment, 'size', None),
            len(data),
            getattr(attachment, 'content_type', None),
            exc,
        )
        raise
    logger.info(
        'Stored image attachment: kind=%s entity_id=%d filename=%r '
        'downloaded_size=%d destination=%s',
        kind,
        entity_id,
        getattr(attachment, 'filename', None),
        len(data),
        destination,
    )
    return destination


def activate_remote_url(entity, kind: str, url: str) -> None:
    """Store a URL and remove the local override with rollback on DB failure."""

    url = validate_http_url(url)
    path = entity_image_path(kind, entity.id)
    backup_path = None
    if path.exists():
        fd, backup_name = tempfile.mkstemp(
            prefix=f'.{path.stem}-',
            suffix='.url-backup',
            dir=str(path.parent),
        )
        os.close(fd)
        backup_path = Path(backup_name)
        backup_path.unlink()
        os.replace(path, backup_path)

    old_url = entity.image_url
    entity.image_url = url
    try:
        entity.save()
    except Exception:
        entity.image_url = old_url
        if backup_path and backup_path.exists():
            os.replace(backup_path, path)
        raise
    else:
        if backup_path and backup_path.exists():
            backup_path.unlink()
