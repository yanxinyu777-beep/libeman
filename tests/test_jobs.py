from __future__ import annotations

import time
import unittest
from unittest import mock

from tests import support

from bookvault import database, jobs


class PersistentJobTests(unittest.TestCase):
    def setUp(self) -> None:
        support.reset_workspace()
        with database.connect() as conn:
            conn.execute("DELETE FROM background_jobs")
        jobs._reset_runtime_state_for_tests()

    def test_job_history_survives_runtime_reset(self) -> None:
        created = jobs.create_job("scan", {"root": "temporary"})
        jobs.update_job(created["id"], status="finished", processed=80000, total=80000, message="完成")

        jobs._reset_runtime_state_for_tests()
        history = jobs.get_jobs()

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], created["id"])
        self.assertEqual(history[0]["status"], "finished")
        self.assertEqual(history[0]["processed"], 80000)

    def test_restart_marks_running_and_queued_jobs_interrupted(self) -> None:
        scan = jobs.create_job("scan", {"root": "temporary"})
        metadata = jobs.create_job("metadata", {"count": 80000})
        jobs.update_job(scan["id"], status="running", processed=123, total=80000)

        jobs._reset_runtime_state_for_tests()
        interrupted = jobs.initialize_after_restart()
        history = {job["id"]: job for job in jobs.get_jobs()}

        self.assertEqual(interrupted, 2)
        self.assertEqual(history[scan["id"]]["status"], "interrupted")
        self.assertIn("扫描中断", history[scan["id"]]["message"])
        self.assertEqual(history[scan["id"]]["processed"], 123)
        self.assertEqual(history[metadata["id"]]["status"], "interrupted")
        self.assertIn("重新排队", history[metadata["id"]]["message"])
        self.assertTrue(history[metadata["id"]]["errors"])

        jobs._reset_runtime_state_for_tests()
        persisted = {job["id"]: job for job in jobs.get_jobs()}
        self.assertEqual(persisted[scan["id"]]["status"], "interrupted")
        self.assertEqual(persisted[metadata["id"]]["status"], "interrupted")

    def test_progress_persistence_is_throttled_but_terminal_state_is_immediate(self) -> None:
        created = jobs.create_job("metadata", {"count": 80000})
        with mock.patch.object(database, "save_background_job") as save:
            for processed in range(1, 200):
                jobs.update_job(created["id"], processed=processed, total=80000, message=f"{processed}")
            self.assertEqual(save.call_count, 0)

            jobs.update_job(created["id"], processed=250, total=80000)
            self.assertEqual(save.call_count, 1)

            jobs.update_job(created["id"], status="finished", processed=80000, total=80000)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(save.call_args.args[0]["status"], "finished")

    def test_restart_recovers_active_job_outside_history_window(self) -> None:
        stale = jobs.create_job("scan", {"root": "temporary"})
        jobs.update_job(stale["id"], status="running", processed=77, total=80000)
        now = time.time()
        for index in range(105):
            database.save_background_job(
                {
                    "id": f"finished-{index}",
                    "kind": "metadata",
                    "status": "finished",
                    "message": "完成",
                    "processed": 1,
                    "total": 1,
                    "created_at": now + index,
                    "updated_at": now + index,
                    "payload": {},
                    "errors": [],
                }
            )

        jobs._reset_runtime_state_for_tests()
        interrupted = jobs.initialize_after_restart()
        with database.connect() as conn:
            row = conn.execute(
                "SELECT status, processed FROM background_jobs WHERE id = ?",
                (stale["id"],),
            ).fetchone()

        self.assertEqual(interrupted, 1)
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["processed"], 77)

    def test_errors_are_persisted_immediately_and_capped(self) -> None:
        created = jobs.create_job("scan")
        with mock.patch.object(database, "save_background_job") as save:
            for index in range(55):
                jobs.append_error(created["id"], f"error-{index}")
        self.assertEqual(save.call_count, 55)
        current = jobs.get_jobs()[0]
        self.assertEqual(len(current["errors"]), 50)
        self.assertEqual(current["errors"][0], "error-5")
        self.assertEqual(current["errors"][-1], "error-54")


if __name__ == "__main__":
    unittest.main()
