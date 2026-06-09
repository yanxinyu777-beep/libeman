from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from html import unescape

from .metadata import clean_text, is_suspicious_title
from .tagger import infer_tags, merge_tags


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BookVault/1.0"
DOUBAN_ACCESS_LIMITED_STATUS = "豆瓣访问受限"


def lookup_book_metadata(title: str, authors: str = "", filename: str = "", local_description: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    candidates = _lookup_candidates(title, filename, local_description)
    chinese_book = _looks_chinese_book(title, authors, local_description)
    clean_authors = _clean_authors(authors)

    douban: dict[str, Any] = {}
    for candidate in candidates:
        try:
            douban = lookup_douban_rating(candidate, clean_authors)
        except urllib.error.HTTPError as exc:
            douban = {"douban_rating_status": DOUBAN_ACCESS_LIMITED_STATUS if _is_access_limited(exc) else "查询失败"}
        except Exception:
            douban = {"douban_rating_status": "查询失败"}
        if chinese_book and douban.get("title") and not _has_chinese(str(douban.get("title"))):
            douban = {"douban_rating_status": "未找到"}
            continue
        anchor_title = title if title and not is_suspicious_title(title) else candidate
        if chinese_book and douban.get("title") and not _remote_title_can_replace(anchor_title, str(douban.get("title"))):
            douban = {"douban_rating_status": "未找到"}
            continue
        if douban.get("douban_rating") or douban.get("title"):
            break

    if not chinese_book:
        try:
            result.update(lookup_open_library(_best_open_library_title(candidates, title), authors))
        except Exception:
            result = {}
    if authors.strip():
        result["authors"] = authors.strip()

    douban_summary = str(douban.pop("description", "") or "").strip()
    if douban_summary and not result.get("description"):
        result["description"] = douban_summary
    if douban:
        result.update(douban)
    remote_tags = infer_tags(
        str(result.get("title") or title or ""),
        authors,
        str(result.get("description") or douban_summary or local_description or ""),
        filename,
    )
    if remote_tags:
        result["tags"] = merge_tags(result.get("tags"), remote_tags)

    if result.get("remote_source") and result.get("douban_rating"):
        result["remote_source"] = f"{result['remote_source']}; 豆瓣"
    elif result.get("douban_rating"):
        result["remote_source"] = "豆瓣"
    if not _remote_title_can_replace(title, str(result.get("title") or "")):
        result.pop("title", None)
    return result


def lookup_open_library(title: str, authors: str = "") -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        return {}

    params = {"title": title, "limit": "1"}
    if authors:
        params["author"] = authors.split(";")[0].split(",")[0].strip()
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    search = _fetch_json(url)
    docs = search.get("docs") or []
    if not docs:
        return {}

    doc = docs[0]
    work_key = str(doc.get("key") or "")
    details: dict[str, Any] = {}
    if work_key.startswith("/works/"):
        details = _fetch_json(f"https://openlibrary.org{work_key}.json")
        time.sleep(0.15)

    remote_title = doc.get("title") or title
    remote_authors = "; ".join(doc.get("author_name") or []) or authors
    subjects = list(doc.get("subject") or [])[:18]
    subjects.extend(details.get("subjects") or [])
    description = _description_from_work(details)
    first_year = doc.get("first_publish_year") or ""
    isbn = ""
    if doc.get("isbn"):
        isbn = str(doc["isbn"][0])
    language = ""
    if doc.get("language"):
        language = str(doc["language"][0])

    tags = merge_tags(infer_tags(remote_title, remote_authors, description, subjects=subjects))
    return {
        "title": remote_title,
        "authors": remote_authors,
        "description": description,
        "published_year": str(first_year),
        "isbn": isbn,
        "language": language,
        "tags": tags,
        "remote_source": "Open Library",
        "remote_url": f"https://openlibrary.org{work_key}" if work_key else "",
    }


def lookup_douban_rating(title: str, authors: str = "") -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        return {"douban_rating_status": "缺少书名"}

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    normalized_title = _normalize_match_text(title)
    for query in _douban_queries(title, authors):
        url = "https://www.douban.com/search?" + urllib.parse.urlencode({"cat": "1001", "q": query})
        try:
            html_text = _fetch_text(url)
        except urllib.error.HTTPError as exc:
            if _is_access_limited(exc):
                return {"douban_rating_status": DOUBAN_ACCESS_LIMITED_STATUS}
            raise
        except urllib.error.URLError:
            return {"douban_rating_status": "查询失败"}
        for candidate in _parse_douban_candidates(html_text):
            key = candidate.get("url") or f"{candidate.get('title')}|{candidate.get('cast')}"
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
        if any(item.get("rating") and _normalize_match_text(item.get("title", "")) == normalized_title for item in candidates):
            break
    if not candidates:
        return {"douban_rating_status": "未找到"}

    normalized_author = _normalize_match_text(_primary_author(authors))
    title_matches = [
        candidate
        for candidate in candidates
        if _normalize_match_text(candidate.get("title", "")) == normalized_title
    ]
    if not title_matches:
        return {"douban_rating_status": "未找到"}
    title_matches.sort(key=lambda item: _candidate_score(item, normalized_title, normalized_author), reverse=True)
    best = title_matches[0]
    base = {
        "title": best.get("title", ""),
        "douban_url": best.get("url", ""),
    }
    if not best.get("rating"):
        base["douban_rating_status"] = "无评分"
        if best.get("summary"):
            base["description"] = best.get("summary", "")
        return base

    base.update({
        "douban_rating": best.get("rating", ""),
        "douban_rating_status": "已获取",
        "description": best.get("summary", ""),
    })
    return base


def _lookup_candidates(title: str, filename: str = "", local_description: str = "") -> list[str]:
    candidates: list[str] = []
    if title and not is_suspicious_title(title):
        candidates.append(clean_text(title, 120))
    for item in _titles_from_description(local_description):
        candidates.append(item)
    file_title = _title_from_filename(filename)
    if file_title:
        candidates.append(file_title)
    if title:
        candidates.append(clean_text(title, 120))

    result: list[str] = []
    for item in candidates:
        item = clean_text(item, 120)
        if item and item not in result and not is_suspicious_title(item):
            result.append(item)
    return result or [clean_text(title or filename, 120)]


def _titles_from_description(description: str) -> list[str]:
    text = clean_text(description, 500)
    if not text:
        return []
    titles = [match.strip() for match in re.findall(r"《([^》]{2,60})》", text)]
    if len(text) <= 40 and not re.search(r"[。！？.!?，,；;]", text):
        titles.append(text)
    return titles[:3]


def _title_from_filename(filename: str) -> str:
    if not filename:
        return ""
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename)
    stem = re.sub(r"\([^)]*\)", " ", stem)
    stem = re.sub(r"\[[^\]]*\]", " ", stem)
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.split(r"\s+[-—–－]\s+", stem, maxsplit=1)[0]
    known = _known_romanized_title(stem)
    if known:
        return known
    return clean_text(stem, 120)


def _best_open_library_title(candidates: list[str], fallback: str) -> str:
    for candidate in candidates:
        if re.search(r"[A-Za-z]", candidate):
            return candidate
    return fallback or (candidates[0] if candidates else "")


def _looks_chinese_book(title: str, authors: str, description: str) -> bool:
    return _has_chinese(" ".join([title or "", authors or "", description or ""]))


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _remote_title_can_replace(local_title: str, remote_title: str) -> bool:
    if not remote_title:
        return False
    if not local_title or is_suspicious_title(local_title):
        return True
    local_norm = _normalize_match_text(local_title)
    remote_norm = _normalize_match_text(remote_title)
    return bool(local_norm and remote_norm and local_norm == remote_norm)


def _known_romanized_title(value: str) -> str:
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    known = {
        "ya li guan li": "压力管理",
        "yi zi qi yuan xin jie": "汉字起源新解",
        "shan hai jing xiao zhu": "山海经校注",
        "ting xia lai zhuan shen": "停下来，转身",
        "zhi ri zhi ri zhe ci che di l": "知日！知日！这次彻底了解日本",
    }
    return known.get(normalized, "")


def _parse_douban_candidates(html_text: str) -> list[dict[str, str]]:
    blocks = re.split(r'<div\s+class="result">', html_text, flags=re.I)
    candidates: list[dict[str, str]] = []
    for block in blocks[1:]:
        block = block.split('<div class="result"', 1)[0]
        title = _first_match(block, r'<a[^>]+title="([^"]+)"')
        if not title:
            title = _strip_tags(_first_match(block, r"<h3>.*?<a[^>]*>(.*?)</a>", re.S))
        rating = _first_match(block, r'<span\s+class="rating_nums">([0-9.]+)</span>')
        cast = _strip_tags(_first_match(block, r'<span\s+class="subject-cast">(.*?)</span>', re.S))
        summary = _strip_tags(_first_match(block, r"<p>(.*?)</p>", re.S))
        raw_link = _first_match(block, r'href="([^"]*book\.douban\.com%2Fsubject%2F[^"]+)"')
        if not raw_link:
            raw_link = _first_match(block, r'href="(https://book\.douban\.com/subject/\d+/)"')
        subject_url = _decode_douban_link(raw_link)
        if title or rating:
            candidates.append(
                {
                    "title": unescape(title).strip(),
                    "rating": rating.strip(),
                    "cast": unescape(cast).strip(),
                    "summary": unescape(summary).strip(),
                    "url": subject_url,
                }
            )
    return candidates


def _candidate_score(candidate: dict[str, str], title: str, author: str) -> int:
    candidate_title = _normalize_match_text(candidate.get("title", ""))
    cast = _normalize_match_text(candidate.get("cast", ""))
    score = 0
    if title and candidate_title == title:
        score += 80
    if author and author in cast:
        score += 35
    return score


def _decode_douban_link(raw_link: str) -> str:
    if not raw_link:
        return ""
    link = unescape(raw_link)
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)
    target = query.get("url", [""])[0]
    if target:
        return urllib.parse.unquote(target)
    return link


def _first_match(value: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, value, flags | re.I)
    return match.group(1) if match else ""


def _strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _primary_author(authors: str) -> str:
    return re.split(r"[;,，、/；]", _clean_authors(authors), maxsplit=1)[0].strip()


def _clean_authors(authors: str) -> str:
    text = clean_text(authors, 180)
    text = re.sub(r"(著者|作者|译者|编者|主编|校注|点校)\s*[:：]", "", text)
    text = re.sub(r"\[[^\]]+\]|【[^】]+】|（[^）]+）|\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ;,，、/")


def _douban_queries(title: str, authors: str) -> list[str]:
    clean_author = _primary_author(authors)
    raw_queries = []
    if clean_author:
        raw_queries.append(f"{title} {clean_author}")
    raw_queries.append(title)
    queries: list[str] = []
    for query in raw_queries:
        query = clean_text(query, 140)
        if query and query not in queries:
            queries.append(query)
    return queries


def _normalize_match_text(value: str) -> str:
    value = unescape(value or "").lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[《》“”\"'：:·.\-—–_()\[\]（）【】]", "", value)
    return value


def _fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(1024 * 1024)
    return json.loads(data.decode("utf-8", errors="ignore"))


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        data = response.read(2 * 1024 * 1024)
    return data.decode("utf-8", errors="ignore")


def _is_access_limited(exc: urllib.error.HTTPError) -> bool:
    return exc.code in {403, 429}


def _description_from_work(work: dict[str, Any]) -> str:
    description = work.get("description") or ""
    if isinstance(description, dict):
        description = description.get("value") or ""
    return str(description).strip()[:1400]
