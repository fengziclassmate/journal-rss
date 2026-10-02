import datetime as dt
import unittest
from tmlr_rss import parse_papers, update_state


class TmlrTests(unittest.TestCase):
    def test_parser(self):
        raw = '<li class="item"><h4>A &amp; B</h4><p><i>X, Y</i>, September 2026 <a href="https://openreview.net/forum?id=abc">openreview</a></p></li>'
        self.assertEqual(parse_papers(raw)['abc'], dict(title='A & B', authors='X, Y', month='2026-09'))

    def test_reject_empty_or_changed_markup(self):
        for raw in ('<html>Error</html>', '<li class="item">broken</li>'):
            with self.assertRaises(ValueError):
                parse_papers(raw)

    def test_fixed_start_and_stable_discovery(self):
        now = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
        papers = {m: dict(title=m, authors='A', month=m) for m in ['2026-08', '2026-09', '2026-10', '2026-11']}
        state = update_state({}, papers, now)
        self.assertEqual(set(state), {'2026-09', '2026-10'})
        self.assertEqual(update_state(state, papers, now + dt.timedelta(days=1)), state)
        self.assertEqual(update_state(state, {}, now), state)
