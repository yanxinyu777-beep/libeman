from __future__ import annotations

import unittest

from tests import support  # noqa: F401

from bookvault.metadata import _merge_metadata, clean_description, is_suspicious_title
from bookvault.tagger import infer_tags, refine_tags


class MetadataQualityTests(unittest.TestCase):
    def test_meaningful_four_digit_titles_are_preserved(self) -> None:
        self.assertFalse(is_suspicious_title("1988"))
        self.assertFalse(is_suspicious_title("2666"))

    def test_numeric_codes_and_section_titles_are_rejected(self) -> None:
        for title in ("002-62", "0001", "第一章 破晓", "第12节：开始", "Chapter 3 Arrival"):
            with self.subTest(title=title):
                self.assertTrue(is_suspicious_title(title))

    def test_web_page_noise_is_rejected(self) -> None:
        self.assertTrue(is_suspicious_title("欢迎光临翠微居"))
        self.assertTrue(is_suspicious_title("最新章节全文阅读"))

    def test_literary_work_drops_incidental_history_and_career_tags(self) -> None:
        tags = refine_tags(
            "钢穴",
            "艾萨克·阿西莫夫",
            "这是一部描写未来历史的科幻小说，人物需要面对工作压力。",
            ["小说", "科幻", "历史", "职场"],
        )
        self.assertEqual(tags, ["小说", "科幻"])

    def test_childrens_literature_is_not_parenting(self) -> None:
        tags = refine_tags(
            "小王子",
            "圣埃克絮佩里",
            "法国著名儿童文学与童话作品。",
            infer_tags("小王子", "圣埃克絮佩里", "法国著名儿童文学与童话作品。"),
        )
        self.assertIn("儿童读物", tags)
        self.assertNotIn("育儿", tags)

    def test_parenting_book_keeps_parenting_tag(self) -> None:
        tags = refine_tags(
            "父母课堂",
            "",
            "一本关于亲子沟通和家庭教育的育儿指南。",
            infer_tags("父母课堂", "", "一本关于亲子沟通和家庭教育的育儿指南。"),
        )
        self.assertIn("育儿", tags)

    def test_description_cleanup_removes_markup_url_and_download_tail(self) -> None:
        cleaned = clean_description(
            '<![CDATA[<p>这是一本关于阅读与成长的作品。</p>]]>'
            ' https://example.com/book 本书由某下载站制作，更多免费电子书请访问网站'
        )
        self.assertEqual(cleaned, "这是一本关于阅读与成长的作品。")

    def test_clean_description_beats_long_generated_placeholder(self) -> None:
        target = {"description": "《测试书》是一本本地电子书，作者未知，本地文件格式为 PDF。"}
        _merge_metadata(target, {"description": "这是一部讨论家庭关系、成长选择与个人责任的长篇作品。"})
        self.assertEqual(target["description"], "这是一部讨论家庭关系、成长选择与个人责任的长篇作品。")

    def test_history_record_phrase_does_not_create_history_tag(self) -> None:
        self.assertNotIn("历史", infer_tags("清除 WinXP 任务栏图标历史记录"))
        self.assertIn("历史", infer_tags("中国近代历史"))


if __name__ == "__main__":
    unittest.main()
