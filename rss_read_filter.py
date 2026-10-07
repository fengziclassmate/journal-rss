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
from rss_ops import write_json


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
        tokens.update(publisher_identity_tokens(clean_link))
    if clean_title:
        tokens.add(f"title:{clean_title}")

    combined = " ".join((guid or "", link or "", description or "", doi or ""))
    clean_doi = _doi(combined)
    if clean_doi:
        tokens.add(f"doi:{clean_doi}")
    for match in ARXIV_RE.finditer(combined):
        tokens.add(f"arxiv:{match.group(1).lower()}")
    return tokens


def publisher_identity_tokens(url: str) -> set[str]:
    """Bridge publisher URL variants without relying on article titles."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return set()
    host = (parts.hostname or '').lower()
    if host in ('jmlr.org', 'www.jmlr.org'):
        match = re.fullmatch(r'/papers/(v\d+/[^/]+\.html)', parts.path)
        if match:
            path = '/papers/' + match.group(1)
            return {'jmlr-paper:' + match.group(1)} | {
                'url:' + scheme + '://' + domain + path
                for scheme in ('http', 'https') for domain in ('jmlr.org', 'www.jmlr.org')}
    if host == 'ieeexplore.ieee.org':
        match = re.search(r'/document/(\d+)', parts.path)
        if match:
            number = match.group(1)
            return {'ieee-document:' + number,
                    'url:http://ieeexplore.ieee.org/document/' + number,
                    'url:https://ieeexplore.ieee.org/document/' + number}
    if host in ('www.sciencedirect.com', 'sciencedirect.com', 'linkinghub.elsevier.com'):
        match = re.search(r'/pii/(S[0-9A-Z]+)', parts.path, re.I)
        if match:
            pii = match.group(1).upper()
            return {'elsevier-pii:' + pii,
                    'url:https://www.sciencedirect.com/science/article/pii/' + pii,
                    'url:http://www.sciencedirect.com/science/article/pii/' + pii}
    return set()


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
    return validate_suppression(payload, key)


def validate_suppression(payload, key):
    if not key or not key.strip():
        raise ValueError('Empty suppression key')
    if payload.get("version") != 1 or payload.get("algorithm") != "hmac-sha256":
        raise ValueError('Unsupported suppression file format')
    if payload.get("key_id") != key_id(key):
        raise ValueError("Suppression file and RSS_READ_FILTER_KEY do not match")
    hashes = payload.get("hashes", [])
    if not isinstance(hashes, list) or any(not isinstance(value, str) for value in hashes):
        raise ValueError('Invalid suppression hashes')
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
    guids = _child_values(item, "guid", "id")
    is_digest = any(
        guid.lower().startswith(("research-daily:", "arxiv-email-daily:", "conference:digest:", "rss-health:"))
        for guid in guids
    )
    descriptions = [] if is_digest else _child_values(
        item, "description", "summary", "content", "encoded", "identifier"
    )
    tokens = identity_tokens(
        guid=" ".join(guids),
        link=links[0] if links else "",
        title=" ".join(_child_values(item, "title")),
        doi=" ".join(_child_values(item, "doi", "identifier")),
    )
    for child in item:
        if child.tag == '{https://fengziclassmate.github.io/journal-rss/ns/unified/1}identity':
            token = child.text or ''
            if token.startswith(('guid:', 'url:', 'doi:', 'ieee-document:', 'elsevier-pii:', 'jmlr-paper:', 'arxiv:')):
                tokens.add(token)
    # A reference inside an abstract is not the identity of the article itself.
    if not any(token.startswith(('doi:', 'arxiv:')) for token in tokens) and descriptions:
        text=' '.join(descriptions)
        dois={_doi(match.group(0)) for match in DOI_RE.finditer(text)}
        arxiv={match.group(1).lower() for match in ARXIV_RE.finditer(text)}
        if len(dois)==1:
            tokens.add('doi:'+next(iter(dois)))
        if len(arxiv)==1 and not dois:
            tokens.add('arxiv:'+next(iter(arxiv)))
    return tokens


def filter_feed(path: Path, suppressed: set[str], key: bytes) -> FilterResult:
    tree = ET.parse(path)
    parent, entries = _feed_entries(tree.getroot(), path)
    removed = 0
    for item in entries:
        tokens = {token for token in _item_tokens(item) if not token.startswith('title:')}
        if hash_tokens(tokens, key) & suppressed:
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
    patterns = args.patterns or ["*.xml", "conference-feeds/*.xml", "official-feeds/*.xml", "journal-feeds/*.xml"]
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


def write_receipt(root=Path('.')):
    root=Path(root)
    suppression=root/'read-suppression.json'
    paths=_paths(root,['*.xml','conference-feeds/*.xml','official-feeds/*.xml','journal-feeds/*.xml'])
    write_json(root/'read-filter-status.json', {
        'version':2,
        'suppression_sha256':suppression_digest(suppression),
        'feeds':{path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
    })


def suppression_digest(path):
    value=json.loads(Path(path).read_text('utf-8'))
    return suppression_payload_digest(value)


def suppression_payload_digest(value):
    canonical=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


if __name__ == "__main__":
    main()
