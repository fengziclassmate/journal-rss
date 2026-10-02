"""Accepted TMLR papers with separate publication months and discovery times."""
import datetime as dt
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from journal_rss_aggregator import FeedItem, fetch_text, write_rss

SOURCE = 'https://jmlr.org/tmlr/papers/'
FEED = 'https://fengziclassmate.github.io/journal-rss/tmlr.xml'
START = '2026-09'


def parse_papers(document):
    papers = {}
    for row in BeautifulSoup(document, 'html.parser').select('li.item'):
        title = row.select_one('h4')
        authors = row.select_one('p i')
        forum = row.select_one('a[href*="openreview.net/forum?id="]')
        date = re.search(r'\b(January|February|March|April|May|June|July|August|September|October|November|December) (20\d{2})\b', row.get_text(' ', strip=True))
        if not all((title, authors, forum, date)):
            raise ValueError('Unexpected TMLR paper markup; retaining previous feed')
        ident = parse_qs(urlparse(forum['href']).query)['id'][0]
        month = dt.datetime.strptime(date.group(), '%B %Y').strftime('%Y-%m')
        papers[ident] = dict(title=title.get_text(' ', strip=True), authors=authors.get_text(' ', strip=True), month=month)
    if not papers:
        raise ValueError('No accepted papers found; retaining previous feed')
    return papers


def update_state(state, papers, now):
    result = dict(state)
    for ident, paper in papers.items():
        if START <= paper['month'] <= now.strftime('%Y-%m'):
            first = result.get(ident, {}).get('first_seen', now.isoformat())
            result[ident] = dict(paper, first_seen=first)
    return result


def main():
    path = Path('research-data/tmlr-state.json')
    state = json.loads(path.read_text('utf-8')) if path.exists() else {}
    state = update_state(state, parse_papers(fetch_text(SOURCE)), dt.datetime.now(dt.timezone.utc))
    items = []
    for ident, paper in state.items():
        link = 'https://openreview.net/forum?id=' + ident
        description = '<p>Authors: ' + html.escape(paper['authors']) + '</p>'
        description += '<p>Publication month: ' + paper['month'] + ' (publisher provides no exact day).</p>'
        description += '<p><a href="' + html.escape(link, quote=True) + '">OpenReview</a></p>'
        items.append(FeedItem(source='TMLR', title=paper['title'], link=link, description=description,
                              published=dt.datetime.fromisoformat(paper['first_seen']),
                              guid='tmlr:' + ident, source_url=SOURCE,
                              dates={'publication': paper['month'], 'publication_source': 'publisher directory',
                                     'first_seen': paper['first_seen']}))
    write_rss(items, Path('tmlr.xml'), feed_title='TMLR_FZTX', feed_link=FEED,
              feed_description='Accepted TMLR papers since September 2026. Publication months and first discovery times are separate.',
              max_items=max(1, len(items)), prefix_item_titles=False, feed_language='en')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    print(f'TMLR: {len(items)} accepted papers')


if __name__ == '__main__':
    main()
