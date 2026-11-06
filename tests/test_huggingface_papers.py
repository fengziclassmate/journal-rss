import datetime as dt
import email.utils
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import huggingface_papers_rss as hf
from rss_ops import write_json
from rss_read_filter import filter_feed, hash_tokens, identity_tokens


def paper(ident='2611.08237', **changes):
    nested = dict(id=ident, title='A < B & "C"', summary='Use <script> safely & compare.',
                  authors=[{'name': 'A & B'}], publishedAt='2026-11-05T16:18:35Z',
                  submittedOnDailyAt='2026-11-06T00:00:00Z')
    nested.update(changes)
    return {'paper': nested, 'title': 'Wrong outer title', 'summary': 'Wrong outer summary',
            'publishedAt': '2026-11-05T11:18:35Z'}


def raw(rows):
    return json.dumps(rows).encode()


class HuggingFacePapersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_nested_metadata_time_escaping_and_stable_deduplication(self):
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper(), paper(title='Updated < title & value')])) as fetch:
            result = hf.run(self.root)
        self.assertEqual(result['papers'], 1)
        self.assertEqual(fetch.call_count, 1)
        item = ET.parse(self.root / hf.OUTPUT).find('channel/item')
        self.assertEqual(item.findtext('guid'), 'hf-papers:arxiv:2611.08237')
        self.assertEqual(item.findtext('title'), 'Updated < title & value')
        self.assertEqual(item.findtext('link'), 'https://huggingface.co/papers/2611.08237')
        self.assertEqual(item.findtext('{' + hf.DC + '}creator'), 'A & B')
        self.assertEqual(email.utils.parsedate_to_datetime(item.findtext('pubDate')),
                         dt.datetime(2026,11,5,16,18,35,tzinfo=dt.timezone.utc))
        self.assertIn('&lt;script&gt;', item.findtext('description'))
        self.assertNotIn('<script>', item.findtext('description'))
        self.assertIn('https://arxiv.org/pdf/2611.08237', item.findtext('description'))

    def test_three_page_limit_and_short_page_stop(self):
        full = [paper(f'2611.{i:05d}') for i in range(100)]
        with patch.object(hf, 'fetch_bytes', return_value=raw(full)) as fetch:
            self.assertEqual(len(hf.collect()), 100)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args[0].split('p=')[-1] for call in fetch.call_args_list], ['0','1','2'])
        with patch.object(hf, 'fetch_bytes', side_effect=[raw(full), raw([])]) as fetch:
            self.assertEqual(len(hf.collect()), 100)
        self.assertEqual(fetch.call_count, 2)

    def test_upsert_retains_prior_records_and_cached_rebuild_uses_no_network(self):
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper('2611.00001')])):
            hf.run(self.root)
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper('2611.00002')])):
            self.assertEqual(hf.run(self.root)['papers'], 2)
        state_before = (self.root / hf.STATE).read_bytes()
        (self.root / hf.OUTPUT).unlink()
        with patch.object(hf, 'fetch_bytes') as fetch:
            self.assertEqual(hf.run(self.root, cached=True)['papers'], 2)
        fetch.assert_not_called()
        self.assertEqual((self.root / hf.STATE).read_bytes(), state_before)

    def test_cache_capped_by_recommendation_date_not_publication_date(self):
        old = {f'2610.{i:05d}': hf.parse_record(paper(f'2610.{i:05d}', submittedOnDailyAt='2026-11-01T00:00:00Z')) for i in range(1000)}
        write_json(self.root / hf.STATE, old)
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper(publishedAt='2026-10-25T00:00:00Z')])):
            self.assertEqual(hf.run(self.root)['papers'], 1000)
        saved = json.loads((self.root / hf.STATE).read_text())
        self.assertEqual(next(iter(saved)), '2611.08237')
        self.assertNotIn('2610.00000', saved)

    def test_failed_empty_or_malformed_download_preserves_feed_and_cache(self):
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper()])):
            hf.run(self.root)
        before = {path: path.read_bytes() for path in (self.root / hf.STATE, self.root / hf.OUTPUT)}
        for failure in (TimeoutError('upstream timed out'), raw([]), raw({'error':'blocked'}), raw([paper(title='')])):
            with self.subTest(failure=failure), patch.object(hf, 'fetch_bytes', side_effect=failure if isinstance(failure, Exception) else None, return_value=failure):
                with self.assertRaises((TimeoutError, ValueError)):
                    hf.run(self.root)
            self.assertTrue(all(path.read_bytes() == contents for path,contents in before.items()))

    def test_shared_filter_matches_same_arxiv_from_other_feed(self):
        with patch.object(hf, 'fetch_bytes', return_value=raw([paper(),paper('2611.08238')])):
            hf.run(self.root)
        key = b'unit-test-key'
        suppressed = hash_tokens(identity_tokens(link='https://arxiv.org/abs/2611.08237v2'), key)
        result = filter_feed(self.root / hf.OUTPUT, suppressed, key)
        self.assertEqual((result.removed,result.kept), (1,1))
        self.assertEqual(ET.parse(self.root / hf.OUTPUT).findtext('channel/item/guid'), 'hf-papers:arxiv:2611.08238')


if __name__ == '__main__':
    unittest.main()
