import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from conference_rss import (
    ConferencePaper,
    _crossref_date,
    _is_main_paper,
    _parse_dblp_toc,
    deduplicate,
    fetch_virtual_json,
    fetch_crossref_fallback,
    match_keywords,
    parse_copernicus_volume,
    parse_ieee_vis_page,
    parse_robotics_proceedings,
    write_opml,
    write_rss,
    write_rss_preserving_existing,
)


class ConferenceConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path("conference-config.json").read_text(encoding="utf-8"))

    def test_exactly_35_unique_conferences_are_configured(self):
        conferences = self.config["conferences"]
        self.assertEqual(len(conferences), 35)
        self.assertEqual([item["id"] for item in conferences], list(range(1, 36)))
        self.assertEqual(len({item["slug"] for item in conferences}), 35)

    def test_opml_contains_all_feeds_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feeds.opml"
            write_opml(self.config, output)
            outlines = ET.parse(output).findall(".//outline[@type='rss']")
            self.assertEqual(len(outlines), 36)
            self.assertTrue(any(node.get("xmlUrl", "").endswith("top-conference-daily.xml") for node in outlines))

    def test_fallback_archive_contains_every_nonempty_conference_feed(self):
        with zipfile.ZipFile("conference-feed-fallback.zip") as archive:
            for conference in self.config["conferences"]:
                path = f"conference-feeds/{conference['slug']}.xml"
                root = ET.fromstring(archive.read(path))
                self.assertGreater(len(root.findall("./channel/item")), 0, path)
            self.assertIn("conference-feeds/top-conference-daily.xml", archive.namelist())


class ConferenceFeedTests(unittest.TestCase):
    def test_crossref_fallback_enumerates_exact_containers_not_the_entire_fuzzy_search(self):
        import urllib.parse
        def work(n):
            return {'type':'proceedings-article','title':[f'Paper {n}'],'DOI':f'10.1234/{n}','URL':f'https://doi.org/10.1234/{n}','container-title':['Test Conference'],'published':{'date-parts':[[2026,6,2]]}}
        class Client:
            failures=0
            errors=[]
            def __init__(self): self.urls=[]
            def get(self,url):
                self.urls.append(url)
                q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                if 'query.container-title' in q:
                    book=dict(work(9999),type='proceedings')
                    data={'items':[work(0),book],'total-results':500000}
                elif q['cursor']==['*']:
                    data={'items':[work(n) for n in range(1000)],'total-results':1001,'next-cursor':'second'}
                else:
                    data={'items':[work(1000)],'total-results':1001,'next-cursor':'end'}
                return json.dumps({'message':data}).encode()
        client=Client()
        papers=fetch_crossref_fallback(client,{'acronym':'Test','name':'Test Conference','crossref_titles':['Test Conference']},2026)
        self.assertEqual(len(papers),1001)
        self.assertEqual(len(client.urls),3)
        for url in client.urls[1:]:
            self.assertIn('container-title:Test Conference',urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['filter'][0])

    def test_failure_in_one_year_does_not_discard_successful_other_year(self):
        from unittest.mock import patch
        from conference_rss import collect_conference, PoliteClient
        from source_response import SourceResponseError
        paper=ConferencePaper('C','C','Paper','https://test/paper','paper',2025)
        def source(client,conference,year):
            if year==2026: raise SourceResponseError('Blocked')
            return [paper]
        with patch('conference_rss.fetch_virtual_json',side_effect=source), \
             patch('conference_rss.fetch_cvf_event',return_value=[]), \
             patch('conference_rss.fetch_eccv_event',return_value=[]), \
             patch('conference_rss.fetch_copernicus_volumes',return_value=[]), \
             patch('conference_rss.fetch_robotics_proceedings',return_value=[]), \
             patch('conference_rss.fetch_ieee_vis_page',return_value=[]), \
             patch('conference_sources.collect_official_alternative',return_value=[]):
            client=PoliteClient(delay_seconds=0)
            result=collect_conference(client,{'name':'C','acronym':'C'},2025,2026)
        self.assertEqual(result,[paper])
        self.assertEqual(client.failures,1)
    def test_empty_collection_preserves_existing_nonempty_feed(self):
        old = ConferencePaper("C", "C", "Existing", "https://test/old", "old", 2026)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.xml"
            write_rss([old], output, title="Test", link="https://test/feed", description="Test")

            count, preserved = write_rss_preserving_existing(
                [], output, title="Test", link="https://test/feed", description="Test"
            )

            self.assertEqual(count, 1)
            self.assertTrue(preserved)
            self.assertEqual(ET.parse(output).findtext("./channel/item/title"), "Existing")

    def test_empty_collection_can_create_initial_empty_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.xml"

            count, preserved = write_rss_preserving_existing(
                [], output, title="Test", link="https://test/feed", description="Test"
            )

            self.assertEqual(count, 0)
            self.assertFalse(preserved)
            self.assertEqual(ET.parse(output).findall("./channel/item"), [])

    def test_partial_refresh_does_not_remove_previous_papers(self):
        old=ConferencePaper('C','C','Existing','https://test/old','old',2025)
        new=ConferencePaper('C','C','New','https://test/new','new',2026)
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'feed.xml'
            write_rss([old],output,title='Test',link='https://test/feed',description='Test')
            count,preserved=write_rss_preserving_existing([new],output,title='Test',link='https://test/feed',description='Test')
            self.assertEqual(count,2)
            self.assertFalse(preserved)
            self.assertEqual(ET.parse(output).findtext('./channel/item/title'),'New')
            self.assertEqual({i.findtext('title') for i in ET.parse(output).findall('./channel/item')},{'Existing','New'})

    def test_enriched_metadata_keeps_existing_guid_and_avoids_duplicate(self):
        old=ConferencePaper('C','C','Paper','https://test/paper','old',2025,published='2025-06-02')
        new=ConferencePaper('C','C','Paper','https://test/paper','new',2025,doi='10.1234/paper')
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'feed.xml'
            write_rss([old],output,title='Test',link='https://test/feed',description='Test')
            guid=ET.parse(output).findtext('./channel/item/guid')
            count,_=write_rss_preserving_existing([new],output,title='Test',link='https://test/feed',description='Test')
            self.assertEqual(count,1)
            self.assertEqual(ET.parse(output).findtext('./channel/item/guid'),guid)
            self.assertIn('02 Jun 2025',ET.parse(output).findtext('./channel/item/pubDate'))

    def test_anthology_paper_without_doi_matches_its_previous_registered_doi(self):
        old=ConferencePaper('ACL','ACL','Paper','https://doi.org/10.18653/v1/2026.acl-long.1','old',2026,doi='10.18653/v1/2026.acl-long.1')
        new=ConferencePaper('ACL','ACL','Paper','https://aclanthology.org/2026.acl-long.1/','new',2026)
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/'feed.xml'
            write_rss([old],output,title='Test',link='https://test/feed',description='Test')
            guid=ET.parse(output).findtext('./channel/item/guid')
            count,_=write_rss_preserving_existing([new],output,title='Test',link='https://test/feed',description='Test')
            self.assertEqual(count,1)
            self.assertEqual(ET.parse(output).findtext('./channel/item/guid'),guid)

    def test_newer_year_precedes_older_exact_date(self):
        papers = [
            ConferencePaper("C", "C", "Older", "https://test/old", "old", 2025, published="2025-12-31"),
            ConferencePaper("C", "C", "Newer", "https://test/new", "new", 2026),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "feed.xml"
            write_rss(papers, output, title="Test", link="https://test/feed", description="Test")
            self.assertEqual(ET.parse(output).findtext("./channel/item/title"), "Newer")

    def test_crossref_does_not_invent_precision_or_use_creation_date(self):
        self.assertEqual(_crossref_date({"published": {"date-parts": [[2026]]}}), "")
        self.assertEqual(_crossref_date({"published": {"date-parts": [[2026, 6]]}}), "")
        self.assertEqual(_crossref_date({"created": {"date-parts": [[2026, 6, 1]]}}), "")
        self.assertEqual(_crossref_date({"published-online": {"date-parts": [[2026, 6, 3]]}}), "2026-06-03")

    def test_virtual_event_date_is_not_publication_date(self):
        class Client:
            def get(self, url):
                return json.dumps({"results": [{"name": "Paper", "paper_url": "https://test/paper", "starttime": "2026-04-25T11:15:00-07:00"}]}).encode()

        paper = fetch_virtual_json(Client(), {"acronym": "C", "name": "C", "slug": "c", "virtual_json": "https://test/{year}"}, 2026)[0]
        self.assertEqual(paper.published, "")

    def test_copernicus_volume_parser_reads_real_dates_and_doi(self):
        document = b'''<div class="paperlist-object"><div class="published-date">03 Jul 2026</div><a class="article-title" href="https://example.test/paper">A Mapping Paper</a><div class="authors">Alice Example, Bob Example, and Carol Example</div><div class="citation">https://doi.org/10.5194/isprs-annals-test-1-2026, 2026</div></div>'''
        papers = parse_copernicus_volume(document, {"acronym": "ISPRS", "name": "ISPRS"}, 2026)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].published, "2026-07-03")
        self.assertEqual(papers[0].doi, "10.5194/isprs-annals-test-1-2026")
        self.assertEqual(papers[0].authors, ["Alice Example", "Bob Example", "Carol Example"])

    def test_robotics_proceedings_parser_reads_all_index_rows(self):
        document = b'''<table><tr><td><a href="p043.html">Robot Mapping</a><br><i>Alice, Bob</i></td></tr><tr><td><a href="p044.html">Robot Planning</a><br><i>Carol</i></td></tr></table>'''
        papers = parse_robotics_proceedings(document, {"acronym": "RSS", "name": "Robotics RSS"}, 2026, 22, "https://example.test/rss22/index.html")
        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0].doi, "10.15607/RSS.2026.XXII.043")

    def test_ieee_vis_parser_excludes_short_papers(self):
        document = b'''<h4>Maps [Full Papers]</h4><p><strong>Urban Visual Analytics [TVCG]</strong><br>by Alice &amp; Bob</p><h4>Maps [Short Papers]</h4><p><strong>Short Work</strong><br>by Carol</p>'''
        papers = parse_ieee_vis_page(document, {"acronym": "VIS", "name": "IEEE VIS"}, 2026, "https://example.test")
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, "Urban Visual Analytics")

    def test_dblp_toc_parser_keeps_every_main_entry(self):
        document = b'''<html><head><title>TEST 2026</title></head><body><ul>
        <li class="entry inproceedings" id="conf/test/One26"><span itemprop="author"><span itemprop="name">Alice</span></span><span class="title">First Paper.</span><span itemprop="datePublished">2026</span><nav class="publ"><li class="ee"><a href="https://doi.org/10.1/one">paper</a></li></nav></li>
        <li class="entry inproceedings" id="conf/test/Two26"><span class="title">Second Paper.</span><span itemprop="datePublished">2026</span></li>
        </ul></body></html>'''
        papers = _parse_dblp_toc(document, {"acronym": "TEST", "name": "Test Conference"}, 2026)
        self.assertEqual([paper.title for paper in papers], ["First Paper", "Second Paper"])
        self.assertEqual(papers[0].doi, "10.1/one")
        self.assertEqual(papers[0].authors, ["Alice"])

    def test_virtual_conference_pagination_is_followed(self):
        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url):
                self.urls.append(url)
                if len(self.urls) == 1:
                    return json.dumps({"results": [{"name": "One", "paper_url": "https://one"}], "next": "http://api.test/page2"}).encode()
                return json.dumps({"results": [{"name": "Two", "paper_url": "https://two"}], "next": None}).encode()

        client = Client()
        papers = fetch_virtual_json(client, {"acronym": "C", "name": "Conference", "slug": "c", "virtual_json": "https://test/{year}"}, 2026)
        self.assertEqual([paper.title for paper in papers], ["One", "Two"])
        self.assertEqual(client.urls[1], "https://api.test/page2")

    def test_independent_feed_does_not_truncate_or_prefix_titles(self):
        papers = [
            ConferencePaper("TEST", "Test Conference", f"Paper {index}", f"https://example.test/{index}", f"test:{index}", 2026)
            for index in range(1201)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test.xml"
            count = write_rss(papers, output, title="TEST Papers", link="https://example.test/test.xml", description="All papers")
            root = ET.parse(output)
            self.assertEqual(count, 1201)
            self.assertEqual(len(root.findall("./channel/item")), 1201)
            self.assertEqual(root.findtext("./channel/language"), "en")
            self.assertEqual(root.findtext("./channel/item/title"), "Paper 999")
            self.assertTrue(root.findtext("./channel/item/guid").startswith("conference:sha256:"))

    def test_feed_guids_remain_unique_when_provider_ids_are_reused(self):
        papers = [
            ConferencePaper("TEST", "Test", "First", "https://example.test/1", "reused", 2026),
            ConferencePaper("TEST", "Test", "Second", "https://example.test/2", "reused", 2026),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "test.xml"
            write_rss(papers, output, title="Test", link="https://example.test/feed", description="Test")
            guids = [item.findtext("guid") for item in ET.parse(output).findall("./channel/item")]
            self.assertEqual(len(guids), len(set(guids)))

    def test_deduplication_prefers_doi_and_also_matches_title(self):
        first = ConferencePaper("A", "A", "Same Paper", "https://a.test", "a", 2025)
        second = ConferencePaper("B", "B", "Same paper.", "https://doi.org/10.1/test", "b", 2026, doi="10.1/test")
        result = deduplicate([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].doi, "10.1/test")

    def test_keyword_matching_uses_token_boundaries(self):
        paper = ConferencePaper("A", "A", "A GIS method for urban remote sensing", "https://a.test", "a", 2026)
        self.assertEqual(match_keywords(paper, ["GIS", "urban", "UAI"]), ["GIS", "urban"])

    def test_main_conference_posters_are_kept_but_workshops_are_excluded(self):
        conference = {"acronym": "ICLR"}
        self.assertTrue(_is_main_paper({"title": "Accepted poster", "venue": "ICLR"}, conference))
        self.assertFalse(_is_main_paper({"title": "Accepted paper", "venue": "ICLR Workshops"}, conference))


if __name__ == "__main__":
    unittest.main()
