"""Official ACL Anthology alternatives to DBLP, with no per-paper requests.

The official repository's data/xml/{year}.{slug}.xml supplies complete volumes,
including inline titles, author names and explicit DOIs. Event HTML is a fallback.
Missing DOIs stay empty; the current event list does not include DOI links.
Main-track long and short volumes are admitted. EMNLP's combined
main volume does not identify individual short papers; page counts are not a
safe substitute. Industry, Findings, demonstrations and workshops are excluded.

Anthology year/month and ingest-date are not exact publication dates. The
ConferencePaper contract only supports day-precision published values, so these
sources leave published and presentation_date empty instead of inventing days.
"""

from __future__ import annotations

import http.client
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from source_response import SourceResponseError, validate_response

if TYPE_CHECKING:
    from conference_rss import ConferencePaper


ANTHOLOGY = "https://aclanthology.org"
XML_BASE = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml"
MAIN_VOLUMES = {"acl": {"long", "short"}, "emnlp": {"main", "long", "short"}, "naacl": {"long", "short"}}
EXCLUDED_VOLUMES = re.compile(
    r"\b(workshop|findings|demonstrations?|demos?|tutorials?|"
    r"industry|companion|doctoral consortium|challenge report|competition report)\b",
    re.I,
)


def _text(value: str) -> str:
    return " ".join(value.split())


def _xml_text(node: ET.Element | None) -> str:
    # No separator: inline fixed-case tags split words, not just sentences.
    return _text("".join(node.itertext())) if node is not None else ""


def _doi(value: str) -> str:
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", _text(value).casefold())
    return value.removeprefix("doi:").strip()


def _is_main_volume(slug: str, volume: str, title: str, conference: dict[str, Any]) -> bool:
    if volume not in MAIN_VOLUMES.get(slug, set()) or EXCLUDED_VOLUMES.search(title):
        return False
    includes = conference.get("venue_include", [])
    return not includes or any(term.casefold() in title.casefold() for term in includes)


def _deduplicate(papers: list[ConferencePaper]) -> list[ConferencePaper]:
    by_id: dict[str, ConferencePaper] = {}
    by_doi: dict[str, ConferencePaper] = {}
    result: list[ConferencePaper] = []
    for paper in papers:
        existing = by_id.get(paper.guid) or (by_doi.get(paper.doi) if paper.doi else None)
        if existing is not None:
            if not existing.doi:
                existing.doi = paper.doi
            if not existing.authors:
                existing.authors = paper.authors
        else:
            existing = paper
            result.append(paper)
        by_id[paper.guid] = existing
        if paper.doi:
            by_doi[paper.doi] = existing
    return result


def parse_acl_anthology_xml(
    content: bytes, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    from conference_rss import ConferencePaper

    slug = str(conference.get("slug", "")).casefold()
    collection_id = f"{year}.{slug}"
    validate_response(content, f"{XML_BASE}/{collection_id}.xml")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise SourceResponseError(f"Invalid Anthology XML: {collection_id}") from error
    if root.tag != "collection" or root.get("id") != collection_id:
        raise SourceResponseError(f"Unexpected Anthology collection: {collection_id}")
    if not root.findall("volume"):
        raise SourceResponseError(f"Anthology XML has no volumes: {collection_id}")

    papers = []
    for volume in root.findall("volume"):
        volume_id = volume.get("id", "")
        title = _xml_text(volume.find("meta/booktitle"))
        if not _is_main_volume(slug, volume_id, title, conference):
            continue
        venues = {_xml_text(node).casefold() for node in volume.findall("meta/venue")}
        if "ws" in venues:
            continue
        if not title or venues != {slug} or volume.findtext("meta/year") != str(year):
            raise SourceResponseError(f"Unexpected Anthology volume metadata: {collection_id}-{volume_id}")
        if not volume.findall("paper"):
            raise SourceResponseError(f"Anthology main volume has no papers: {collection_id}-{volume_id}")
        for record in volume.findall("paper"):
            paper_id = record.get("id", "")
            if paper_id == "0":  # Anthology reserves zero for proceedings front matter.
                continue
            paper_title = _xml_text(record.find("title"))
            if not re.fullmatch(r"[1-9]\d*", paper_id) or not paper_title:
                raise SourceResponseError(f"Incomplete Anthology paper: {collection_id}-{volume_id}.{paper_id}")
            authors = [
                _text(" ".join((_xml_text(author.find("first")), _xml_text(author.find("last")))))
                for author in record.findall("author")
            ]
            anthology_id = f"{collection_id}-{volume_id}.{paper_id}"
            papers.append(ConferencePaper(
                conference=conference.get("acronym", slug.upper()),
                conference_name=conference.get("name", slug.upper()),
                title=paper_title, url=f"{ANTHOLOGY}/{anthology_id}/",
                guid=f"acl-anthology:{anthology_id}", year=year,
                authors=[author for author in authors if author],
                doi=_doi(_xml_text(record.find("doi"))),
            ))
    return _deduplicate(papers)


def parse_acl_anthology_event(
    content: bytes, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    from conference_rss import ConferencePaper

    slug = str(conference.get("slug", "")).casefold()
    event_url = f"{ANTHOLOGY}/events/{slug}-{year}/"
    validate_response(content, event_url)
    soup = BeautifulSoup(content, "html.parser", from_encoding="utf-8")
    if not soup.select_one("h2#title") or not soup.title or "ACL Anthology" not in soup.title.get_text():
        raise SourceResponseError(f"Unexpected Anthology event HTML: {event_url}")
    # Match the exact conference collection, never another event's workshops.
    pattern = re.compile(rf"/{year}\.{re.escape(slug)}-([a-z]+)\.([0-9]+)/?")
    volumes = set()
    for heading in soup.find_all("h4"):
        for anchor in heading.find_all("a", href=True):
            path = urllib.parse.urlsplit(anchor["href"]).path
            match = re.fullmatch(rf"/(?:volumes/)?{year}\.{re.escape(slug)}-([a-z]+)/?", path)
            if match and _is_main_volume(slug, match[1], anchor.get_text(), conference):
                volumes.add(match[1])

    papers = []
    for anchor in soup.select("strong > a[href]"):
        link = urllib.parse.urlsplit(urllib.parse.urljoin(event_url, anchor["href"]))
        match = pattern.fullmatch(link.path)
        if link.hostname != "aclanthology.org" or not match or match[1] not in volumes or match[2] == "0":
            continue
        title = _text(anchor.get_text())
        if not title:
            raise SourceResponseError(f"Incomplete Anthology title: {link.path}")
        row = anchor.parent.parent
        authors = [
            _text(author.get_text()) for author in row.find_all("a", href=True)
            if urllib.parse.urlsplit(author["href"]).path.startswith("/people/")
        ]
        doi = ""
        container = anchor.find_parent("p") or row
        for ref in container.find_all("a", href=True):
            if urllib.parse.urlsplit(ref["href"]).hostname in {"doi.org", "dx.doi.org"}:
                doi = _doi(ref["href"])
                break
        anthology_id = link.path.strip("/")
        papers.append(ConferencePaper(
            conference=conference.get("acronym", slug.upper()),
            conference_name=conference.get("name", slug.upper()),
            title=title, url=f"{ANTHOLOGY}/{anthology_id}/",
            guid=f"acl-anthology:{anthology_id}", year=year,
            authors=[author for author in authors if author], doi=doi,
        ))
    if volumes and not papers:
        raise SourceResponseError(f"Anthology main volumes have no paper rows: {event_url}")
    return _deduplicate(papers)


def _get_source(client: Any, url: str) -> bytes:
    try:
        content = client.get(url)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return b""
        raise
    validate_response(content, url)
    return content


def collect_official_alternative(
    client: Any, conference: dict[str, Any], year: int
) -> list[ConferencePaper]:
    """Fetch one complete XML collection, or one event page if XML is unavailable.

    Unsupported conferences and genuinely missing proceedings return []. Source
    errors remain errors if HTML cannot recover them, for the caller's health log.
    """
    slug = str(conference.get("slug", "")).casefold()
    if slug not in MAIN_VOLUMES:
        return []
    xml_error = None
    try:
        content = _get_source(client, f"{XML_BASE}/{year}.{slug}.xml")
        if content:
            return parse_acl_anthology_xml(content, conference, year)
    except (urllib.error.URLError, OSError, http.client.HTTPException, SourceResponseError) as error:
        xml_error = error

    content = _get_source(client, f"{ANTHOLOGY}/events/{slug}-{year}/")
    if content:
        return parse_acl_anthology_event(content, conference, year)
    if xml_error is not None:
        raise xml_error
    return []
