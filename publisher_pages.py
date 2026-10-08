"""Recover publisher articles from verified semantic homepage metadata."""
import datetime as dt
import email.utils
import re
import urllib.parse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from journal_dates import DC, append_metadata
from source_response import SourceResponseError, validate_response


def buildings_cities_feed(raw, spec):
    url = spec['publisher_page']
    validate_response(raw, url)
    soup = BeautifulSoup(raw, 'html.parser', from_encoding='utf-8')
    if (not soup.title or not re.search(r'Buildings\s*(?:&|and)\s*Cities', soup.title.get_text(), re.I)
            or '2632-6655' not in soup.get_text()):
        raise SourceResponseError('Unexpected Buildings & Cities publisher page')
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    ET.SubElement(channel, 'title').text = spec['name']
    ET.SubElement(channel, 'link').text = url
    ET.SubElement(channel, 'description').text = 'Articles and publication dates from the official journal homepage.'
    seen = set()
    for anchor in soup.find_all('a', href=True):
        link = urllib.parse.urlsplit(urllib.parse.urljoin(url, anchor['href']))
        match = re.fullmatch(r'/(?:en/)?articles/(10\.5334/bc\.\d+)/?', link.path)
        if link.hostname != 'journal-buildingscities.org' or not match or match[1] in seen:
            continue
        title = ' '.join(anchor.get_text(' ', strip=True).split())
        if not title:
            continue
        row = anchor.parent
        while row is not None and row.find('time') is None:
            row = row.parent
        if row is None:
            continue
        article_ids = {a.get('href') for a in row.find_all('a', href=True)
                       if re.search(r'/articles/10\.5334/bc\.\d+', a['href'])}
        if len(article_ids) != 1:
            continue
        dates = {time.get('datetime') for time in row.find_all('time')}
        if len(dates) != 1:
            raise SourceResponseError('Ambiguous publisher article date')
        try:
            published = dt.date.fromisoformat(dates.pop())
        except (TypeError, ValueError) as error:
            raise SourceResponseError('Invalid publisher article publication date') from error
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = title
        ET.SubElement(item, 'link').text = urllib.parse.urlunsplit(link)
        ET.SubElement(item, 'guid', isPermaLink='false').text = 'doi:' + match[1]
        ET.SubElement(item, '{'+DC+'}identifier').text = 'doi:' + match[1]
        address = row.find('address')
        if address:
            for author in address.get_text(' ', strip=True).split(','):
                if author.strip():
                    ET.SubElement(item, '{'+DC+'}creator').text = author.strip()
        append_metadata(item, {'publication': published.isoformat(), 'publication_source': 'publisher-page'})
        ET.SubElement(item, 'pubDate').text = email.utils.format_datetime(
            dt.datetime.combine(published, dt.time(), tzinfo=dt.timezone.utc))
        ET.SubElement(item, 'source', url=url).text = 'Buildings & Cities official homepage'
        seen.add(match[1])
    if not seen:
        raise SourceResponseError('Publisher homepage has no valid article rows')
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True)
