import tempfile
import unittest
from pathlib import Path

from official_feed_proxy import build_crossref_rss, feed_entry_count, load_config, mirror_one
from zotero_switch_official_feeds import migration_script


RSS = b"<rss version='2.0'><channel><title>x</title><item><title>one</title></item></channel></rss>"
ATOM = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>x</title><entry><title>one</title></entry></feed>"


class OfficialFeedProxyTests(unittest.TestCase):
    def test_config_has_76_unique_official_mirrors(self):
        mirrors = load_config(Path("official-feed-config.json"))
        self.assertEqual(len(mirrors), 76)
        self.assertEqual(len({item["mirror_url"] for item in mirrors}), 76)

    def test_counts_rss_and_atom_entries(self):
        self.assertEqual(feed_entry_count(RSS), 1)
        self.assertEqual(feed_entry_count(ATOM), 1)

    def test_failed_refresh_preserves_existing_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "official-feeds/test.xml"
            output.parent.mkdir()
            output.write_bytes(RSS)
            result = mirror_one(
                {"name": "Test", "source_url": "https://example.invalid/rss", "output": "official-feeds/test.xml"},
                root,
                lambda _: (_ for _ in ()).throw(OSError("offline")),
            )
            self.assertEqual(result.entries, 1)
            self.assertTrue(result.status.startswith("preserved:"))
            self.assertEqual(output.read_bytes(), RSS)

    def test_crossref_fallback_uses_publisher_url_as_guid(self):
        raw = build_crossref_rss(
            {"name": "Remote Sensing", "source_url": "https://example.test/rss"},
            {"message": {"items": [{
                "title": ["Mapping paper"],
                "DOI": "10.3390/rs123",
                "URL": "https://doi.org/10.3390/rs123",
                "resource": {"primary": {"URL": "https://www.mdpi.com/2072-4292/18/1/23"}},
                "published": {"date-parts": [[2026, 9, 10]]},
                "author": [{"given": "A", "family": "Author"}],
            }] }},
        )
        self.assertEqual(feed_entry_count(raw), 1)
        self.assertIn(b"https://www.mdpi.com/2072-4292/18/1/23", raw)
        self.assertIn(b"doi:10.3390/rs123", raw)

    def test_zotero_migration_is_reversible(self):
        mirrors = [{
            "name": "Test",
            "source_url": "https://publisher.test/rss",
            "mirror_url": "https://mirror.test/rss.xml",
        }]
        forward = migration_script(mirrors)
        rollback = migration_script(mirrors, rollback=True)
        self.assertIn('"current_url": "https://publisher.test/rss"', forward)
        self.assertIn('"target_url": "https://publisher.test/rss"', rollback)
        self.assertIn('feed.cleanupReadAfter = 1', forward)
        self.assertIn('feed.cleanupUnreadAfter = 999', forward)


if __name__ == "__main__":
    unittest.main()
