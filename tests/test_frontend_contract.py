from __future__ import annotations

import unittest
from pathlib import Path

from tests import support  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_stale_book_and_facet_responses_are_discarded(self) -> None:
        self.assertIn("requestSequence !== booksRequestSequence", self.source)
        self.assertIn("requestSequence !== facetsRequestSequence", self.source)

    def test_delete_execution_sends_frozen_operation_id(self) -> None:
        execute_block = self.source.split("async function confirmDelete", 1)[1].split("function bindEvents", 1)[0]
        self.assertIn("operation_id: state.pendingDelete.operation_id", execute_block)
        self.assertNotIn("selectionPayload(", execute_block)


if __name__ == "__main__":
    unittest.main()
