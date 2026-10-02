"""Collect JMLR with publication months verified from PDF first pages."""
import datetime as dt
import html
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pypdf import PdfReader
from journal_rss_aggregator import FeedItem, fetch_bytes, fetch_text, write_rss
from rss_ops import write_json

BASE = 'https://www.jmlr.org'
FEED = 'https://fengziclassmate.github.io/journal-rss/jmlr.xml'
START = '2026-09'


def publication_month(text):
    match = re.search(r'\bPublished\s+(\d{1,2})\s*/\s*(\d{4}|\d{2})\b', text, re.I)
    if not match:
        raise ValueError('No Published month/year in PDF first page')
    month, year = map(int, match.groups())
    if year < 100:
        year += 2000
    return dt.date(year, month, 1).strftime('%Y-%m')


def parse_volume(document):
    papers = {}
    for row in BeautifulSoup(document, 'html.parser').select('dl'):
        title = row.select_one('dt')
        abstract = row.select_one('a[href$=".html"]')
        pdf = row.select_one('a[href$=".pdf"]')
        authors = row.select_one('i')
        if not all((title, abstract, pdf, authors)):
            raise ValueError('Unexpected JMLR directory markup')
        link = urljoin(BASE, abstract['href'])
        papers[link] = dict(title=title.get_text(' ', strip=True),
                            authors=authors.get_text(' ', strip=True),
                            pdf=urljoin(BASE, pdf['href']))
    if not papers:
        raise ValueError('Empty JMLR directory; previous feed retained')
    return papers


def inspect_pdf(paper):
    raw = fetch_bytes(paper['pdf'], timeout=30, retries=1)
    text = PdfReader(io.BytesIO(raw)).pages[0].extract_text()
    return publication_month(text)


def feed_items(state, now):
    items = []
    for link, paper in state.items():
        if not START <= paper['month'] <= now.strftime('%Y-%m'):
            continue
        description = '<p>Authors: ' + html.escape(paper['authors']) + '</p>'
        description += '<p>Publication month (PDF first page): ' + paper['month'] + '.</p>'
        description += '<p><a href="' + html.escape(paper['pdf'], quote=True) + '">Publisher PDF</a></p>'
        items.append(FeedItem(source='JMLR', title=paper['title'], link=link,
                              guid=link, description=description,
                              published=dt.datetime.fromisoformat(paper['first_seen']), source_url=BASE,
                              dates={'publication': paper['month'], 'publication_source': 'publisher PDF',
                                     'first_seen': paper['first_seen']}))
    return items


def main():
    now = dt.datetime.now(dt.timezone.utc)
    path = Path('research-data/jmlr-state.json')
    state = json.loads(path.read_text('utf-8')) if path.exists() else {}
    papers = {}
    for year in range(2026, now.year + 1):
        papers.update(parse_volume(fetch_text(f'{BASE}/papers/v{year-1999}/')))
    pending = {link: paper for link, paper in papers.items() if link not in state}
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(inspect_pdf, paper): (link, paper) for link, paper in pending.items()}
        for job in as_completed(jobs):
            link, paper = jobs[job]
            try:
                month = job.result()
                state[link] = dict(paper, month=month, first_seen=now.isoformat())
                write_json(path, state)
                print(f'Checked {len(state)}: {month} {link}', flush=True)
            except Exception as error:
                errors.append(link)
                print(f'Unresolved {link}: {error}', flush=True)
    # Unknown dates are never assigned the current month or silently discarded.
    if errors:
        raise RuntimeError(f'{len(errors)} unresolved publication dates; cached successes, previous feed retained')
    items = feed_items(state, now)
    write_rss(items, Path('jmlr.xml'), feed_title='JMLR_FZTX', feed_link=FEED,
              feed_description='JMLR since September 2026, verified by PDF publication month; discovery times are separate.',
              max_items=max(1, len(items)), prefix_item_titles=False, feed_language='en')
    print(f'JMLR: {len(state)} checked, {len(items)} eligible before official/read filtering')


if __name__ == '__main__':
    main()
