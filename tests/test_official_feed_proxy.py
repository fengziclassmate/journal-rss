import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from official_feed_proxy import build_crossref_rss, feed_entry_count, fetch_bytes, fetch_crossref_feed, load_config, mirror_all, mirror_one
from zotero_switch_official_feeds import migration_script


RSS = b"<rss version='2.0'><channel><title>x</title><item><title>one</title></item></channel></rss>"
ATOM = b"<feed xmlns='http://www.w3.org/2005/Atom'><title>x</title><entry><title>one</title></entry></feed>"
SPEC = {"name": "Test", "source_url": "https://publisher.test/rss", "output": "official-feeds/test.xml"}
BACKUP_SPEC = dict(SPEC, crossref_issn="2220-9964", crossref_from="2026-09-01", crossref_date_filter="pub")


def response(raw):
    return io.BytesIO(raw)


def article_feed(items, kind='rss'):
    if kind == 'atom':
        root = ET.Element('feed', xmlns='http://www.w3.org/2005/Atom')
        parent = root
    elif kind == 'rdf':
        root = ET.Element('{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF')
        parent = root
    else:
        root = ET.Element('rss', version='2.0')
        parent = ET.SubElement(root, 'channel')
        ET.SubElement(parent, 'title').text = 'Publisher journal'
    for guid, link, doi, title in items:
        item = ET.SubElement(parent, 'entry' if kind == 'atom' else 'item')
        ET.SubElement(item, 'title').text = title
        if kind == 'atom':
            ET.SubElement(item, 'id').text = guid
            ET.SubElement(item, 'link', href=link)
        elif kind == 'rdf':
            item.set('{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about', guid)
            ET.SubElement(item, 'link').text = link
        else:
            ET.SubElement(item, 'guid', isPermaLink='false').text = guid
            ET.SubElement(item, 'link').text = link
        if doi:
            ET.SubElement(item, '{http://purl.org/dc/elements/1.1/}identifier').text = doi
    return ET.tostring(root)


def mirrored_items(path):
    from rss_read_filter import _feed_entries
    from journal_unified import normalize
    return [normalize(node, SPEC['source_url']) for node in _feed_entries(ET.parse(path).getroot(), path)[1]]


class OfficialFeedProxyTests(unittest.TestCase):
    def test_shared_url_with_distinct_fresh_dois_does_not_reuse_unknown_old_guid(self):
        old=b'<rss><channel><item><guid>old-guid</guid><title>Historical</title><link>https://publisher.test/shared</link></item></channel></rss>'
        payload={'message':{'items':[{'title':[f'Article {n}'],'DOI':f'10.1234/{n}','URL':f'https://doi.org/10.1234/{n}','resource':{'primary':{'URL':'https://publisher.test/shared'}}} for n in ('a','b')]}}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            output=root/SPEC['output']
            output.parent.mkdir()
            output.write_bytes(old)
            raw=build_crossref_rss(BACKUP_SPEC,payload)
            with patch('official_feed_proxy.fetch_crossref_feed',return_value=raw):
                mirror_one(BACKUP_SPEC,root,lambda _:(_ for _ in ()).throw(OSError('offline')))
            items=ET.parse(output).findall('./channel/item')
            self.assertEqual(len(items),3)
            self.assertEqual(len({i.findtext('guid') for i in items}),3)
            self.assertEqual({i.findtext('title') for i in items},{'Historical','Article a','Article b'})
    def test_config_has_unique_official_mirrors_including_ijgi(self):
        mirrors = load_config(Path("official-feed-config.json"))
        self.assertEqual(len(mirrors), 72)
        self.assertEqual(len({item["mirror_url"] for item in mirrors}), 72)
        self.assertIn('https://rss.sciencedirect.com/publication/science/02648377', {item['source_url'] for item in mirrors})
        self.assertIn('https://www.mdpi.com/rss/journal/ijgi', {item['source_url'] for item in mirrors})

    def test_counts_rss_and_atom_entries(self):
        self.assertEqual(feed_entry_count(RSS), 1)
        self.assertEqual(feed_entry_count(ATOM), 1)

    def test_counts_rdf_entries(self):
        raw = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><item/></rdf:RDF>'
        self.assertEqual(feed_entry_count(raw), 1)

    def test_embedded_html_in_rss_is_not_an_html_response(self):
        raw = b'<rss><channel><title>Journal</title><item><description><![CDATA[<html><head><title>Article</title></head><body>Abstract</body></html>]]></description></item></channel></rss>'
        self.assertEqual(feed_entry_count(raw), 1)

    def test_verification_response_is_not_retried(self):
        raw = b'<html><title>Just a moment...</title></html>'
        with patch('official_feed_proxy.urllib.request.urlopen', return_value=response(raw)) as fetch, patch('official_feed_proxy.time.sleep') as sleep:
            with self.assertRaises(ValueError):
                fetch_bytes(SPEC['source_url'])
            self.assertEqual(fetch.call_count, 1)
            sleep.assert_not_called()

    def test_valid_refresh_preserves_publisher_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('official_feed_proxy.fetch_crossref_feed') as backup:
                result = mirror_one(BACKUP_SPEC, root, lambda _: ATOM)
            self.assertEqual(result.status, 'updated')
            self.assertEqual((root / SPEC['output']).read_bytes(), ATOM)
            backup.assert_not_called()

    def test_html_error_is_not_reported_as_invalid_xml(self):
        for raw in (b'<!DOCTYPE html><html><title>Access Denied</title><p>A & B</html>',
                    b'<html><title>Just a moment...</title><body>Checking your browser</body></html>'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, '(?i)html|blocked|denied|challenge'):
                feed_entry_count(raw)

    def test_permanent_http_errors_are_not_retried(self):
        for code in (403, 404):
            error = HTTPError(SPEC['source_url'], code, 'Denied', {}, None)
            with self.subTest(code=code), patch('official_feed_proxy.urllib.request.urlopen', side_effect=error) as fetch, patch('official_feed_proxy.time.sleep') as sleep:
                with self.assertRaises(HTTPError):
                    fetch_bytes(SPEC['source_url'])
                self.assertEqual(fetch.call_count, 1)
                sleep.assert_not_called()

    def test_transient_network_errors_still_retry(self):
        with patch('official_feed_proxy.urllib.request.urlopen', side_effect=[TimeoutError('timed out'), response(RSS)]) as fetch, patch('official_feed_proxy.time.sleep') as sleep:
            self.assertEqual(fetch_bytes(SPEC['source_url']), RSS)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_crossref_backups_match_registry_and_initial_windows(self):
        from journal_rss_aggregator import CROSSREF_JOURNALS, OFFICIAL_FEED_URLS, journal_start_date
        registry = {OFFICIAL_FEED_URLS[s['issn']]: s for s in CROSSREF_JOURNALS if s.get('current_issue_only') != 'true'}
        for spec in load_config(Path('official-feed-config.json')):
            needs_backup = any(host in spec['source_url'] for host in ('www.mdpi.com', 'www.nature.com', 'link.springer.com', 'journal-buildingscities.org'))
            if not needs_backup and not spec.get('crossref_issn'):
                continue
            with self.subTest(name=spec['name']):
                source = registry[spec['source_url']]
                self.assertEqual(spec.get('crossref_issn'), source['issn'])
                self.assertEqual(spec.get('crossref_from'), journal_start_date(source, 2020))
                self.assertEqual(spec.get('crossref_date_filter', 'created'), source.get('date_filter', 'pub'))

    def test_crossref_request_bounds_dates_and_rows(self):
        payload = {'status': 'ok', 'message': {'items': []}}
        spec = dict(BACKUP_SPEC, crossref_until='2026-10-08')
        with patch('official_feed_proxy.urllib.request.urlopen', return_value=response(json.dumps(payload).encode())) as fetch:
            self.assertEqual(feed_entry_count(fetch_crossref_feed(spec)), 0)
        request = fetch.call_args.args[0]
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(urlsplit(request.full_url).path, '/journals/2220-9964/works')
        self.assertEqual(params['filter'], ['from-pub-date:2026-09-01,until-pub-date:2026-10-08,type:journal-article'])
        self.assertEqual(params['sort'], ['published'])
        self.assertEqual(params['rows'], ['300'])

    def test_crossref_rate_limit_retries_after_requested_delay(self):
        payload = {'status': 'ok', 'message': {'items': []}}
        error = HTTPError('https://api.crossref.org/works', 429, 'Too Many Requests', {'Retry-After': '7'}, None)
        with patch('official_feed_proxy.urllib.request.urlopen', side_effect=[error, response(json.dumps(payload).encode())]) as fetch, patch('official_feed_proxy.time.sleep') as sleep:
            self.assertEqual(feed_entry_count(fetch_crossref_feed(BACKUP_SPEC)), 0)
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_long_rate_limit_does_not_retry_early_or_wait_indefinitely(self):
        error = HTTPError('https://api.crossref.org/works', 429, 'Too Many Requests', {'Retry-After': '600'}, None)
        with patch('official_feed_proxy.urllib.request.urlopen', side_effect=error) as fetch, patch('official_feed_proxy.time.sleep') as sleep:
            with self.assertRaises(HTTPError):
                fetch_crossref_feed(BACKUP_SPEC)
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()

    def test_crossref_invalid_query_is_rejected_before_fetch(self):
        for changes in ({'crossref_rows': 1001}, {'crossref_from': 'invalid'},
                        {'crossref_date_filter': 'unexpected'}, {'crossref_issn': '../other'},
                        {'crossref_from': '2026-10-09', 'crossref_until': '2026-10-08'}):
            with self.subTest(changes=changes), patch('official_feed_proxy.urllib.request.urlopen') as fetch:
                with self.assertRaises(ValueError):
                    fetch_crossref_feed(dict(BACKUP_SPEC, **changes))
                fetch.assert_not_called()

    def test_crossref_error_payloads_are_not_empty_successes(self):
        for raw in (b'{"status":"failed","message":{"items":[]}}', b'{"message":"not found"}',
                    b'{"status":"ok","message":{}}', b'{"status":"ok","message":{"items":{}}}',
                    b'<html><body>Access Denied</body></html>'):
            with self.subTest(raw=raw), patch('official_feed_proxy.urllib.request.urlopen', return_value=response(raw)):
                with self.assertRaises(ValueError):
                    fetch_crossref_feed(BACKUP_SPEC)

    def test_crossref_wrong_journal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ISSN'):
            build_crossref_rss(BACKUP_SPEC, {'message': {'items': [{
                'title': ['Wrong journal'], 'URL': 'https://doi.org/10.1/wrong', 'ISSN': ['2071-1050'],
            }]}})

    def test_crossref_unusable_items_are_not_empty_successes(self):
        for items in ([{'title': ['No link']}], [None]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                build_crossref_rss(BACKUP_SPEC, {'message': {'items': items}})

    def test_invalid_refresh_preserves_existing_mirror(self):
        for raw in (b'<rss>', b'<html><body>Access Denied</body></html>', b'<error>upstream failed</error>'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / SPEC['output']
                output.parent.mkdir()
                output.write_bytes(RSS)
                result = mirror_one(SPEC, root, lambda _: raw)
                self.assertTrue(result.status.startswith('preserved:'))
                self.assertEqual(output.read_bytes(), RSS)

    def test_backup_success_and_failure_keep_original_cause(self):
        for backup, prefix in ((RSS, 'crossref-fallback:'), (b'<rss>', 'preserved:')):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / SPEC['output']
                output.parent.mkdir()
                output.write_bytes(ATOM)
                with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup):
                    result = mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(HTTPError(SPEC['source_url'], 403, 'Forbidden', {}, None)))
                self.assertTrue(result.status.startswith(prefix))
                self.assertIn('403', result.status)
                if backup == RSS:
                    self.assertEqual(result.entries, 2)
                    self.assertEqual(feed_entry_count(output.read_bytes()), 2)
                else:
                    self.assertEqual(output.read_bytes(), ATOM)
                if prefix == 'preserved:':
                    self.assertIn('Crossref', result.status)
                    self.assertIn('ParseError', result.status)

    def test_failed_initial_fetch_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory, patch('official_feed_proxy.fetch_crossref_feed', side_effect=TimeoutError('backup timed out')):
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, '403.*Crossref.*timed out'):
                mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(HTTPError(SPEC['source_url'], 403, 'Forbidden', {}, None)))
            self.assertFalse((root / SPEC['output']).exists())

    def test_empty_backup_never_replaces_good_old_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(RSS)
            with patch('official_feed_proxy.fetch_crossref_feed', return_value=b'<rss><channel/></rss>'):
                result = mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
            self.assertTrue(result.status.startswith('preserved:'))
            self.assertIn('publisher timed out', result.status)
            self.assertIn('Crossref', result.status)
            self.assertIn('no entries', result.status)
            self.assertEqual(output.read_bytes(), RSS)

    def test_empty_backup_is_not_a_successful_initial_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('official_feed_proxy.fetch_crossref_feed', return_value=b'<rss><channel/></rss>'):
                with self.assertRaisesRegex(RuntimeError, 'publisher timed out.*Crossref.*no entries'):
                    mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
            self.assertFalse((root / SPEC['output']).exists())

    def test_health_records_both_causes_for_preserved_and_failed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'mirrors': [BACKUP_SPEC]}), encoding='utf-8')
            for existing in (False, True):
                with self.subTest(existing=existing):
                    if existing:
                        output = root / SPEC['output']
                        output.parent.mkdir()
                        output.write_bytes(RSS)
                    with patch('official_feed_proxy.fetch_bytes', side_effect=HTTPError(SPEC['source_url'], 403, 'Forbidden', {}, None)), patch('official_feed_proxy.fetch_crossref_feed', side_effect=TimeoutError('backup timed out')), patch('official_feed_proxy.record') as health:
                        mirror_all(config, root, workers=1)
                    self.assertEqual(health.call_args.args[1], 'preserved' if existing else 'failed')
                    detail = health.call_args.kwargs['detail']
                    self.assertIn('403', detail)
                    self.assertIn('Crossref', detail)
                    self.assertIn('timed out', detail)

    def test_health_records_original_cause_for_successful_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'mirrors': [BACKUP_SPEC]}), encoding='utf-8')
            with patch('official_feed_proxy.fetch_bytes', side_effect=HTTPError(SPEC['source_url'], 403, 'Forbidden', {}, None)), patch('official_feed_proxy.fetch_crossref_feed', return_value=RSS), patch('official_feed_proxy.record') as health:
                results = mirror_all(config, root, workers=1)
            self.assertEqual(health.call_args.args[1], 'fallback')
            self.assertIn('403', health.call_args.kwargs['detail'])
            self.assertEqual(results[0].entries, 1)

    def test_health_failure_detail_redacts_url_secrets(self):
        spec = dict(SPEC, source_url='https://user:secret@publisher.test/rss?token=private#fragment')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'mirrors': [spec]}), encoding='utf-8')
            with patch('official_feed_proxy.fetch_bytes', side_effect=OSError('failed at ' + spec['source_url'] + '\n<script>raw page</script>')), patch('official_feed_proxy.record') as health:
                mirror_all(config, root, workers=1)
            detail = health.call_args.kwargs['detail']
            self.assertNotIn('secret', detail)
            self.assertNotIn('private', detail)
            self.assertNotIn('<script>', detail)
            self.assertNotIn('\n', detail)

    def test_corrupt_existing_mirror_does_not_hide_source_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(b'<rss>')
            with patch('official_feed_proxy.fetch_crossref_feed', side_effect=TimeoutError('backup timed out')):
                with self.assertRaisesRegex(RuntimeError, '403.*Crossref.*timed out.*existing'):
                    mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(HTTPError(SPEC['source_url'], 403, 'Forbidden', {}, None)))
            self.assertEqual(output.read_bytes(), b'<rss>')

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

    def test_new_crossref_records_use_doi_guid_and_publisher_link(self):
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
        item = ET.fromstring(raw).find('channel/item')
        self.assertEqual(item.findtext('guid'), 'doi:10.3390/rs123')
        self.assertEqual(item.find('guid').get('isPermaLink'), 'false')

    def test_backup_atomically_unions_history_and_keeps_previous_guids(self):
        for kind in ('rss', 'rdf', 'atom'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / SPEC['output']
                output.parent.mkdir()
                output.write_bytes(article_feed([
                    ('publisher-kept', 'https://publisher.test/old-only', '10.1234/old', 'Publisher-only paper'),
                    ('publisher-matched', 'https://publisher.test/original', '10.1234/shared', 'Publisher abstract'),
                ], kind))
                backup = build_crossref_rss(BACKUP_SPEC, {'message': {'items': [
                    {'title': ['Backup title'], 'DOI': '10.1234/shared', 'URL': 'https://doi.org/10.1234/shared'},
                    {'title': ['New fallback paper'], 'DOI': '10.1234/new', 'URL': 'https://doi.org/10.1234/new'},
                ]}})
                with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup), patch('official_feed_proxy.os.replace', wraps=__import__('os').replace) as replace:
                    result = mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
                self.assertEqual(result.entries, 3)
                self.assertTrue(result.status.startswith('crossref-fallback:'))
                replace.assert_called_once()
                items = {item.findtext('guid'): item for item in mirrored_items(output)}
                self.assertEqual(set(items), {'publisher-kept', 'publisher-matched', 'doi:10.1234/new'})
                self.assertEqual(items['publisher-matched'].findtext('title'), 'Publisher abstract')

    def test_publisher_recovery_keeps_backup_ids_across_format_and_url_changes(self):
        for kind in ('rss', 'rdf', 'atom'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / SPEC['output']
                backup = build_crossref_rss(BACKUP_SPEC, {'message': {'items': [
                    {'title': ['Fallback shared'], 'DOI': '10.1234/shared', 'URL': 'https://doi.org/10.1234/shared'},
                    {'title': ['Fallback only'], 'DOI': '10.1234/only', 'URL': 'https://doi.org/10.1234/only'},
                ]}})
                with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup):
                    mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
                publisher = article_feed([
                    ('publisher-new-id', 'https://publisher.test/new-location', '10.1234/shared', 'Fresh publisher metadata'),
                    ('publisher-new-paper', 'https://publisher.test/new', '10.1234/new-paper', 'New publisher paper'),
                ], kind)
                result = mirror_one(BACKUP_SPEC, root, lambda _: publisher)
                self.assertEqual(result.status, 'updated')
                self.assertEqual(result.entries, 3)
                items = {item.findtext('guid'): item for item in mirrored_items(output)}
                self.assertEqual(set(items), {'doi:10.1234/shared', 'doi:10.1234/only', 'publisher-new-paper'})
                self.assertEqual(items['doi:10.1234/shared'].findtext('title'), 'Fresh publisher metadata')
                from rss_read_filter import _item_tokens
                self.assertIn('guid:publisher-new-id', _item_tokens(items['doi:10.1234/shared']))

    def test_strong_publisher_url_identity_keeps_old_guid_without_doi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(article_feed([('original-guid', 'http://ieeexplore.ieee.org/document/123', '', 'Old title')]))
            fresh = article_feed([('changed-guid', 'https://ieeexplore.ieee.org/document/123/', '', 'Updated title')])
            result = mirror_one(SPEC, root, lambda _: fresh)
            self.assertEqual(result.entries, 1)
            self.assertEqual(mirrored_items(output)[0].findtext('guid'), 'original-guid')
            self.assertEqual(mirrored_items(output)[0].findtext('title'), 'Updated title')

    def test_shared_url_and_title_cannot_merge_conflicting_dois_or_reuse_guid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(article_feed([('original-guid', 'https://publisher.test/shared-url', '10.1234/old', 'Same paper title')]))
            backup = build_crossref_rss(BACKUP_SPEC, {'message': {'items': [
                {'title': ['Same paper title'], 'DOI': '10.1234/different', 'URL': 'https://publisher.test/shared-url'},
            ]}})
            with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup):
                result = mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
            self.assertEqual(result.entries, 2)
            items = mirrored_items(output)
            self.assertEqual({item.findtext('guid') for item in items}, {'original-guid', 'doi:10.1234/different'})

    def test_publisher_reused_guid_with_conflicting_doi_keeps_both_articles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(article_feed([('reused-guid', 'https://publisher.test/shared', '10.1234/old', 'Same title')]))
            fresh = article_feed([('reused-guid', 'https://publisher.test/shared', '10.1234/new', 'Same title')])
            result = mirror_one(SPEC, root, lambda _: fresh)
            self.assertEqual(result.entries, 2)
            self.assertEqual({item.findtext('guid') for item in mirrored_items(output)}, {'reused-guid', 'doi:10.1234/new'})

    def test_empty_publisher_refresh_retains_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(article_feed([('original-guid', 'https://publisher.test/old', '', 'Old paper')]))
            result = mirror_one(SPEC, root, lambda _: b'<rss><channel/></rss>')
            self.assertEqual(result.status, 'updated')
            self.assertEqual(result.entries, 1)
            self.assertEqual(mirrored_items(output)[0].findtext('guid'), 'original-guid')

    def test_repeated_fallback_recovery_is_idempotent_and_remembers_old_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            output.write_bytes(article_feed([('original-guid', 'https://publisher.test/old', '10.1234/shared', 'Publisher paper')]))
            backup = build_crossref_rss(BACKUP_SPEC, {'message': {'items': [
                {'title': ['Backup paper'], 'DOI': '10.1234/shared', 'URL': 'https://doi.org/10.1234/shared'},
            ]}})
            with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup):
                mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
            fresh = article_feed([('fresh-guid', 'https://publisher.test/new', '10.1234/shared', 'Fresh publisher paper')])
            mirror_one(SPEC, root, lambda _: fresh)
            first = output.read_bytes()
            mirror_one(SPEC, root, lambda _: fresh)
            self.assertEqual(output.read_bytes(), first)
            final = article_feed([('third-guid', 'https://doi.org/10.1234/shared', '', 'Publisher URL without DOI metadata')])
            result = mirror_one(SPEC, root, lambda _: final)
            self.assertEqual(result.entries, 1)
            self.assertEqual(mirrored_items(output)[0].findtext('guid'), 'original-guid')

    def test_union_write_failure_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / SPEC['output']
            output.parent.mkdir()
            old = article_feed([('old-guid', 'https://publisher.test/old', '', 'Old paper')])
            output.write_bytes(old)
            backup = build_crossref_rss(BACKUP_SPEC, {'message': {'items': [
                {'title': ['New paper'], 'DOI': '10.1234/new', 'URL': 'https://doi.org/10.1234/new'},
            ]}})
            with patch('official_feed_proxy.fetch_crossref_feed', return_value=backup), patch('official_feed_proxy.os.replace', side_effect=OSError('atomic rename failed')):
                result = mirror_one(BACKUP_SPEC, root, lambda _: (_ for _ in ()).throw(TimeoutError('publisher timed out')))
            self.assertTrue(result.status.startswith('preserved:'))
            self.assertEqual(output.read_bytes(), old)
            self.assertEqual(list(output.parent.iterdir()), [output])

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
