from __future__ import annotations

import unittest
from unittest import mock

from tests import support  # noqa: F401

from bookvault import database, network


def _result_html(title: str, rating: str, subject_id: int, cast: str = "") -> str:
    return f"""
    <div class="result">
      <h3><a title="{title}" href="https://book.douban.com/subject/{subject_id}/">{title}</a></h3>
      <span class="rating_nums">{rating}</span>
      <span class="subject-cast">{cast}</span>
      <p>{title} summary</p>
    </div>
    """


class DoubanMatchingTests(unittest.TestCase):
    def test_high_rating_cannot_override_exact_title_identity(self) -> None:
        html = (
            _result_html("活着的意义", "9.9", 1, "其他作者")
            + _result_html("活着", "7.1", 2, "余华")
        )
        with mock.patch.object(network, "_fetch_text", return_value=html):
            result = network.lookup_douban_rating("活着", "余华")

        self.assertEqual(result["title"], "活着")
        self.assertEqual(result["douban_rating"], "7.1")
        self.assertIn("/2/", result["douban_url"])

    def test_obviously_different_title_is_rejected(self) -> None:
        html = _result_html("活着的意义", "9.9", 1, "其他作者")
        with mock.patch.object(network, "_fetch_text", return_value=html):
            result = network.lookup_douban_rating("活着", "余华")

        self.assertEqual(result, {"douban_rating_status": "未找到"})

    def test_rating_is_not_part_of_candidate_identity_score(self) -> None:
        low = {"title": "活着", "cast": "", "rating": "1.0"}
        high = {"title": "活着", "cast": "", "rating": "9.9"}
        normalized = network._normalize_match_text("活着")
        self.assertEqual(
            network._candidate_score(low, normalized, ""),
            network._candidate_score(high, normalized, ""),
        )


class DuplicateMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        support.reset_workspace()

    def test_identical_file_copy_reuses_existing_rating(self) -> None:
        source_id, _ = support.add_book(
            "source/same.mobi",
            title="小王子",
            authors="圣埃克苏佩里",
            content=b"identical",
        )
        target_id, _ = support.add_book(
            "copy/same.mobi",
            title="小王子",
            authors="圣埃克苏佩里",
            content=b"identical",
        )
        with database.connect() as conn:
            source_mtime = conn.execute("SELECT mtime FROM books WHERE id = ?", (source_id,)).fetchone()["mtime"]
            conn.execute("UPDATE books SET mtime = ? WHERE id = ?", (source_mtime, target_id))
            conn.execute(
                """
                UPDATE books
                SET douban_rating = '9.1',
                    douban_url = 'https://book.douban.com/subject/1084336/',
                    douban_rating_status = '已获取',
                    remote_source = '豆瓣',
                    remote_lookup_version = 2
                WHERE id = ?
                """,
                (source_id,),
            )

        changed = database.reuse_remote_metadata_for_identical_copies()

        self.assertEqual(changed, 1)
        with database.connect() as conn:
            target = conn.execute(
                "SELECT douban_rating, douban_url, douban_rating_status FROM books WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(target["douban_rating"], "9.1")
        self.assertEqual(target["douban_url"], "https://book.douban.com/subject/1084336/")
        self.assertEqual(target["douban_rating_status"], "已获取")

    def test_pending_remote_lookup_prioritizes_chinese_books(self) -> None:
        english_id, _ = support.add_book(
            "english.mobi",
            title="An English Book",
            authors="An Author",
        )
        chinese_id, _ = support.add_book(
            "chinese.mobi",
            title="中文书",
            authors="中文作者",
        )

        due = database.select_remote_lookup_due_ids(limit=2)

        self.assertEqual(due, [chinese_id, english_id])


if __name__ == "__main__":
    unittest.main()
