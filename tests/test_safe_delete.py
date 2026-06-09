from __future__ import annotations

import inspect
import unittest

from tests import support

from bookvault import database, operations


class SafeDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        support.reset_workspace()

    def test_preview_persists_frozen_items_and_execution_uses_operation_id(self) -> None:
        book_id, source = support.add_book("delete-me.txt", title="Delete Me", content=b"frozen")

        preview = operations.delete_preview([book_id])
        self.assertTrue(preview["operation_id"])
        batch = database.get_operation_batch(preview["operation_id"])
        self.assertIsNotNone(batch)
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(batch["item_count"], 1)
        self.assertEqual(batch["items"][0]["path"], str(source))
        self.assertEqual(batch["items"][0]["size"], source.stat().st_size)
        self.assertAlmostEqual(batch["items"][0]["mtime"], source.stat().st_mtime, places=3)

        result = operations.safe_delete(preview["operation_id"], preview["confirm_phrase"])
        self.assertEqual(result["moved"], 1)
        self.assertFalse(source.exists())
        self.assertTrue((support.DESKTOP_DIR / "BookVault_Quarantine").exists())

        completed = database.get_operation_batch(preview["operation_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["items"][0]["status"], "moved")
        with database.connect() as conn:
            row = conn.execute("SELECT status, quarantine_path FROM books WHERE id = ?", (book_id,)).fetchone()
        self.assertEqual(row["status"], "quarantined")
        self.assertTrue(row["quarantine_path"])

    def test_source_change_rejects_entire_operation_before_move(self) -> None:
        book_id, source = support.add_book("drift.txt", title="Drift", content=b"before")
        preview = operations.delete_preview([book_id])

        source.write_bytes(b"changed after preview")

        with self.assertRaisesRegex(ValueError, "状态已变化"):
            operations.safe_delete(preview["operation_id"], preview["confirm_phrase"])

        self.assertTrue(source.exists())
        rejected = database.get_operation_batch(preview["operation_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["items"][0]["status"], "planned")
        with database.connect() as conn:
            status = conn.execute("SELECT status FROM books WHERE id = ?", (book_id,)).fetchone()["status"]
        self.assertEqual(status, "active")

    def test_safe_delete_protocol_accepts_operation_id_not_book_ids(self) -> None:
        parameters = list(inspect.signature(operations.safe_delete).parameters)
        self.assertEqual(parameters, ["operation_id", "confirm_text"])


if __name__ == "__main__":
    unittest.main()
