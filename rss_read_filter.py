"""Remove previously read Zotero items from generated RSS files.

The public suppression file contains only HMAC digests. The matching key stays in
GitHub Actions secrets and on the local Zotero machine.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import tempfile
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARXIV_RE = re.compile(
    r"(?:arxiv:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
TRACKING_QUERY_KEYS = {"dgcid", "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term"}

ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("rdf", RDF_NS)


@dataclass(frozen=True)
class FilterResult:
    path: Path
    kept: int
    removed: int


def _plain_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return " ".join(unicodedata.normalize("NFKC", value).split()).strip()


def _doi(value: str) -> str:
    match = DOI_RE.search(html.unescape(value or ""))
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}").lower()


def _canonical_url(value: str) -> str:
    value = html.unescape(value or "").strip()
    if not value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return value.lower()
    if not parts.scheme or not parts.netloc:
        return value.lower()
    query = [
        (key, item)
        for key, item in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urllib.parse.urlencode(sorted(query)), "")
    )


def identity_tokens(
    *,
    guid: str = "",
    link: str = "",
    title: str = "",
    description: str = "",
    doi: str = "",
) -> set[str]:
    """Return stable, namespaced identities shared by Zotero and RSS XML."""
    tokens: set[str] = set()
    clean_guid = _plain_text(guid).lower()
    clean_title = _plain_text(title).lower()
    clean_link = _canonical_url(link)
    if clean_guid:
        tokens.add(f"guid:{clean_guid}")
    if clean_link:
        tokens.add(f"url:{clean_link}")
    if clean_title:
        tokens.add(f"title:{clean_title}")

    combined = " ".join((guid or "", link or "", description or "", doi or ""))
    clean_doi = _doi(combined)
    if clean_doi:
        tokens.add(f"doi:{clean_doi}")
    for match in ARXIV_RE.finditer(combined):
        tokens.add(f"arxiv:{match.group(1).lower()}")
    return tokens


def hash_tokens(tokens: Iterable[str], key: bytes) -> set[str]:
    if not key:
        raise ValueError("RSS read-filter key must not be empty")
    return {hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest() for token in tokens}


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def load_suppression(path: Path, key: bytes) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or payload.get("algorithm") != "hmac-sha256":
        raise ValueError(f"Unsupported suppression file format: {path}")
    if payload.get("key_id") != key_id(key):
        raise ValueError("Suppression file and RSS_READ_FILTER_KEY do not match")
    hashes = payload.get("hashes", [])
    if not isinstance(hashes, list) or any(not isinstance(value, str) for value in hashes):
        raise ValueError(f"Invalid suppression hashes: {path}")
    return set(hashes)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_values(item: ET.Element, *names: str) -> list[str]:
    wanted = {name.lower() for name in names}
    values: list[str] = []
    for child in item:
        if _local_name(child.tag) not in wanted:
            continue
        value = child.get("href", "") if _local_name(child.tag) == "link" else ""
        value = value or "".join(child.itertext())
        if value:
            values.append(value)
    return values


def _feed_entries(root: ET.Element, path: Path) -> tuple[ET.Element, list[ET.Element]]:
    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = next((node for node in root if _local_name(node.tag) == "channel"), None)
        if channel is None:
            raise ValueError(f"RSS document has no channel: {path}")
        return channel, [node for node in channel if _local_name(node.tag) == "item"]
    if root_name == "rdf":
        return root, [node for node in root if _local_name(node.tag) == "item"]
    if root_name == "feed":
        return root, [node for node in root if _local_name(node.tag) == "entry"]
    raise ValueError(f"Unsupported RSS, RDF, or Atom document: {path}")


def _item_tokens(item: ET.Element) -> set[str]:
    links = _child_values(item, "link")
    descriptions = _child_values(item, "description", "summary", "content", "encoded", "identifier")
    return identity_tokens(
        guid=" ".join(_child_values(item, "guid", "id")),
        link=links[0] if links else "",
        title=" ".join(_child_values(item, "title")),
        description=" ".join(descriptions),
        doi=" ".join(_child_values(item, "doi", "identifier")),
    )


def filter_feed(path: Path, suppressed: set[str], key: bytes) -> FilterResult:
    tree = ET.parse(path)
    parent, entries = _feed_entries(tree.getroot(), path)
    removed = 0
    for item in entries:
        if hash_tokens(_item_tokens(item), key) & suppressed:
            parent.remove(item)
            removed += 1
    kept = len(entries) - removed
    if removed:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return FilterResult(path=path, kept=kept, removed=removed)


def _key_from_args(key_file: Path | None) -> bytes:
    if key_file:
        return key_file.read_text(encoding="ascii").strip().encode("ascii")
    value = os.environ.get("RSS_READ_FILTER_KEY", "").strip()
    if not value:
        raise ValueError("Set RSS_READ_FILTER_KEY or pass --key-file")
    return value.encode("utf-8")


def _paths(root: Path, patterns: list[str]) -> list[Path]:
    return sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--suppression", type=Path, default=Path("read-suppression.json"))
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--glob", action="append", dest="patterns")
    args = parser.parse_args()

    key = _key_from_args(args.key_file)
    suppression_path = args.suppression
    if not suppression_path.is_absolute():
        suppression_path = args.root / suppression_path
    suppressed = load_suppression(suppression_path, key)
    patterns = args.patterns or ["*.xml", "conference-feeds/*.xml"]
    results = [filter_feed(path, suppressed, key) for path in _paths(args.root, patterns)]
    print(
        json.dumps(
            {
                "files": len(results),
                "kept": sum(result.kept for result in results),
                "removed": sum(result.removed for result in results),
                "details": [
                    {"path": str(result.path), "kept": result.kept, "removed": result.removed}
                    for result in results
                    if result.removed
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
