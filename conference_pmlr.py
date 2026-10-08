"""Read complete PMLR volumes from the publisher's structured citation export."""
import datetime as dt
import re
import urllib.parse

from bs4 import BeautifulSoup
import yaml

from source_response import SourceResponseError, validate_response

BASE = 'https://proceedings.mlr.press/'
NAMES = {'aistats': 'Artificial Intelligence and Statistics',
         'uai': 'Uncertainty in Artificial Intelligence'}


def parse_citations(raw, conference, year, volume):
    from conference_rss import ConferencePaper, _is_main_paper
    validate_response(raw, BASE + volume + '/assets/bib/citeproc.yaml')
    try:
        records = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise SourceResponseError('Invalid PMLR citation YAML') from error
    if not isinstance(records, list) or not records:
        raise SourceResponseError('PMLR citation export has no records')
    papers = []
    for record in records:
        if not isinstance(record, dict):
            raise SourceResponseError('Invalid PMLR citation record')
        venue = str(record.get('container-title', ''))
        title = ' '.join(str(record.get('title', '')).split())
        url = str(record.get('URL', ''))
        link = urllib.parse.urlsplit(url)
        if (record.get('publisher') != 'PMLR' or NAMES[conference['slug']].casefold() not in venue.casefold()
                or str(record.get('volume', '')) != volume[1:]
                or link.hostname != 'proceedings.mlr.press'
                or not re.fullmatch('/' + re.escape(volume) + r'/[\w-]+\.html', link.path)
                or not title):
            raise SourceResponseError('PMLR record does not match the requested proceedings')
        if not _is_main_paper({'venue': venue, 'title': title}, conference) or title.casefold() in {'preface', 'foreword'}:
            continue
        issued = record.get('issued') or {}
        if not isinstance(issued, dict):
            raise SourceResponseError('Invalid PMLR issued-date metadata')
        parts = issued.get('date-parts', [])
        if not isinstance(parts, list):
            raise SourceResponseError('Invalid PMLR date parts')
        if parts and isinstance(parts[0], list):
            parts = parts[0]
        published = ''
        if len(parts) == 3:
            try:
                published = dt.date(*(int(p) for p in parts)).isoformat()
            except (TypeError, ValueError) as error:
                raise SourceResponseError('Invalid PMLR publication date') from error
        authors = [' '.join(filter(None, (a.get('given', ''), a.get('family', '')))).strip()
                   for a in record.get('author', [])]
        papers.append(ConferencePaper(
            conference=conference['acronym'], conference_name=conference['name'],
            title=title, url=url, guid='pmlr:' + link.path.strip('/'), year=year,
            authors=[name for name in authors if name], published=published,
            doi=str(record.get('DOI', '')).strip().lower()))
    return papers


def collect_pmlr(client, conference, year):
    if conference.get('slug') not in NAMES:
        return []
    raw = client.get(BASE)
    validate_response(raw, BASE)
    soup = BeautifulSoup(raw, 'html.parser', from_encoding='utf-8')
    if not soup.title or 'Proceedings of Machine Learning Research' not in soup.title.get_text():
        raise SourceResponseError('Unexpected PMLR volume index')
    volumes = set()
    for anchor in soup.find_all('a', href=True):
        row = anchor.find_parent('li')
        if row is None or not re.search(r'\b' + conference['acronym'] + r'\s+' + str(year) + r'\b', row.get_text()):
            continue
        link = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, anchor['href']))
        if link.hostname == 'proceedings.mlr.press' and re.fullmatch(r'/v\d+/?', link.path):
            volumes.add(link.path.strip('/'))
    papers = []
    for volume in sorted(volumes):
        raw = client.get(BASE + volume + '/assets/bib/citeproc.yaml')
        papers.extend(parse_citations(raw, conference, year, volume))
    return papers
