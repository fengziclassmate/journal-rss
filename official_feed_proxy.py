#!/usr/bin/env python3
"""Mirror publisher RSS/Atom feeds while preserving their original entries."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from rss_health import record
from rss_read_filter import _feed_entries, _item_tokens
from source_response import SourceResponseError, format_source_error, validate_response


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
CROSSREF_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class MirrorResult:
    name: str
    output: str
    entries: int
    status: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def feed_entry_count(raw: bytes, url: str = "") -> int:
    validate_response(raw, url)
    if re.match(br'\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head)\b',
                raw[:65536].lstrip(b'\xef\xbb\xbf'), re.I | re.S):
        raise SourceResponseError("HTML document, not an RSS/Atom feed")
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


def fetch_bytes(url: str, attempts: int = 3, *, timeout: int = 60, headers: dict | None = None) -> bytes:
    if attempts < 1:
        raise ValueError("Fetch attempts must be positive")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            **(headers or {}),
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            validate_response(raw, url)
            return raw
        except Exception as exc:  # pragma: no cover - network dependent
            if isinstance(exc, SourceResponseError) or (
                isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404, 418)
            ):
                raise
            last_error = exc
            if attempt + 1 < attempts:
                delay = 2**attempt
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    retry_after = exc.headers.get('Retry-After') if exc.headers else None
                    if retry_after:
                        try:
                            delay = int(retry_after)
                        except ValueError:
                            try:
                                retry_at = email.utils.parsedate_to_datetime(retry_after)
                                delay = (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
                            except (TypeError, ValueError):
                                pass
                    if delay > 120:
                        raise
                time.sleep(max(1, delay))
    assert last_error is not None
    raise last_error


def build_crossref_rss(spec: dict, payload: dict) -> bytes:
    from journal_dates import DC
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise ValueError("Invalid Crossref work-list response: missing items list")
    if payload.get("status", "ok") != "ok":
        raise ValueError("Crossref returned an error response")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = spec["name"]
    ET.SubElement(channel, "link").text = spec["source_url"]
    ET.SubElement(channel, "description").text = f"Official-feed fallback metadata for {spec['name']}"
    written = 0
    for record in message["items"]:
        if not isinstance(record, dict):
            raise ValueError("Invalid Crossref work record")
        issns = record.get("ISSN")
        if spec.get("crossref_issn") and issns is not None:
            if not isinstance(issns, list) or spec["crossref_issn"] not in issns:
                raise ValueError("Crossref work ISSN does not match the configured journal")
        titles = record.get("title") or []
        if not isinstance(titles, list) or any(not isinstance(title, str) for title in titles):
            raise ValueError("Invalid Crossref work title")
        resource = record.get("resource", {}).get("primary", {}).get("URL", "")
        link = resource or record.get("URL", "")
        title = titles[0].strip() if titles else ""
        if not title or not link:
            continue
        written += 1
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = link
        doi = record.get("DOI", "")
        ET.SubElement(item, "guid", {"isPermaLink": "false" if doi else "true"}).text = f"doi:{doi.lower()}" if doi else link
        if doi:
            ET.SubElement(item, '{' + DC + '}identifier').text = f"doi:{doi}"
        backup_url = f"https://api.crossref.org/journals/{spec['crossref_issn']}/works" if spec.get('crossref_issn') else 'https://api.crossref.org'
        ET.SubElement(item, 'source', url=backup_url).text = 'Crossref'
        for author in record.get("author", []):
            name = " ".join(filter(None, (author.get("given", ""), author.get("family", "")))).strip()
            if name:
                ET.SubElement(item, '{' + DC + '}creator').text = name
        from journal_dates import crossref_dates, append_metadata, describe
        dates = crossref_dates(record)
        append_metadata(item, dates)
        ET.SubElement(item, 'description').text = describe('', dates)
        value = dates.get('publication', '')
        if len(value) == 10:
            published = dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)
            ET.SubElement(item, "pubDate").text = email.utils.format_datetime(published)
    if message["items"] and not written:
        raise ValueError("Crossref returned records but no usable feed entries")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def fetch_crossref_feed(spec: dict) -> bytes:
    issn = spec["crossref_issn"]
    if not re.fullmatch(r"\d{4}-\d{3}[\dX]", issn):
        raise ValueError("Invalid Crossref journal ISSN")
    from_date = dt.date.fromisoformat(spec.get("crossref_from", "2026-06-01"))
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    until_date = dt.date.fromisoformat(spec.get("crossref_until", today.isoformat()))
    if from_date > until_date:
        raise ValueError("Crossref start date is after its end date")
    date_filter = spec.get("crossref_date_filter", "created")
    if date_filter not in ("created", "pub"):
        raise ValueError("Unsupported Crossref date filter")
    rows = int(spec.get("crossref_rows", 300))
    if not 1 <= rows <= 1000:
        raise ValueError("Crossref rows must be between 1 and 1000")
    params = {
        "filter": f"from-{date_filter}-date:{from_date},until-{date_filter}-date:{until_date},type:journal-article",
        "sort": "published" if date_filter == "pub" else "created",
        "order": "desc",
        "rows": str(rows),
        "select": "DOI,title,URL,resource,published,published-online,published-print,created,author,ISSN",
    }
    paginate = spec.get('crossref_paginate', False)
    cursor = '*'
    seen_cursors = set()
    works = {}
    total = None
    while True:
        if paginate:
            params['cursor'] = cursor
        url = f"https://api.crossref.org/journals/{issn}/works?{urllib.parse.urlencode(params)}"
        with CROSSREF_SLOTS:
            raw = fetch_bytes(url, timeout=90, headers={
                'User-Agent': 'journal-rss/1.0 (mailto:rss@example.com)', 'Accept': 'application/json'})
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ValueError("Crossref returned an error response")
        validated_feed = build_crossref_rss(spec, payload)
        if not paginate:
            return validated_feed
        message = payload['message']
        if total is None:
            total = message.get('total-results')
            if type(total) is not int or total < 0:
                raise ValueError('Paginated Crossref response has no valid total-results')
        previous_count = len(works)
        for work in message['items']:
            doi = work.get('DOI')
            if not isinstance(doi, str) or not doi.strip():
                raise ValueError('Paginated Crossref work has no DOI')
            works.setdefault(doi.strip().lower(), work)
        if len(works) >= total:
            return build_crossref_rss(spec, {'status': 'ok', 'message': {'items': list(works.values())}})
        # Never publish a partial page as if it covered the whole initial window.
        if len(works) == previous_count:
            raise ValueError(f'Incomplete Crossref pagination: {len(works)} of {total} works')
        seen_cursors.add(cursor)
        cursor = message.get('next-cursor')
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError('Incomplete Crossref pagination: missing or repeated cursor')


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(raw)
        temp_name = handle.name
    try:
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _merge_mirror(spec: dict, output: Path, raw: bytes, *, fallback: bool = False, source_url: str = '') -> bytes:
    from journal_unified import identities, normalize, union
    from journal_rss_aggregator import child_text

    if not output.exists():
        return raw
    previous_raw = output.read_bytes()
    try:
        feed_entry_count(previous_raw)
    except (ValueError, ET.ParseError):
        return raw
    fresh_root = ET.fromstring(raw)
    previous = _feed_entries(ET.fromstring(previous_raw), output)[1]
    fresh = _feed_entries(fresh_root, output)[1]
    backup_url = f"https://api.crossref.org/journals/{spec.get('crossref_issn', '')}/works"

    def previous_source(node):
        return next((c.get('url') for c in node if _local_name(c.tag) == 'source' and c.get('url')
                     and c.get('url') in {backup_url, spec.get('publisher_page')}), spec['source_url'])
    previous = [normalize(node, previous_source(node)) for node in previous]
    fresh = [normalize(node, source_url or (backup_url if fallback else spec['source_url'])) for node in fresh]
    doi_evidence = {}
    for node in previous + fresh:
        tokens = identities(node)
        dois = {token for token in tokens if token.startswith('doi:')}
        for token in tokens:
            doi_evidence.setdefault(token, set()).update(dois)
    ambiguous = {token for token, dois in doi_evidence.items() if len(dois)>1}
    old_tokens = [identities(node) for node in previous]
    old_dois = [{token for token in tokens if token.startswith('doi:')} for tokens in old_tokens]
    by_token = {}
    for index, tokens in enumerate(old_tokens):
        for token in tokens:
            if token not in ambiguous:
                by_token.setdefault(token, set()).add(index)
    historical_guids = {child_text(node, 'guid') for node in previous} - {''}
    targets, token_dois = {}, {}

    def seed(tokens, guid):
        dois = {token for token in tokens if token.startswith('doi:')}
        for token in tokens:
            if guid:
                targets.setdefault(token, set()).add(guid)
            token_dois.setdefault(token, set()).update(dois)

    for node, tokens in zip(previous, old_tokens):
        seed(tokens, child_text(node, 'guid'))
    for node in fresh:
        tokens = identities(node) - ambiguous
        dois = {token for token in tokens if token.startswith('doi:')}
        candidates = {index for token in tokens for index in by_token.get(token, ())}
        matches = [index for index in sorted(candidates)
                   if not (dois and old_dois[index] and dois.isdisjoint(old_dois[index]))
                   and child_text(previous[index], 'guid')]
        guid = child_text(previous[matches[0]], 'guid') if matches else child_text(node, 'guid')
        if not matches and (not guid or guid in historical_guids or 'guid:'+guid.lower() in ambiguous) and dois:
            guid = sorted(dois)[0]
        seed(tokens, guid)

    # Shared URL/GUID aliases must not assign one historical GUID to conflicting DOIs.
    aliases = {token: next(iter(guids)) for token, guids in targets.items()
               if token not in ambiguous and len(guids) == 1 and len(token_dois[token]) <= 1}
    keyed_fresh = [node for node in fresh if identities(node)]
    keyed_previous = [node for node in previous if identities(node)]
    merged = union({'custom': spec['output'], 'official_url': source_url or spec['source_url']},
                   [keyed_fresh, keyed_previous], aliases, ignored_tokens=ambiguous, allow_weak=False)
    # Without a strong identity, retain the entry rather than guessing from its title.
    merged.extend(node for node in fresh + previous if not identities(node))
    if _local_name(fresh_root.tag) == 'rss':
        channel, current = _feed_entries(fresh_root, output)
        for node in current:
            channel.remove(node)
    else:
        fresh_root = ET.Element('rss', version='2.0')
        channel = ET.SubElement(fresh_root, 'channel')
        for field, value in [('title', spec['name']), ('link', spec['source_url']),
                             ('description', 'Publisher feed and retained journal history.')]:
            ET.SubElement(channel, field).text = value
    channel.extend(merged)
    result = ET.tostring(fresh_root, encoding='utf-8', xml_declaration=True)
    feed_entry_count(result)
    return result


def _source_error(error: Exception, url: str = "") -> str:
    # Keep each cause bounded so both publisher and backup fit in health detail.
    return re.sub(r"<[^>]*>", "", format_source_error(error, url))[:240]


def mirror_one(spec: dict, root: Path, fetcher: Callable[[str], bytes] | None = None) -> MirrorResult:
    output = root / spec["output"]
    fetcher = fetcher or fetch_bytes
    try:
        raw = fetcher(spec["source_url"])
        feed_entry_count(raw, spec["source_url"])
        raw = _merge_mirror(spec, output, raw)
        entries = feed_entry_count(raw)
        if not output.exists() or output.read_bytes() != raw:
            _atomic_write(output, raw)
        return MirrorResult(spec["name"], spec["output"], entries, "updated")
    except Exception as exc:
        detail = "publisher: " + _source_error(exc, spec["source_url"])
        if spec.get('publisher_page'):
            try:
                from publisher_pages import buildings_cities_feed
                page_url = spec['publisher_page']
                raw = buildings_cities_feed(fetcher(page_url), spec)
                raw = _merge_mirror(spec, output, raw, source_url=page_url)
                entries = feed_entry_count(raw)
                _atomic_write(output, raw)
                return MirrorResult(spec['name'], spec['output'], entries,
                                    f'publisher-page-fallback: {detail}; source={page_url}')
            except Exception as page_error:
                detail += '; publisher page: ' + _source_error(page_error, spec['publisher_page'])
        if spec.get("crossref_issn"):
            try:
                raw = fetch_crossref_feed(spec)
                entries = feed_entry_count(raw)
                if not entries:
                    raise ValueError("Crossref backup has no entries in the configured window")
                raw = _merge_mirror(spec, output, raw, fallback=True)
                entries = feed_entry_count(raw)
                _atomic_write(output, raw)
                return MirrorResult(spec["name"], spec["output"], entries, f"crossref-fallback: {detail}")
            except Exception as backup_error:
                backup_url = f"https://api.crossref.org/journals/{spec['crossref_issn']}/works"
                detail += "; Crossref: " + _source_error(backup_error, backup_url)
        if output.exists():
            try:
                entries = feed_entry_count(output.read_bytes())
            except Exception as existing_error:
                detail += "; existing mirror invalid: " + _source_error(existing_error)
            else:
                return MirrorResult(spec["name"], spec["output"], entries, f"preserved: {detail}")
        raise RuntimeError(f"{spec['name']}: mirror failed: {detail}") from exc


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
        results = []
        for spec, future in zip(specs, futures):
            try:
                result = future.result()
            except Exception as error:
                detail = format_source_error(error)
                record(spec['output'], 'failed', detail=detail)
                results.append(MirrorResult(spec['name'], spec['output'], 0, 'failed'))
                continue
            status = 'ok' if result.status == 'updated' else 'fallback' if result.status.startswith(('crossref-fallback', 'publisher-page-fallback')) else 'preserved'
            identities = None
            if status == 'ok':
                _, entries = _feed_entries(ET.parse(root/spec['output']).getroot(), Path(spec['output']))
                identities = []
                for item in entries:
                    tokens = _item_tokens(item)
                    identity = next((token for prefix in ('doi:', 'arxiv:', 'guid:', 'url:', 'title:')
                                     for token in sorted(tokens) if token.startswith(prefix)), '')
                    identities.append(identity)
            record(spec['output'], status, result.entries, identities=identities,
                   detail=spec['name'] if status == 'ok' else spec['name'] + '; ' + result.status)
            results.append(result)
        return results


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
