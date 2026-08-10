import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import warnings

warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)

import discord
from PIL import Image

from modules import image_storage


def image_bytes(image_format='PNG', size=(40, 30), mode='RGBA'):
    image = Image.new(mode, size, (255, 0, 0, 128) if mode == 'RGBA' else (255, 0, 0))
    stream = BytesIO()
    image.save(stream, format=image_format)
    return stream.getvalue()


class FakeAttachment:
    def __init__(self, data, reported_size=None):
        self.data = data
        self.size = len(data) if reported_size is None else reported_size

    async def read(self):
        return self.data


class FakeEntity:
    def __init__(self, entity_id, image_url=None, save_error=None):
        self.id = entity_id
        self.image_url = image_url
        self.save_error = save_error

    def save(self):
        if self.save_error:
            raise self.save_error


class FakeDestination:
    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


class FakeMessage:
    def __init__(self, attachments):
        self.attachments = attachments
        self.calls = []

    async def edit(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.image_root_patch = mock.patch.object(
            image_storage, 'IMAGE_ROOT', Path(self.tempdir.name)
        )
        self.image_root_patch.start()
        image_storage.ensure_image_directories()

    def tearDown(self):
        self.image_root_patch.stop()
        self.tempdir.cleanup()

    def test_normalises_supported_image_to_bounded_png(self):
        destination = image_storage.team_image_path(12)
        image_storage._normalise_image(
            image_bytes('JPEG', size=(1600, 800), mode='RGB'),
            destination,
        )

        self.assertTrue(destination.is_file())
        with Image.open(destination) as stored:
            self.assertEqual(stored.format, 'PNG')
            self.assertEqual(stored.mode, 'RGBA')
            self.assertEqual(stored.size, (1024, 512))

    def test_staging_keeps_visible_image_until_publish_and_cleanup_is_idempotent(self):
        destination = image_storage.team_image_path(12)
        original = image_bytes('PNG', size=(20, 20))
        image_storage._normalise_image(original, destination)
        visible_before = destination.read_bytes()

        staged = image_storage.stage_normalised_image(
            image_bytes('JPEG', size=(30, 10), mode='RGB'),
            'team',
            12,
        )
        self.assertTrue(Path(staged.path).is_file())
        self.assertEqual(destination.read_bytes(), visible_before)

        image_storage.publish_staged_image(staged.path, 'team', 12)
        self.assertFalse(Path(staged.path).exists())
        self.assertEqual(destination.read_bytes(), staged.data)

        image_storage.cleanup_staged_image(staged.path)

    def test_remove_local_image_neutralises_effective_override(self):
        destination = image_storage.team_image_path(13)
        image_storage._normalise_image(image_bytes(), destination)

        image_storage.remove_local_image('team', 13)

        self.assertFalse(destination.exists())
        self.assertIsNone(image_storage.local_image_bytes('team', 13))

    def test_rejects_corrupt_and_oversized_data(self):
        destination = image_storage.team_image_path(1)
        with self.assertRaises(image_storage.ImageStorageError):
            image_storage._normalise_image(b'not an image', destination)
        with self.assertRaises(image_storage.ImageStorageError):
            image_storage._normalise_image(
                b'x' * (image_storage.MAX_UPLOAD_BYTES + 1),
                destination,
            )
        self.assertFalse(destination.exists())

    def test_rejects_excessive_pixel_count(self):
        destination = image_storage.team_image_path(1)
        data = image_bytes('PNG', size=(3, 2))
        with mock.patch.object(image_storage, 'MAX_IMAGE_PIXELS', 5):
            with self.assertRaisesRegex(
                image_storage.ImageStorageError,
                r'3x2 \(6 pixels\).*5-pixel limit',
            ):
                image_storage._normalise_image(data, destination)

    def test_attachment_reported_size_is_checked_before_read(self):
        attachment = FakeAttachment(
            image_bytes(),
            reported_size=image_storage.MAX_UPLOAD_BYTES + 1,
        )
        with self.assertRaises(image_storage.ImageStorageError):
            asyncio.run(image_storage.save_attachment(attachment, 'team', 2))
        self.assertFalse(image_storage.team_image_path(2).exists())

    def test_local_image_precedes_url_and_builds_attachment_thumbnail(self):
        team = FakeEntity(4, image_url='https://example.com/old.png')
        destination = image_storage.team_image_path(team.id)
        image_storage._normalise_image(image_bytes(), destination)

        self.assertEqual(image_storage.resolve_image('team', team), destination)
        embed = discord.Embed()
        attachment = image_storage.set_entity_thumbnail(embed, 'team', team)
        self.assertEqual(attachment.path, destination)
        self.assertEqual(
            embed.thumbnail.url,
            f'attachment://team-logo-{team.id}.png',
        )

        destination.unlink()
        self.assertEqual(
            image_storage.resolve_image('team', team),
            'https://example.com/old.png',
        )

    def test_remote_url_activation_removes_local_override(self):
        team = FakeEntity(7, image_url='https://example.com/old.png')
        destination = image_storage.team_image_path(team.id)
        image_storage._normalise_image(image_bytes(), destination)

        image_storage.activate_remote_url(
            team, 'team', 'https://example.com/new.png'
        )

        self.assertFalse(destination.exists())
        self.assertEqual(team.image_url, 'https://example.com/new.png')

    def test_remote_url_activation_restores_local_file_on_save_failure(self):
        error = RuntimeError('database unavailable')
        team = FakeEntity(
            8,
            image_url='https://example.com/old.png',
            save_error=error,
        )
        destination = image_storage.team_image_path(team.id)
        original = image_bytes()
        image_storage._normalise_image(original, destination)

        with self.assertRaises(RuntimeError):
            image_storage.activate_remote_url(
                team, 'team', 'https://example.com/new.png'
            )

        self.assertTrue(destination.exists())
        self.assertEqual(team.image_url, 'https://example.com/old.png')

    def test_http_url_validation(self):
        self.assertEqual(
            image_storage.validate_http_url('https://example.com/a.png'),
            'https://example.com/a.png',
        )
        for invalid in ('example.com/a.png', 'ftp://example.com/a.png', 'http:bad'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(image_storage.ImageStorageError):
                    image_storage.validate_http_url(invalid)

    def test_game_attachment_only_applies_to_multiplayer_team_winner(self):
        team = FakeEntity(5)
        image_storage._normalise_image(
            image_bytes(), image_storage.team_image_path(team.id)
        )
        game = SimpleNamespace(
            is_completed=1,
            winner=SimpleNamespace(team=team, lineup=[object(), object()]),
        )
        self.assertIsNotNone(image_storage.game_local_attachment(game))

        game.winner.lineup = [object()]
        self.assertIsNone(image_storage.game_local_attachment(game))

    def test_game_embed_send_creates_fresh_file_for_each_destination(self):
        team = FakeEntity(6)
        image_storage._normalise_image(
            image_bytes(), image_storage.team_image_path(team.id)
        )
        game = SimpleNamespace(
            is_completed=1,
            winner=SimpleNamespace(team=team, lineup=[object(), object()]),
        )
        destination = FakeDestination()

        asyncio.run(
            image_storage.send_game_embed(
                destination, game, embed=discord.Embed(), content='result'
            )
        )
        asyncio.run(
            image_storage.send_game_embed(
                destination, game, embed=discord.Embed(), content='result'
            )
        )

        first_file = destination.calls[0]['file']
        second_file = destination.calls[1]['file']
        self.assertIsNot(first_file, second_file)
        self.assertEqual(first_file.filename, 'team-logo-6.png')
        first_file.close()
        second_file.close()

    def test_game_embed_edit_replaces_managed_and_preserves_other_attachments(self):
        team = FakeEntity(9)
        image_storage._normalise_image(
            image_bytes(), image_storage.team_image_path(team.id)
        )
        game = SimpleNamespace(
            is_completed=1,
            winner=SimpleNamespace(team=team, lineup=[object(), object()]),
        )
        old_logo = SimpleNamespace(filename='team-logo-2.png')
        unrelated = SimpleNamespace(filename='notes.txt')
        message = FakeMessage([old_logo, unrelated])

        asyncio.run(
            image_storage.edit_game_embed(
                message, game, embed=discord.Embed(), content='updated'
            )
        )

        attachments = message.calls[0]['attachments']
        self.assertEqual(attachments[0], unrelated)
        self.assertEqual(attachments[1].filename, 'team-logo-9.png')
        attachments[1].close()


if __name__ == '__main__':
    unittest.main()
