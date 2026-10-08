"""Small inline fixtures mirror official Anthology XML and event-row markup."""

import subprocess
import sys
import unittest
import urllib.error
from unittest.mock import patch

from conference_sources import (
    ANTHOLOGY,
    XML_BASE,
    collect_official_alternative,
    parse_acl_anthology_event,
    parse_acl_anthology_xml,
)
from source_response import SourceResponseError


ACL = {"slug": "acl", "acronym": "ACL", "name": "Association for Computational Linguistics"}
BLOCKED = b'''<html><head><title>Making sure you're not a bot!</title></head>
<body><script id="anubis_challenge">{}</script></body></html>'''


def paper_xml(number=1, title="A Research Paper", doi="10.18653/v1/2026.acl-long.1"):
    return f'''<paper id="{number}"><title>{title}</title>
    <author><first>Alice</first><last>Example</last><affiliation>Not an author</affiliation></author>
    <author><first>Bob</first><last>de Test</last></author>
    <pages>1-15</pages><doi>{doi}</doi></paper>'''


def volume_xml(papers, volume="long", slug="acl", year=2026, title="Proceedings: Long Papers", extra=""):
    return f'''<volume id="{volume}" ingest-date="{year}-06-22" type="proceedings">
    <meta><booktitle>{title}</booktitle><year>{year}</year><month>July</month>
    <venue>{slug}</venue>{extra}<doi>10.18653/v1/{year}.{slug}-{volume}</doi></meta>
    {papers}</volume>'''


def collection_xml(volumes, slug="acl", year=2026):
    return f'<collection id="{year}.{slug}">{volumes}</collection>'.encode()


def row_html(number=1, volume="2026.acl-long", title="A Research Paper", doi=""):
    doi_link = f'<a href="https://doi.org/{doi}">doi</a>' if doi else ""
    return f'''<p class="d-flex"><span class="list-button-row"><a href="/{volume}.{number}.pdf">pdf</a>{doi_link}</span>
    <span class="d-block"><strong><a class="align-middle" href="/{volume}.{number}/">{title}</a></strong><br>
    <a href="/people/alice-example/">Alice Example</a> | <a href="/people/bob-de-test/unverified/">Bob de Test</a></span></p>'''


def event_html(rows, headings=None):
    if headings is None:
        headings = '<h4><a href="/volumes/2026.acl-long/">Proceedings (Volume 1: Long Papers)</a></h4>'
    return f'''<!doctype html><html><head><title>Annual Meeting - ACL Anthology</title></head>
    <body><h2 id="title">Annual Meeting</h2>{headings}{rows}</body></html>'''.encode()


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


class AnthologyXmlTests(unittest.TestCase):
    def test_title_inline_markup_authors_explicit_doi_and_config(self):
        content = collection_xml(volume_xml(paper_xml(title='<fixed-case>E</fixed-case>com<fixed-case>S</fixed-case>cript &amp; <i>Maps</i>')))
        paper = parse_acl_anthology_xml(content, ACL, 2026)[0]
        self.assertEqual(paper.title, 'EcomScript & Maps')
        self.assertEqual(paper.authors, ['Alice Example', 'Bob de Test'])
        self.assertEqual(paper.doi, '10.18653/v1/2026.acl-long.1')
        self.assertEqual(paper.url, f'{ANTHOLOGY}/2026.acl-long.1/')
        self.assertEqual(paper.guid, 'acl-anthology:2026.acl-long.1')
        self.assertEqual((paper.conference, paper.conference_name, paper.year), ('ACL', ACL['name'], 2026))

    def test_all_papers_without_limit_and_different_ids_with_same_title_survive(self):
        records = ''.join(paper_xml(i, doi=f'10.18653/v1/2026.acl-long.{i}') for i in range(1, 2223))
        papers = parse_acl_anthology_xml(collection_xml(volume_xml(records)), ACL, 2026)
        self.assertEqual(len(papers), 2222)
        self.assertEqual(papers[-1].guid, 'acl-anthology:2026.acl-long.2222')

    def test_duplicate_ids_and_dois_removed_not_titles(self):
        records = paper_xml() + paper_xml() + paper_xml(2) + paper_xml(3, doi='')
        papers = parse_acl_anthology_xml(collection_xml(volume_xml(records)), ACL, 2026)
        self.assertEqual(len(papers), 2)
        self.assertTrue(papers[-1].guid.endswith('.3'))
        self.assertEqual(papers[-1].doi, '')

    def test_main_tracks_no_workshops_findings_industry_or_frontmatter(self):
        volumes = volume_xml(paper_xml(0) + paper_xml(title='A workshop scheduling method'))
        volumes += volume_xml(paper_xml(2,title='Short Paper',doi='10.18653/v1/2026.acl-short.2'),volume='short',title='Short Papers')
        for volume in ('demo', 'demos', 'srw', 'tutorials', 'industry', 'findings', 'main'):
            volumes += volume_xml(paper_xml(), volume=volume)
        volumes += volume_xml(paper_xml(99), title='Workshop on Long Papers')
        volumes += volume_xml(paper_xml(100), extra='<venue>ws</venue>')
        papers = parse_acl_anthology_xml(collection_xml(volumes), ACL, 2026)
        self.assertEqual([p.title for p in papers], ['A workshop scheduling method', 'Short Paper'])

    def test_year_and_month_ingest_revision_are_not_publication_or_presentation_dates(self):
        records = paper_xml().replace('</paper>', '<revision date="2026-07-02"/><date>2026-07-01</date></paper>')
        for volume in (volume_xml(records), volume_xml(records).replace('<month>July</month>', '')):
            with self.subTest(volume=volume[:150]):
                paper = parse_acl_anthology_xml(collection_xml(volume), ACL, 2026)[0]
                self.assertEqual(paper.published, '')
                self.assertEqual(paper.presentation_date, '')
                self.assertEqual(paper.year, 2026)

    def test_emnlp_unified_main_and_separate_long_volumes_kept_without_page_heuristics(self):
        conf = {'slug': 'emnlp'}
        volumes = volume_xml(paper_xml(1), slug='emnlp', volume='main', title='Proceedings of EMNLP')
        volumes += volume_xml(paper_xml(1, doi='10.18653/v1/2026.emnlp-long.1'), slug='emnlp')
        volumes += volume_xml(paper_xml(1), slug='emnlp', volume='short')
        papers = parse_acl_anthology_xml(collection_xml(volumes, slug='emnlp'), conf, 2026)
        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0].conference, 'EMNLP')

    def test_naacl_2025_slug_and_year(self):
        content = collection_xml(volume_xml(paper_xml(), slug='naacl', year=2025), slug='naacl', year=2025)
        paper = parse_acl_anthology_xml(content, {'slug': 'naacl'}, 2025)[0]
        self.assertEqual(paper.url, f'{ANTHOLOGY}/2025.naacl-long.1/')

    def test_malformed_wrong_collection_wrong_year_and_missing_authoritative_metadata_fail(self):
        for content in (
            b'<collection', b'<html><title>404 Not Found</title></html>',
            collection_xml(''), collection_xml(volume_xml('')),
            collection_xml(volume_xml(paper_xml()), slug='findings'),
            collection_xml(volume_xml(paper_xml(), year=2025)),
            collection_xml(volume_xml(paper_xml()).replace('<venue>acl</venue>', '')),
            collection_xml(volume_xml(paper_xml(title=''))),
        ):
            with self.subTest(content=content[:80]), self.assertRaises(SourceResponseError):
                parse_acl_anthology_xml(content, ACL, 2026)

    def test_missing_paper_doi_never_inherits_volume_doi(self):
        content = collection_xml(volume_xml(paper_xml(doi='')))
        self.assertEqual(parse_acl_anthology_xml(content, ACL, 2026)[0].doi, '')

    def test_doi_url_and_prefix_normalized_and_duplicate_record_enriches_metadata(self):
        records = paper_xml(doi='') + paper_xml(doi='HTTPS://DOI.ORG/10.18653/V1/2026.ACL-LONG.1')
        papers = parse_acl_anthology_xml(collection_xml(volume_xml(records)), ACL, 2026)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].doi, '10.18653/v1/2026.acl-long.1')
        content = collection_xml(volume_xml(paper_xml(doi='DOI:10.18653/V1/2026.ACL-LONG.1')))
        self.assertEqual(parse_acl_anthology_xml(content, ACL, 2026)[0].doi, papers[0].doi)


class AnthologyEventTests(unittest.TestCase):
    def test_main_short_papers_are_collected_alongside_long_papers(self):
        headings='<h4><a href="/volumes/2026.acl-long/">Long Papers</a></h4><h4><a href="/volumes/2026.acl-short/">Short Papers</a></h4>'
        papers=parse_acl_anthology_event(event_html(row_html()+row_html(volume='2026.acl-short'),headings),ACL,2026)
        self.assertEqual(len(papers),2)
    def test_event_inline_title_authors_actual_doi_duplicates(self):
        row = row_html(title='<span class="acl-fixed-case">O</span>ctoTools &amp; Maps', doi='10.18653/v1/2026.acl-long.1')
        papers = parse_acl_anthology_event(event_html(row + row), ACL, 2026)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].title, 'OctoTools & Maps')
        self.assertEqual(papers[0].authors, ['Alice Example', 'Bob de Test'])
        self.assertEqual(papers[0].doi, '10.18653/v1/2026.acl-long.1')
        self.assertEqual(papers[0].published, '')

    def test_event_collects_all_rows_and_excludes_other_tracks_and_events(self):
        rows = ''.join(row_html(i) for i in range(1, 1202))
        for volume in ('2026.acl-short', '2026.acl-demo', '2026.acl-srw', '2026.acl-industry', '2026.findings-acl', '2026.workshop-main', '2025.acl-long'):
            rows += row_html(volume=volume)
        rows += row_html(0)
        papers = parse_acl_anthology_event(event_html(rows), ACL, 2026)
        self.assertEqual(len(papers), 1201)
        self.assertTrue(papers[-1].url.endswith('.1201/'))
        self.assertEqual(papers[0].doi, '')

    def test_main_id_with_workshop_heading_is_not_main_track(self):
        headings = '<h4><a href="/volumes/2026.acl-long/">Workshop Proceedings</a></h4>'
        self.assertEqual(parse_acl_anthology_event(event_html(row_html(), headings), ACL, 2026), [])

    def test_foreign_host_and_navigation_links_are_not_papers(self):
        rows = row_html() + row_html(2).replace('href="/2026.acl-long.2/"', 'href="https://example.test/2026.acl-long.2/"')
        rows += '<a href="/2026.acl-long.3/">Navigation</a>'
        self.assertEqual(len(parse_acl_anthology_event(event_html(rows), ACL, 2026)), 1)

    def test_invalid_html_and_main_volume_without_rows_raise_not_empty_success(self):
        for content in (BLOCKED, b'<html><title>404 Not Found</title></html>', event_html('')):
            with self.subTest(content=content[:50]), self.assertRaises(SourceResponseError):
                parse_acl_anthology_event(content, ACL, 2026)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.xml_url = f'{XML_BASE}/2026.acl.xml'
        self.event_url = f'{ANTHOLOGY}/events/acl-2026/'
        self.xml = collection_xml(volume_xml(paper_xml()))

    def test_xml_first_one_request_and_shared_validator_applied(self):
        client = Client({self.xml_url: self.xml})
        with patch('conference_sources.validate_response') as validator:
            self.assertEqual(len(collect_official_alternative(client, ACL, 2026)), 1)
        self.assertEqual(client.urls, [self.xml_url])
        validator.assert_any_call(self.xml, self.xml_url)

    def test_xml_missing_or_failed_uses_single_event_page_no_per_paper_fetches(self):
        for missing in (b'', urllib.error.HTTPError(self.xml_url, 404, 'Not Found', {}, None),
                        urllib.error.URLError('offline'), BLOCKED, b'<collection'):
            client = Client({self.xml_url: missing, self.event_url: event_html(row_html())})
            with self.subTest(missing=missing):
                self.assertEqual(len(collect_official_alternative(client, ACL, 2026)), 1)
                self.assertEqual(client.urls, [self.xml_url, self.event_url])

    def test_genuinely_missing_2026_event_returns_empty(self):
        client = Client({self.xml_url: b'', self.event_url: urllib.error.HTTPError(self.event_url, 404, 'Not Found', {}, None)})
        self.assertEqual(collect_official_alternative(client, ACL, 2026), [])

    def test_error_not_disguised_as_empty_when_fallback_missing_or_blocked(self):
        for fallback in (b'', BLOCKED):
            client = Client({self.xml_url: BLOCKED, self.event_url: fallback})
            with self.subTest(fallback=fallback), self.assertRaises(SourceResponseError):
                collect_official_alternative(client, ACL, 2026)

    def test_validator_applied_to_event_and_unknown_conferences_make_no_requests(self):
        html = event_html(row_html())
        client = Client({self.xml_url: b'', self.event_url: html})
        with patch('conference_sources.validate_response') as validator:
            collect_official_alternative(client, ACL, 2026)
        validator.assert_any_call(html, self.event_url)
        client = Client({})
        for slug in ('aistats', 'uai', 'unknown'):
            self.assertEqual(collect_official_alternative(client, {'slug': slug}, 2026), [])
        self.assertEqual(client.urls, [])

    def test_import_does_not_eagerly_import_conference_rss(self):
        result = subprocess.run(
            [sys.executable, '-c', "import sys; import conference_sources; assert 'conference_rss' not in sys.modules"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
