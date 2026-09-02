import datetime as dt
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from research_rss import (
    EmailDelivery,
    Paper,
    PaperStore,
    analyze_with_deepseek,
    build_daily_record,
    crossref_date_filter,
    imap_received_datetime,
    keyword_score,
    parse_arxiv_email,
    run_email_only,
    prune_unselected,
    run,
    select_papers,
    write_daily_archive,
    write_daily_feed,
    write_paper_feed,
)


UTC = dt.timezone.utc


class ArxivEmailTests(unittest.TestCase):
    def test_imap_internal_date_controls_mailbox_day(self):
        received = imap_received_datetime(
            b'1 (RFC822 {42} INTERNALDATE "02-Sep-2026 00:15:00 +0800")'
        )

        self.assertEqual(received.isoformat(), "2026-09-02T00:15:00+08:00")

    def test_folded_headers_and_versions_are_preserved(self):
        body = """\\\\
arXiv:2608.12345v2
Date: Mon, 31 Aug 2026 00:00:00 GMT

Title: A folded paper title for
  urban remote sensing
Authors: Alice Example,
  Bob Example
Categories: cs.CV cs.AI
\\\\
This is the abstract text.
\\\\
"""

        papers = parse_arxiv_email(body)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].paper_id, "2608.12345v2")
        self.assertEqual(papers[0].title, "A folded paper title for urban remote sensing")
        self.assertEqual(papers[0].authors, ["Alice Example", "Bob Example"])
        self.assertEqual(papers[0].abstract, "This is the abstract text.")
        self.assertIn("arxiv:2608.12345", papers[0].aliases())


class IdentityAndScoringTests(unittest.TestCase):
    def test_crossref_daily_collection_uses_new_record_date(self):
        now = dt.datetime(2026, 9, 1, 8, tzinfo=UTC)
        self.assertEqual(
            crossref_date_filter(now, {"created_lookback_days": 7}),
            "from-created-date:2026-08-25,until-created-date:2026-09-01,type:journal-article",
        )

    def test_arxiv_record_keeps_identity_when_doi_is_added(self):
        store = PaperStore.empty()
        original = Paper(
            source="arxiv-email",
            paper_id="2608.12345v1",
            title="A paper",
            url="https://arxiv.org/abs/2608.12345v1",
        )
        enriched = Paper(
            source="arxiv-api",
            paper_id="2608.12345v2",
            title="A paper, revised",
            url="https://arxiv.org/abs/2608.12345v2",
            doi="10.1234/example",
        )

        key = store.upsert(original, now="2026-08-31T00:00:00+00:00")
        enriched_key = store.upsert(enriched, now="2026-09-01T00:00:00+00:00")

        self.assertEqual(enriched_key, key)
        self.assertEqual(len(store.data["papers"]), 1)
        self.assertIn("doi:10.1234/example", store.data["papers"][key]["aliases"])
        self.assertEqual(store.data["papers"][key]["arxiv_version"], 2)

    def test_acronyms_match_whole_tokens(self):
        config = {"topics": {"primary": ["GIS"], "methods": []}}
        false_positive = Paper("arxiv-api", "1", "Logistic regression", "https://example.test/1")
        true_positive = Paper("arxiv-api", "2", "A GIS workflow", "https://example.test/2")

        self.assertEqual(keyword_score(false_positive, config)[0], 5)
        self.assertGreater(keyword_score(true_positive, config)[0], 5)

    def test_same_day_rerun_fills_remaining_digest_slots(self):
        store = PaperStore.empty()
        first = store.upsert(Paper("arxiv-api", "2608.1", "GIS paper", "https://example.test/1"))
        second = store.upsert(Paper("arxiv-api", "2608.2", "GIS update", "https://example.test/2"))
        store.data["papers"][first]["digest_dates"] = ["2026-09-01"]
        store.data["digests"]["2026-09-01"] = {"paper_keys": [first]}
        config = {
            "topics": {"primary": ["GIS"], "methods": []},
            "selection": {"min_score": 0, "max_papers_per_day": 2},
        }

        selected = select_papers(store, config, "2026-09-01")

        self.assertEqual(selected, [first, second])

    def test_cached_llm_score_is_not_replaced_by_keyword_score(self):
        store = PaperStore.empty()
        key = store.upsert(Paper("arxiv-api", "2608.3", "Unmatched title", "https://example.test/3"))
        config = {
            "research_profile": "GIS research",
            "topics": {"primary": ["GIS"], "methods": []},
            "selection": {"min_score": 0, "max_papers_per_day": 1},
            "llm": {"model": "deepseek-chat", "candidate_limit": 30},
        }
        record = store.data["papers"][key]
        record["score"] = 91
        record["reason"] = "LLM cached reason"
        from research_rss import _analysis_hash

        record["analysis_hash"] = _analysis_hash(record, config)

        selected = select_papers(store, config, "2026-09-01")

        self.assertEqual(selected, [key])
        self.assertEqual(record["score"], 91)
        self.assertEqual(record["reason"], "LLM cached reason")

    def test_unselected_state_is_capped_without_removing_digest_papers(self):
        store = PaperStore.empty()
        selected = store.upsert(Paper("source", "selected", "Selected", "https://example.test/selected"), now="2026-08-01T00:00:00+00:00")
        store.data["papers"][selected]["digest_dates"] = ["2026-08-01"]
        for index in range(4):
            store.upsert(
                Paper("source", str(index), f"Paper {index}", f"https://example.test/{index}"),
                now=f"2026-08-0{index + 1}T00:00:00+00:00",
            )

        removed = prune_unselected(store, {"state": {"max_unselected_papers": 2}}, dt.datetime(2026, 9, 1, tzinfo=UTC))

        self.assertEqual(removed, 2)
        self.assertIn(selected, store.data["papers"])
        self.assertEqual(len(store.data["papers"]), 3)

    @mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @mock.patch("research_rss._request")
    def test_deepseek_analysis_is_batched(self, request_mock):
        store = PaperStore.empty()
        keys = [
            store.upsert(Paper("source", str(index), f"Paper {index}", f"https://example.test/{index}"))
            for index in range(21)
        ]
        batch_sizes = []

        def response(_url, *, data, **_kwargs):
            request = json.loads(data)
            prompt = json.loads(request["messages"][1]["content"])
            batch_sizes.append(len(prompt["papers"]))
            results = [
                {
                    "id": paper["id"],
                    "relevance_score": 80,
                    "tags": ["GIS"],
                    "reason_zh": "相关",
                    "title_zh": f"译文 {paper['id']}",
                    "summary_zh": "摘要",
                    "insight_zh": "启发",
                }
                for paper in prompt["papers"]
            ]
            return json.dumps({"choices": [{"message": {"content": json.dumps(results, ensure_ascii=False)}}]}).encode()

        request_mock.side_effect = response
        config = {"research_profile": "GIS", "llm": {"batch_size": 10, "max_tokens": 4096}}

        status = analyze_with_deepseek(store, keys, config)

        self.assertEqual(status, "ok:21")
        self.assertEqual(batch_sizes, [10, 10, 1])
        self.assertTrue(all(store.data["papers"][key]["title_zh"] for key in keys))


class FeedOutputTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
    @mock.patch("research_rss.analyze_with_deepseek")
    @mock.patch("research_rss.fetch_arxiv_email_deliveries")
    def test_email_only_run_uses_mailbox_papers_and_separate_guids(
        self, fetch_mock, analyze_mock
    ):
        first = Paper(
            source="arxiv-email",
            paper_id="2609.00001",
            title="GIS from email",
            url="https://arxiv.org/abs/2609.00001",
        )
        second = Paper(
            source="arxiv-email",
            paper_id="2609.00002",
            title="Remote sensing from email",
            url="https://arxiv.org/abs/2609.00002",
        )
        fetch_mock.return_value = (
            [
                EmailDelivery("2026-09-02", "hash-1", [first]),
                EmailDelivery("2026-09-02", "hash-2", [second]),
            ],
            "ok:2",
        )

        def analyze(store, keys, _config):
            for key in keys:
                record = store.data["papers"][key]
                record["score"] = 90
                record["title_zh"] = f"译文 {record['title']}"
                record["analysis_hash"] = "cached-for-test"
            return "ok:2"

        analyze_mock.side_effect = analyze

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            state = root / "email-state.json"
            output = root / "output"
            config.write_text(
                json.dumps(
                    {
                        "base_url": "https://example.test",
                        "research_profile": "GIS",
                        "topics": {"primary": ["GIS", "Remote Sensing"], "methods": []},
                        "selection": {"min_score": 45, "max_papers_per_day": 10},
                        "llm": {"candidate_limit": 30},
                        "sources": {"email": {"enabled": True}},
                    }
                ),
                encoding="utf-8",
            )

            run_email_only(
                config,
                state,
                output,
                now=dt.datetime(2026, 9, 2, 12, tzinfo=UTC),
            )

            paper_root = ET.parse(output / "arxiv-email-papers.xml")
            daily_root = ET.parse(output / "arxiv-email-daily.xml")
            titles = [item.findtext("title") for item in paper_root.findall("./channel/item")]
            guids = [item.findtext("guid") for item in paper_root.findall("./channel/item")]
            daily_description = daily_root.findtext("./channel/item/description") or ""

            self.assertCountEqual(titles, [first.title, second.title])
            self.assertTrue(all(guid.startswith("arxiv-email:") for guid in guids))
            self.assertEqual(
                daily_root.findtext("./channel/item/guid"),
                "arxiv-email-daily:2026-09-02",
            )
            self.assertIn("arxiv-email: ok:2", daily_description)
            self.assertNotIn("crossref", daily_description.lower())
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertCountEqual(saved["processed_email_hashes"], ["hash-1", "hash-2"])

    def test_daily_record_is_idempotent_and_feeds_have_stable_guids(self):
        store = PaperStore.empty()
        paper = Paper(
            source="arxiv-api",
            paper_id="2608.12345v1",
            title="An English title",
            url="https://arxiv.org/abs/2608.12345v1",
            abstract="An abstract.",
            authors=["Alice Example"],
            published=dt.datetime(2026, 8, 31, tzinfo=UTC),
        )
        key = store.upsert(paper, now="2026-09-01T00:00:00+00:00")
        store.data["papers"][key]["score"] = 88
        store.data["papers"][key]["reason"] = "相关"
        store.data["papers"][key]["summary_zh"] = "中文摘要"
        digest = build_daily_record(store, "2026-09-01", [key], {"arxiv-api": "ok"})
        store.data["digests"]["2026-09-01"] = digest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper_feed = root / "research-papers.xml"
            daily_feed = root / "research-daily.xml"
            write_paper_feed(store, paper_feed, "https://example.test/research-papers.xml")
            write_daily_feed(store, daily_feed, "https://example.test/research-daily.xml")
            write_daily_archive(store, root / "research-archive", "https://example.test")

            paper_root = ET.parse(paper_feed)
            daily_root = ET.parse(daily_feed)
            self.assertEqual(paper_root.findtext("./channel/language"), "en")
            self.assertEqual(daily_root.findtext("./channel/language"), "zh-CN")
            self.assertEqual(paper_root.findtext("./channel/item/guid"), "research:arxiv:2608.12345")
            self.assertEqual(
                paper_root.findtext("./channel/item/{http://purl.org/dc/elements/1.1/}creator"),
                "Alice Example",
            )
            self.assertEqual(
                paper_root.findtext("./channel/item/pubDate"),
                "Tue, 01 Sep 2026 00:00:00 +0800",
            )
            self.assertEqual(daily_root.findtext("./channel/item/guid"), "research-daily:2026-09-01")
            archive = json.loads((root / "research-archive/2026-09-01.json").read_text(encoding="utf-8"))
            self.assertEqual(archive["paper_count"], 1)
            self.assertNotIn("message_id", json.dumps(archive))

    def test_offline_rebuild_does_not_modify_state(self):
        store = PaperStore.empty()
        key = store.upsert(Paper("arxiv-api", "2608.9", "GIS paper", "https://example.test/9"))
        store.data["papers"][key]["digest_dates"] = ["2026-09-01"]
        store.data["digests"]["2026-09-01"] = build_daily_record(store, "2026-09-01", [key], {"arxiv-api": "ok:1"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            config = root / "config.json"
            store.save(state)
            config.write_text(json.dumps({"base_url": "https://example.test"}), encoding="utf-8")
            before = state.read_bytes()

            run(config, state, root / "output", offline=True)

            self.assertEqual(state.read_bytes(), before)
            self.assertTrue((root / "output/research-papers.xml").exists())


if __name__ == "__main__":
    unittest.main()
