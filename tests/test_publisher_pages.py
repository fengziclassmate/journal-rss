import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from official_feed_proxy import mirror_one
from publisher_pages import buildings_cities_feed
from source_response import SourceResponseError


SPEC = {'name': 'Buildings & Cities', 'source_url': 'https://account.journal-buildingscities.org/feed',
        'publisher_page': 'https://journal-buildingscities.org/', 'output': 'official.xml'}


def document(rows):
    return ('<html><title>Buildings &amp; Cities</title><body>E-ISSN: 2632-6655' + rows + '</body></html>').encode()


ROW = '<div><div><a href="/en/articles/10.5334/bc.941">Climate change risk and decision-making</a><address>Simon Foxell, Ian Cooper</address></div><time datetime="2026-10-07">Oct 7, 2026</time></div>'


class PublisherPageTests(unittest.TestCase):
    def test_dates_dois_authors_and_duplicate_homepage_links(self):
        image = '<div><a href="/en/articles/10.5334/bc.941"><img src="cover.png"></a></div>'
        root = ET.fromstring(buildings_cities_feed(document(image + ROW + ROW), SPEC))
        items = root.findall('./channel/item')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].findtext('guid'), 'doi:10.5334/bc.941')
        self.assertIn('07 Oct 2026', items[0].findtext('pubDate'))
        self.assertEqual(len(items[0].findall('{http://purl.org/dc/elements/1.1/}creator')), 2)
        self.assertEqual(items[0].find('source').get('url'), SPEC['publisher_page'])

    def test_ambiguous_outer_container_is_not_treated_as_a_paper(self):
        row = '<div><a href="/en/articles/10.5334/bc.941">One</a><a href="/en/articles/10.5334/bc.942">Two</a><time datetime="2026-10-07"></time></div>'
        with self.assertRaises(SourceResponseError):
            buildings_cities_feed(document(row), SPEC)

    def test_invalid_publisher_and_dates_do_not_overwrite_history(self):
        for raw in [document(ROW).replace(b'2632-6655', b'0000-0000'),
                    document(ROW.replace('datetime="2026-10-07"', 'datetime="2026-10"')),
                    document('')]:
            with self.subTest(raw=raw), self.assertRaises(SourceResponseError):
                buildings_cities_feed(raw, SPEC)

    def test_publisher_page_is_used_only_after_feed_failure_and_preserves_guid(self):
        def fetch(url):
            if url == SPEC['source_url']:
                raise TimeoutError('publisher feed timed out')
            return document(ROW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / SPEC['output']
            path.write_text('<rss><channel><item><title>Existing title</title><guid>original-guid</guid><link>https://doi.org/10.5334/bc.941</link><source>Publisher</source></item></channel></rss>')
            result = mirror_one(SPEC, Path(directory), fetch)
            self.assertTrue(result.status.startswith('publisher-page-fallback'))
            self.assertEqual(result.entries, 1)
            self.assertEqual(ET.parse(path).findtext('./channel/item/guid'), 'original-guid')
            self.assertEqual(ET.parse(path).find('./channel/item/source').get('url'), SPEC['publisher_page'])

    def test_invalid_page_keeps_existing_feed_untouched(self):
        def fetch(url):
            if url == SPEC['source_url']:
                raise TimeoutError()
            return document('')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / SPEC['output']
            raw = b'<rss><channel><item><guid>old</guid></item></channel></rss>'
            path.write_bytes(raw)
            result = mirror_one(SPEC, Path(directory), fetch)
            self.assertTrue(result.status.startswith('preserved'))
            self.assertEqual(path.read_bytes(), raw)
