#!/usr/bin/env python3
"""Build a combined RSS feed for several journal sites.

Sources:
- dqxxkx.cn current issue page, because its listed RSS XML is currently 404.
- ygxb.ac.cn official per-issue RSS endpoints discovered from its RSS page JS.
- ch.whu.edu.cn official per-issue RSS files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import email.utils
import html
import json
import re
import shutil
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: beautifulsoup4. Install with: pip install -r requirements.txt"
    ) from exc


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}

UTC = dt.timezone.utc


@dataclasses.dataclass
class FeedItem:
    source: str
    title: str
    link: str
    description: str = ""
    published: dt.datetime | None = None
    guid: str = ""
    source_url: str = ""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def fetch_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
    retries: int = 2,
) -> bytes:
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=merged_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - report and retry network errors.
            last_exc = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        command = [curl, "-L", "--silent", "--show-error", "--max-time", str(timeout)]
        for key, value in merged_headers.items():
            command.extend(["-H", f"{key}: {value}"])
        command.append(url)
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=timeout + 5,
            )
            if completed.stdout:
                return completed.stdout
        except Exception as exc:  # noqa: BLE001 - curl is only a fallback.
            last_exc = exc

    raise RuntimeError(f"Failed to fetch {url}: {last_exc}") from last_exc


def fetch_text(url: str, **kwargs: object) -> str:
    raw = fetch_bytes(url, **kwargs)
    return raw.decode("utf-8", errors="replace")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(element):
        if local_name(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def parse_datetime(value: str) -> dt.datetime | None:
    value = html.unescape((value or "").strip())
    if not value:
        return None

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        pass

    patterns = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m",
        "%Y",
    ]
    for pattern in patterns:
        try:
            parsed = dt.datetime.strptime(value[: len(pattern)], pattern)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def issue_to_datetime(year: int | None, issue: str | int | None) -> dt.datetime | None:
    if not year:
        return None
    issue_text = str(issue or "1")
    match = re.search(r"\d+", issue_text)
    month = int(match.group(0)) if match else 1
    month = min(max(month, 1), 12)
    return dt.datetime(int(year), month, 1, tzinfo=UTC)


def parse_year_issue_from_text(text: str) -> tuple[int | None, str | None]:
    patterns = [
        r"(?P<year>20\d{2})\s*年第\s*(?P<issue>[0-9A-Za-z]+)\s*期",
        r"(?P<year>20\d{2})\s*,\s*\d+\((?P<issue>[^)]+)\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group("year")), match.group("issue")
    return None, None


def parse_rss(
    raw: bytes,
    *,
    source: str,
    source_url: str,
    default_year: int | None = None,
    default_issue: str | int | None = None,
) -> list[FeedItem]:
    if not raw.strip():
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    channel_title = ""
    for element in root.iter():
        if local_name(element.tag) == "channel":
            channel_title = child_text(element, "title")
            break

    year, issue = parse_year_issue_from_text(channel_title)
    if default_year is not None:
        year = default_year
    if default_issue is not None:
        issue = str(default_issue)
    fallback_date = issue_to_datetime(year, issue)

    items: list[FeedItem] = []
    rdf_about_key = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
    for element in root.iter():
        if local_name(element.tag) != "item":
            continue
        title = child_text(element, "title")
        link = child_text(element, "link") or element.attrib.get(rdf_about_key, "")
        description = child_text(element, "description", "encoded")
        published = (
            parse_datetime(child_text(element, "pubDate", "date", "published"))
            or fallback_date
        )
        if title and link:
            items.append(
                FeedItem(
                    source=source,
                    title=html.unescape(title).strip(),
                    link=html.unescape(link).strip(),
                    description=description.strip(),
                    published=published,
                    guid=html.unescape(link).strip(),
                    source_url=source_url,
                )
            )
    return items


def parse_dqxxkx_current() -> list[FeedItem]:
    url = "https://www.dqxxkx.cn/CN/current"
    text = fetch_text(url, timeout=60, retries=5)
    soup = BeautifulSoup(text, "html.parser")

    items: list[FeedItem] = []
    for block in soup.select("div.article-l"):
        title_link = block.find(
            "a",
            href=lambda href: bool(
                href
                and href.startswith("https://www.dqxxkx.cn/CN/10.12082/")
            ),
        )
        if not title_link:
            continue

        title = " ".join(title_link.get_text(" ", strip=True).split())
        link = urllib.parse.urljoin(url, title_link["href"])
        block_text = " ".join(block.get_text(" ", strip=True).split())
        year, issue = parse_year_issue_from_text(block_text)

        doi = ""
        doi_link = block.find("a", class_="j-doi", href=lambda href: href and "doi.org" in href)
        if doi_link:
            doi = doi_link.get_text(" ", strip=True)

        if "收藏" in block_text:
            description = block_text.split("收藏", 1)[1].strip()
        else:
            description = block_text
        if doi:
            description = f"DOI: {doi}<br/>{description}"

        items.append(
            FeedItem(
                source="地球信息科学学报",
                title=title,
                link=link,
                description=description,
                published=issue_to_datetime(year, issue),
                guid=doi or link,
                source_url=url,
            )
        )
    return items


def get_ygxb_site_id() -> int:
    url = "https://www.ygxb.ac.cn/rc-pub/front/site/findBySld?sld=www"
    headers = {
        "Referer": "https://www.ygxb.ac.cn/rssList?lang=zh",
        "Accept": "application/json, text/plain, */*",
        "language": "zh",
    }
    data = json.loads(fetch_text(url, headers=headers, timeout=30, retries=2))
    site_id = data.get("data", {}).get("id")
    if not site_id:
        raise RuntimeError(f"Could not detect ygxb siteId from {url}: {data}")
    return int(site_id)


def get_ygxb_periods(start_year: int, end_year: int) -> list[tuple[int, str, int, int]]:
    site_id = get_ygxb_site_id()
    url = (
        "https://www.ygxb.ac.cn/rc-pub/front/front-period/getPeriodTree"
        f"?siteId={site_id}"
    )
    headers = {
        "Referer": "https://www.ygxb.ac.cn/rssList?lang=zh",
        "Accept": "application/json, text/plain, */*",
        "language": "zh",
        "siteId": str(site_id),
    }
    data = json.loads(fetch_text(url, headers=headers, timeout=60, retries=2))
    periods: list[tuple[int, str, int, int]] = []
    for publication in data.get("data", []):
        for year_group in publication.get("periods", []):
            year = int(year_group.get("year", 0))
            if not (start_year <= year <= end_year):
                continue
            for period in year_group.get("periods", []):
                period_id = period.get("id")
                issue = period.get("issue")
                if period_id and issue:
                    periods.append((year, str(issue), int(period_id), site_id))
    return periods


def fetch_ygxb_feed(period: tuple[int, str, int, int]) -> list[FeedItem]:
    year, issue, period_id, site_id = period
    url = f"https://www.ygxb.ac.cn/rc-pub/front/rss?periodId={period_id}"
    headers = {
        "Referer": "https://www.ygxb.ac.cn/rssList?lang=zh",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "language": "zh",
        "siteId": str(site_id),
    }
    raw = fetch_bytes(url, headers=headers, timeout=45, retries=2)
    return parse_rss(
        raw,
        source="遥感学报",
        source_url=url,
        default_year=year,
        default_issue=issue,
    )


def fetch_ch_whu_feed(year: int, issue: int) -> list[FeedItem]:
    url = f"https://ch.whu.edu.cn/rss/{year}_{issue}.xml"
    raw = fetch_bytes(url, timeout=45, retries=2)
    return parse_rss(
        raw,
        source="武汉大学学报(信息科学版)",
        source_url=url,
        default_year=year,
        default_issue=issue,
    )


def collect_parallel(
    jobs: Iterable[object],
    fetcher,
    *,
    label: str,
    workers: int,
) -> list[FeedItem]:
    items: list[FeedItem] = []
    jobs = list(jobs)
    if not jobs:
        return items

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fetcher, job): job for job in jobs}
        done_count = 0
        for future in concurrent.futures.as_completed(future_map):
            job = future_map[future]
            done_count += 1
            try:
                new_items = future.result()
                if new_items:
                    items.extend(new_items)
            except Exception as exc:  # noqa: BLE001 - continue other feeds.
                log(f"[warn] {label} job failed: {job}: {exc}")
            if done_count % 20 == 0 or done_count == len(jobs):
                log(f"[info] {label}: {done_count}/{len(jobs)} feeds checked")
    return items


def dedupe_items(items: Iterable[FeedItem]) -> list[FeedItem]:
    seen: set[str] = set()
    result: list[FeedItem] = []
    for item in items:
        key = item.guid or item.link or f"{item.source}:{item.title}"
        key = key.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def read_existing_feed_items(path: Path) -> list[FeedItem]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []

    items: list[FeedItem] = []
    for element in root.findall("./channel/item"):
        source = child_text(element, "category")
        title = child_text(element, "title")
        if title.startswith(f"[{source}] "):
            title = title[len(source) + 3 :]
        link = child_text(element, "link")
        description = child_text(element, "description")
        published = parse_datetime(child_text(element, "pubDate"))
        guid = child_text(element, "guid")
        source_url = ""
        for child in list(element):
            if local_name(child.tag) == "source":
                source_url = child.attrib.get("url", "")
                break
        if source and title and link:
            items.append(
                FeedItem(
                    source=source,
                    title=title,
                    link=link,
                    description=description,
                    published=published,
                    guid=guid or link,
                    source_url=source_url,
                )
            )
    return items


def preserve_failed_sources(
    new_items: list[FeedItem],
    output_path: Path,
    failed_sources: set[str],
) -> list[FeedItem]:
    if not failed_sources:
        return new_items
    existing_items = read_existing_feed_items(output_path)
    if not existing_items:
        return new_items
    preserved = [
        item for item in existing_items if item.source in failed_sources
    ]
    if preserved:
        log(
            "[info] preserved "
            f"{len(preserved)} existing items for failed sources: "
            f"{', '.join(sorted(failed_sources))}"
        )
    return new_items + preserved


def write_rss(
    items: list[FeedItem],
    output_path: Path,
    *,
    feed_title: str,
    feed_link: str,
    feed_description: str,
    max_items: int,
) -> int:
    items = sorted(
        items,
        key=lambda item: item.published or dt.datetime(1900, 1, 1, tzinfo=UTC),
        reverse=True,
    )[:max_items]

    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed_title
    ET.SubElement(channel, "link").text = feed_link
    ET.SubElement(channel, "description").text = feed_description
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.format_datetime(
        dt.datetime.now(UTC)
    )
    atom_link = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    atom_link.set("href", feed_link)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for item in items:
        item_el = ET.SubElement(channel, "item")
        ET.SubElement(item_el, "title").text = f"[{item.source}] {item.title}"
        ET.SubElement(item_el, "link").text = item.link
        ET.SubElement(item_el, "guid").text = item.guid or item.link
        ET.SubElement(item_el, "category").text = item.source
        if item.description:
            ET.SubElement(item_el, "description").text = item.description
        if item.published:
            ET.SubElement(item_el, "pubDate").text = email.utils.format_datetime(
                item.published
            )
        if item.source_url:
            ET.SubElement(item_el, "source", url=item.source_url).text = item.source

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return len(items)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--output", default="feed.xml")
    parser.add_argument("--feed-link", default="https://example.com/feed.xml")
    parser.add_argument("--max-items", type=int, default=5000)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    all_items: list[FeedItem] = []
    failed_sources: set[str] = set()
    output_path = Path(args.output)

    log("[info] fetching dqxxkx current issue")
    try:
        dqxxkx_items = parse_dqxxkx_current()
        all_items.extend(dqxxkx_items)
        log(f"[info] dqxxkx: {len(dqxxkx_items)} items")
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] dqxxkx failed: {exc}")
        failed_sources.add("地球信息科学学报")

    log("[info] discovering ygxb periods")
    try:
        ygxb_periods = get_ygxb_periods(args.start_year, args.end_year)
        log(f"[info] ygxb: {len(ygxb_periods)} period feeds")
        all_items.extend(
            collect_parallel(
                ygxb_periods,
                fetch_ygxb_feed,
                label="ygxb",
                workers=args.workers,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] ygxb failed: {exc}")
        failed_sources.add("遥感学报")

    ch_whu_jobs = [
        (year, issue)
        for year in range(args.start_year, args.end_year + 1)
        for issue in range(1, 13)
    ]
    log(f"[info] ch.whu: {len(ch_whu_jobs)} possible issue feeds")
    all_items.extend(
        collect_parallel(
            ch_whu_jobs,
            lambda job: fetch_ch_whu_feed(job[0], job[1]),
            label="ch.whu",
            workers=args.workers,
        )
    )

    all_items = preserve_failed_sources(all_items, output_path, failed_sources)
    final_items = dedupe_items(all_items)
    written_count = write_rss(
        final_items,
        output_path,
        feed_title="Journal RSS Aggregator",
        feed_link=args.feed_link,
        feed_description=(
            f"Combined journal feed for dqxxkx current issue and "
            f"ygxb/ch.whu issues from {args.start_year} to {args.end_year}."
        ),
        max_items=args.max_items,
    )
    log(
        f"[info] wrote {written_count} items to {args.output} "
        f"({len(final_items)} unique items collected)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
