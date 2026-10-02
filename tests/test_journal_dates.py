import datetime as dt
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from journal_dates import DC, NS, crossref_dates, partial_date
from journal_rss_aggregator import FeedItem, parse_datetime, write_rss, read_existing_feed_items, crossref_item_to_feed_item


class JournalDateTests(unittest.TestCase):
    def test_parse_iso_rfc_partial_and_invalid(self):
        for raw in ['2026-09-02T00:00:00+00:00','2026-09-02T08:00:00+08:00',
                    '2026-09-02T00:00:00Z','2026-09-02','2026/09/02',
                    '2026-09','2026','Wed, 02 Sep 2026 00:00:00 +0000']:
            self.assertIsNotNone(parse_datetime(raw), raw)
        for raw in ['2026-02-30','2026-13','2026-09-02garbage','']:
            self.assertIsNone(parse_datetime(raw), raw)
        self.assertEqual(parse_datetime('2026-09-02T08:00:00+08:00').utcoffset(),dt.timedelta(hours=8))

    def test_crossref_precision_and_semantics(self):
        dates=crossref_dates({'created':{'date-parts':[[2026,10,2]]},'published-print':{'date-parts':[[2026,9]]}})
        self.assertEqual(dates['publication'],'2026-09')
        self.assertEqual(dates['registered'],'2026-10-02')
        self.assertNotIn('publication',crossref_dates({'created':{'date-parts':[[2026,10,2]]}}))
        self.assertEqual(partial_date({'date-parts':[[2026]]}),'2026')
        self.assertEqual(partial_date({'date-parts':[[2026,13]]}),'')

    def test_roundtrip_month_has_no_fabricated_day_and_stable_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'a.xml'
            def write(items):
                write_rss(items,path,feed_title='Test',feed_link='https://example.org',feed_description='',max_items=10)
            item=FeedItem(source='TMLR',title='Title',link='https://example.org/a',guid='stable',
                          published=dt.datetime(2026,10,2,tzinfo=dt.timezone.utc),dates={'publication':'2026-09'})
            write([item]);root=ET.parse(path)
            self.assertIsNone(root.find('./channel/item/pubDate'))
            self.assertEqual(root.findtext('./channel/item/{'+DC+'}date'),'2026-09')
            before=path.read_bytes(); write(read_existing_feed_items(path))
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(root.findtext('./channel/item/guid'),'stable')

    def test_registration_does_not_override_publication(self):
        item=crossref_item_to_feed_item({'DOI':'10.1/test','title':['Test'],
            'created':{'date-parts':[[2026,10,2]]},'published-online':{'date-parts':[[2026,9,23]]}},
            source='Test',source_url='https://example.org',date_fields=['created'])
        self.assertEqual(item.dates['publication'],'2026-09-23')

    def test_year_only_and_unknown_have_no_pubdate(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'a.xml'
            items=[FeedItem(source='Test',title=str(n),link=str(n),dates=d) for n,d in enumerate([
                {'publication':'2026'},{'registered':'2026-09-02'}])]
            write_rss(items,path,feed_title='Test',feed_link='https://example.org',feed_description='',max_items=10)
            self.assertEqual(ET.parse(path).findall('.//pubDate'),[])
