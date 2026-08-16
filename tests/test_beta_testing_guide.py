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

    def test_guide_parses_sections_and_small_item_pages(self):
        guide = beta_testing_guide.load_guide()

        self.assertEqual(guide.title, 'WHAT TO TEST')
        self.assertGreater(len(guide.sections), 5)
        games = next(item for item in guide.sections if item.key == 'games')
        self.assertIn('/game win', ' '.join(games.items))
        pages = beta_testing_guide.item_pages(
            games,
            maximum_items=3,
            maximum_characters=1500,
        )
        self.assertTrue(all(1 <= len(page) <= 3 for page in pages))
        self.assertEqual(sum(len(page) for page in pages), len(games.items))

    def test_privileged_tests_have_dedicated_sections(self):
        sections = {
            section.key: ' '.join(section.items)
            for section in beta_testing_guide.load_guide().sections
        }

        self.assertIn('helper-commands-to-test', sections)
        self.assertIn('mod-commands-to-test', sections)
        self.assertIn('owner-operator-commands-to-test', sections)
        self.assertNotIn('/game result confirm', sections['games'])
        self.assertNotIn('/game ranked', sections['games'])
        self.assertNotIn('/team create', sections['teams'])
        self.assertNotIn('/team emoji', sections['teams'])
        self.assertNotIn('/team image', sections['teams'])
        self.assertNotIn('/house create', sections['houses'])
        self.assertNotIn('/league free-agents post', sections['league'])
        self.assertNotIn('/league maintenance export', sections['league'])
        self.assertIn('/game result confirm', sections['helper-commands-to-test'])
        self.assertIn('/league maintenance export', sections['helper-commands-to-test'])
        self.assertIn('/team create', sections['mod-commands-to-test'])
        self.assertIn('/house create', sections['mod-commands-to-test'])
        self.assertIn('/league free-agents post', sections['mod-commands-to-test'])
        self.assertIn('/elo recalculate', sections['owner-operator-commands-to-test'])
