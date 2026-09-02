#!/usr/bin/env python3
"""Build persistent research-paper and daily-digest RSS feeds."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email as email_module
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


UTC = dt.timezone.utc
ARXIV_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_META_NS = "{http://arxiv.org/schemas/atom}"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"
PRISM_NS = "http://prismstandard.org/namespaces/basic/2.0/"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_doi(value: str | None) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text)
    return text.rstrip(".,; ")


def arxiv_identity(value: str | None) -> tuple[str, int | None]:
    match = re.search(r"(?i)(?:arxiv:|/abs/)?(\d{4}\.\d{4,5})(?:v(\d+))?", value or "")
    if not match:
        return "", None
    return match.group(1), int(match.group(2)) if match.group(2) else None


def canonical_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            return ""
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, item) for key, item in query if not key.lower().startswith("utm_")]
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(query), "")
        )
    except ValueError:
        return ""


def iso_datetime(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def parse_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class Paper:
    source: str
    paper_id: str
    title: str
    url: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    doi: str = ""
    venue: str = ""
    published: dt.datetime | None = None
    updated: dt.datetime | None = None

    def aliases(self) -> list[str]:
        aliases: list[str] = []
        arxiv_id, _ = arxiv_identity(f"{self.paper_id} {self.url}")
        doi = normalize_doi(self.doi)
        url = canonical_url(self.url)
        if arxiv_id:
            aliases.append(f"arxiv:{arxiv_id}")
        if doi:
            aliases.append(f"doi:{doi}")
        if self.paper_id:
            aliases.append(f"source:{self.source.lower()}:{clean_text(self.paper_id).lower()}")
        if url:
            aliases.append(f"url:{url}")
        return list(dict.fromkeys(aliases))


@dataclass(slots=True)
class EmailDelivery:
    received_day: str
    message_hash: str
    papers: list[Paper]


class PaperStore:
    def __init__(self, data: dict[str, Any]):
        if data.get("version") != 1 or not isinstance(data.get("papers"), dict):
            raise ValueError("Unsupported or invalid research RSS state")
        self.data = data

    @classmethod
    def empty(cls) -> "PaperStore":
        return cls(
            {
                "version": 1,
                "papers": {},
                "digests": {},
                "processed_email_hashes": [],
                "last_run": "",
            }
        )

    @classmethod
    def load(cls, path: Path) -> "PaperStore":
        if not path.exists():
            return cls.empty()
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _alias_index(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, record in self.data["papers"].items():
            for alias in record.get("aliases", []):
                result[alias] = key
        return result

    def upsert(self, paper: Paper, *, now: str | None = None) -> str:
        now = now or dt.datetime.now(UTC).isoformat()
        aliases = paper.aliases()
        index = self._alias_index()
        key = next((index[item] for item in aliases if item in index), "")
        if not key:
            key = next((item for item in aliases if item.startswith("arxiv:")), "")
            key = key or next((item for item in aliases if item.startswith("doi:")), "")
            key = key or hashlib.sha256("\0".join(aliases or [paper.title]).encode()).hexdigest()[:24]

        record = self.data["papers"].get(key)
        arxiv_id, arxiv_version = arxiv_identity(f"{paper.paper_id} {paper.url}")
        if record is None:
            record = {
                "key": key,
                "aliases": [],
                "source_ids": {},
                "sources": [],
                "title": "",
                "url": "",
                "abstract": "",
                "authors": [],
                "categories": [],
                "doi": "",
                "venue": "",
                "arxiv_id": arxiv_id,
                "arxiv_version": arxiv_version,
                "published": "",
                "updated": "",
                "first_seen": now,
                "last_seen": now,
                "digest_dates": [],
                "score": 0,
                "reason": "",
                "tags": [],
                "title_zh": "",
                "summary_zh": "",
                "insight_zh": "",
                "analysis_hash": "",
            }
            self.data["papers"][key] = record

        record["aliases"] = list(dict.fromkeys([*record.get("aliases", []), *aliases]))
        record["source_ids"][paper.source] = paper.paper_id
        record["sources"] = list(dict.fromkeys([*record.get("sources", []), paper.source]))
        record["last_seen"] = now
        incoming_version = arxiv_version or 0
        current_version = record.get("arxiv_version") or 0
        prefer_incoming = incoming_version >= current_version
        if paper.title and (not record["title"] or prefer_incoming):
            record["title"] = clean_text(paper.title)
        if paper.url and (not record["url"] or prefer_incoming):
            record["url"] = paper.url
        if len(clean_text(paper.abstract)) > len(record.get("abstract", "")):
            record["abstract"] = clean_text(paper.abstract)[:12000]
        if paper.authors and (not record["authors"] or prefer_incoming):
            record["authors"] = [clean_text(item) for item in paper.authors if clean_text(item)]
        record["categories"] = list(dict.fromkeys([*record.get("categories", []), *paper.categories]))
        record["doi"] = normalize_doi(paper.doi) or record.get("doi", "")
        record["venue"] = clean_text(paper.venue) or record.get("venue", "")
        record["arxiv_id"] = arxiv_id or record.get("arxiv_id", "")
        record["arxiv_version"] = max(current_version, incoming_version) or None
        incoming_published = iso_datetime(paper.published)
        if incoming_published and (not record["published"] or incoming_published < record["published"]):
            record["published"] = incoming_published
        record["updated"] = iso_datetime(paper.updated) or record.get("updated", "")
        return key


def parse_arxiv_email(body: str) -> list[Paper]:
    matches = list(re.finditer(r"(?mi)^arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)\s*$", body or ""))
    papers: list[Paper] = []
    for index, match in enumerate(matches):
        block = body[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(body)]
        fields: dict[str, str] = {}
        current = ""
        abstract_lines: list[str] = []
        in_abstract = False
        for raw in block.splitlines():
            stripped = raw.strip()
            if stripped.startswith("\\"):
                if fields.get("Title"):
                    in_abstract = not in_abstract
                continue
            if in_abstract:
                if stripped:
                    abstract_lines.append(stripped)
                continue
            header = re.match(r"^(Date|Title|Authors?|Categories|Subjects):\s*(.*)$", stripped)
            if header:
                name = "Authors" if header.group(1) in {"Author", "Authors"} else header.group(1)
                name = "Categories" if name == "Subjects" else name
                current = name
                fields[current] = header.group(2)
            elif raw[:1].isspace() and current and stripped:
                fields[current] = f"{fields[current]} {stripped}"
            elif not stripped:
                current = ""

        title = clean_text(fields.get("Title"))
        if not title:
            continue
        authors = [clean_text(item) for item in re.split(r",\s*|\s+and\s+", fields.get("Authors", "")) if clean_text(item)]
        categories = clean_text(fields.get("Categories", "")).split()
        paper_id = match.group(1)
        papers.append(
            Paper(
                source="arxiv-email",
                paper_id=paper_id,
                title=title,
                url=f"https://arxiv.org/abs/{paper_id}",
                abstract=clean_text(" ".join(abstract_lines)),
                authors=authors,
                categories=categories,
                venue="arXiv",
                published=parse_datetime(fields.get("Date")),
            )
        )
    return papers


def term_matches(term: str, text: str) -> bool:
    term = clean_text(term).lower()
    if not term:
        return False
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text.lower()) is not None


def keyword_score(paper: Paper, config: dict[str, Any]) -> tuple[int, list[str], str]:
    topics = config.get("topics", {})
    text = "\n".join([paper.title, paper.abstract, paper.venue])
    primary = [term for term in topics.get("primary", []) if term_matches(term, text)]
    methods = [term for term in topics.get("methods", []) if term_matches(term, text)]
    score = len(primary) * 18 + len(methods) * 12
    if primary and methods:
        score += 20
    if paper.source.startswith("arxiv"):
        score += 5
    if paper.doi:
        score += 3
    tags = list(dict.fromkeys([*primary, *methods]))[:8]
    reason = "关键词命中：" + "、".join(tags) if tags else "未命中核心关键词"
    return min(score, 100), tags, reason


def record_as_paper(record: dict[str, Any]) -> Paper:
    source = record.get("sources", ["unknown"])[0]
    source_ids = record.get("source_ids", {})
    return Paper(
        source=source,
        paper_id=source_ids.get(source, record.get("key", "")),
        title=record.get("title", ""),
        url=record.get("url", ""),
        abstract=record.get("abstract", ""),
        authors=record.get("authors", []),
        categories=record.get("categories", []),
        doi=record.get("doi", ""),
        venue=record.get("venue", ""),
        published=parse_datetime(record.get("published")),
        updated=parse_datetime(record.get("updated")),
    )


def _request(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 45) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_arxiv(config: dict[str, Any]) -> list[Paper]:
    settings = config.get("sources", {}).get("arxiv", {})
    if not settings.get("enabled", True):
        return []
    maximum = max(1, min(int(settings.get("max_results", 100)), 500))
    query = settings.get("query", "all:geospatial")
    papers: list[Paper] = []
    for start in range(0, maximum, 100):
        size = min(100, maximum - start)
        params = urllib.parse.urlencode(
            {"search_query": query, "sortBy": "lastUpdatedDate", "sortOrder": "descending", "start": start, "max_results": size}
        )
        payload = _request(
            f"https://export.arxiv.org/api/query?{params}",
            headers={"User-Agent": "journal-rss/1.0 (research digest)"},
        )
        root = ET.fromstring(payload)
        entries = root.findall(f"{ARXIV_NS}entry")
        for entry in entries:
            identifier = clean_text(entry.findtext(f"{ARXIV_NS}id"))
            paper_id = identifier.rstrip("/").split("/")[-1]
            category = entry.find(f"{ARXIV_META_NS}primary_category")
            papers.append(
                Paper(
                    source="arxiv-api",
                    paper_id=paper_id,
                    title=clean_text(entry.findtext(f"{ARXIV_NS}title")),
                    url=identifier.replace("http://", "https://"),
                    abstract=clean_text(entry.findtext(f"{ARXIV_NS}summary")),
                    authors=[clean_text(node.findtext(f"{ARXIV_NS}name")) for node in entry.findall(f"{ARXIV_NS}author")],
                    categories=[category.get("term")] if category is not None and category.get("term") else [],
                    doi=clean_text(entry.findtext(f"{ARXIV_META_NS}doi")),
                    venue="arXiv",
                    published=parse_datetime(entry.findtext(f"{ARXIV_NS}published")),
                    updated=parse_datetime(entry.findtext(f"{ARXIV_NS}updated")),
                )
            )
        if len(entries) < size:
            break
        if start + size < maximum:
            time.sleep(float(settings.get("page_delay_seconds", 3)))
    return papers


def _crossref_date(item: dict[str, Any], *names: str) -> dt.datetime | None:
    for name in names:
        value = item.get(name, {})
        parts = value.get("date-parts", []) if isinstance(value, dict) else []
        if not parts:
            continue
        values = parts[0]
        try:
            return dt.datetime(values[0], values[1] if len(values) > 1 else 1, values[2] if len(values) > 2 else 1, tzinfo=UTC)
        except (TypeError, ValueError):
            continue
    return None


def crossref_date_filter(now: dt.datetime, settings: dict[str, Any]) -> str:
    lookback = max(1, int(settings.get("created_lookback_days", 7)))
    from_date = (now.date() - dt.timedelta(days=lookback)).isoformat()
    return f"from-created-date:{from_date},until-created-date:{now.date().isoformat()},type:journal-article"


def fetch_crossref_journal(journal: dict[str, Any], config: dict[str, Any], now: dt.datetime) -> list[Paper]:
    settings = config.get("sources", {}).get("crossref", {})
    mailto = os.environ.get("CROSSREF_MAILTO", settings.get("mailto", "rss@example.com"))
    cursor = "*"
    maximum = max(1, int(settings.get("max_records_per_journal", 300)))
    rows = min(100, maximum)
    papers: list[Paper] = []
    while len(papers) < maximum:
        params = {
            "filter": crossref_date_filter(now, settings),
            "rows": min(rows, maximum - len(papers)),
            "cursor": cursor,
            "select": "DOI,title,abstract,author,published-online,published-print,published,created,indexed,container-title,URL",
            "mailto": mailto,
        }
        url = f"https://api.crossref.org/journals/{urllib.parse.quote(journal['issn'])}/works?{urllib.parse.urlencode(params)}"
        payload = json.loads(
            _request(url, headers={"User-Agent": f"journal-rss/1.0 (mailto: {mailto})"}).decode("utf-8")
        )["message"]
        items = payload.get("items", [])
        for item in items:
            title = clean_text((item.get("title") or [""])[0])
            if not title:
                continue
            doi = normalize_doi(item.get("DOI"))
            authors = [clean_text(" ".join(filter(None, [author.get("given"), author.get("family")]))) for author in item.get("author", [])]
            papers.append(
                Paper(
                    source=journal["short_name"],
                    paper_id=doi or item.get("URL", title),
                    title=title,
                    url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                    abstract=clean_text(re.sub(r"<[^>]+>", " ", item.get("abstract", ""))),
                    authors=[author for author in authors if author],
                    categories=[journal["short_name"]],
                    doi=doi,
                    venue=journal.get("name", journal["short_name"]),
                    published=_crossref_date(item, "published-online", "published-print", "published", "created"),
                    updated=parse_datetime((item.get("indexed") or {}).get("date-time")),
                )
            )
        next_cursor = payload.get("next-cursor")
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return papers[:maximum]


def _email_text(message: email_module.message.Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    fallback = ""
    for part in parts:
        if part.get_content_maintype() != "text":
            continue
        raw = part.get_payload(decode=True)
        if not raw:
            continue
        text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            return text
        fallback = fallback or text
    return fallback


def imap_received_datetime(metadata: bytes | str) -> dt.datetime | None:
    text = metadata.decode("ascii", errors="replace") if isinstance(metadata, bytes) else str(metadata)
    match = re.search(r'INTERNALDATE\s+"([^"]+)"', text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def _email_received_day(
    message: email_module.message.Message,
    fallback: dt.datetime,
    received_at: dt.datetime | None = None,
) -> str:
    received = received_at or parse_datetime(message.get("Date")) or fallback.astimezone(UTC)
    china_time = received.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return china_time.date().isoformat()


def fetch_arxiv_email_deliveries(
    store: PaperStore,
    config: dict[str, Any],
    *,
    max_emails: int | None = None,
    now: dt.datetime | None = None,
) -> tuple[list[EmailDelivery], str]:
    settings = config.get("sources", {}).get("email", {})
    if not settings.get("enabled", True):
        return [], "disabled"
    address = os.environ.get("ARXIV_EMAIL_ADDRESS")
    auth_code = os.environ.get("ARXIV_EMAIL_AUTH_CODE")
    if not address or not auth_code:
        return [], "disabled:no-secrets"
    now = now or dt.datetime.now(UTC)
    connection = imaplib.IMAP4_SSL(settings.get("server", "imap.qq.com"), int(settings.get("port", 993)))
    try:
        connection.login(address, auth_code)
        connection.select(settings.get("mailbox", "INBOX"), readonly=True)
        status, message_ids = connection.search(None, '(FROM "arXiv.org" SUBJECT "daily")')
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        known = set(store.data.get("processed_email_hashes", []))
        deliveries: list[EmailDelivery] = []
        maximum = max(1, int(max_emails if max_emails is not None else settings.get("max_emails", 30)))
        for message_number in message_ids[0].split()[-maximum:]:
            status, values = connection.fetch(message_number, "(RFC822 INTERNALDATE)")
            if status != "OK" or not values or not isinstance(values[0], tuple):
                continue
            message = email_module.message_from_bytes(values[0][1])
            message_id = message.get("Message-ID") or hashlib.sha256(values[0][1]).hexdigest()
            digest = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()
            if digest in known:
                continue
            deliveries.append(
                EmailDelivery(
                    received_day=_email_received_day(message, now, imap_received_datetime(values[0][0])),
                    message_hash=digest,
                    papers=parse_arxiv_email(_email_text(message)),
                )
            )
        return deliveries, f"ok:{len(deliveries)}"
    finally:
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass


def fetch_arxiv_emails(store: PaperStore, config: dict[str, Any]) -> tuple[list[Paper], list[str], str]:
    deliveries, status = fetch_arxiv_email_deliveries(store, config)
    papers = [paper for delivery in deliveries for paper in delivery.papers]
    hashes = [delivery.message_hash for delivery in deliveries]
    return papers, hashes, status


def _analysis_hash(record: dict[str, Any], config: dict[str, Any]) -> str:
    llm = config.get("llm", {})
    profile = config.get("research_profile", "")
    value = "\0".join([llm.get("model", "deepseek-chat"), profile, record.get("title", ""), record.get("abstract", "")])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def analyze_with_deepseek(store: PaperStore, keys: list[str], config: dict[str, Any]) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "disabled:no-secret"
    pending = [key for key in keys if store.data["papers"][key].get("analysis_hash") != _analysis_hash(store.data["papers"][key], config)]
    if not pending:
        return "cached"
    settings = config.get("llm", {})
    batch_size = max(1, min(int(settings.get("batch_size", 10)), 20))
    batches = [pending[offset : offset + batch_size] for offset in range(0, len(pending), batch_size)]
    workers = max(1, min(int(settings.get("workers", 1)), 16, len(batches)))
    completed = 0
    failures = 0

    def request_batch(batch: list[str]) -> tuple[list[str], list[dict[str, Any]] | None]:
        entries = []
        for key in batch:
            record = store.data["papers"][key]
            entries.append(
                {"id": key, "title": record["title"], "abstract": record.get("abstract", "")[:1800], "venue": record.get("venue", "")}
            )
        prompt = {
            "research_profile": config.get("research_profile", ""),
            "instructions": (
                "Return only a JSON array. For every input id return id, relevance_score (0-100), tags (array), "
                "reason_zh, title_zh, summary_zh, insight_zh. Translate faithfully. Treat insight as an inference, "
                "do not invent datasets, results, novelty or 'first' claims not stated in the abstract."
            ),
            "papers": entries,
        }
        body = json.dumps(
            {
                "model": settings.get("model", "deepseek-chat"),
                "temperature": 0.2,
                "max_tokens": int(settings.get("max_tokens", 4096)),
                "messages": [
                    {"role": "system", "content": "You are a careful GIS and remote-sensing literature editor."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = json.loads(
                _request(
                    settings.get("endpoint", "https://api.deepseek.com/chat/completions"),
                    data=body,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    timeout=120,
                ).decode("utf-8")
            )
            text = response["choices"][0]["message"]["content"].strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            results = json.loads(text)
        except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
            return batch, None
        return batch, results if isinstance(results, list) else None

    if workers == 1:
        responses = map(request_batch, batches)
    else:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        responses = executor.map(request_batch, batches)

    try:
        for batch_index, (batch, results) in enumerate(responses, 1):
            if results is None:
                failures += 1
                continue
            by_id = {item.get("id"): item for item in results if isinstance(item, dict)}
            for key in batch:
                item = by_id.get(key)
                if not item:
                    continue
                record = store.data["papers"][key]
                try:
                    record["score"] = max(0, min(100, int(item.get("relevance_score", record.get("score", 0)))))
                except (TypeError, ValueError):
                    pass
                record["tags"] = [clean_text(tag) for tag in item.get("tags", []) if clean_text(tag)][:8]
                record["reason"] = clean_text(item.get("reason_zh"))
                record["title_zh"] = clean_text(item.get("title_zh"))
                record["summary_zh"] = clean_text(item.get("summary_zh"))
                record["insight_zh"] = clean_text(item.get("insight_zh"))
                record["analysis_hash"] = _analysis_hash(record, config)
                completed += 1
            print(
                f"[info] deepseek batch={batch_index}/{len(batches)} completed={completed}/{len(pending)}"
            )
    finally:
        if workers > 1:
            executor.shutdown(wait=True)
    return f"ok:{completed}" if not failures else f"partial:{completed}/{len(pending)};failed-batches:{failures}"


def prune_unselected(store: PaperStore, config: dict[str, Any], now: dt.datetime) -> int:
    maximum = max(0, int(config.get("state", {}).get("max_unselected_papers", 2500)))
    unselected = [
        (key, record) for key, record in store.data["papers"].items() if not record.get("digest_dates")
    ]
    unselected.sort(key=lambda item: item[1].get("last_seen", ""), reverse=True)
    remove = unselected[maximum:] if maximum else unselected
    for key, _record in remove:
        del store.data["papers"][key]
    store.data.setdefault("runtime", {})["last_pruned_at"] = now.astimezone(UTC).isoformat()
    store.data["runtime"]["last_pruned_count"] = len(remove)
    return len(remove)


def build_daily_record(
    store: PaperStore,
    day: str,
    paper_keys: list[str],
    source_status: dict[str, str],
    *,
    guid_prefix: str = "research-daily",
) -> dict[str, Any]:
    for key in dict.fromkeys(paper_keys):
        record = store.data["papers"].get(key)
        if record is not None:
            record["digest_dates"] = list(dict.fromkeys([*record.get("digest_dates", []), day]))
    return {
        "date": day,
        "guid": f"{guid_prefix}:{day}",
        "paper_keys": list(dict.fromkeys(paper_keys)),
        "paper_count": len(set(paper_keys)),
        "source_status": dict(sorted(source_status.items())),
        "updated_at": dt.datetime.now(UTC).isoformat(),
    }


def _rss_root(title: str, link: str, description: str, language: str) -> tuple[ET.Element, ET.Element]:
    ET.register_namespace("atom", ATOM_NS)
    ET.register_namespace("content", CONTENT_NS)
    ET.register_namespace("dc", DC_NS)
    ET.register_namespace("prism", PRISM_NS)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = language
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(dt.datetime.now(UTC))
    ET.SubElement(channel, "generator").text = "journal-rss research_rss.py"
    ET.SubElement(channel, "ttl").text = "1440"
    atom_link = ET.SubElement(channel, f"{{{ATOM_NS}}}link")
    atom_link.set("href", link)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    return rss, channel


def _write_xml(root: ET.Element, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _paper_description(record: dict[str, Any]) -> str:
    parts = []
    if record.get("title_zh"):
        parts.append(f"<p><strong>中文标题：</strong>{html.escape(record['title_zh'])}</p>")
    if record.get("authors"):
        parts.append(f"<p><strong>作者：</strong>{html.escape(', '.join(record['authors']))}</p>")
    if record.get("abstract"):
        parts.append(f"<p><strong>Abstract:</strong> {html.escape(record['abstract'])}</p>")
    if record.get("summary_zh"):
        parts.append(f"<p><strong>中文摘要：</strong>{html.escape(record['summary_zh'])}</p>")
    if record.get("insight_zh"):
        parts.append(f"<p><strong>研究启发（模型推断）：</strong>{html.escape(record['insight_zh'])}</p>")
    parts.append(f"<p><strong>相关度：</strong>{record.get('score', 0)}/100 {html.escape(record.get('reason', ''))}</p>")
    return "".join(parts)


def write_paper_feed(
    store: PaperStore,
    path: Path,
    feed_url: str,
    max_items: int = 500,
    *,
    title: str = "科研精选论文",
    description: str = "arXiv 与期刊来源的个性化科研论文精选",
    guid_prefix: str = "research",
) -> None:
    rss, channel = _rss_root(title, feed_url, description, "en")
    records = [record for record in store.data["papers"].values() if record.get("digest_dates")]
    records.sort(key=lambda item: (max(item["digest_dates"]), item.get("published", "")), reverse=True)
    for record in records[:max_items]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = record["title"]
        ET.SubElement(item, "link").text = record["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = f"{guid_prefix}:{record['key']}"
        description = _paper_description(record)
        ET.SubElement(item, "description").text = description
        ET.SubElement(item, f"{{{CONTENT_NS}}}encoded").text = description
        for author in record.get("authors", []):
            ET.SubElement(item, f"{{{DC_NS}}}creator").text = author
        if record.get("doi"):
            ET.SubElement(item, f"{{{DC_NS}}}identifier").text = f"doi:{record['doi']}"
            ET.SubElement(item, f"{{{PRISM_NS}}}doi").text = record["doi"]
        if record.get("venue"):
            ET.SubElement(item, f"{{{PRISM_NS}}}publicationName").text = record["venue"]
        if record.get("published"):
            ET.SubElement(item, f"{{{PRISM_NS}}}publicationDate").text = record["published"]
        for category in list(dict.fromkeys([*record.get("tags", []), *record.get("categories", [])])):
            ET.SubElement(item, "category").text = category
        discovered = dt.datetime.fromisoformat(min(record["digest_dates"])).replace(
            tzinfo=dt.timezone(dt.timedelta(hours=8))
        )
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(discovered)
    _write_xml(rss, path)


def _daily_description(store: PaperStore, digest: dict[str, Any]) -> str:
    parts = [f"<p>本期收录 {digest['paper_count']} 篇论文。</p>", "<ol>"]
    for key in digest.get("paper_keys", []):
        record = store.data["papers"].get(key)
        if not record:
            continue
        title_zh = f" — {html.escape(record['title_zh'])}" if record.get("title_zh") else ""
        parts.append(
            f'<li><a href="{html.escape(record["url"], quote=True)}">{html.escape(record["title"])}</a>{title_zh} '
            f'({record.get("score", 0)}/100)</li>'
        )
    parts.append("</ol>")
    stats = digest.get("analysis_stats", {})
    if stats:
        parts.append(
            f"<p><strong>完整分析：</strong>DeepSeek 已分析 {stats.get('analyzed', 0)}/{stats.get('total', 0)}；"
            f"80–100 分 {stats.get('high', 0)} 篇；60–79 分 {stats.get('medium', 0)} 篇；"
            f"45–59 分 {stats.get('low', 0)} 篇；低于 45 分 {stats.get('below_threshold', 0)} 篇。</p>"
        )
    status = "；".join(f"{name}: {value}" for name, value in digest.get("source_status", {}).items())
    parts.append(f"<p><strong>采集状态：</strong>{html.escape(status)}</p>")
    return "".join(parts)


def write_daily_feed(
    store: PaperStore,
    path: Path,
    feed_url: str,
    max_items: int = 400,
    *,
    title: str = "科研论文每日速递",
    description: str = "每天一条的科研论文采集与精选记录",
    item_title: str = "科研论文每日速递",
    archive_path: str = "research-archive",
) -> None:
    rss, channel = _rss_root(title, feed_url, description, "zh-CN")
    for day, digest in sorted(store.data.get("digests", {}).items(), reverse=True)[:max_items]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"{item_title} | {day} | {digest['paper_count']} 篇"
        base_url = feed_url.rsplit("/", 1)[0]
        ET.SubElement(item, "link").text = f"{base_url}/{archive_path}/{day}.html"
        ET.SubElement(item, "guid", isPermaLink="false").text = digest["guid"]
        ET.SubElement(item, "description").text = _daily_description(store, digest)
        published = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
    _write_xml(rss, path)


def _public_digest(store: PaperStore, digest: dict[str, Any]) -> dict[str, Any]:
    papers = []
    for key in digest.get("paper_keys", []):
        record = store.data["papers"].get(key)
        if not record:
            continue
        papers.append(
            {
                name: record.get(name)
                for name in (
                    "key", "title", "title_zh", "url", "doi", "arxiv_id", "authors", "venue", "published",
                    "score", "reason", "tags", "abstract", "summary_zh", "insight_zh",
                )
            }
        )
    return {**digest, "papers": papers}


def write_daily_archive(
    store: PaperStore,
    directory: Path,
    base_url: str,
    *,
    title: str = "科研论文每日速递",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    links = []
    for day, digest in sorted(store.data.get("digests", {}).items(), reverse=True):
        public = _public_digest(store, digest)
        (directory / f"{day}.json").write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows = []
        for paper in public["papers"]:
            translated = f"<br><span>{html.escape(paper['title_zh'])}</span>" if paper.get("title_zh") else ""
            summary = paper.get("summary_zh") or paper.get("abstract") or "摘要暂缺"
            rows.append(
                f'<article><h2><a href="{html.escape(paper["url"], quote=True)}">{html.escape(paper["title"])}</a>{translated}</h2>'
                f'<p>{html.escape(clean_text(summary))}</p><small>相关度 {paper.get("score", 0)}/100 · {html.escape(paper.get("venue") or "")}</small></article>'
            )
        status = "；".join(f"{name}: {value}" for name, value in digest.get("source_status", {}).items())
        document = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>科研论文每日速递 {day}</title><style>body{{max-width:900px;margin:40px auto;padding:0 20px;font:16px/1.7 sans-serif}}"
            "article{padding:16px 0;border-bottom:1px solid #ddd}h2{font-size:19px}span,small{color:#566}a{color:#165d9c}</style></head><body>"
            f"<h1>{html.escape(title)} | {day}</h1><p>本期 {digest['paper_count']} 篇。采集状态：{html.escape(status)}</p>"
            + "".join(rows)
            + "</body></html>"
        )
        (directory / f"{day}.html").write_text(document, encoding="utf-8")
        links.append(f'<li><a href="{day}.html">{day}</a> ({digest["paper_count"]} 篇)</li>')
    index = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}归档</title></head><body><h1>{html.escape(title)}归档</h1><ul>"
        + "".join(links)
        + "</ul></body></html>"
    )
    (directory / "index.html").write_text(index, encoding="utf-8")


def select_papers(
    store: PaperStore,
    config: dict[str, Any],
    day: str,
    *,
    candidate_keys: Iterable[str] | None = None,
) -> list[str]:
    existing = list(dict.fromkeys(store.data.get("digests", {}).get(day, {}).get("paper_keys", [])))
    maximum = int(config.get("selection", {}).get("max_papers_per_day", 10))
    available = max(0, maximum - len(existing))
    analyze_all = bool(config.get("llm", {}).get("analyze_all", False))
    if available == 0 and not analyze_all:
        store.data.setdefault("runtime", {})["llm_status"] = "not-run:digest-full"
        return existing[:maximum]
    candidates = []
    keys = list(dict.fromkeys(candidate_keys)) if candidate_keys is not None else list(store.data["papers"])
    for key in keys:
        record = store.data["papers"].get(key)
        if record is None:
            continue
        if record.get("digest_dates"):
            continue
        paper = record_as_paper(record)
        if record.get("analysis_hash") != _analysis_hash(record, config):
            score, tags, reason = keyword_score(paper, config)
            record["score"], record["tags"], record["reason"] = score, tags, reason
        candidates.append(key)
    candidates.sort(
        key=lambda key: (store.data["papers"][key].get("score", 0), store.data["papers"][key].get("published", "")),
        reverse=True,
    )
    pending = [
        key for key in candidates
        if store.data["papers"][key].get("analysis_hash") != _analysis_hash(store.data["papers"][key], config)
    ]
    configured_limit = int(config.get("llm", {}).get("candidate_limit", 30))
    analyze_limit = len(pending) if configured_limit <= 0 else min(len(pending), configured_limit)
    try:
        status = analyze_with_deepseek(store, pending[:analyze_limit], config)
    except Exception as error:
        status = f"error:{type(error).__name__}"
    store.data.setdefault("runtime", {})["llm_status"] = status
    candidates.sort(
        key=lambda key: (store.data["papers"][key].get("score", 0), store.data["papers"][key].get("published", "")),
        reverse=True,
    )
    minimum = int(config.get("selection", {}).get("min_score", 45))
    additions = [key for key in candidates if store.data["papers"][key].get("score", 0) >= minimum][:available]
    return [*existing, *additions]


def _write_email_only_outputs(store: PaperStore, output_dir: Path, base_url: str) -> None:
    write_paper_feed(
        store,
        output_dir / "arxiv-email-papers.xml",
        f"{base_url}/arxiv-email-papers.xml",
        title="QQ 邮箱 arXiv 精选论文",
        description="仅从 QQ 邮箱当天收到的 arXiv 推送邮件中筛选的论文",
        guid_prefix="arxiv-email",
    )
    write_daily_feed(
        store,
        output_dir / "arxiv-email-daily.xml",
        f"{base_url}/arxiv-email-daily.xml",
        title="QQ 邮箱 arXiv 每日速递",
        description="仅记录 QQ 邮箱 arXiv 推送邮件的每日筛选结果",
        item_title="QQ 邮箱 arXiv 每日速递",
        archive_path="arxiv-email-analysis",
    )
    write_daily_archive(
        store,
        output_dir / "arxiv-email-archive",
        base_url,
        title="QQ 邮箱 arXiv 每日速递",
    )


def _email_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("email_only", {})
    runtime = dict(config)
    runtime["selection"] = {
        **config.get("selection", {}),
        "min_score": int(settings.get("min_score", 45)),
        "max_papers_per_day": int(settings.get("max_papers_per_day", 30)),
    }
    runtime["llm"] = {
        **config.get("llm", {}),
        "candidate_limit": 0,
        "analyze_all": True,
        "workers": int(settings.get("llm_workers", 8)),
    }
    runtime["state"] = {
        **config.get("state", {}),
        "max_unselected_papers": int(settings.get("max_state_papers", 10000)),
    }
    return runtime


def _email_analysis_stats(store: PaperStore, keys: list[str], config: dict[str, Any]) -> dict[str, int]:
    records = [store.data["papers"][key] for key in dict.fromkeys(keys) if key in store.data["papers"]]
    analyzed = [record for record in records if record.get("analysis_hash") == _analysis_hash(record, config)]
    scores = [int(record.get("score", 0)) for record in analyzed]
    return {
        "total": len(records),
        "analyzed": len(analyzed),
        "high": sum(score >= 80 for score in scores),
        "medium": sum(60 <= score < 80 for score in scores),
        "low": sum(45 <= score < 60 for score in scores),
        "below_threshold": sum(score < 45 for score in scores),
    }


def write_email_analysis_archive(
    store: PaperStore,
    directory: Path,
    day: str,
    keys: list[str],
    stats: dict[str, int],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    records = [store.data["papers"][key] for key in dict.fromkeys(keys) if key in store.data["papers"]]
    records.sort(key=lambda record: (record.get("score", 0), record.get("title", "")), reverse=True)
    fields = (
        "key", "title", "title_zh", "url", "arxiv_id", "authors", "categories", "published",
        "score", "reason", "tags", "summary_zh", "insight_zh",
    )
    papers = [{name: record.get(name) for name in fields} for record in records]
    payload = {"date": day, "analysis_stats": stats, "papers": papers}
    (directory / f"{day}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rows = []
    for paper in papers:
        translated = f"<br><span>{html.escape(paper['title_zh'])}</span>" if paper.get("title_zh") else ""
        summary = paper.get("summary_zh") or "分析暂缺"
        reason = paper.get("reason") or ""
        rows.append(
            f'<article><h2><a href="{html.escape(paper["url"], quote=True)}">{html.escape(paper["title"])}</a>{translated}</h2>'
            f'<p>{html.escape(clean_text(summary))}</p><small>相关度 {paper.get("score", 0)}/100 · {html.escape(reason)}</small></article>'
        )
    document = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>QQ 邮箱 arXiv 完整分析 {day}</title><style>body{{max-width:1000px;margin:40px auto;padding:0 20px;font:16px/1.7 sans-serif}}"
        "article{padding:16px 0;border-bottom:1px solid #ddd}h2{font-size:18px}span,small{color:#566}a{color:#165d9c}</style></head><body>"
        f"<h1>QQ 邮箱 arXiv 完整分析 | {day}</h1><p>DeepSeek 已分析 {stats['analyzed']}/{stats['total']} 篇；"
        f"80–100 分 {stats['high']} 篇，60–79 分 {stats['medium']} 篇，45–59 分 {stats['low']} 篇，"
        f"低于 45 分 {stats['below_threshold']} 篇。</p>"
        + "".join(rows)
        + "</body></html>"
    )
    (directory / f"{day}.html").write_text(document, encoding="utf-8")


def write_email_analysis_index(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    days = sorted((path.stem for path in directory.glob("*.json")), reverse=True)
    links = "".join(f'<li><a href="{day}.html">{day}</a></li>' for day in days)
    document = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>QQ 邮箱 arXiv 完整分析归档</title></head><body><h1>QQ 邮箱 arXiv 完整分析归档</h1><ul>"
        + links
        + "</ul></body></html>"
    )
    (directory / "index.html").write_text(document, encoding="utf-8")


def load_email_analysis_cache(directory: Path) -> dict[str, dict[str, Any]]:
    cached: dict[str, dict[str, Any]] = {}
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for paper in payload.get("papers", []):
            if isinstance(paper, dict) and paper.get("key") and paper.get("title_zh"):
                cached[paper["key"]] = paper
    return cached


def run_email_only(
    config_path: Path,
    state_path: Path,
    output_dir: Path,
    *,
    offline: bool = False,
    now: dt.datetime | None = None,
) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    email_config = _email_runtime_config(config)
    store = PaperStore.load(state_path)
    now = now or dt.datetime.now(UTC)
    base_url = config["base_url"].rstrip("/")

    if offline:
        _write_email_only_outputs(store, output_dir, base_url)
        write_email_analysis_index(output_dir / "arxiv-email-analysis")
        print(f"[info] email-only offline rebuild papers={len(store.data['papers'])} digests={len(store.data.get('digests', {}))}")
        return 0

    settings = config.get("email_only", {})
    analysis_mode = settings.get("analysis_mode", "all-v1")
    runtime = store.data.setdefault("runtime", {})
    if runtime.get("email_analysis_mode") != analysis_mode:
        store.data["processed_email_hashes"] = []
        store.data["digests"] = {}
        for record in store.data["papers"].values():
            record["digest_dates"] = []
            record["email_days"] = []
        runtime["email_analysis_mode"] = analysis_mode

    maximum = int(settings.get("max_emails", 10))
    deliveries, fetch_status = fetch_arxiv_email_deliveries(
        store,
        config,
        max_emails=maximum,
        now=now,
    )
    archived_analysis = load_email_analysis_cache(output_dir / "arxiv-email-analysis")
    grouped: dict[str, dict[str, Any]] = {}
    seen_at = now.astimezone(UTC).isoformat()
    for delivery in deliveries:
        group = grouped.setdefault(delivery.received_day, {"keys": [], "hashes": [], "messages": 0, "papers": 0})
        group["hashes"].append(delivery.message_hash)
        group["messages"] += 1
        group["papers"] += len(delivery.papers)
        for paper in delivery.papers:
            key = store.upsert(paper, now=seen_at)
            record = store.data["papers"][key]
            record["email_days"] = list(dict.fromkeys([*record.get("email_days", []), delivery.received_day]))
            cached = archived_analysis.get(key)
            if cached and record.get("analysis_hash") != _analysis_hash(record, email_config):
                for name in ("score", "reason", "tags", "title_zh", "summary_zh", "insight_zh"):
                    record[name] = cached.get(name, record.get(name))
                record["analysis_hash"] = _analysis_hash(record, email_config)
            group["keys"].append(key)

    successful_hashes: list[str] = []
    for day, group in sorted(grouped.items()):
        day_keys = [
            key for key, record in store.data["papers"].items()
            if day in record.get("email_days", [])
        ]
        previous = store.data.get("digests", {}).get(day, {})
        for key in previous.get("paper_keys", []):
            record = store.data["papers"].get(key)
            if record:
                record["digest_dates"] = [value for value in record.get("digest_dates", []) if value != day]
        store.data.setdefault("digests", {}).pop(day, None)
        selected = select_papers(store, email_config, day, candidate_keys=day_keys)
        stats = _email_analysis_stats(store, day_keys, email_config)
        statuses = {
            "arxiv-email": f"ok:{group['messages']};papers:{group['papers']}",
            "deepseek": store.data.get("runtime", {}).get("llm_status", "not-run"),
        }
        store.data.setdefault("digests", {})[day] = build_daily_record(
            store,
            day,
            selected,
            statuses,
            guid_prefix="arxiv-email-daily",
        )
        store.data["digests"][day]["analysis_stats"] = stats
        write_email_analysis_archive(store, output_dir / "arxiv-email-analysis", day, day_keys, stats)
        if stats["analyzed"] == stats["total"]:
            successful_hashes.extend(group["hashes"])

    processed = list(dict.fromkeys([*store.data.get("processed_email_hashes", []), *successful_hashes]))
    store.data["processed_email_hashes"] = processed[-5000:]
    store.data["last_run"] = seen_at
    store.data.setdefault("runtime", {})["email_fetch_status"] = fetch_status
    prune_unselected(store, email_config, now)
    _write_email_only_outputs(store, output_dir, base_url)
    write_email_analysis_index(output_dir / "arxiv-email-analysis")
    store.save(state_path)
    print(
        f"[info] email-only messages={len(deliveries)} papers={sum(len(item.papers) for item in deliveries)} "
        f"days={len(grouped)}"
    )
    return 0


def run(config_path: Path, state_path: Path, output_dir: Path, *, offline: bool = False, now: dt.datetime | None = None) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    store = PaperStore.load(state_path)
    now = now or dt.datetime.now(UTC)
    local_now = now.astimezone(dt.timezone(dt.timedelta(hours=8)))
    day = local_now.date().isoformat()
    base_url = config["base_url"].rstrip("/")

    if offline:
        write_paper_feed(store, output_dir / "research-papers.xml", f"{base_url}/research-papers.xml")
        write_daily_feed(store, output_dir / "research-daily.xml", f"{base_url}/research-daily.xml")
        write_daily_archive(store, output_dir / "research-archive", base_url)
        print(f"[info] offline rebuild papers={len(store.data['papers'])} digests={len(store.data.get('digests', {}))}")
        return 0

    statuses: dict[str, str] = {}
    email_hashes: list[str] = []
    papers: list[Paper] = []

    try:
        fetched = fetch_arxiv(config)
        papers.extend(fetched)
        statuses["arxiv-api"] = f"ok:{len(fetched)}"
    except Exception as error:
        statuses["arxiv-api"] = f"error:{type(error).__name__}"
    crossref_settings = config.get("sources", {}).get("crossref", {})
    journals = [journal for journal in crossref_settings.get("journals", []) if journal.get("enabled", True)]
    workers = max(1, min(int(crossref_settings.get("workers", 3)), len(journals) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_journals = {
            executor.submit(fetch_crossref_journal, journal, config, now): journal for journal in journals
        }
        for future in concurrent.futures.as_completed(future_journals):
            journal = future_journals[future]
            try:
                fetched = future.result()
                papers.extend(fetched)
                statuses[f"crossref:{journal['short_name']}"] = f"ok:{len(fetched)}"
            except Exception as error:
                statuses[f"crossref:{journal['short_name']}"] = f"error:{type(error).__name__}"
    try:
        fetched, email_hashes, email_status = fetch_arxiv_emails(store, config)
        papers.extend(fetched)
        statuses["arxiv-email"] = email_status
    except Exception as error:
        statuses["arxiv-email"] = f"error:{type(error).__name__}"

    seen_at = now.astimezone(UTC).isoformat()
    for paper in papers:
        store.upsert(paper, now=seen_at)
    selected = select_papers(store, config, day)
    statuses["deepseek"] = store.data.get("runtime", {}).get("llm_status", "not-run")
    digest = build_daily_record(store, day, selected, statuses)
    store.data.setdefault("digests", {})[day] = digest
    prune_unselected(store, config, now)
    processed = list(dict.fromkeys([*store.data.get("processed_email_hashes", []), *email_hashes]))
    store.data["processed_email_hashes"] = processed[-5000:]
    store.data["last_run"] = seen_at

    write_paper_feed(store, output_dir / "research-papers.xml", f"{base_url}/research-papers.xml")
    write_daily_feed(store, output_dir / "research-daily.xml", f"{base_url}/research-daily.xml")
    write_daily_archive(store, output_dir / "research-archive", base_url)
    store.save(state_path)
    print(f"[info] collected={len(papers)} selected={len(selected)} day={day}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research-config.json"))
    parser.add_argument("--state", type=Path, default=Path("research-data/state.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--offline", action="store_true", help="regenerate feeds from state without network or mailbox access")
    parser.add_argument("--email-only", action="store_true", help="build feeds only from QQ mailbox arXiv alerts")
    args = parser.parse_args()
    if args.email_only:
        return run_email_only(args.config, args.state, args.output_dir, offline=args.offline)
    return run(args.config, args.state, args.output_dir, offline=args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
