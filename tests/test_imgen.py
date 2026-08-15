from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
import tempfile
import sys
import unittest
from unittest import mock
import warnings

warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)

from PIL import Image
import discord

from modules import imgen


def exact_colour_bbox(image, colour):
    """Return the bounds of pixels exactly matching an RGBA colour."""
    mask = Image.new('1', image.size)
    mask.putdata([
        pixel == colour for pixel in image.get_flattened_data()
    ])
    return mask.getbbox()


class ImageGenerationCompatibilityTests(unittest.TestCase):
    def test_text_measurement_uses_current_pillow_api(self):
        width = imgen.get_text_width('PolyChampions', 24)
        self.assertGreater(width, 0)

        canvas = Image.new('RGBA', (500, 200))
        imgen.draw_inverse_text(canvas, 'TEST', size=24, left=10, top=10)

    def test_fetch_image_reads_local_path_without_requests(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / 'logo.png'
            Image.new('RGBA', (12, 8), (0, 255, 0, 255)).save(path)

            with mock.patch.object(imgen.requests, 'get') as request_get:
                loaded = imgen.fetch_image(path)

            request_get.assert_not_called()
            self.assertEqual(loaded.mode, 'RGBA')
            self.assertEqual(loaded.size, (12, 8))

    def test_fetch_image_retains_remote_url_support(self):
        stream = BytesIO()
        Image.new('RGB', (5, 6), (0, 0, 255)).save(stream, format='PNG')
        response = mock.Mock()
        response.content = stream.getvalue()
        response.raise_for_status.return_value = None

        with mock.patch.object(imgen.requests, 'get', return_value=response) as request_get:
            loaded = imgen.fetch_image('https://example.com/logo.png')

        request_get.assert_called_once()
        self.assertEqual(request_get.call_args.kwargs['timeout'], (2, 5))
        self.assertEqual(loaded.mode, 'RGBA')
        self.assertEqual(loaded.size, (5, 6))

    def test_paste_image_contained_centers_wide_and_tall_images(self):
        cases = (
            ((820, 633), (10, 36, 240, 214)),
            ((633, 820), (36, 10, 214, 240)),
            ((100, 100), (10, 10, 240, 240)),
        )

        for source_size, expected_bbox in cases:
            with self.subTest(source_size=source_size):
                canvas = Image.new('RGBA', (250, 250), (0, 0, 0, 0))
                source = Image.new('RGBA', source_size, (255, 0, 255, 255))

                imgen.paste_image_contained(
                    canvas, source, left=10, top=10, width=230, height=230
                )

                self.assertEqual(canvas.getchannel('A').getbbox(), expected_bbox)

    def test_paste_image_contained_rejects_invalid_boxes(self):
        canvas = Image.new('RGBA', (10, 10))
        source = Image.new('RGBA', (1, 1))

        with self.assertRaisesRegex(ValueError, 'must be positive'):
            imgen.paste_image_contained(
                canvas, source, left=0, top=0, width=0, height=10
            )

    def test_arrow_card_renders_from_local_sources(self):
        with tempfile.TemporaryDirectory() as tempdir:
            left = Path(tempdir) / 'left.png'
            right = Path(tempdir) / 'right.png'
            Image.new('RGBA', (40, 50), (255, 0, 0, 255)).save(left)
            Image.new('RGBA', (50, 40), (0, 0, 255, 255)).save(right)

            rendered = imgen.arrow_card(
                'PROMOTION',
                'TO TEST TEAM',
                left,
                right,
                [('u', '#00ff00')],
            )

            self.assertEqual(rendered.filename, 'PROMOTION_TO TEST TEAM.png')
            self.assertGreater(rendered.fp.getbuffer().nbytes, 0)
            rendered.close()

    def test_arrow_card_contains_incident_ratio_in_both_image_boxes(self):
        left = Image.new('RGBA', (820, 633), (255, 0, 255, 255))
        right = Image.new('RGBA', (633, 820), (0, 255, 255, 255))

        with mock.patch.object(imgen, 'fetch_image', side_effect=[left, right]):
            rendered = imgen.arrow_card(
                'TRADE', 'TEST', 'left', 'right', []
            )

        try:
            with Image.open(rendered.fp) as card:
                self.assertEqual(
                    exact_colour_bbox(card, (255, 0, 255, 255)),
                    (100, 262, 330, 440),
                )
                self.assertEqual(
                    exact_colour_bbox(card, (0, 255, 255, 255)),
                    (532, 236, 710, 466),
                )
        finally:
            rendered.close()

    def test_draft_card_contains_incident_ratio_in_logo_box(self):
        team = SimpleNamespace(id=1, name='Test Team', image_url=None)

        class Team:
            @staticmethod
            def get_or_except(**_kwargs):
                return team

        model_stubs = ModuleType('modules.models')
        model_stubs.Team = Team
        member = SimpleNamespace(
            id=1,
            name='Test Player',
            guild=SimpleNamespace(id=2),
            display_avatar=SimpleNamespace(
                replace=lambda **_kwargs: 'avatar'
            ),
        )
        role = SimpleNamespace(
            name=team.name,
            colour=discord.Colour.blue(),
            color=discord.Colour.blue(),
        )
        images = [
            Image.new('RGBA', (820, 633), (255, 0, 255, 255)),
            Image.new('RGBA', (100, 100), (0, 255, 255, 255)),
        ]

        with mock.patch.dict(sys.modules, {'modules.models': model_stubs}):
            with mock.patch.object(
                    imgen.image_storage, 'resolve_image', return_value='logo'):
                with mock.patch.object(
                        imgen, 'fetch_image', side_effect=images):
                    with mock.patch.object(
                            imgen, 'get_player_summary', return_value='SUMMARY'):
                        rendered = imgen.player_draft_card(member, role)

        try:
            with Image.open(rendered.fp) as card:
                self.assertEqual(
                    exact_colour_bbox(card, (255, 0, 255, 255)),
                    (20, 11, 120, 88),
                )
        finally:
            rendered.close()


if __name__ == '__main__':
    unittest.main()
