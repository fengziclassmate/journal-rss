"""Publish Hugging Face Daily Papers from its official API, without topic filtering."""
import argparse
import datetime as dt
import email.utils
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from official_feed_proxy import fetch_bytes
from rss_ops import write_feed, write_json

API = 'https://huggingface.co/api/daily_papers'
PAGE = 'https://huggingface.co/papers'
STATE = Path('research-data/huggingface-papers-state.json')
OUTPUT = Path('huggingface-papers.xml')
DC = 'http://purl.org/dc/elements/1.1/'
ET.register_namespace('dc', DC)


def parse_record(record):
    paper = record['paper']
    ident = paper['id']
    if not isinstance(ident, str) or not re.fullmatch(r'\d{4}\.\d{4,5}', ident):
        raise ValueError('Invalid Hugging Face arXiv identifier')
    row = dict(id=ident, title=paper['title'], summary=paper['summary'],
               authors=[author['name'] for author in paper['authors']],
               published_at=paper['publishedAt'], submitted_at=paper['submittedOnDailyAt'])
    if not isinstance(row['title'], str) or not row['title'].strip() or not isinstance(row['summary'], str):
        raise ValueError('Invalid Hugging Face paper title or abstract: ' + ident)
    if any(not isinstance(name, str) or not name.strip() for name in row['authors']):
        raise ValueError('Invalid Hugging Face author: ' + ident)
    for field in ('published_at', 'submitted_at'):
        date = dt.datetime.fromisoformat(row[field])
        if date.tzinfo is None:
            raise ValueError('Hugging Face timestamp has no timezone: ' + ident)
        row[field] = date.astimezone(dt.timezone.utc).isoformat()
    return row


def collect():
    rows = {}
    for page in range(3):
        url = f'{API}?limit=100&sort=publishedAt&p={page}'
        payload = json.loads(fetch_bytes(url, attempts=2, timeout=30,
                                        headers={'Accept': 'application/json'}))
        if not isinstance(payload, list):
            raise ValueError('Hugging Face Daily Papers response is not a list')
        for record in payload:
            row = parse_record(record)
            rows[row['id']] = row
        if len(payload) < 100:
            break
    if not rows:
        raise ValueError('Hugging Face Daily Papers returned no papers; existing feed retained')
    return rows


def build_feed(rows):
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    for name, value in [('title', 'Hugging Face Daily Papers'), ('link', PAGE),
                        ('description', 'Papers featured on Hugging Face Daily Papers, without topic filtering.'),
                        ('language', 'en')]:
        ET.SubElement(channel, name).text = value
    for row in rows.values():
        ident = row['id']
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = row['title']
        ET.SubElement(item, 'link').text = PAGE + '/' + ident
        ET.SubElement(item, 'guid', isPermaLink='false').text = 'hf-papers:arxiv:' + ident
        ET.SubElement(item, 'pubDate').text = email.utils.format_datetime(dt.datetime.fromisoformat(row['published_at']))
        for author in row['authors']:
            ET.SubElement(item, '{' + DC + '}creator').text = author
        body = '<p>' + html.escape(row['summary']) + '</p>'
        body += f'<p><a href="https://arxiv.org/abs/{ident}">arXiv:{ident}</a> | <a href="https://arxiv.org/pdf/{ident}">PDF</a></p>'
        ET.SubElement(item, 'description').text = body
        ET.SubElement(item, 'source', url=PAGE).text = 'Hugging Face Daily Papers'
    # Reject malformed upstream characters before replacing a usable feed.
    ET.fromstring(ET.tostring(rss, encoding='utf-8'))
    return rss


def run(root, cached=False):
    root = Path(root)
    path = root / STATE
    saved = json.loads(path.read_text('utf-8')) if path.exists() else {}
    fresh = {} if cached else collect()
    rows = {**saved, **fresh}
    if not rows:
        raise ValueError('No cached Hugging Face Daily Papers records')
    rows = dict(sorted(rows.items(), key=lambda pair: (pair[1]['submitted_at'], pair[1]['published_at'], pair[0]), reverse=True)[:1000])
    feed = build_feed(rows)
    if not cached:
        write_json(path, rows)
    write_feed(root / OUTPUT, feed)
    result = {'fetched': len(fresh), 'papers': len(rows), 'cached': cached}
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--cached', action='store_true')
    args = parser.parse_args()
    run(args.root, args.cached)
