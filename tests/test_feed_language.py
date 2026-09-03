import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from journal_rss_aggregator import (
    CROSSREF_JOURNALS,
    FeedItem,
    crossref_query_params,
    fetch_crossref_journal_items,
    journal_item_limit,
    write_rss,
)


class FeedLanguageTests(unittest.TestCase):
    def test_english_feed_does_not_inherit_chinese_skip_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feed.xml'
            write_rss(
                [FeedItem(source='CEUS', title='An English title', link='https://doi.org/10.1/test', guid='10.1/test')],
                path, feed_title='CEUS', feed_link='https://example.com/rss.xml',
                feed_description='English journal', max_items=10, prefix_item_titles=False, feed_language='en',
            )
            root = ET.parse(path)
            self.assertEqual(root.findtext('./channel/language'), 'en')
            self.assertEqual(root.findtext('./channel/item/guid'), '10.1/test')
            self.assertEqual(root.find('./channel/item/guid').get('isPermaLink'), 'false')

    def test_combined_chinese_feed_keeps_chinese_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feed.xml'
            write_rss([], path, feed_title='Combined', feed_link='https://example.com/rss.xml',
                      feed_description='Chinese journals', max_items=10)
            self.assertEqual(ET.parse(path).findtext('./channel/language'), 'zh-CN')

    def test_journal_specific_limit_caps_large_early_access_feed(self):
        self.assertEqual(journal_item_limit({'max_items': 125}, 500), 125)
        self.assertEqual(journal_item_limit({'max_items': 125}, 50), 50)
        self.assertEqual(journal_item_limit({}, 500), 500)

    def test_jgsa_has_latest_and_current_issue_feeds(self):
        feeds = {item['output']: item for item in CROSSREF_JOURNALS if item['issn'] == '2509-8829'}

        self.assertEqual(set(feeds), {'jgsa.xml', 'jgsa-current-issue.xml'})
        self.assertNotIn('current_issue_only', feeds['jgsa.xml'])
        self.assertEqual(feeds['jgsa-current-issue.xml']['current_issue_only'], 'true')
        self.assertEqual(feeds['jgsa.xml']['from_date'], '2026-06-01')

    def test_tgrs_current_volume_uses_precise_metadata_dates(self):
        feed = next(item for item in CROSSREF_JOURNALS if item['output'] == 'tgrs-current-issue.xml')

        self.assertEqual(feed['date_filter'], 'created')
        self.assertEqual(
            feed['date_fields'],
            'created,deposited,published-online,published-print,published',
        )

    def test_essd_has_latest_and_current_issue_feeds(self):
        feeds = {item['output']: item for item in CROSSREF_JOURNALS if item['issn'] == '1866-3516'}

        self.assertEqual(set(feeds), {'essd.xml', 'essd-current-issue.xml'})
        self.assertEqual(feeds['essd.xml']['from_date'], '2026-06-01')
        self.assertNotIn('current_issue_only', feeds['essd.xml'])
        self.assertEqual(feeds['essd-current-issue.xml']['current_issue_only'], 'true')

    def test_crossref_cursor_query_avoids_unsupported_publication_sort(self):
        params = crossref_query_params(
            from_filter='from-pub-date',
            from_date='2026-06-01',
            until_filter='until-pub-date',
            until_date='2026-09-02',
            cursor='*',
            rows=1000,
            mailto='rss@example.com',
        )

        self.assertEqual(params['cursor'], '*')
        self.assertNotIn('sort', params)
        self.assertNotIn('order', params)

    @mock.patch('journal_rss_aggregator.fetch_bytes')
    def test_current_issue_stops_after_all_crossref_records_are_read(self, fetch_bytes):
        response = {
            'message': {
                'total-results': 1,
                'next-cursor': 'another-page',
                'items': [{
                    'DOI': '10.5194/essd-18-6225-2026',
                    'title': ['Test dataset'],
                    'URL': 'https://doi.org/10.5194/essd-18-6225-2026',
                    'volume': '18',
                    'issue': '9',
                    'published': {'date-parts': [[2026, 9, 1]]},
                }],
            },
        }
        fetch_bytes.side_effect = [json.dumps(response).encode(), AssertionError('extra page requested')]

        items = fetch_crossref_journal_items(
            {
                'source': 'Earth System Science Data Current Issue',
                'issn': '1866-3516',
                'homepage': 'https://essd.copernicus.org/articles/volumes.html',
                'from_date': '2025-01-01',
                'current_issue_only': 'true',
            },
            start_year=2020,
            end_year=2026,
            mailto='rss@example.com',
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(fetch_bytes.call_count, 1)


if __name__ == '__main__':
    unittest.main()
