import unittest
from unittest.mock import Mock

import yaml

from conference_pmlr import BASE, collect_pmlr, parse_citations
from conference_rss import fetch_crossref_fallback
from source_response import SourceResponseError


AISTATS = {'slug': 'aistats', 'acronym': 'AISTATS', 'name': 'Artificial Intelligence and Statistics'}


def record():
    return {'publisher': 'PMLR', 'volume': 300, 'title': 'A complete paper',
            'container-title': 'Proceedings of Artificial Intelligence and Statistics',
            'URL': BASE + 'v300/paper26a.html', 'author': [{'given': 'Alice', 'family': 'Example'}],
            'issued': {'date-parts': [2026, 8, 30]}}


class PmlrTests(unittest.TestCase):
    def test_publication_date_is_not_the_event_or_collection_date(self):
        papers = parse_citations(yaml.safe_dump([record()]).encode(), AISTATS, 2026, 'v300')
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].published, '2026-08-30')
        self.assertEqual(papers[0].presentation_date, '')
        self.assertEqual(papers[0].doi, '')
        self.assertEqual(papers[0].authors, ['Alice Example'])

    def test_partial_dates_do_not_become_january_first(self):
        value = record()
        value['issued']['date-parts'] = [[2026]]
        self.assertEqual(parse_citations(yaml.safe_dump([value]).encode(), AISTATS, 2026, 'v300')[0].published, '')

    def test_wrong_volume_publisher_and_paper_host_are_rejected(self):
        for key, value in [('volume', 299), ('publisher', 'Other'), ('URL', 'https://other.test/paper.html')]:
            with self.subTest(key=key):
                changed = dict(record(), **{key: value})
                with self.assertRaises(SourceResponseError):
                    parse_citations(yaml.safe_dump([changed]).encode(), AISTATS, 2026, 'v300')

    def test_index_selects_exact_conference_year_and_fetches_full_export(self):
        index = b'<html><title>Proceedings of Machine Learning Research</title><ul><li><a href="v300/">Volume 300</a> Proceedings of AISTATS 2026</li><li><a href="v258/">Volume 258</a> AISTATS 2025</li><li><a href="v337/">Volume 337</a> UAI 2026</li></ul></html>'
        client = Mock()
        client.get.side_effect = [index, yaml.safe_dump([record()]).encode()]
        self.assertEqual(len(collect_pmlr(client, AISTATS, 2026)), 1)
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         [BASE, BASE + 'v300/assets/bib/citeproc.yaml'])

    def test_html_and_empty_export_are_rejected(self):
        for raw in [b'<html><title>Just a moment</title></html>', b'[]', b'error: offline']:
            with self.subTest(raw=raw), self.assertRaises(SourceResponseError):
                parse_citations(raw, AISTATS, 2026, 'v300')


class JournalProceedingsTests(unittest.TestCase):
    def test_verified_conference_issue_does_not_import_other_journal_tracks(self):
        import json
        item = {'type': 'journal-article', 'ISSN': ['0730-0301'], 'DOI': '10.1234/a',
                'title': ['Main paper'], 'container-title': ['ACM Transactions on Graphics'],
                'volume': '45', 'issue': '4'}
        config = {'acronym': 'SIGGRAPH', 'name': 'SIGGRAPH', 'crossref_titles': ['SIGGRAPH'],
                  'crossref_issn': '0730-0301', 'crossref_issues': {'2026': {'volume': '45', 'issue': '4'}}}
        client = Mock()
        client.get.return_value = json.dumps({'message': {'items': [item,
            dict(item, DOI='10.1234/b', issue='6'), dict(item, DOI='10.1234/c', volume='44')], 'total-results': 3}}).encode()
        self.assertEqual([paper.doi for paper in fetch_crossref_fallback(client, config, 2026)], ['10.1234/a'])

    def test_missing_conference_issue_never_expands_to_a_whole_journal_year(self):
        client = Mock()
        with self.assertRaises(ValueError):
            fetch_crossref_fallback(client, {'crossref_issn': '0730-0301',
                'crossref_issues': {'2026': {'volume': '45', 'issue': '4'}},
                'crossref_titles': ['SIGGRAPH']}, 2027)
        client.get.assert_not_called()

    def test_publisher_name_may_be_in_event_metadata_instead_of_book_title(self):
        import json
        item = {'type': 'proceedings-article', 'DOI': '10.1234/a', 'title': ['A paper'],
                'container-title': ['Conference on Mapping'],
                'event': {'name': 'ACM International Conference on Mapping'}}
        client = Mock()
        client.get.side_effect = [json.dumps({'message': {'items': [item]}}).encode(),
                                 json.dumps({'message': {'items': [item], 'total-results': 1}}).encode()]
        config = {'acronym': 'MAP', 'name': 'Mapping',
                  'crossref_titles': ['ACM International Conference on Mapping']}
        self.assertEqual(len(fetch_crossref_fallback(client, config, 2026)), 1)
        self.assertEqual(client.get.call_count, 2)

    def test_book_series_is_not_enumerated_as_an_exact_conference_container(self):
        import json
        item = {'type': 'book-chapter', 'DOI': '10.1234/a', 'title': ['A paper'],
                'container-title': ['Lecture Notes in Computer Science',
                                    'Machine Learning and Knowledge Discovery in Databases. Research Track']}
        client = Mock()
        client.get.side_effect = [json.dumps({'message': {'items': [item]}}).encode(),
                                 json.dumps({'message': {'items': [item], 'total-results': 1}}).encode()]
        config = {'acronym': 'ECML', 'name': 'ECML',
                  'crossref_titles': ['Machine Learning and Knowledge Discovery in Databases']}
        self.assertEqual(len(fetch_crossref_fallback(client, config, 2026)), 1)
        self.assertEqual(client.get.call_count, 2)
        self.assertNotIn('Lecture+Notes', client.get.call_args_list[1].args[0])

    def test_issn_collection_pages_without_ambiguous_comma_title_filters(self):
        import json
        conference = {'name': 'UbiComp/IMWUT', 'acronym': 'IMWUT',
                      'crossref_issn': '2474-9567', 'crossref_titles': ['IMWUT']}
        client = Mock()
        item = {'type': 'journal-article', 'ISSN': ['2474-9567'], 'DOI': '10.1234/a',
                'title': ['A paper'], 'container-title': ['Interactive, Mobile, Wearable and Ubiquitous Technologies']}
        client.get.side_effect = [json.dumps({'message': {'items': [item], 'total-results': 2, 'next-cursor': 'page2'}}).encode(),
                                 json.dumps({'message': {'items': [dict(item, DOI='10.1234/b', title=['B paper'])], 'total-results': 2}}).encode()]
        papers = fetch_crossref_fallback(client, conference, 2026)
        self.assertEqual(len(papers), 2)
        urls = [call.args[0] for call in client.get.call_args_list]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all('/journals/2474-9567/works?' in url for url in urls))
        self.assertFalse(any('container-title' in url.split('select=')[0] for url in urls))

    def test_wrong_issn_is_not_silently_accepted(self):
        import json
        client = Mock()
        client.get.return_value = json.dumps({'message': {'items': [{'ISSN': ['0000-0000']}], 'total-results': 1}}).encode()
        with self.assertRaises(ValueError):
            fetch_crossref_fallback(client, {'crossref_issn': '2474-9567', 'crossref_titles': ['IMWUT']}, 2026)
