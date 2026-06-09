from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .config import MAX_EXCERPT_CHARS, MAX_READ_BYTES
from .tagger import infer_tags, merge_tags

UNKNOWN_AUTHOR = "未知"
UNKNOWN_TITLE = "待识别书名"
KNOWN_SHORT_TITLES = {"呐喊", "吶喊", "彷徨", "边城", "圍城", "围城", "活着", "红岩", "茶馆", "雷雨"}
PINYIN_SYLLABLES = {
    "a",
    "ai",
    "an",
    "bian",
    "bi",
    "biao",
    "bo",
    "cai",
    "chang",
    "cheng",
    "ci",
    "da",
    "dao",
    "de",
    "di",
    "ding",
    "dong",
    "du",
    "er",
    "fa",
    "feng",
    "fu",
    "gong",
    "gu",
    "guan",
    "hao",
    "huo",
    "ji",
    "jia",
    "jian",
    "jie",
    "jing",
    "ju",
    "ke",
    "li",
    "lin",
    "lou",
    "luo",
    "mian",
    "meng",
    "mo",
    "ni",
    "qi",
    "qiong",
    "quan",
    "sha",
    "shang",
    "shi",
    "si",
    "sui",
    "tan",
    "ti",
    "tian",
    "wei",
    "wen",
    "xiu",
    "xun",
    "yan",
    "yang",
    "yi",
    "yong",
    "you",
    "zhi",
    "zhong",
}
GENERIC_AUTHORS = {
    "",
    "未知",
    "佚名",
    "administrator",
    "wei zhi",
    "coay.com",
    "coay",
    "user",
    "cnki",
    "pic2pdf",
    "multing",
    "unknown",
}
KNOWN_NON_CHINESE_AUTHORS = {"37signals"}


def clean_text(value: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
    return value[:limit].strip()


def clean_description(value: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    text = html.unescape(value or "")
    text = text.replace("<![CDATA[", " ").replace("]]>", " ")
    text = _strip_markup(text)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.I)
    text = re.sub(
        r"(?:本书由|更多(?:免费)?电子书|关注公众号|下载(?:地址|链接)|手机阅读|返回目录|"
        r"版权归原作者所有|仅供学习交流|请在下载后.{0,20}删除).*$",
        " ",
        text,
        flags=re.I,
    )
    return clean_text(text, limit)


def guess_title_author(path: Path) -> tuple[str, str]:
    stem = path.stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\[[^\]]{1,30}\]", " ", stem)
    stem = _clean_title_candidate(re.sub(r"\s+", " ", stem))
    stem = re.sub(r"\s*\(?v\d+(?:\.\d+)?\)?$", "", stem, flags=re.I).strip()
    stem = re.sub(r"\s*[-—–－]\s*(?:wei\s+zhi|unknown|未知|佚名)$", "", stem, flags=re.I)
    if not stem:
        return path.name, ""
    known_pair = _known_title_author_from_romanized(stem)
    if known_pair:
        return known_pair

    authored = re.match(r"^《([^》]+)》\s*(?:作者)?[:：]\s*(.+)$", stem, flags=re.I)
    if authored:
        return authored.group(1).strip(), authored.group(2).strip()

    authored = re.match(r"^(.+?)\s*(?:作者)?[:：]\s*(.+)$", stem, flags=re.I)
    if authored:
        return _known_title_from_romanized(authored.group(1)) or authored.group(1).strip("《》 "), authored.group(2).strip()

    paren = re.match(r"^《?(.+?)》?\s*[（(]([^()（）]{1,60})[）)]$", stem)
    if paren:
        author = paren.group(2).strip()
        if _looks_like_author(author):
            return paren.group(1).strip(), _known_author_from_romanized(author) or author
        stem = paren.group(1).strip()

    parts = re.split(r"\s*[-—–－]\s*", stem, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()
        known_pair = _known_title_author_from_romanized(left)
        if known_pair:
            return known_pair
        left_title = _known_title_from_romanized(left) or left
        right_author = _known_author_from_romanized(right) or right
        if left_title != left and right_author != right:
            return left_title, right_author
        if _looks_like_author(left):
            return _known_title_from_romanized(right) or right, _known_author_from_romanized(left) or left
        if _looks_like_author(right):
            return left_title, right_author
        if left_title != left:
            return left_title, ""
    return _known_title_from_romanized(stem) or stem, ""


def _looks_like_author(value: str) -> bool:
    if not value:
        return False
    if _known_author_from_romanized(value):
        return True
    if is_suspicious_author(value):
        return False
    if re.fullmatch(r"\d+(?:[\s_-]+\d+)*", value):
        return False
    if len(value) <= 12 and not re.search(r"(第|卷|册|全集|套装|修订|版)", value):
        return True
    if "," in value and len(value) <= 40:
        return True
    return False


def _known_title_from_romanized(value: str) -> str:
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    known = {
        "ya li guan li": "压力管理",
        "yi zi qi yuan xin jie": "汉字起源新解",
        "shan hai jing xiao zhu": "山海经校注",
        "ne han": "呐喊",
        "nahan": "呐喊",
    }
    return known.get(normalized, "")


def _known_title_author_from_romanized(value: str) -> tuple[str, str] | None:
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    known = {
        "lu xun ne han": ("呐喊", "鲁迅"),
        "lu xun nahan": ("呐喊", "鲁迅"),
    }
    return known.get(normalized)


def _known_author_from_romanized(value: str) -> str:
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    known = {
        "lu xun": "鲁迅",
        "mo yan": "莫言",
        "su jing": "苏静",
    }
    return known.get(normalized, "")


def extract_book_metadata(path: str | Path, extract_text: bool = True) -> dict[str, Any]:
    book_path = Path(path)
    guessed_title, guessed_author = guess_title_author(book_path)
    ext = book_path.suffix.lower()
    data: dict[str, Any] = {
        "title": guessed_title,
        "authors": guessed_author,
        "description": "",
        "language": "",
        "publisher": "",
        "published_year": "",
        "isbn": "",
        "cover_path": "",
        "source": "local",
        "tags": [],
    }

    try:
        sidecar = _read_sidecar_opf(book_path)
        if sidecar:
            _merge_metadata(data, sidecar)
        if ext == ".pdf":
            data.update(_read_pdf(book_path, extract_text))
        elif ext == ".docx":
            data.update(_read_docx(book_path, extract_text))
        elif ext == ".epub":
            data.update(_read_epub(book_path, extract_text))
        elif ext in {".txt", ".md"}:
            data.update(_read_plain_text(book_path, extract_text))
        elif ext == ".rtf":
            data.update(_read_rtf(book_path, extract_text))
        elif ext in {".mobi", ".azw", ".azw3"}:
            data.update(_read_mobi_like(book_path))
        if sidecar:
            _merge_metadata(data, sidecar)
    except Exception as exc:  # Metadata extraction should never stop a library scan.
        data["scan_error"] = str(exc)[:240]

    data["title"] = _choose_final_title(
        str(data.get("title") or ""),
        guessed_title,
        str(data.get("description") or ""),
        guessed_author,
    )
    data["authors"] = _choose_final_author(str(data.get("authors") or ""), guessed_author)
    data["description"] = clean_description(str(data.get("description") or ""), MAX_EXCERPT_CHARS)

    subjects = data.pop("subjects", []) or []
    inferred = infer_tags(
        data.get("title"),
        data.get("authors"),
        book_path.name,
        str(data.get("description") or "")[:240],
        subjects=subjects,
    )
    data["tags"] = merge_tags(data.get("tags"), subjects, inferred)
    if not data.get("cover_path"):
        cover = _find_sidecar_cover(book_path)
        data["cover_path"] = str(cover) if cover else ""
    if not data["description"]:
        data["description"] = build_simple_description(
            str(data.get("title") or guessed_title),
            str(data.get("authors") or guessed_author),
            data["tags"],
            ext,
        )
    return data


def _merge_metadata(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if not value:
            continue
        if key == "title":
            current = str(target.get("title") or "")
            candidate = str(value)
            if _should_replace_title(current, candidate):
                target[key] = candidate
            continue
        if key == "authors":
            current = str(target.get("authors") or "")
            candidate = _clean_author(str(value))
            if _should_replace_author(current, candidate):
                target[key] = candidate
            continue
        if key == "description":
            current = clean_description(str(target.get("description") or ""))
            candidate = clean_description(str(value))
            if _description_quality(candidate) > _description_quality(current):
                target[key] = candidate
            continue
        target[key] = value


def is_suspicious_title(value: str) -> bool:
    if _is_generic_section_title(clean_text(value, 80)):
        return True
    title = _clean_title_candidate(clean_text(value, 80))
    if not title:
        return True
    lower = title.lower()
    has_chinese = _has_chinese(title)
    if lower in {
        "mobi",
        "azw",
        "azw3",
        "epub",
        "pdf",
        "txt",
        "doc",
        "docx",
        "rtf",
        "cnki",
        "pic2pdf",
        "multing",
        "unknown",
        "untitled",
        "document",
        "print",
        "print job",
        "cover",
        "cover.pdf",
    }:
        return True
    if re.search(r"\b(wei zhi|wei biao ti|coay\.?com|cnki|pic2pdf|multing)\b", lower):
        return True
    if re.search(r"(https?://|www\.)", lower):
        return True
    if "#" in title and title.count("#") >= 3:
        return True
    if re.search(r"目\s*录", title):
        return True
    if "65533" in lower:
        return True
    if "_" in title and not has_chinese:
        return True
    if re.search(r"\b(name|date|line|read)\b", lower) and re.search(r"\d{2,}", lower):
        return True
    if re.search(r"\.(gif|jpg|jpeg|png|webp)\b", lower):
        return True
    if re.search(r"(欢迎光临|最新章节|全文阅读|手机阅读|电子书制作|本书下载|返回目录)", title):
        return True
    if re.fullmatch(r"[?？_\-\[\]\s]+", title):
        return True
    if title.count("�") >= 2:
        return True
    if title.count("?") >= max(3, len(title) // 2):
        return True
    if _looks_mojibake(title):
        return True
    if re.fullmatch(r"0\d{2,}(?:[\s_-]\d+)?", title):
        return True
    if re.fullmatch(r"\d{1,4}(?:[\s_-]\d{1,4})+", title):
        return True
    if re.fullmatch(r"\d{3,}", title):
        return not (len(title) == 4 and not title.startswith("0"))
    if _is_generic_section_title(title):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", title)
    if not has_chinese and re.fullmatch(r"[A-Za-z0-9]+", compact) and len(compact) <= 3:
        return True
    if not has_chinese and re.fullmatch(r"[A-Za-z]{1,4}'[A-Za-z]{0,4}(?:\s*[-—–]\s*[A-Za-z0-9]{1,4})?", title):
        return True
    if not has_chinese and re.fullmatch(r"[A-Z0-9!@#$%^&*()\-_' .:;]+", title) and len(compact) >= 12:
        return True
    if not has_chinese and re.search(r"[A-Z]{2,}[a-z]", title):
        return True
    if title == compact and title.isupper() and 2 <= len(title) <= 24:
        if title.isalpha():
            return True
        if len(title) <= 3:
            return True
        vowels = sum(1 for char in title if char in "AEIOU")
        return vowels <= max(1, len(title) // 5)
    return False


def is_suspicious_author(value: str) -> bool:
    author = clean_text(value, 80).strip(" []()（）【】")
    if not author:
        return True
    lower = author.lower()
    if lower in KNOWN_NON_CHINESE_AUTHORS:
        return False
    if lower in GENERIC_AUTHORS:
        return True
    if re.fullmatch(r"[-—–_.,;:|/\\\s]+", author):
        return True
    if "65533" in lower:
        return True
    if re.search(r"(https?://|www\.|\.(gif|jpg|jpeg|png|webp)\b)", lower):
        return True
    if author.count("�") >= 1 or author.count("?") >= max(1, len(author) // 2):
        return True
    if re.fullmatch(r"v?\d+(?:[._-]\d+)*", lower):
        return True
    if _looks_mojibake(author):
        return True
    if re.fullmatch(r"\d+(?:[\s_-]\d+)*", author):
        return True
    if "_" in author:
        return True
    if re.search(r"\bv\d+(?:\.\d+)?\b", lower):
        return True
    if not _has_chinese(author) and re.search(r"\d", author) and re.search(r"[A-Za-z]", author):
        return True
    if not _has_chinese(author) and re.search(r"(^|\s)by($|\s)", lower):
        return True
    if re.search(r"\b(notes?|novels?|stories|volume|vol\.?|book)\b", lower):
        return True
    if _looks_like_pinyin_phrase(author):
        return True
    if not _has_chinese(author) and re.search(r"[A-Z]{2,}[a-z]", author):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", author)
    if compact and len(compact) <= 2 and not _has_chinese(author):
        return True
    return False


def _choose_final_title(extracted: str, guessed: str, description: str, guessed_author: str = "") -> str:
    preferred = _prefer_better_title(extracted, guessed, guessed_author)
    if _is_good_title(preferred):
        return preferred
    for candidate in (
        _title_from_short_text(description),
        guessed,
        extracted,
    ):
        candidate = _clean_title_candidate(clean_text(str(candidate or ""), 180))
        if _is_good_title(candidate):
            return candidate
    return UNKNOWN_TITLE


def _choose_final_author(extracted: str, guessed: str) -> str:
    for candidate in (extracted, guessed):
        candidate = _clean_author(str(candidate or ""))
        if not is_suspicious_author(candidate):
            return candidate
    return UNKNOWN_AUTHOR


def _is_good_title(value: str) -> bool:
    title = _clean_title_candidate(clean_text(value, 180))
    return bool(title and not is_suspicious_title(title) and not _is_generic_section_title(title))


def _prefer_better_title(extracted: str, guessed: str, guessed_author: str = "") -> str:
    extracted = _clean_title_candidate(clean_text(extracted, 180))
    guessed = _clean_title_candidate(clean_text(guessed, 180))
    guessed_author = _clean_author(guessed_author)
    if not extracted:
        return guessed
    if guessed and _is_generic_section_title(extracted):
        return guessed
    if is_suspicious_title(extracted):
        return guessed or extracted
    if guessed and guessed_author and _normalize_title(extracted) == _normalize_title(guessed_author):
        return guessed
    if guessed and not _has_chinese(extracted) and _looks_like_author(extracted) and not _looks_like_author(guessed):
        return guessed
    if guessed and _has_chinese(guessed) and _has_chinese(extracted):
        extracted_norm = _normalize_title(extracted)
        guessed_norm = _normalize_title(guessed)
        if extracted_norm and guessed_norm and extracted_norm in guessed_norm and len(guessed_norm) > len(extracted_norm):
            return guessed
    return extracted


def _is_generic_section_title(value: str) -> bool:
    normalized = _normalize_title(value)
    if normalized in {
        "序",
        "序言",
        "序章",
        "自序",
        "前言",
        "楔子",
        "目录",
        "正文",
        "引言",
        "导言",
        "简介",
        "内容简介",
        "后记",
        "尾声",
        "附录",
        "上一页",
        "下一页",
        "目录页",
    }:
        return True
    if re.fullmatch(r"第[一二三四五六七八九十百千万零〇\d]+[章节回节集].*", normalized):
        return True
    if re.fullmatch(r".{1,8}第[一二三四五六七八九十百千万零〇\d]+", normalized):
        return True
    if re.fullmatch(r"chapter\d+.*", normalized, flags=re.I):
        return True
    return False


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _normalize_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.lower())


def build_simple_description(title: str, authors: str = "", tags: list[str] | None = None, ext: str = "") -> str:
    clean_title = clean_text(title, 120) or "这本书"
    clean_authors = clean_text(authors, 120)
    useful_tags = [tag for tag in (tags or []) if tag and tag != "未分类"]
    category_part = f"{'、'.join(useful_tags[:3])}类" if useful_tags else "本地"
    author_part = f"，作者为 {clean_authors}" if clean_authors else ""
    format_part = f"，本地文件格式为 {ext.lstrip('.').upper()}" if ext else ""
    return f"《{clean_title}》是一本{category_part}电子书{author_part}{format_part}。"


def _description_quality(value: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    score = min(len(text), 240)
    if re.search(r"[\u4e00-\u9fff]", text):
        score += 20
    if re.search(r"[。！？.!?]", text):
        score += 10
    if len(text) < 20:
        score -= 25
    if "本地文件格式为" in text or "是一本本地电子书" in text:
        score -= 80
    return score


def _read_pdf(path: Path, extract_text: bool) -> dict[str, Any]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return {}

    reader = PdfReader(str(path))
    result: dict[str, Any] = {}
    meta = reader.metadata or {}
    title = getattr(meta, "title", None) or meta.get("/Title") if hasattr(meta, "get") else None
    author = getattr(meta, "author", None) or meta.get("/Author") if hasattr(meta, "get") else None
    if title:
        result["title"] = str(title)
    if author:
        result["authors"] = str(author)

    if extract_text and not getattr(reader, "is_encrypted", False):
        fragments: list[str] = []
        for page in reader.pages[:2]:
            try:
                fragments.append(page.extract_text() or "")
            except Exception:
                continue
        if fragments:
            result["description"] = clean_text(" ".join(fragments))
    return result


def _read_docx(path: Path, extract_text: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with zipfile.ZipFile(path) as archive:
        if "docProps/core.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("docProps/core.xml"))
            for elem in root.iter():
                tag = _local_name(elem.tag)
                text = clean_text(elem.text or "", 240)
                if not text:
                    continue
                if tag == "title":
                    result["title"] = text
                elif tag == "creator":
                    result["authors"] = text
                elif tag == "description":
                    result["description"] = text
        if extract_text and "word/document.xml" in archive.namelist() and not result.get("description"):
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            pieces = [node.text or "" for node in root.iter() if _local_name(node.tag) == "t"]
            result["description"] = clean_text(" ".join(pieces))
    return result


def _read_sidecar_opf(path: Path) -> dict[str, Any]:
    opf_path = _find_sidecar_opf(path)
    if not opf_path:
        return {}
    try:
        return _read_opf_path(opf_path, base_dir=opf_path.parent)
    except Exception:
        return {}


def _find_sidecar_opf(path: Path) -> Path | None:
    exact = path.with_suffix(".opf")
    if exact.exists():
        return exact
    siblings = sorted(path.parent.glob("*.opf"))
    if len(siblings) == 1:
        return siblings[0]
    return None


def _read_opf_path(path: Path, base_dir: Path) -> dict[str, Any]:
    root = ElementTree.fromstring(path.read_bytes())
    result: dict[str, Any] = {"subjects": []}
    cover_id = ""
    manifest: dict[str, str] = {}
    for elem in root.iter():
        tag = _local_name(elem.tag).lower()
        text = clean_text(elem.text or "", 800)
        if tag == "title" and text and not result.get("title"):
            result["title"] = text
        elif tag in {"creator", "author"} and text:
            result["authors"] = _append_value(result.get("authors", ""), _clean_author(text))
        elif tag == "description" and text:
            result["description"] = text
        elif tag == "language" and text:
            result["language"] = text
        elif tag == "publisher" and text:
            result["publisher"] = text
        elif tag == "date" and text:
            year = re.search(r"\d{4}", text)
            if year:
                result["published_year"] = year.group(0)
        elif tag == "identifier" and text and _looks_like_isbn(text):
            result["isbn"] = text
        elif tag == "subject" and text:
            result["subjects"].append(text)
        elif tag == "meta":
            if elem.attrib.get("name", "").lower() == "cover":
                cover_id = elem.attrib.get("content", "")
        elif tag == "item":
            item_id = elem.attrib.get("id", "")
            href = elem.attrib.get("href", "")
            if item_id and href:
                manifest[item_id] = href

    if cover_id and cover_id in manifest:
        cover = (base_dir / manifest[cover_id]).resolve()
        if cover.exists():
            result["cover_path"] = str(cover)
    if not result.get("cover_path"):
        cover = _find_sidecar_cover(path)
        if cover:
            result["cover_path"] = str(cover)
    return result


def _looks_like_isbn(value: str) -> bool:
    compact = re.sub(r"[^0-9Xx]", "", value)
    return len(compact) in {10, 13}


def _find_sidecar_cover(path: Path) -> Path | None:
    candidates: list[Path] = []
    for pattern in ("cover.*", "folder.*", f"{path.stem}.*"):
        candidates.extend(path.parent.glob(pattern))
    for cover in candidates:
        if cover.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and cover.exists():
            return cover
    return None


def _read_epub(path: Path, extract_text: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"subjects": []}
    with zipfile.ZipFile(path) as archive:
        opf_name = _find_epub_opf(archive)
        if opf_name:
            opf_root = ElementTree.fromstring(archive.read(opf_name))
            cover_id = ""
            manifest: dict[str, str] = {}
            for elem in opf_root.iter():
                tag = _local_name(elem.tag).lower()
                text = clean_text(elem.text or "", 400)
                if not text:
                    continue
                if tag == "title" and not result.get("title"):
                    result["title"] = text
                elif tag in {"creator", "author"}:
                    result["authors"] = _append_value(result.get("authors", ""), text)
                elif tag == "description":
                    result["description"] = text
                elif tag == "language":
                    result["language"] = text
                elif tag == "publisher":
                    result["publisher"] = text
                elif tag == "subject":
                    result["subjects"].append(text)
                elif tag == "meta" and elem.attrib.get("name", "").lower() == "cover":
                    cover_id = elem.attrib.get("content", "")
                elif tag == "item":
                    item_id = elem.attrib.get("id", "")
                    href = elem.attrib.get("href", "")
                    if item_id and href:
                        manifest[item_id] = href
            if cover_id and cover_id in manifest:
                result["cover_path"] = f"epub:{opf_name}:{manifest[cover_id]}"

        if extract_text and not result.get("description"):
            html_name = _first_epub_text_item(archive)
            if html_name:
                raw = archive.read(html_name)[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
                result["description"] = clean_text(_strip_markup(raw))
    return result


def _find_epub_opf(archive: zipfile.ZipFile) -> str:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except Exception:
        return ""
    for elem in container.iter():
        if _local_name(elem.tag) == "rootfile":
            full_path = elem.attrib.get("full-path", "")
            if full_path in archive.namelist():
                return full_path
    return ""


def _first_epub_text_item(archive: zipfile.ZipFile) -> str:
    for name in archive.namelist():
        lower = name.lower()
        if lower.endswith((".xhtml", ".html", ".htm")) and "cover" not in lower:
            return name
    return ""


def _read_plain_text(path: Path, extract_text: bool) -> dict[str, Any]:
    if not extract_text:
        return {}
    raw = path.read_bytes()[:MAX_READ_BYTES]
    text = _decode_bytes(raw)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    display_lines = [_clean_title_candidate(re.sub(r"^#+\s*", "", line)) for line in lines]
    display_lines = [line for line in display_lines if line]
    result: dict[str, Any] = {}
    for candidate in display_lines[:12]:
        if _looks_like_title_line(candidate):
            line_title, line_author = _title_author_from_line(candidate)
            result["title"] = line_title
            if line_author:
                result["authors"] = line_author
            break
    if len(display_lines) > 1 and re.match(r"^(作者|author)[:：]", display_lines[1], flags=re.I):
        result["authors"] = re.sub(r"^(作者|author)[:：]\s*", "", display_lines[1], flags=re.I)
    result["description"] = clean_text("\n".join(display_lines[:24]))
    return result


def _looks_like_title_line(value: str) -> bool:
    line = _clean_title_candidate(clean_text(value, 120)).strip("《》")
    if not 2 <= len(line) <= 80:
        return False
    if is_suspicious_title(line):
        return False
    if _is_generic_section_title(line):
        return False
    if re.search(r"[。；;]", line):
        return False
    if len(re.findall(r"[，,]", line)) > 1:
        return False
    if re.search(r"[，,]", line) and not _looks_like_short_punctuated_title(line):
        return False
    if len(line) > 20 and re.search(r"(起来|我的|他的|她的|它的|于是|因为|所以|但是|然后|已经|被评为|将)", line):
        return False
    if re.search(r"[！？!?]", line) and len(line) > 32:
        return False
    if re.match(r"^[★☆*#\-=]+", line):
        return False
    if re.search(r"(本书|简介|目录|版权|出版社|制作|下载|www\.|http|上一页|下一页|被评为|话题包括|专栏)", line, flags=re.I):
        return False
    return True


def _title_author_from_line(value: str) -> tuple[str, str]:
    line = _clean_title_candidate(clean_text(value, 180))
    match = re.match(r"^《([^》]+)》\s*(?:作者)?[:：]\s*(.+)$", line, flags=re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return line.strip("《》"), ""


def _clean_title_candidate(value: str) -> str:
    text = clean_text(value, 180)
    text = re.sub(r"[_]+", " ", text)
    previous = ""
    while previous != text:
        previous = text
        text = re.sub(r"^(上一页|下一页|目录页|回目录|正文回目录|当前位置|您所在的位置)\s*[>＞:：|｜\\/\-]*\s*", "", text)
    text = re.sub(r"^(第\s*\d+\s*节[:：]\s*)", "", text)
    text = text.strip(" \t\r\n-_—–.。·•:：|｜>＞")
    return text


def _read_rtf(path: Path, extract_text: bool) -> dict[str, Any]:
    if not extract_text:
        return {}
    raw = path.read_bytes()[:MAX_READ_BYTES]
    text = _decode_bytes(raw)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\d* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return {"description": clean_text(text)}


def _read_mobi_like(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()[:MAX_READ_BYTES]
    result: dict[str, Any] = {}
    palm_title = _decode_bytes(raw[:32]).strip("\x00 ")
    if palm_title and not is_suspicious_title(palm_title):
        result["title"] = palm_title

    offset = raw.find(b"EXTH")
    if offset == -1 or offset + 12 > len(raw):
        return result
    try:
        exth_len = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        count = int.from_bytes(raw[offset + 8 : offset + 12], "big")
    except Exception:
        return result

    subjects: list[str] = []
    cursor = offset + 12
    end = min(len(raw), offset + exth_len)
    for _ in range(count):
        if cursor + 8 > end:
            break
        rec_type = int.from_bytes(raw[cursor : cursor + 4], "big")
        rec_len = int.from_bytes(raw[cursor + 4 : cursor + 8], "big")
        payload = raw[cursor + 8 : cursor + rec_len]
        text = clean_text(_decode_bytes(payload), 500)
        if text:
            if rec_type == 100:
                result["authors"] = _append_value(result.get("authors", ""), _clean_author(text))
            elif rec_type == 101:
                result["publisher"] = text
            elif rec_type == 103:
                result["description"] = text
            elif rec_type == 104:
                result["isbn"] = text
            elif rec_type == 105:
                subjects.append(text)
            elif rec_type == 106:
                result["published_year"] = text[:4]
            elif rec_type == 503:
                if not is_suspicious_title(text):
                    result["title"] = text
        cursor += max(rec_len, 8)
    if subjects:
        result["subjects"] = subjects
    if not result.get("title") and result.get("description"):
        short_title = _title_from_short_text(str(result["description"]))
        if short_title:
            result["title"] = short_title
    return result


def _title_from_short_text(value: str) -> str:
    text = clean_text(value, 120)
    if not text:
        return ""
    quoted = _quoted_title_candidates(text)
    if quoted:
        return quoted[0]
    if _looks_like_title_line(text) and len(text) <= 40:
        return text.strip("《》")
    return ""


def _quoted_title_candidates(text: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    quoted_patterns = [r"《([^》]{2,60})》", r"“([^”]{2,40})”", r"\"([^\"]{2,40})\""]
    for pattern in quoted_patterns:
        for match in re.finditer(pattern, text):
            raw = match.group(1)
            title = re.sub(r"[·\-—–]序$", "", raw.strip())
            if _is_good_quoted_title(title):
                matches.append((match.start(), title))
    candidates: list[str] = []
    for _, title in sorted(matches, key=lambda item: item[0]):
        if title not in candidates:
            candidates.append(title)
    return candidates


def _clean_author(value: str) -> str:
    text = clean_text(value, 180)
    text = text.replace("#91;", "[").replace("#93;", "]")
    text = re.sub(r"^(作者|author)[:：]\s*", "", text, flags=re.I)
    return text.strip(" []()（）【】")


def _should_replace_title(current: str, candidate: str) -> bool:
    candidate = _clean_title_candidate(candidate)
    if not candidate:
        return False
    if not current:
        return True
    current_bad = is_suspicious_title(current) or _is_generic_section_title(current)
    candidate_bad = is_suspicious_title(candidate) or _is_generic_section_title(candidate)
    if current_bad:
        return not candidate_bad
    if candidate_bad:
        return False
    if _has_chinese(candidate) and not _has_chinese(current):
        return True
    return True


def _should_replace_author(current: str, candidate: str) -> bool:
    candidate = _clean_author(candidate)
    if not candidate:
        return False
    if not current:
        return not is_suspicious_author(candidate)
    current_bad = is_suspicious_author(current)
    candidate_bad = is_suspicious_author(candidate)
    if current_bad:
        return not candidate_bad
    if candidate_bad:
        return False
    if _has_chinese(candidate) and not _has_chinese(current):
        return True
    return True


def _looks_like_short_punctuated_title(value: str) -> bool:
    line = clean_text(value, 120)
    if re.search(r"[。；;]", line):
        return False
    if len(line) > 32:
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", line))


def _is_good_quoted_title(value: str) -> bool:
    title = _clean_title_candidate(clean_text(value, 80))
    if not _is_good_title(title):
        return False
    if len(title) <= 2 and title not in KNOWN_SHORT_TITLES:
        return False
    if re.search(r"(专栏|年度|读者最喜欢|话题|评论|访谈|简介|目录)", title):
        return False
    return True


def _looks_like_pinyin_phrase(value: str) -> bool:
    if _has_chinese(value) or _known_author_from_romanized(value):
        return False
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    tokens = [token for token in normalized.split() if token]
    if len(tokens) < 2:
        return False
    return all(token in PINYIN_SYLLABLES for token in tokens)


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    candidates = []
    for encoding in ("gb18030", "big5", "utf-8", "cp1252", "latin-1"):
        text = raw.decode(encoding, errors="ignore")
        candidates.append((_decoded_text_score(text), text))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else raw.decode("utf-8", errors="ignore")


def _decoded_text_score(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    mojibake = len(re.findall(r"[ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ]", text))
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    return cjk * 3 - mojibake - controls * 2


def _first_title_line_from_text(value: str) -> str:
    for raw in str(value or "").splitlines():
        line = _clean_title_candidate(raw)
        if _looks_like_title_line(line):
            return _title_author_from_line(line)[0]
    text = clean_text(str(value or ""), 500)
    for sentence in re.split(r"[。！？!?]\s*", text):
        line = _clean_title_candidate(sentence)
        if _looks_like_title_line(line) and len(line) <= 40:
            return _title_author_from_line(line)[0]
    return ""


def _looks_mojibake(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    suspicious = len(re.findall(r"[�ÃÂÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõøùúûüýþÿ]", text))
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    return suspicious >= 2 and suspicious > chinese


def _strip_markup(value: str) -> str:
    value = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _append_value(existing: str, value: str) -> str:
    if not existing:
        return value
    if value in existing:
        return existing
    return f"{existing}; {value}"
