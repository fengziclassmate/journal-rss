import datetime as dt
import unittest
from jmlr_rss import publication_month, parse_volume, feed_items
from rss_read_filter import publisher_identity_tokens
from journal_rss_aggregator import FeedItem, filter_official_duplicates


class JmlrTests(unittest.TestCase):
    def test_official_legacy_url_matches_custom(self):
        link = 'https://www.jmlr.org/papers/v27/test.html'
        self.assertIn('url:http://jmlr.org/papers/v27/test.html', publisher_identity_tokens(link))
        self.assertEqual(publisher_identity_tokens('https://other.org/papers/v27/test.html'), set())
        item = FeedItem(source='JMLR', title='Test', link=link)
        self.assertEqual(filter_official_duplicates([item], {'url:http://jmlr.org/papers/v27/test.html'}, official_url='official'), [])

    def test_publication_not_submission(self):
        self.assertEqual(publication_month('Submitted 9/25; Revised 1/26; Published 6/26'), '2026-06')
        self.assertEqual(publication_month('Published 09/2026'), '2026-09')
        for text in ['Submitted 9/26', 'Published 13/26']:
            with self.assertRaises(ValueError):
                publication_month(text)

    def test_directory(self):
        raw = '<dl><dt>A &amp; B</dt><dd><i>X, Y</i><a href="/papers/v27/x.html">abs</a><a href="/papers/volume27/x/x.pdf">pdf</a></dd></dl>'
        self.assertEqual(parse_volume(raw)['https://www.jmlr.org/papers/v27/x.html']['title'], 'A & B')
        with self.assertRaises(ValueError):
            parse_volume('error')

    def test_month_cutoff_and_stable_dates(self):
        now = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
        state = {m: dict(month=m, first_seen=now.isoformat(), title=m, authors='A', pdf='https://example.org/a.pdf')
                 for m in ['2026-08', '2026-09', '2026-10', '2026-11']}
        items = feed_items(state, now + dt.timedelta(days=1))
        self.assertEqual([i.title for i in items], ['2026-09', '2026-10'])
        self.assertTrue(all(i.published == now for i in items))
