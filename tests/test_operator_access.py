"""Focused configured-identity tests for cross-guild operator access."""

from types import SimpleNamespace
import unittest
from unittest import mock

import settings


class OperatorAccessTests(unittest.TestCase):
    def test_superuser_check_uses_only_the_runtime_profile_identity_set(self):
        with mock.patch.object(settings, 'superuser_ids', (10, 20, 30)):
            self.assertTrue(settings.is_superuser(SimpleNamespace(id=10)))
            self.assertTrue(settings.is_superuser(SimpleNamespace(id=30)))
            self.assertFalse(settings.is_superuser(SimpleNamespace(id=40)))

    def test_superuser_check_normalizes_discord_ids_to_integers(self):
        with mock.patch.object(settings, 'superuser_ids', (10,)):
            self.assertTrue(settings.is_superuser(SimpleNamespace(id='10')))

