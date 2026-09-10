import json
import sqlite3
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from rss_read_filter import filter_feed, hash_tokens, identity_tokens
from zotero_read_sync import export_read_state


RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
  <title>Test feed</title>
  <item><title>Read paper</title><link>https://doi.org/10.1234/ABC</link><guid isPermaLink="false">scope:read</guid></item>
  <item><title>Unread paper</title><link>https://example.test/unread</link><guid isPermaLink="false">scope:unread</guid></item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Official Atom</title>
  <entry><title>Read paper</title><id>atom:read</id><link href="https://doi.org/10.1234/ABC" /></entry>
  <entry><title>Unread paper</title><id>atom:unread</id><link href="https://example.test/unread" /></entry>
</feed>
"""

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">
  <channel rdf:about="https://example.test"><title>Official RDF</title></channel>
  <item rdf:about="https://doi.org/10.1234/ABC"><title>Read paper</title><link>https://doi.org/10.1234/ABC</link></item>
  <item rdf:about="https://example.test/unread"><title>Unread paper</title><link>https://example.test/unread</link></item>
</rdf:RDF>
"""


class IdentityTests(unittest.TestCase):
    def test_doi_urls_and_plain_dois_share_an_identity(self):
        from_url = identity_tokens(link="https://doi.org/10.1234/ABC?utm_source=rss")
        from_field = identity_tokens(doi="doi:10.1234/abc")

        self.assertIn("doi:10.1234/abc", from_url)
        self.assertIn("doi:10.1234/abc", from_field)

    def test_title_identity_matches_across_feed_guid_scopes(self):
        individual = identity_tokens(guid="conference:cvpr:dblp:one", title="A Mapping Paper")
        digest = identity_tokens(guid="conference:digest:dblp:one", title="A  Mapping  Paper")

        self.assertIn("title:a mapping paper", individual & digest)


class FeedFilterTests(unittest.TestCase):
    def test_filter_removes_suppressed_item_and_keeps_other_items(self):
        key = b"test-key"
        suppressed = hash_tokens(identity_tokens(doi="10.1234/abc"), key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_text(RSS, encoding="utf-8")

            result = filter_feed(path, suppressed, key)

            titles = [node.text for node in ET.parse(path).findall("./channel/item/title")]
            self.assertEqual(result.removed, 1)
            self.assertEqual(result.kept, 1)
            self.assertEqual(titles, ["Unread paper"])

    def test_filter_is_idempotent(self):
        key = b"test-key"
        suppressed = hash_tokens(identity_tokens(guid="scope:read"), key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_text(RSS, encoding="utf-8")

            first = filter_feed(path, suppressed, key)
            second = filter_feed(path, suppressed, key)

            self.assertEqual((first.removed, second.removed), (1, 0))

    def test_filter_supports_atom(self):
        key = b"test-key"
        suppressed = hash_tokens(identity_tokens(doi="10.1234/abc"), key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.atom"
            path.write_text(ATOM, encoding="utf-8")
            result = filter_feed(path, suppressed, key)
            self.assertEqual((result.removed, result.kept), (1, 1))

    def test_filter_supports_rdf_rss(self):
        key = b"test-key"
        suppressed = hash_tokens(identity_tokens(doi="10.1234/abc"), key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.rdf"
            path.write_text(RDF, encoding="utf-8")
            result = filter_feed(path, suppressed, key)
            self.assertEqual((result.removed, result.kept), (1, 1))

    def test_digest_is_not_removed_when_only_a_paper_in_its_description_was_read(self):
        key = b"test-key"
        suppressed = hash_tokens(identity_tokens(doi="10.1234/read-paper"), key)
        digest = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><item>
  <title>QQ mailbox arXiv daily | 2026-09-10 | 30 papers</title>
  <link>https://example.test/archive/2026-09-10.html</link>
  <guid isPermaLink="false">arxiv-email-daily:2026-09-10</guid>
  <description>Includes DOI 10.1234/read-paper among 30 papers</description>
</item></channel></rss>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.xml"
            path.write_text(digest, encoding="utf-8")

            result = filter_feed(path, suppressed, key)

            self.assertEqual((result.removed, result.kept), (0, 1))
            self.assertEqual(
                ET.parse(path).findtext("./channel/item/guid"),
                "arxiv-email-daily:2026-09-10",
            )


class WorkflowTests(unittest.TestCase):
    def test_filter_runs_after_generation_and_before_publish(self):
        workflow = Path(".github/workflows/update-feed.yml").read_text(encoding="utf-8")

        generated = workflow.index("- name: Generate conference feeds")
        restored = workflow.index("- name: Restore conference fallback")
        filtered = workflow.index("- name: Remove previously read Zotero items")
        committed = workflow.index("- name: Commit feed changes")
        published = workflow.index("- name: Prepare Pages artifact")
        self.assertLess(restored, generated)
        self.assertLess(generated, filtered)
        self.assertLess(filtered, committed)
        self.assertLess(committed, published)
        self.assertIn("secrets.RSS_READ_FILTER_KEY", workflow)
        self.assertIn("Generate official feed mirrors", workflow)
        self.assertIn('--glob "official-feeds/*.xml"', workflow)

    def test_official_snapshot_is_generated_before_custom_journal_feeds(self):
        workflow = Path(".github/workflows/update-feed.yml").read_text(encoding="utf-8")

        official_snapshot = workflow.index("- name: Generate official feed mirrors")
        custom_journals = workflow.index("- name: Generate feed")
        self.assertLess(official_snapshot, custom_journals)


class ZoteroExportTests(unittest.TestCase):
    def test_export_is_append_only_and_uses_read_feed_items(self):
        key = b"test-key"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "zotero.sqlite"
            suppression = root / "read-suppression.json"
            con = sqlite3.connect(database)
            con.executescript(
                """
                CREATE TABLE feedItems (itemID INTEGER PRIMARY KEY, guid TEXT, readTime TEXT, translatedTime TEXT);
                CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
                CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
                CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
                INSERT INTO fields VALUES (1, 'title'), (13, 'url'), (59, 'DOI');
                INSERT INTO feedItems VALUES (1, 'scope:read', '2026-09-10 00:00:00', NULL);
                INSERT INTO feedItems VALUES (2, 'scope:unread', NULL, NULL);
                INSERT INTO itemDataValues VALUES (1, 'Read paper'), (2, '10.1234/ABC'), (3, 'Unread paper');
                INSERT INTO itemData VALUES (1, 1, 1), (1, 59, 2), (2, 1, 3);
                """
            )
            con.commit()
            con.close()
            old_hash = "f" * 64
            suppression.write_text(
                json.dumps({
                    "version": 1,
                    "algorithm": "hmac-sha256",
                    "key_id": __import__("hashlib").sha256(key).hexdigest()[:16],
                    "hashes": [old_hash],
                }),
                encoding="utf-8",
            )

            result = export_read_state(database, suppression, key)
            payload = json.loads(suppression.read_text(encoding="utf-8"))

            self.assertEqual(result.read_items, 1)
            self.assertGreater(result.new_hashes, 0)
            self.assertIn(old_hash, payload["hashes"])
            self.assertTrue(hash_tokens({"guid:scope:read"}, key) <= set(payload["hashes"]))
            self.assertFalse(hash_tokens({"guid:scope:unread"}, key) <= set(payload["hashes"]))

            first_content = suppression.read_bytes()
            first_mtime = suppression.stat().st_mtime_ns
            time.sleep(0.02)
            second = export_read_state(database, suppression, key)
            self.assertEqual(second.new_hashes, 0)
            self.assertEqual(suppression.read_bytes(), first_content)
            self.assertEqual(suppression.stat().st_mtime_ns, first_mtime)


if __name__ == "__main__":
    unittest.main()
