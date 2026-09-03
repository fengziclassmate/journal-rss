#!/usr/bin/env python3
"""Build complete conference feeds and a keyword-filtered combined feed."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup


UTC = dt.timezone.utc
ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
PRISM_NS = "http://prismstandard.org/namespaces/basic/2.0/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
USER_AGENT = "journal-rss/1.0 (https://github.com/fengziclassmate/journal-rss)"

ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("prism", PRISM_NS)
ET.register_namespace("content", CONTENT_NS)


@dataclass
class ConferencePaper:
    conference: str
    conference_name: str
    title: str
    url: str
    guid: str
    year: int
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    published: str = ""
    topics: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)


class PoliteClient:
    def __init__(self, delay_seconds: float = 1.5, cache_dir: Path | None = None) -> None:
        self.delay_seconds = delay_seconds
        self.last_request = 0.0
        self.cache_dir = cache_dir

    def get(self, url: str, *, timeout: int = 90) -> bytes:
        cache_path = None
        if self.cache_dir:
            cache_path = self.cache_dir / hashlib.sha256(url.encode("utf-8")).hexdigest()
            if cache_path.exists() and time.time() - cache_path.stat().st_mtime < 20 * 60 * 60:
                return cache_path.read_bytes()
        waits = (2, 5, 15, 30, 60, 120)
        for attempt, wait_after_error in enumerate(waits, start=1):
            elapsed = time.monotonic() - self.last_request
            if elapsed < self.delay_seconds:
                time.sleep(self.delay_seconds - elapsed)
            request_url = url
            if attempt % 2 == 0 and url.startswith("https://dblp.org/"):
                request_url = url.replace("https://dblp.org/", "https://dblp.uni-trier.de/", 1)
            request = urllib.request.Request(
                request_url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,application/xml"},
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    self.last_request = time.monotonic()
                    content = response.read()
                    if cache_path:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(content)
                    return content
            except urllib.error.HTTPError as exc:
                self.last_request = time.monotonic()
                if exc.code == 404:
                    if cache_path:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(b"")
                    return b""
                if attempt == len(waits):
                    raise
                time.sleep(wait_after_error)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.RemoteDisconnected):
                self.last_request = time.monotonic()
                if attempt == len(waits):
                    raise
                time.sleep(wait_after_error)
        raise AssertionError("unreachable")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).casefold())


def normalize_doi(value: str) -> str:
    value = clean_text(value).casefold()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def identity(paper: ConferencePaper) -> str:
    doi = normalize_doi(paper.doi)
    if doi:
        return f"doi:{doi}"
    url = clean_text(paper.url)
    if url and not any(host in url for host in ("ieeevis.org/year/",)):
        return f"url:{url.casefold()}"
    return f"title:{normalize_title(paper.title)}"


def feed_guid(paper: ConferencePaper, scope: str) -> str:
    digest = hashlib.sha256(identity(paper).encode("utf-8")).hexdigest()
    return f"{scope}:sha256:{digest}"


def eligible_years(start_year: int, end_year: int, parity: str = "") -> list[int]:
    years = list(range(start_year, end_year + 1))
    if parity == "odd":
        return [year for year in years if year % 2]
    if parity == "even":
        return [year for year in years if not year % 2]
    return years


def _author_names(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("author", [])
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    names: list[str] = []
    for author in raw:
        name = author.get("text", "") if isinstance(author, dict) else author
        name = re.sub(r"\s+\d{4}$", "", clean_text(name))
        if name:
            names.append(name)
    return names


def _is_main_paper(info: dict[str, Any], conference: dict[str, Any]) -> bool:
    text = " ".join((clean_text(info.get("venue")), clean_text(info.get("title")))).casefold()
    excluded = (
        "workshop", "companion", "doctoral consortium", "demonstration", "demo paper",
        "tutorial", "challenge report", "competition report",
    )
    if any(term in text for term in excluded):
        return False
    includes = conference.get("venue_include", [])
    if includes and not any(term.casefold() in clean_text(info.get("venue")).casefold() for term in includes):
        return False
    return bool(clean_text(info.get("title")))


def _parse_dblp_hit(hit: dict[str, Any], conference: dict[str, Any], year: int) -> ConferencePaper | None:
    info = hit.get("info", {})
    if not _is_main_paper(info, conference):
        return None
    title = clean_text(info.get("title")).removesuffix(".")
    key = clean_text(info.get("key"))
    doi = normalize_doi(info.get("doi", ""))
    link = clean_text(info.get("ee")) or clean_text(info.get("url"))
    if isinstance(info.get("ee"), list):
        link = clean_text(info["ee"][0])
    if not link:
        link = f"https://dblp.org/rec/{key}" if key else "https://dblp.org/"
    return ConferencePaper(
        conference=conference["acronym"], conference_name=conference["name"],
        title=title, url=link, guid=f"dblp:{key}" if key else identity_from_fields(doi, title),
        year=int(info.get("year") or year), authors=_author_names(info.get("authors")), doi=doi,
    )


def identity_from_fields(doi: str, title: str) -> str:
    if doi:
        return f"doi:{normalize_doi(doi)}"
    digest = hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()
    return f"urn:sha256:{digest}"


def fetch_dblp_year(
    client: PoliteClient, conference: dict[str, Any], stream: str, year: int, page_size: int = 1000
) -> list[ConferencePaper]:
    papers: list[ConferencePaper] = []
    offset = 0
    while True:
        query = f"stream:streams/{stream}: year:{year}"
        params = urllib.parse.urlencode({"q": query, "h": page_size, "f": offset, "format": "json"})
        payload = json.loads(client.get(f"https://dblp.uni-trier.de/search/publ/api?{params}"))
        hits_data = payload.get("result", {}).get("hits", {})
        total = int(hits_data.get("@total", 0))
        raw_hits = hits_data.get("hit", [])
        if isinstance(raw_hits, dict):
            raw_hits = [raw_hits]
        for hit in raw_hits:
            paper = _parse_dblp_hit(hit, conference, year)
            if paper:
                papers.append(paper)
        sent = int(hits_data.get("@sent", len(raw_hits)) or 0)
        offset += sent
        if not sent or offset >= total:
            break
    return papers


def _parse_dblp_toc(content: bytes, conference: dict[str, Any], fallback_year: int) -> list[ConferencePaper]:
    document = content.decode("utf-8", errors="replace")
    page_title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", document)
    page_title = clean_html_text(page_title_match.group(1)) if page_title_match else conference["acronym"]
    papers: list[ConferencePaper] = []
    entry_pattern = re.compile(
        r'(?is)<li class="entry (?:inproceedings|article)"([^>]*)>(.*?)(?=<li class="entry |\Z)'
    )
    for attributes, entry in entry_pattern.findall(document):
        record_key_match = re.search(r'id="([^"]+)"', attributes)
        record_key = clean_text(record_key_match.group(1) if record_key_match else "")
        title_match = re.search(r'(?is)<span class="title"[^>]*>(.*?)</span>', entry)
        title = clean_html_text(title_match.group(1) if title_match else "").removesuffix(".")
        if not title or not _is_main_paper({"title": title, "venue": page_title}, conference):
            continue
        authors = []
        author_pattern = re.compile(
            r'(?is)<span itemprop="author".*?<span itemprop="name"[^>]*>(.*?)</span>.*?</span>'
        )
        for raw_name in author_pattern.findall(entry):
            name = re.sub(r"\s+\d{4}$", "", clean_html_text(raw_name))
            if name and name not in authors:
                authors.append(name)
        year_match = re.search(r'(?is)<span itemprop="datePublished"[^>]*>(.*?)</span>', entry)
        try:
            year = int(clean_html_text(year_match.group(1)) if year_match else fallback_year)
        except ValueError:
            year = fallback_year
        doi = ""
        link = ""
        hrefs = re.findall(r'(?is)<a href="([^"]+)"[^>]*>', entry)
        for raw_href in hrefs:
            href = clean_text(raw_href)
            if not link and href.startswith("http"):
                link = href
            candidate = normalize_doi(href)
            if candidate.startswith("10."):
                doi = candidate
                link = href
                break
        if not link:
            link = f"https://dblp.org/rec/{record_key}" if record_key else "https://dblp.org/"
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=link,
            guid=f"dblp:{record_key}" if record_key else identity_from_fields(doi, title),
            year=year, authors=authors, doi=doi,
        ))
    return papers


def clean_html_text(value: str) -> str:
    return clean_text(re.sub(r"(?is)<[^>]+>", " ", value or ""))


def fetch_dblp_toc_year(
    client: PoliteClient, conference: dict[str, Any], stream: str, year: int
) -> list[ConferencePaper]:
    toc_urls = list(conference.get("dblp_tocs", {}).get(str(year), []))
    if stream.startswith("conf/"):
        index_url = f"https://dblp.org/db/{stream}/index.html"
        try:
            index_content = client.get(index_url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                index_content = b""
            else:
                raise
        soup = BeautifulSoup(index_content, "html.parser")
        prefix = f"/db/{stream}/"
        for anchor in soup.find_all("a", href=True):
            absolute = urllib.parse.urljoin(index_url, anchor["href"])
            parsed = urllib.parse.urlparse(absolute)
            if not parsed.path.startswith(prefix) or not parsed.path.endswith(".html"):
                continue
            if str(year) not in Path(parsed.path).stem:
                continue
            toc_urls.append(urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")))
    papers: list[ConferencePaper] = []
    for url in dict.fromkeys(toc_urls):
        try:
            papers.extend(_parse_dblp_toc(client.get(url), conference, year))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    return deduplicate(paper for paper in papers if paper.year == year)


def fetch_virtual_json(
    client: PoliteClient, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    template = conference.get("virtual_json")
    if not template:
        return []
    try:
        payload = json.loads(client.get(template.format(year=year)))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    except json.JSONDecodeError:
        return []
    rows = payload.get("results", payload if isinstance(payload, list) else [])
    next_url = payload.get("next") if isinstance(payload, dict) else None
    if next_url and conference.get("downloads_page"):
        return fetch_downloads_event(client, conference, year)
    while next_url:
        next_url = re.sub(r"^http://", "https://", next_url)
        page = json.loads(client.get(next_url))
        rows.extend(page.get("results", []))
        next_url = page.get("next")
    papers: list[ConferencePaper] = []
    for row in rows:
        title = clean_text(row.get("name") or row.get("title"))
        link = clean_text(row.get("paper_url") or row.get("url"))
        if not title or not link:
            continue
        source_url = clean_text(row.get("sourceurl"))
        if any(marker in source_url.casefold() for marker in ("workshop", "tutorial")):
            continue
        authors = [clean_text(item.get("fullname")) for item in row.get("authors", []) if item.get("fullname")]
        event_time = clean_text(row.get("starttime"))
        published = event_time[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", event_time) else ""
        topic = clean_text(row.get("topic"))
        guid = clean_text(row.get("uid")) or identity_from_fields("", title)
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=link, guid=f"{conference['slug']}:{guid}", year=year,
            authors=authors, published=published, topics=[topic] if topic else [],
        ))
    return papers


def fetch_downloads_event(
    client: PoliteClient, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    template = conference.get("downloads_page")
    if not template:
        return []
    url = template.format(year=year)
    try:
        soup = BeautifulSoup(client.get(url), "html.parser")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    papers: list[ConferencePaper] = []
    pattern = re.compile(rf"^/virtual/{year}/poster/\d+")
    for anchor in soup.find_all("a", href=pattern):
        title = clean_text(anchor.get_text(" "))
        if not title:
            continue
        link = urllib.parse.urljoin(url, anchor["href"])
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=link, guid=identity_from_fields("", title), year=year,
        ))
    return deduplicate(papers)


def fetch_cvf_event(client: PoliteClient, conference: dict[str, Any], year: int) -> list[ConferencePaper]:
    event = conference.get("cvf_event")
    if not event:
        return []
    url = f"https://openaccess.thecvf.com/{event}{year}?day=all"
    try:
        soup = BeautifulSoup(client.get(url), "html.parser")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    papers: list[ConferencePaper] = []
    for title_node in soup.select("dt.ptitle"):
        anchor = title_node.find("a", href=True)
        if not anchor:
            continue
        title = clean_text(anchor.get_text(" "))
        authors_node = title_node.find_next_sibling("dd")
        authors = [clean_text(node.get("value")) for node in authors_node.select('input[name="query_author"]')] if authors_node else []
        link = urllib.parse.urljoin(url, anchor["href"])
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=link, guid=identity_from_fields("", title), year=year, authors=authors,
        ))
    return papers


def fetch_eccv_event(client: PoliteClient, conference: dict[str, Any], year: int) -> list[ConferencePaper]:
    if not conference.get("eccv_event"):
        return []
    url = f"https://eccv.ecva.net/Conferences/{year}/AcceptedPapers"
    try:
        soup = BeautifulSoup(client.get(url), "html.parser")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    papers: list[ConferencePaper] = []
    for row in soup.select("table tr"):
        anchor = row.find("a", href=re.compile(rf"/virtual/{year}/poster/"))
        if not anchor:
            continue
        title = clean_text(anchor.get_text(" "))
        italic = row.find("i")
        authors = [clean_text(name) for name in re.split(r"\s*[\u22c5\u00b7]\s*", italic.get_text(" ") if italic else "") if clean_text(name)]
        topic_node = row.select_one(".elc-keywords")
        topics = [clean_text(topic_node.get_text(" "))] if topic_node else []
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=urllib.parse.urljoin(url, anchor["href"]),
            guid=identity_from_fields("", title), year=year, authors=authors, topics=topics,
        ))
    return papers


def parse_copernicus_volume(
    content: bytes, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    soup = BeautifulSoup(content, "html.parser")
    papers: list[ConferencePaper] = []
    for block in soup.select(".paperlist-object"):
        title_node = block.select_one("a.article-title[href]")
        title = clean_text(title_node.get_text(" ") if title_node else "")
        if not title or title.casefold().startswith(("preface", "editorial")):
            continue
        author_node = block.select_one(".authors")
        author_text = clean_text(author_node.get_text(" ") if author_node else "")
        author_text = re.sub(r",?\s+and\s+", ", ", author_text)
        authors = [clean_text(value) for value in author_text.split(",") if clean_text(value)]
        citation_node = block.select_one(".citation")
        citation = clean_text(citation_node.get_text(" ") if citation_node else "")
        doi_match = re.search(r"10\.5194/[A-Za-z0-9./_-]+", citation)
        doi = normalize_doi(doi_match.group(0).rstrip(".,")) if doi_match else ""
        date_node = block.select_one(".published-date")
        published = ""
        if date_node:
            try:
                published = dt.datetime.strptime(clean_text(date_node.get_text(" ")), "%d %b %Y").date().isoformat()
            except ValueError:
                pass
        link = urllib.parse.urljoin("https://isprs.org/", title_node["href"])
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=link, guid=identity_from_fields(doi, title), year=year,
            authors=authors, doi=doi, published=published,
        ))
    return deduplicate(papers)


def fetch_copernicus_volumes(
    client: PoliteClient, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    papers: list[ConferencePaper] = []
    for url in conference.get("copernicus_volumes", {}).get(str(year), []):
        content = client.get(url)
        if content:
            papers.extend(parse_copernicus_volume(content, conference, year))
    return deduplicate(papers)


def parse_robotics_proceedings(
    content: bytes, conference: dict[str, Any], year: int, edition: int, base_url: str
) -> list[ConferencePaper]:
    soup = BeautifulSoup(content, "html.parser")
    papers: list[ConferencePaper] = []
    roman = to_roman(edition)
    for anchor in soup.find_all("a", href=re.compile(r"^p\d+\.html$")):
        title = clean_text(anchor.get_text(" "))
        number_match = re.search(r"p(\d+)\.html$", anchor["href"])
        if not title or not number_match:
            continue
        number = number_match.group(1)
        author_node = anchor.find_next("i")
        authors = [clean_text(name) for name in clean_text(author_node.get_text(" ") if author_node else "").split(",") if clean_text(name)]
        doi = f"10.15607/RSS.{year}.{roman}.{number}"
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=urllib.parse.urljoin(base_url, anchor["href"]),
            guid=identity_from_fields(doi, title), year=year, authors=authors, doi=doi,
        ))
    return deduplicate(papers)


def fetch_robotics_proceedings(
    client: PoliteClient, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    edition = conference.get("robotics_proceedings", {}).get(str(year))
    if not edition:
        return []
    url = f"https://www.roboticsproceedings.org/rss{edition}/index.html"
    return parse_robotics_proceedings(client.get(url), conference, year, int(edition), url)


def to_roman(value: int) -> str:
    numerals = ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    result = ""
    for number, numeral in numerals:
        while value >= number:
            result += numeral
            value -= number
    return result


def parse_ieee_vis_page(
    content: bytes, conference: dict[str, Any], year: int, page_url: str
) -> list[ConferencePaper]:
    soup = BeautifulSoup(content, "html.parser")
    papers: list[ConferencePaper] = []
    for paragraph in soup.find_all("p"):
        title_node = paragraph.find("strong")
        if not title_node:
            continue
        section = paragraph.find_previous(["h3", "h4", "h5"])
        section_text = clean_text(section.get_text(" ") if section else "")
        title = clean_text(title_node.get_text(" "))
        if "short paper" in section_text.casefold() or "[short paper" in title.casefold():
            continue
        if "paper" not in section_text.casefold() and "[tvcg" not in title.casefold():
            continue
        title = re.sub(r"\s*\[(?:TVCG|Honorable Mention|Best Paper)(?:\s*\+\s*Virtual Talk)?\]\s*", " ", title, flags=re.I).strip()
        whole_text = clean_text(paragraph.get_text(" "))
        author_text = re.sub(r"^.*?\bby\s+", "", whole_text, count=1, flags=re.I)
        authors = [clean_text(name) for name in re.split(r",\s*|\s*&\s*", author_text) if clean_text(name)]
        papers.append(ConferencePaper(
            conference=conference["acronym"], conference_name=conference["name"],
            title=title, url=page_url, guid=identity_from_fields("", title), year=year, authors=authors,
        ))
    return deduplicate(papers)


def fetch_ieee_vis_page(
    client: PoliteClient, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    url = conference.get("ieee_vis_pages", {}).get(str(year))
    if not url:
        return []
    return parse_ieee_vis_page(client.get(url), conference, year, url)


def _crossref_date(item: dict[str, Any]) -> str:
    for field_name in ("published-online", "published-print", "published", "created"):
        parts = item.get(field_name, {}).get("date-parts", [])
        if parts and parts[0]:
            values = list(parts[0]) + [1, 1]
            try:
                return dt.date(int(values[0]), int(values[1]), int(values[2])).isoformat()
            except ValueError:
                continue
    return ""


def fetch_crossref_fallback(client: PoliteClient, conference: dict[str, Any], year: int) -> list[ConferencePaper]:
    papers: list[ConferencePaper] = []
    for title_query in conference.get("crossref_titles", []):
        filters = f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31,type:proceedings-article"
        query_field = conference.get("crossref_query_field", "query.container-title")
        params = urllib.parse.urlencode({query_field: title_query, "filter": filters, "rows": 1000})
        try:
            payload = json.loads(client.get(f"https://api.crossref.org/works?{params}"))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            continue
        for item in payload.get("message", {}).get("items", []):
            containers = [clean_text(value) for value in item.get("container-title", [])]
            event_name = clean_text(item.get("event", {}).get("name"))
            haystack = " ".join([*containers, event_name]).casefold()
            if not all(token in haystack for token in title_query.casefold().split() if len(token) > 2):
                continue
            title = clean_text((item.get("title") or [""])[0])
            if not title:
                continue
            doi = normalize_doi(item.get("DOI", ""))
            authors = [clean_text(" ".join((a.get("given", ""), a.get("family", "")))) for a in item.get("author", [])]
            papers.append(ConferencePaper(
                conference=conference["acronym"], conference_name=conference["name"], title=title,
                url=clean_text(item.get("URL")) or f"https://doi.org/{doi}",
                guid=identity_from_fields(doi, title), year=year, authors=authors, doi=doi,
                published=_crossref_date(item),
            ))
    return papers


def deduplicate(papers: Iterable[ConferencePaper]) -> list[ConferencePaper]:
    by_identity: dict[str, ConferencePaper] = {}
    by_title: dict[str, str] = {}
    for paper in papers:
        title_key = normalize_title(paper.title)
        key = identity(paper)
        existing_key = by_title.get(title_key)
        if existing_key:
            current = by_identity[existing_key]
            if not current.doi and paper.doi:
                paper.matched_keywords = list(dict.fromkeys([*current.matched_keywords, *paper.matched_keywords]))
                by_identity.pop(existing_key)
                by_identity[key] = paper
                by_title[title_key] = key
            continue
        if key not in by_identity:
            by_identity[key] = paper
            by_title[title_key] = key
    return list(by_identity.values())


def collect_conference(
    client: PoliteClient, conference: dict[str, Any], start_year: int, end_year: int
) -> list[ConferencePaper]:
    collected: list[ConferencePaper] = []
    for year in eligible_years(start_year, end_year, conference.get("year_parity", "")):
        official: list[ConferencePaper] = []
        official.extend(fetch_virtual_json(client, conference, year))
        official.extend(fetch_cvf_event(client, conference, year))
        official.extend(fetch_eccv_event(client, conference, year))
        official.extend(fetch_copernicus_volumes(client, conference, year))
        official.extend(fetch_robotics_proceedings(client, conference, year))
        official.extend(fetch_ieee_vis_page(client, conference, year))
        collected.extend(official)
        dblp_for_year: list[ConferencePaper] = []
        if not official:
            for stream in conference.get("streams", []):
                dblp_for_year.extend(fetch_dblp_toc_year(client, conference, stream, year))
        collected.extend(dblp_for_year)
        if not official and not dblp_for_year and conference.get("crossref_titles"):
            collected.extend(fetch_crossref_fallback(client, conference, year))
    return deduplicate(collected)


def match_keywords(paper: ConferencePaper, keywords: list[str]) -> list[str]:
    text = " ".join((paper.title, " ".join(paper.topics))).casefold()
    matches: list[str] = []
    for keyword in keywords:
        pattern = r"(?<![a-z0-9])" + re.escape(keyword.casefold()) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            matches.append(keyword)
    return matches


def write_rss(
    papers: Iterable[ConferencePaper], output: Path, *, title: str, link: str,
    description: str, guid_scope: str = "conference"
) -> int:
    papers = sorted(papers, key=lambda p: (p.published, p.year, p.title.casefold()), reverse=True)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(dt.datetime.now(UTC))
    ET.SubElement(channel, "generator").text = "journal-rss conference_rss.py"
    ET.SubElement(channel, f"{{{ATOM_NS}}}link", {"href": link, "rel": "self", "type": "application/rss+xml"})
    for paper in papers:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = paper.title
        ET.SubElement(item, "link").text = paper.url
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = feed_guid(paper, guid_scope)
        ET.SubElement(item, f"{{{PRISM_NS}}}publicationName").text = paper.conference_name
        date_value = paper.published or str(paper.year)
        ET.SubElement(item, f"{{{DC_NS}}}date").text = date_value
        if paper.published:
            published = dt.datetime.fromisoformat(paper.published).replace(tzinfo=UTC)
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
            ET.SubElement(item, f"{{{PRISM_NS}}}publicationDate").text = paper.published
        if paper.doi:
            ET.SubElement(item, f"{{{PRISM_NS}}}doi").text = paper.doi
        for author in paper.authors:
            ET.SubElement(item, f"{{{DC_NS}}}creator").text = author
        ET.SubElement(item, "category").text = paper.conference
        for keyword in paper.matched_keywords:
            ET.SubElement(item, "category").text = keyword
        parts = [f"<p><strong>Conference:</strong> {html.escape(paper.conference_name)}</p>"]
        if paper.authors:
            parts.append(f"<p><strong>Authors:</strong> {html.escape('; '.join(paper.authors))}</p>")
        parts.append(f"<p><strong>Year:</strong> {paper.year}</p>")
        if paper.matched_keywords:
            parts.append(f"<p><strong>Matched keywords:</strong> {html.escape(', '.join(paper.matched_keywords))}</p>")
        ET.SubElement(item, "description").text = "".join(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return len(papers)


def write_opml(config: dict[str, Any], output: Path) -> None:
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Top Conference RSS"
    body = ET.SubElement(root, "body")
    group = ET.SubElement(body, "outline", {"text": "Top Conferences", "title": "Top Conferences"})
    for conference in config["conferences"]:
        url = f"{config['base_url']}/{conference['slug']}.xml"
        ET.SubElement(group, "outline", {
            "type": "rss", "text": conference["acronym"], "title": conference["acronym"],
            "xmlUrl": url, "htmlUrl": url,
        })
    digest_url = f"{config['base_url']}/top-conference-daily.xml"
    ET.SubElement(group, "outline", {
        "type": "rss", "text": "Top Conference Daily Digest", "title": "Top Conference Daily Digest",
        "xmlUrl": digest_url, "htmlUrl": digest_url,
    })
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output, encoding="utf-8", xml_declaration=True)


def write_index(config: dict[str, Any], output: Path) -> None:
    rows = []
    for conference in config["conferences"]:
        filename = f"{conference['slug']}.xml"
        rows.append(
            f'<li><a href="./{filename}">{html.escape(conference["acronym"])}</a>'
            f' - {html.escape(conference["name"])}</li>'
        )
    content = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Top Conference RSS</title><style>body{max-width:900px;margin:40px auto;padding:0 20px;font-family:system-ui,sans-serif;line-height:1.6}li{margin:5px 0}</style></head>
<body><h1>Top Conference RSS</h1>
<p><a href="./top-conference-daily.xml">Top Conference Daily Digest</a> | <a href="../conference-feeds.opml">OPML</a></p>
<ol>""" + "".join(rows) + "</ol></body></html>\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


def run(config_path: Path, output_dir: Path, start_year: int | None = None, end_year: int | None = None) -> dict[str, int]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    start_year = start_year or int(config.get("start_year", 2025))
    end_year = end_year or int(config.get("end_year", dt.datetime.now(UTC).year))
    client = PoliteClient(cache_dir=Path(".conference-cache"))
    counts: dict[str, int] = {}
    all_papers: list[ConferencePaper] = []
    for conference in config["conferences"]:
        papers = collect_conference(client, conference, start_year, end_year)
        feed_url = f"{config['base_url']}/{conference['slug']}.xml"
        counts[conference["slug"]] = write_rss(
            papers, output_dir / f"{conference['slug']}.xml",
            title=f"{conference['acronym']} Papers",
            link=feed_url,
            description=f"All indexed full papers from {conference['name']} ({start_year}-{end_year}).",
            guid_scope=f"conference:{conference['slug']}",
        )
        print(f"{conference['acronym']}: {counts[conference['slug']]} papers")
        all_papers.extend(papers)
    selected: list[ConferencePaper] = []
    for paper in deduplicate(all_papers):
        paper.matched_keywords = match_keywords(paper, config["keywords"])
        if paper.matched_keywords:
            selected.append(paper)
    digest_url = f"{config['base_url']}/top-conference-daily.xml"
    counts["top-conference-daily"] = write_rss(
        selected, output_dir / "top-conference-daily.xml", title="Top Conference Daily Digest",
        link=digest_url,
        description="Keyword-filtered and deduplicated papers from 35 conference feeds.",
        guid_scope="conference:digest",
    )
    write_opml(config, output_dir.parent / "conference-feeds.opml")
    write_index(config, output_dir / "index.html")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("conference-config.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("conference-feeds"))
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    args = parser.parse_args()
    counts = run(args.config, args.output_dir, args.start_year, args.end_year)
    print(f"Generated {len(counts) - 1} conference feeds and one digest ({sum(counts.values())} feed items).")


if __name__ == "__main__":
    main()
