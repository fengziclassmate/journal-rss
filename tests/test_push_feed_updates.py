import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import push_feed_updates as publisher


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


if __name__ == '__main__':
    unittest.main()
