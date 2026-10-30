import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import push_feed_updates as publisher
from rss_read_filter import hash_tokens, identity_tokens, key_id


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'origin.git'
        subprocess.run(['git', 'init', '--bare', str(self.remote)], check=True, capture_output=True)
        self.writer = self.clone('writer')
        publisher.git(self.writer, 'checkout', '-b', 'main')
        self.commit(self.writer, 'feed.xml', 'initial')
        publisher.git(self.writer, 'push', '-u', 'origin', 'main')
        publisher.git(self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.runner = self.clone('runner')

    def clone(self, name):
        path = self.root / name
        subprocess.run(['git', 'clone', str(self.remote), str(path)], check=True, capture_output=True)
        publisher.git(path, 'config', 'user.name', 'Test')
        publisher.git(path, 'config', 'user.email', 'test@example.test')
        return path

    def commit(self, repo, path, value):
        (repo / path).write_text(value, 'utf-8')
        publisher.git(repo, 'add', '--', path)
        publisher.git(repo, 'commit', '-m', value)

    def test_non_fast_forward_preserves_both_updates(self):
        self.commit(self.runner, 'feed.xml', 'generated RSS')
        self.commit(self.writer, 'library.enc', 'new encrypted library')
        publisher.git(self.writer, 'push')
        self.assertNotEqual(publisher.git(self.runner, 'push', 'origin', 'HEAD:main', check=False).returncode, 0)
        publisher.push_updates(self.runner)
        self.assertEqual(publisher.git(self.remote, 'show', 'main:feed.xml').stdout, 'generated RSS')
        self.assertEqual(publisher.git(self.remote, 'show', 'main:library.enc').stdout, 'new encrypted library')

    def test_conflict_never_overwrites_remote(self):
        self.commit(self.runner, 'feed.xml', 'runner content')
        self.commit(self.writer, 'feed.xml', 'remote content')
        publisher.git(self.writer, 'push')
        with self.assertRaisesRegex(RuntimeError, 'conflict'):
            publisher.push_updates(self.runner)
        self.assertEqual(publisher.git(self.remote, 'show', 'main:feed.xml').stdout, 'remote content')
        self.assertEqual((self.runner / 'feed.xml').read_text(), 'runner content')

    def test_push_race_is_retried(self):
        self.commit(self.runner, 'feed.xml', 'new feed')
        original = publisher.git
        raced = False
        def race(repo, *args, **kwargs):
            nonlocal raced
            if args[:1] == ('push',) and not raced:
                raced = True
                self.commit(self.writer, 'settings.json', 'concurrent settings')
                original(self.writer, 'push')
            return original(repo, *args, **kwargs)
        with patch.object(publisher, 'git', side_effect=race), patch.object(publisher.time, 'sleep'):
            publisher.push_updates(self.runner)
        self.assertTrue(raced)
        self.assertEqual(original(self.remote, 'show', 'main:settings.json').stdout, 'concurrent settings')

    def test_generated_conflicts_keep_new_papers_and_latest_read_filter(self):
        full='<rss version="2.0"><channel><title>Test</title><item><guid>read-paper</guid><title>Read paper</title></item><item><guid>old-paper</guid><title>Old paper</title></item><item><guid>new-paper</guid><title>New paper</title></item></channel></rss>'
        filtered='<rss version="2.0"><channel><title>Test</title><item><guid>old-paper</guid><title>Old paper</title></item></channel></rss>'
        key=b'test-only-key'
        suppression=json.dumps({'version':1,'algorithm':'hmac-sha256','key_id':key_id(key),'hashes':sorted(hash_tokens(identity_tokens(guid='read-paper'),key))})
        for folder in ['official-feeds','journal-feeds']:
            (self.runner/folder).mkdir()
            self.commit(self.runner,folder+'/sample.xml','<rss version="2.0"><channel><title>Test</title></channel></rss>')
        self.commit(self.runner,'feed.xml',full)
        for repo in [self.writer,self.runner]:(repo/'rss-health').mkdir()
        self.commit(self.runner,'rss-health/index.html','fresh collector health')
        self.commit(self.writer,'feed.xml',filtered)
        self.commit(self.writer,'rss-health/index.html','earlier read-filter health')
        self.commit(self.writer,'read-suppression.json',suppression)
        self.commit(self.writer,'settings.json','latest settings')
        publisher.git(self.writer,'push')
        source=Path(__file__).resolve().parents[1]
        for name in ['rss_read_filter.py','rss_ops.py']:shutil.copyfile(source/name,self.runner/name)
        with patch.dict('os.environ',{'RSS_READ_FILTER_KEY':key.decode()}):
            publisher.push_updates(self.runner,refresh_read_filter=True)
        xml=ET.fromstring(publisher.git(self.remote,'show','main:feed.xml').stdout)
        self.assertEqual({n.findtext('guid') for n in xml.findall('./channel/item')},{'old-paper','new-paper'})
        self.assertEqual(publisher.git(self.remote,'show','main:read-suppression.json').stdout,suppression)
        self.assertEqual(publisher.git(self.remote,'show','main:settings.json').stdout,'latest settings')
        self.assertEqual(publisher.git(self.remote,'show','main:rss-health/index.html').stdout,'fresh collector health')

    def test_code_conflict_still_stops_read_filter_publication(self):
        self.commit(self.runner,'collector.py','generated-side code')
        self.commit(self.writer,'collector.py','remote-side code')
        publisher.git(self.writer,'push')
        with self.assertRaisesRegex(RuntimeError,'conflict'):
            publisher.push_updates(self.runner,refresh_read_filter=True)
        self.assertEqual(publisher.git(self.remote,'show','main:collector.py').stdout,'remote-side code')
        self.assertEqual((self.runner/'collector.py').read_text(),'generated-side code')

    def test_read_history_conflict_is_never_auto_resolved(self):
        self.commit(self.runner,'read-suppression.json','local reading hashes')
        self.commit(self.writer,'read-suppression.json','newer remote reading hashes')
        publisher.git(self.writer,'push')
        with self.assertRaisesRegex(RuntimeError,'conflict'):
            publisher.push_updates(self.runner,refresh_read_filter=True)
        self.assertEqual(publisher.git(self.remote,'show','main:read-suppression.json').stdout,'newer remote reading hashes')


if __name__ == '__main__':
    unittest.main()
