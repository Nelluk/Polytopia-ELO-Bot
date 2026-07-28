from pathlib import Path
import unittest


class ProductionDeploymentAssetTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_bot_service_dropin_selects_production_python_312_environment(self):
        dropin = (
            self.root
            / 'deploy/systemd/polytopia.service.d/upgrade.conf'
        ).read_text(encoding='utf-8').splitlines()

        self.assertEqual(
            dropin,
            [
                '[Service]',
                'Environment=POLYBOT_ENV=production',
                'ExecStart=',
                (
                    'ExecStart=/home/nelluk/PolyBot39/.venv/bin/python '
                    '/home/nelluk/PolyBot39/bot.py'
                ),
            ],
        )

    def test_cutover_runbook_preserves_legacy_rollback(self):
        runbook = (
            self.root / 'docs/PRODUCTION_CUTOVER.md'
        ).read_text(encoding='utf-8')

        self.assertIn('uv sync --locked --no-dev --python 3.12.13', runbook)
        self.assertIn('POLYBOT_ROLLBACK_COMMIT=43b3425', runbook)
        self.assertIn(
            '/home/nelluk/PolyBot39/bin/python3 '
            '/home/nelluk/PolyBot39/bot.py',
            runbook,
        )
        self.assertIn('polyapi.service` remains inactive', runbook)


if __name__ == '__main__':
    unittest.main()
