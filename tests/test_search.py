from __future__ import annotations

import unittest

from tests import support

from bookvault import database


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        support.reset_workspace()
        support.add_book("nahan.txt", title="呐喊", authors="鲁迅")
        support.add_book("luxun-biography.txt", title="鲁迅传", authors="张作者")
        support.add_book("percent.txt", title="100% Python", authors="Tester")
        support.add_book("underscore.txt", title="A_B", authors="Tester")
        support.add_book("wildcard-control.txt", title="ACB", authors="Tester")
        support.add_book("plain.txt", title="普通书", authors="普通作者")
        support.add_book(
            "description-only.txt",
            title="将军族",
            authors="陈映真",
            description="阅读毛泽东、鲁迅的著作，也谈到了呐喊。",
        )
        support.add_book(
            "publisher.txt",
            title="出版社检索测试",
            metadata={"publisher": "人民文学出版社"},
        )

    def test_multiword_query_can_match_across_fields(self) -> None:
        result = database.list_books({"query": "鲁迅 呐喊"}, page_size=50)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "呐喊")
        self.assertEqual(result["items"][0]["authors"], "鲁迅")

    def test_percent_is_literal_not_match_all(self) -> None:
        result = database.list_books({"query": "%"}, page_size=50)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "100% Python")

    def test_underscore_is_literal_not_single_character_wildcard(self) -> None:
        result = database.list_books({"query": "A_B"}, page_size=50)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "A_B")

    def test_description_display_removes_html_markup(self) -> None:
        support.reset_workspace()
        support.add_book(
            "html-description.txt",
            title="HTML 简介",
            description='<p class="description">这是一本<strong>好书</strong>。</p><![CDATA[补充]]>',
        )

        result = database.list_books({"query": "HTML 简介"}, page_size=50)

        self.assertEqual(result["items"][0]["description"], "这是一本 好书 。 补充")

    def test_description_only_mentions_do_not_pollute_search(self) -> None:
        result = database.list_books({"query": "鲁迅 呐喊"}, page_size=50)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "呐喊")

    def test_publisher_is_searchable(self) -> None:
        result = database.list_books({"query": "人民文学出版社"}, page_size=50)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "出版社检索测试")


if __name__ == "__main__":
    unittest.main()
