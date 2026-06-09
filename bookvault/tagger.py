from __future__ import annotations

import re
from collections.abc import Iterable


CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "小说": ("小说", "fiction", "novel", "故事", "长篇", "中篇", "短篇", "爱情", "青春", "童话", "小说集", "中短篇", "穿越", "公主"),
    "文学": ("文学", "literature", "散文", "随笔", "诗歌", "poetry", "prose", "儿童文学", "经典文学", "鲁迅", "眷村"),
    "历史": ("历史", "history", "史记", "世界史", "中国史", "近代史", "古代", "晚近", "战争", "开国", "百家争鸣", "帝国", "档案", "阵亡"),
    "科幻": ("科幻", "science fiction", "sci-fi", "sf", "未来", "星际", "银河"),
    "推理": ("推理", "悬疑", "侦探", "mystery", "detective", "crime", "thriller"),
    "玄幻": ("玄幻", "奇幻", "fantasy", "魔法", "修真", "武侠", "仙侠"),
    "经济": ("经济", "economics", "宏观", "微观", "市场", "贸易", "财经", "企业家", "企业", "商业"),
    "金融": ("金融", "finance", "投资", "股票", "基金", "债券", "银行", "证券", "股价", "上市公司", "黑庄"),
    "管理": ("management", "领导力", "组织管理", "创业", "商业", "运营", "企业家", "企业管理", "项目管理", "战略管理", "财务管理", "人力资源"),
    "心理": ("心理", "psychology", "认知", "情绪", "人格", "咨询"),
    "哲学": ("哲学", "philosophy", "伦理", "形而上", "逻辑", "存在主义"),
    "社科": ("社会学", "sociology", "politics", "政治", "人类学", "传播", "文化研究", "日本", "知日"),
    "计算机": ("计算机", "computer", "software", "编程", "程序", "算法", "python", "java", "javascript", "linux", "数据库", "人工智能"),
    "教育": ("教育", "education", "学习", "教学", "课程", "考试", "教材"),
    "医学": ("医学", "medical", "medicine", "临床", "护理", "药学"),
    "法律": ("法律", "law", "法学", "司法", "合同", "宪法", "民法", "刑法"),
    "传记": ("传记", "biography", "memoir", "回忆录", "自传", "生平", "名人传"),
    "艺术": ("艺术史", "艺术学", "art", "设计", "美术", "摄影", "音乐", "电影", "戏剧"),
    "旅行": ("旅行", "旅游", "travel", "地理", "游记", "指南"),
    "育儿": ("育儿", "亲子", "家庭教育", "parenting", "child development"),
    "儿童读物": ("儿童文学", "童话", "少儿读物", "children's literature", "fairy tale"),
    "外语": ("英语", "外语", "language", "grammar", "vocabulary", "ielts", "toefl"),
    "国学": ("国学", "儒家", "道家", "佛学", "易经", "论语", "古文"),
    "自然科学": ("自然科学", "science", "物理", "化学", "生物", "数学", "天文", "地质"),
    "生活": ("家居", "美食", "烹饪", "养生", "情感", "爱情"),
    "职场": ("职场", "求职", "简历", "面试", "职业规划", "职业发展", "工作效率", "办公室", "时间管理", "沟通技巧", "压力管理"),
    "语言文字": ("语言文字", "汉字", "词源", "训诂", "语文", "写作"),
    "神话民俗": ("神话", "民俗", "传说", "山海经", "妖怪", "志怪"),
}

SUBJECT_NORMALIZATION: dict[str, str] = {
    "business": "管理",
    "children": "育儿",
    "computer": "计算机",
    "economics": "经济",
    "education": "教育",
    "fantasy": "玄幻",
    "fiction": "小说",
    "finance": "金融",
    "history": "历史",
    "language": "外语",
    "law": "法律",
    "literature": "文学",
    "medical": "医学",
    "mystery": "推理",
    "myth": "神话民俗",
    "philosophy": "哲学",
    "psychology": "心理",
    "science fiction": "科幻",
    "science": "自然科学",
    "social": "社科",
    "travel": "旅行",
}


def normalize_tag(tag: str) -> str:
    cleaned = re.sub(r"\s+", " ", tag.strip())
    cleaned = cleaned.strip(".,;:，。；：/\\|")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    for key, value in SUBJECT_NORMALIZATION.items():
        if key in lower:
            return value
    for label in CATEGORY_KEYWORDS:
        if label in cleaned:
            return label
    return ""


def infer_tags(*parts: object, subjects: Iterable[str] | None = None, limit: int = 10) -> list[str]:
    text_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, (list, tuple, set)):
            text_parts.extend(str(item) for item in part if item)
        else:
            text_parts.append(str(part))

    haystack = " ".join(text_parts).lower()
    tags: list[str] = []

    for label, keywords in CATEGORY_KEYWORDS.items():
        if any(_keyword_in_text(haystack, keyword) for keyword in keywords):
            tags.append(label)

    for subject in subjects or []:
        normalized = normalize_tag(subject)
        if normalized and normalized not in tags:
            tags.append(normalized)

    return tags[:limit]


def merge_tags(*tag_groups: Iterable[str] | None, limit: int = 6) -> list[str]:
    merged: list[str] = []
    for group in tag_groups:
        for raw_tag in group or []:
            tag = normalize_tag(str(raw_tag))
            if tag and tag not in merged:
                merged.append(tag)
    if "未分类" in merged:
        merged.remove("未分类")
    if "小说" in merged and "社科" in merged:
        merged.remove("社科")
    return merged[:limit]


def refine_tags(title: object = "", authors: object = "", description: object = "", tags: Iterable[str] | None = None) -> list[str]:
    refined = merge_tags(tags)
    title_text = str(title or "")
    authors_text = str(authors or "")
    haystack = " ".join(str(part or "") for part in (title, authors, description)).lower()
    compact_title = _compact_zh(title_text)
    if compact_title in {"呐喊", "吶喊", "鲁迅呐喊", "魯迅吶喊"} and ("鲁迅" in authors_text or "魯迅" in authors_text):
        refined = merge_tags(["小说", "文学"], refined)
    biography_evidence = ("传记", "biography", "memoir", "回忆录", "自传", "生平", "名人传")
    if "传记" in refined and not any(keyword in haystack for keyword in biography_evidence):
        refined.remove("传记")
    literary_evidence = ("小说", "文学", "文集", "散文", "诗歌", "作家", "鲁迅", "莫言", "短篇", "中篇", "长篇")
    is_literary_work = "小说" in refined or "文学" in refined or any(keyword in haystack for keyword in literary_evidence)
    career_evidence = ("职场", "求职", "简历", "面试", "职业规划", "职业发展", "工作效率", "办公室", "时间管理", "沟通技巧", "压力管理")
    career_text = title_text if is_literary_work else haystack
    if "职场" in refined and not any(keyword in career_text for keyword in career_evidence):
        refined.remove("职场")
    parenting_evidence = ("育儿", "亲子", "家庭教育", "父母课堂", "parenting", "child development")
    parenting_text = f"{title_text} {str(description or '')[:240]}".lower()
    if "育儿" in refined and not any(keyword in parenting_text for keyword in parenting_evidence):
        refined.remove("育儿")
    management_evidence = ("管理学", "企业管理", "项目管理", "战略管理", "财务管理", "运营管理", "组织管理", "人力资源", "领导力")
    if "管理" in refined and is_literary_work and not any(keyword in haystack for keyword in management_evidence):
        refined.remove("管理")
    art_evidence = ("艺术", "美术", "摄影", "音乐", "电影", "戏剧", "设计")
    if "艺术" in refined and is_literary_work and not any(keyword in title_text for keyword in art_evidence):
        refined.remove("艺术")
    language_evidence = ("语言", "汉字", "词源", "训诂", "语文", "写作")
    if "语言文字" in refined and is_literary_work and not any(keyword in title_text for keyword in language_evidence):
        refined.remove("语言文字")
    secondary_evidence = {
        "历史": ("历史", "史略", "世界史", "中国史", "近代史", "古代史", "战争史"),
        "社科": ("社会学", "政治", "社科", "人类学", "文化研究"),
        "医学": ("医学", "临床", "护理", "药学", "中医"),
        "自然科学": ("科学", "物理", "化学", "生物", "数学", "天文", "地质"),
        "教育": ("教育", "教学", "教材", "课程", "考试"),
        "旅行": ("旅行", "旅游", "游记", "地理", "指南"),
        "经济": ("经济", "财经", "贸易", "商业"),
        "法律": ("法律", "法学", "司法", "民法", "刑法"),
        "金融": ("金融", "投资", "股票", "基金", "银行"),
    }
    for tag, evidence in secondary_evidence.items():
        if tag in refined and is_literary_work and not any(keyword in title_text for keyword in evidence):
            refined.remove(tag)
    return refined


def _compact_zh(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def _keyword_in_text(haystack: str, keyword: str) -> bool:
    key = keyword.lower()
    if re.fullmatch(r"[a-z0-9 +#.-]+", key):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", haystack))
    filtered = haystack
    if key == "历史":
        filtered = re.sub(r"历史记录|历史纪录|浏览历史|操作历史|未来的历史", " ", filtered)
    elif key == "史记":
        filtered = re.sub(r"历史记录|历史纪录|出版史记录|版本史记录", " ", filtered)
    return key in filtered
