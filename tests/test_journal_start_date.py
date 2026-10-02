import unittest
from journal_rss_aggregator import journal_start_date,CROSSREF_JOURNALS


class JournalStartDateTests(unittest.TestCase):
    def test_fixed_addition_date_not_runtime_date(self):
        for added,expected in [('2026-10-01','2026-09-01'),('2026-10-02','2026-09-02'),
                               ('2026-03-31','2026-02-28'),('2024-03-31','2024-02-29'),
                               ('2026-01-10','2025-12-10')]:
            self.assertEqual(journal_start_date({'added_on':added},2099),expected)

    def test_existing_explicit_ranges_are_preserved(self):
        self.assertEqual(journal_start_date({'added_on':'2026-10-01','from_date':'2026-06-01'},2020),'2026-06-01')
        self.assertEqual(journal_start_date({},2020),'2020-01-01')

    def test_ijgi_configuration(self):
        spec=next(j for j in CROSSREF_JOURNALS if j['output']=='ijgi.xml')
        self.assertEqual(spec['issn'],'2220-9964')
        self.assertEqual(journal_start_date(spec,2020),'2026-09-01')
        self.assertEqual(spec['date_filter'],'pub')

    def test_land_use_policy_configuration(self):
        spec=next(j for j in CROSSREF_JOURNALS if j['output']=='land-use-policy.xml')
        self.assertEqual(spec['issn'],'0264-8377')
        self.assertEqual(journal_start_date(spec,2020),'2026-09-02')
        self.assertEqual(spec['date_filter'],'pub')
