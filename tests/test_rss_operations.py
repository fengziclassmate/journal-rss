import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from arxiv_email_daily import load, save, write_outputs
from rss_health import record, render, guard_sizes
from rss_ops import write_feed
from rss_read_filter import filter_feed, hash_tokens, identity_tokens, write_receipt
from zotero_cleanup import eligible, BASE, verify_published_feeds, main as cleanup_main
from rss_read_filter import key_id, suppression_digest
from journal_rss_aggregator import FeedItem, filter_official_duplicates
from zotero_read_sync import push_suppression
from types import SimpleNamespace


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_sharded_roundtrip_and_no_rewrite(self):
        state={'version':1,'processed_email_hashes':['one'], 'days':{
            '2026-09-28':{'papers':{},'messages':['one'],'coverage':'email'}}}
        path=self.root/'state.json'
        save(state,path)
        shard=self.root/'state/2026-09-28.json'
        before=shard.stat().st_mtime_ns
        self.assertEqual(load(path,self.root/'missing'),state)
        save(state,path)
        self.assertEqual(shard.stat().st_mtime_ns,before)
        state['days']['2026-09-29']={'papers':{},'messages':[],'coverage':'email'}
        save(state,path)
        self.assertEqual(shard.stat().st_mtime_ns,before)
        self.assertEqual(json.loads(path.read_text())['version'],2)

    def test_legacy_monolith_migrates_without_losing_papers(self):
        state={'version':1,'processed_email_hashes':[], 'days':{'2026-09-28':{'papers':{'id':{'title':'A'}},'coverage':'email'}}}
        path=self.root/'state.json';path.write_text(json.dumps(state))
        save(load(path,self.root/'missing'),path)
        self.assertEqual(load(path,self.root/'missing'),state)

    def test_unchanged_xml_preserves_build_time_and_mtime(self):
        path=self.root/'rss.xml'
        for timestamp in ['one','two']:
            root=ET.fromstring(f'<rss><channel><lastBuildDate>{timestamp}</lastBuildDate><item><title>A</title></item></channel></rss>')
            write_feed(path,root)
        self.assertEqual(ET.parse(path).findtext('./channel/lastBuildDate'),'one')

    def test_health_success_no_new_is_different_from_preserved(self):
        folder=self.root/'rss-health-state'
        record('source','ok',2,identities=['a','b'],root=folder)
        record('source','ok',2,identities=['a','b'],root=folder)
        path=next(folder.glob('*.json'));value=json.loads(path.read_text('utf-8'))
        self.assertEqual(value['new_count'],0)
        successful=value['last_success']
        record('source','preserved',2,root=folder)
        value=json.loads(path.read_text('utf-8'))
        self.assertIsNone(value['new_count'])
        self.assertEqual(value['last_success'],successful)
        render(self.root)
        self.assertIn('保留旧数据',(self.root/'rss-health/index.html').read_text('utf-8'))

    def test_drop_warning_is_collection_not_filtered_count(self):
        folder=self.root/'rss-health-state'
        record('source','ok',100,root=folder)
        record('source','ok',20,root=folder)
        self.assertTrue(json.loads(next(folder.glob('*.json')).read_text('utf-8'))['drop_warning'])

    def test_cleanup_gate_rejects_old_receipt_and_unmanaged_feeds(self):
        key=b'test';path=self.root/'read-suppression.json'
        path.write_text(json.dumps({'version':1,'algorithm':'hmac-sha256','key_id':key_id(key),
                                  'hashes':sorted(hash_tokens(identity_tokens(guid='old'),key))}))
        plan={'targets':[{'url':BASE+'feed.xml','guid':'old'},{'url':'https://publisher.test/rss','guid':'old'}]}
        with self.assertRaises(RuntimeError): eligible(plan,{},path,key)
        receipt={'suppression_sha256':suppression_digest(path),'feeds':{'feed.xml':'hash'}}
        accepted,skipped=eligible(plan,receipt,path,key)
        self.assertEqual((len(accepted),len(skipped)),(1,1))

    def test_empty_key_never_authorizes_deletion(self):
        path=self.root/'suppression.json';path.write_text('{}')
        for key in [None,b'',b' ']:
            with self.assertRaises(ValueError):eligible({'targets':[]},{},path,key)

    def test_suppression_authorization_reads_one_immutable_payload(self):
        key=b'k';path=self.root/'state.json'
        path.write_text(json.dumps({'version':1,'algorithm':'hmac-sha256','key_id':key_id(key),'hashes':[]}))
        value=path.read_text();receipt={'suppression_sha256':suppression_digest(path),'feeds':{}}
        with patch.object(Path,'read_text',side_effect=[value,AssertionError('second read')]) as read:
            eligible({'targets':[]},receipt,path,key)
        self.assertEqual(read.call_count,1)

    def test_stale_published_feed_blocks_cleanup(self):
        targets=[{'url':BASE+'feed.xml'}]
        receipt={'feeds':{'feed.xml':hashlib.sha256(b'current').hexdigest()}}
        verify_published_feeds(targets,receipt,lambda _:b'current')
        with self.assertRaises(RuntimeError):verify_published_feeds(targets,receipt,lambda _:b'stale')

    def test_cleanup_cannot_back_up_an_unrelated_database(self):
        with patch('sys.argv',['cleanup','--apply','--database',str(self.root/'old-copy.sqlite')]), \
             patch('zotero_cleanup.fetch_receipt') as fetch:
            with self.assertRaises(ValueError):cleanup_main()
        fetch.assert_not_called()

    def test_receipt_covers_exact_published_files(self):
        (self.root/'read-suppression.json').write_text('{}')
        (self.root/'feed.xml').write_text('<rss><channel/></rss>')
        write_receipt(self.root)
        receipt=json.loads((self.root/'read-filter-status.json').read_text())
        self.assertIn('feed.xml',receipt['feeds'])

    def test_cleanup_skips_items_read_after_last_export(self):
        key=b'test';path=self.root/'read-suppression.json'
        path.write_text(json.dumps({'version':1,'algorithm':'hmac-sha256','key_id':key_id(key),
                                  'hashes':sorted(hash_tokens(identity_tokens(guid='old'),key))}))
        plan={'targets':[{'url':BASE+'feed.xml','guid':'old'},{'url':BASE+'feed.xml','guid':'new'}]}
        receipt={'suppression_sha256':suppression_digest(path),'feeds':{'feed.xml':'hash'}}
        accepted,skipped=eligible(plan,receipt,path,key)
        self.assertEqual([row['guid'] for row in accepted],['old'])
        self.assertEqual([row['guid'] for row in skipped],['new'])

    def test_receipt_digest_is_cross_platform(self):
        path=self.root/'suppression.json'
        path.write_bytes(b'{\r\n  "b": 2,\r\n  "a": 1\r\n}')
        first=suppression_digest(path)
        path.write_bytes(b'{"a":1,"b":2}\n')
        self.assertEqual(first,suppression_digest(path))

    def test_article_reference_does_not_override_primary_doi(self):
        path=self.root/'rss.xml'
        path.write_text('<rss><channel><item><guid>10.1234/new</guid><description>Compare arxiv.org/abs/2609.00001 and DOI 10.1234/old</description></item></channel></rss>')
        suppressed=hash_tokens(identity_tokens(doi='10.1234/old',link='https://arxiv.org/abs/2609.00001'),b'key')
        self.assertEqual(filter_feed(path,suppressed,b'key').removed,0)

    def test_same_title_alone_never_hides_unread_article(self):
        path=self.root/'rss.xml';path.write_text('<rss><channel><item><title>Shared title</title><guid>new</guid></item></channel></rss>')
        suppressed=hash_tokens(identity_tokens(title='Shared title'),b'key')
        self.assertEqual(filter_feed(path,suppressed,b'key').removed,0)

    def test_official_duplicate_audit_explains_strong_and_title_only_matches(self):
        audit=[]
        papers=[FeedItem(source='A',title='Test',guid='10.1234/a',link='https://doi.org/10.1234/a'),
                FeedItem(source='A',title='Same title',guid='b',link='https://example.test/b')]
        kept=filter_official_duplicates(papers,{'doi:10.1234/a','title:sametitle'},audit,'https://publisher.test/rss')
        self.assertEqual(len(kept),1)
        self.assertEqual([row['action'] for row in audit],['hidden','review-title-only'])

    def test_digest_reading_does_not_suppress_its_papers(self):
        # Exporter uses metadata only, never the list of papers inside a daily description.
        tokens=identity_tokens(guid='arxiv-email-daily:2026-09-29',title='Daily')
        self.assertFalse(any(t.startswith('arxiv:') for t in tokens))

    def test_workflow_fast_path_and_no_paid_credentials(self):
        value=Path('.github/workflows/update-feed.yml').read_text('utf-8')
        self.assertIn("set(changed) <= {'read-suppression.json'}",value)
        self.assertNotIn('DEEPSEEK_API_KEY',value)
        self.assertIn('write_receipt()',value)

    def test_retry_pushes_pending_read_history_without_new_reads(self):
        with patch('zotero_read_sync._git',side_effect=[SimpleNamespace(stdout='1'),
                SimpleNamespace(stdout='read-suppression.json\n'),SimpleNamespace(stdout='')]) as git:
            result=push_suppression(self.root,self.root/'read-suppression.json',0)
        self.assertEqual(result,'pushed-pending')
        self.assertEqual(git.call_args.args[1:],('push','origin','HEAD:main'))

    def test_read_sync_does_not_publish_unrelated_pending_commits(self):
        with patch('zotero_read_sync._git',side_effect=[SimpleNamespace(stdout='1'),
                SimpleNamespace(stdout='unrelated.py\n')]) as git:
            with self.assertRaises(RuntimeError):
                push_suppression(self.root,self.root/'read-suppression.json',0)
        self.assertEqual(git.call_count,2)


if __name__=='__main__':unittest.main()
