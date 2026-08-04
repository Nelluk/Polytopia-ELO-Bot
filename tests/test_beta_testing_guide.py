from pathlib import Path
import tempfile
import unittest

from modules import beta_testing_guide


class BetaWhatToTestTests(unittest.TestCase):
    def test_tracked_checklist_emphasizes_native_and_interactive_testing(self):
        checklist = beta_testing_guide.load_checklist()

        self.assertTrue(checklist.startswith('# 🧪 WHAT TO TEST'))
        self.assertIn('/game open', checklist)
        self.assertIn('/leaderboard players', checklist)
        self.assertIn('/team create', checklist)
        self.assertIn('/staffhelp', checklist)
        self.assertNotIn('$team', checklist)

    def test_pages_are_nonempty_and_bounded(self):
        pages = beta_testing_guide.message_pages(
            beta_testing_guide.load_checklist(), maximum=700
        )

        self.assertGreater(len(pages), 1)
        self.assertTrue(all(page and len(page) <= 700 for page in pages))
        self.assertEqual('\n'.join(pages).splitlines()[0], '# 🧪 WHAT TO TEST')

    def test_empty_file_has_clear_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.md'
            path.write_text('', encoding='utf-8')
            pages = beta_testing_guide.message_pages(
                beta_testing_guide.load_checklist(path)
            )

        self.assertIn('No testing items', pages[0])
