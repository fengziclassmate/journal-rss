import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from journal_rss_aggregator import FeedItem, write_rss


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


if __name__ == '__main__':
    unittest.main()
