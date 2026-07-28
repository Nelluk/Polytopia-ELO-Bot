from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import warnings

warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)

from PIL import Image

from modules import imgen


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


if __name__ == '__main__':
    unittest.main()
