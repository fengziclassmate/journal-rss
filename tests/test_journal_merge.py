import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from journal_merge import merge_all, merge_items
from journal_rss_aggregator import FeedItem, UTC, read_existing_feed_items, write_rss


class JournalMergeTests(unittest.TestCase):
    def test_merge_precedes_both_filters(self):
        workflow=Path('.github/workflows/update-feed.yml').read_text('utf-8')
        self.assertLess(workflow.index('run: python journal_merge.py'),workflow.index('run: python official_priority.py'))
        self.assertLess(workflow.index('run: python official_priority.py'),workflow.index('- name: Remove previously read Zotero items'))

    def item(self, guid, link, day=1):
        return FeedItem(source='Journal',title='Paper',guid=guid,link=link,published=dt.datetime(2026,9,day,tzinfo=UTC))

    def test_doi_dedup_preserves_main_guid(self):
        main=self.item('doi:10.1234/abc','https://doi.org/10.1234/abc')
        issue=self.item('https://doi.org/10.1234/ABC','https://publisher.test/article',2)
        self.assertEqual(merge_items([main],[issue]),[main])

    def test_distinct_papers_with_same_title_survive_sorted(self):
        first=self.item('a','https://test/a')
        second=self.item('b','https://test/b',2)
        self.assertEqual(merge_items([first],[second]),[second,first])

    def test_repeat_merge_is_idempotent_and_retains_old_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            main={'output':'main.xml','feed_title':'Journal Early Access RSS','feed_link':'https://test/main.xml'}
            current={'output':'current.xml'}
            def write(items,name):
                write_rss(items,root/name,feed_title='Journal',feed_link='https://test/'+name,feed_description='',max_items=100,prefix_item_titles=False)
            write([self.item('a','https://test/a')],'main.xml')
            write([self.item('b','https://test/b',2)],'current.xml')
            with patch('journal_merge.journal_pairs',return_value=[(main,current)]):
                self.assertEqual(merge_all(root)[0]['added'],1)
                self.assertEqual(merge_all(root)[0]['added'],0)
                write([],'current.xml')
                self.assertEqual(merge_all(root)[0]['total'],2)
            self.assertEqual([i.guid for i in read_existing_feed_items(root/'main.xml')],['b','a'])
