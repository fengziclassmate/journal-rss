import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from source_response import SourceResponseError, format_source_error, validate_response
from conference_rss import PoliteClient


CHALLENGE = b"<html><title>Making sure you&#39;re not a bot!</title><script id='anubis_challenge'>{}</script></html>"


class SourceResponseTests(unittest.TestCase):
    def test_verification_page_is_not_valid_source_data(self):
        with self.assertRaises(SourceResponseError):
            validate_response(CHALLENGE, 'https://dblp.org/db/conf/aaai/index.html')

    def test_regular_html_and_xml_are_allowed(self):
        validate_response(b'<html><title>Conference papers</title><p>Paper</p></html>', 'https://example.test')
        validate_response(b'<rss><channel><item><title>Access Denied: A security paper</title></item></channel></rss>', 'https://example.test/rss')
        validate_response(b'<rss><channel><item><description><![CDATA[<html><title>Access Denied</title></html>]]></description></item></channel></rss>', 'https://example.test/rss')

    def test_error_details_do_not_leak_query_secrets(self):
        url='https://user:password@example.test/feed?api_key=secret'
        text=format_source_error(urllib.error.HTTPError(url,403,'Forbidden',{},None),url)
        self.assertIn('403',text)
        self.assertNotIn('password',text)
        self.assertNotIn('secret',text)

    def test_known_bad_cache_is_discarded_without_network_retry_storm(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            url='https://dblp.org/db/conf/aaai/index.html'
            path=Path(d)/hashlib.sha256(url.encode()).hexdigest()
            path.write_bytes(CHALLENGE)
            client=PoliteClient(delay_seconds=0,cache_dir=Path(d))
            class Response:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def read(self): return CHALLENGE
            with patch('conference_rss.urllib.request.urlopen',return_value=Response()) as fetch:
                with self.assertRaises(SourceResponseError): client.get(url)
                with self.assertRaises(SourceResponseError): client.get('https://dblp.org/db/conf/acl/index.html')
            self.assertEqual(fetch.call_count,2)
            self.assertFalse(path.exists())
            self.assertEqual(client.failures,4)

    def test_crossref_error_schema_and_maintenance_html_are_not_cacheable(self):
        for raw in (b'<html><title>Maintenance</title></html>',b'{"status":"ok","message":{"error":"offline"}}'):
            with self.subTest(raw=raw),self.assertRaises(SourceResponseError):
                validate_response(raw,'https://api.crossref.org/works?query=test')

    def test_cached_valid_response_is_kept_and_measured(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            url='https://example.test/api'
            raw=b'{"results":[]}'
            path=Path(d)/hashlib.sha256(url.encode()).hexdigest()
            path.write_bytes(raw)
            client=PoliteClient(delay_seconds=0,cache_dir=Path(d))
            with patch('conference_rss.urllib.request.urlopen') as fetch:
                self.assertEqual(client.get(url),raw)
            fetch.assert_not_called()
            self.assertEqual(client.cache_hits,1)


if __name__=='__main__': unittest.main()
