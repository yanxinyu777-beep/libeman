from __future__ import annotations

import unittest
from unittest import mock

from tests import support

from bookvault import config, workers
import app


class ConfigAndRemoteTests(unittest.TestCase):
    def test_environment_paths_are_isolated(self) -> None:
        self.assertEqual(config.DATA_DIR, support.DATA_DIR)
        self.assertEqual(config.DB_PATH, support.DB_PATH)
        self.assertEqual(config.DESKTOP_OUTPUT_DIR, support.DESKTOP_DIR)

    def test_workers_do_not_resume_remote_when_disabled(self) -> None:
        with mock.patch.object(
            workers.database,
            "select_remote_lookup_due_ids",
            side_effect=AssertionError("remote queue should not be inspected"),
        ):
            self.assertIsNone(workers.start_pending_remote_lookup())
            self.assertIsNone(workers.start_remote_lookup([1, 2, 3]))

    def test_app_startup_helper_does_not_resume_remote_when_disabled(self) -> None:
        with mock.patch.object(
            app.workers,
            "start_pending_remote_lookup",
            side_effect=AssertionError("remote startup should be disabled"),
        ):
            self.assertIsNone(app._resume_pending_remote_lookup())


if __name__ == "__main__":
    unittest.main()
