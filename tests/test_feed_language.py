import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from journal_rss_aggregator import CROSSREF_JOURNALS, FeedItem, journal_item_limit, write_rss


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


if __name__ == '__main__':
    unittest.main()
