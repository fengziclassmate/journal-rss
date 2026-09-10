#!/usr/bin/env python3
"""Mirror publisher RSS/Atom feeds while preserving their original entries."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


@dataclass(frozen=True)
class MirrorResult:
    name: str
    output: str
    entries: int
    status: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def feed_entry_count(raw: bytes) -> int:
    root = ET.fromstring(raw)
    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next((node for node in root if _local_name(node.tag) == "channel"), None)
        if channel is None:
            raise ValueError("RSS document has no channel")
        return sum(_local_name(node.tag) == "item" for node in channel)
    if root_name == "rdf":
        return sum(_local_name(node.tag) == "item" for node in root)
    if root_name == "feed":
        return sum(_local_name(node.tag) == "entry" for node in root)
    raise ValueError(f"Unsupported feed root: {root.tag}")


def fetch_bytes(url: str, attempts: int = 3) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def build_crossref_rss(spec: dict, payload: dict) -> bytes:
    rss = ET.Element("rss", {"version": "2.0", "xmlns:dc": "http://purl.org/dc/elements/1.1/"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = spec["name"]
    ET.SubElement(channel, "link").text = spec["source_url"]
    ET.SubElement(channel, "description").text = f"Official-feed fallback metadata for {spec['name']}"
    for record in payload.get("message", {}).get("items", []):
        titles = record.get("title") or []
        resource = record.get("resource", {}).get("primary", {}).get("URL", "")
        link = resource or record.get("URL", "")
        title = titles[0].strip() if titles else ""
        if not title or not link:
            continue
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = link
        doi = record.get("DOI", "")
        if doi:
            ET.SubElement(item, "dc:identifier").text = f"doi:{doi}"
        for author in record.get("author", []):
            name = " ".join(filter(None, (author.get("given", ""), author.get("family", "")))).strip()
            if name:
                ET.SubElement(item, "dc:creator").text = name
        date_parts = (record.get("published") or record.get("created") or {}).get("date-parts", [[]])[0]
        if date_parts:
            year, month, day = (date_parts + [1, 1])[:3]
            published = dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def fetch_crossref_feed(spec: dict) -> bytes:
    issn = spec["crossref_issn"]
    params = urllib.parse.urlencode({
        "filter": f"from-created-date:{spec.get('crossref_from', '2026-06-01')}",
        "sort": "created",
        "order": "desc",
        "rows": str(spec.get("crossref_rows", 300)),
        "select": "DOI,title,URL,resource,published,created,author",
    })
    url = f"https://api.crossref.org/journals/{issn}/works?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "journal-rss/1.0 (mailto:rss@example.com)"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return build_crossref_rss(spec, json.load(response))


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(raw)
        temp_name = handle.name
    os.replace(temp_name, path)


def mirror_one(spec: dict, root: Path, fetcher: Callable[[str], bytes] = fetch_bytes) -> MirrorResult:
    output = root / spec["output"]
    try:
        raw = fetcher(spec["source_url"])
        entries = feed_entry_count(raw)
        if not output.exists() or output.read_bytes() != raw:
            _atomic_write(output, raw)
        return MirrorResult(spec["name"], spec["output"], entries, "updated")
    except Exception as exc:
        if spec.get("crossref_issn"):
            try:
                raw = fetch_crossref_feed(spec)
                entries = feed_entry_count(raw)
                _atomic_write(output, raw)
                return MirrorResult(spec["name"], spec["output"], entries, f"crossref-fallback: {exc}")
            except Exception:
                pass
        if output.exists():
            entries = feed_entry_count(output.read_bytes())
            return MirrorResult(spec["name"], spec["output"], entries, f"preserved: {exc}")
        raise RuntimeError(f"{spec['name']}: initial mirror failed: {exc}") from exc


def load_config(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mirrors = payload["mirrors"]
    urls = [item["source_url"] for item in mirrors]
    outputs = [item["output"] for item in mirrors]
    if len(urls) != len(set(urls)) or len(outputs) != len(set(outputs)):
        raise ValueError("Official mirror URLs and outputs must be unique")
    return mirrors


def mirror_all(config: Path, root: Path, workers: int = 8) -> list[MirrorResult]:
    specs = load_config(config)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(mirror_one, spec, root) for spec in specs]
        return [future.result() for future in futures]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("official-feed-config.json"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    results = mirror_all(args.config, args.root, args.workers)
    print(json.dumps({"feeds": len(results), "entries": sum(r.entries for r in results), "details": [r.__dict__ for r in results]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
