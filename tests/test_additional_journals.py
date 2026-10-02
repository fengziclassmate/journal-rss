import json
import unittest
from unittest.mock import patch
from pathlib import Path
from journal_rss_aggregator import ADDITIONAL_JOURNALS,CROSSREF_JOURNALS,OFFICIAL_FEED_URLS,journal_start_date
from journal_rss_aggregator import fetch_crossref_journal_items
from journal_rss_aggregator import parse_official_feed_identity_keys,filter_official_duplicates,FeedItem
from rss_read_filter import identity_tokens


class AdditionalJournalsTests(unittest.TestCase):
    def test_publisher_urls_match_historical_official_ids(self):
        for old,new in [('http://ieeexplore.ieee.org/document/123','https://ieeexplore.ieee.org/document/123/'),
                        ('https://www.sciencedirect.com/science/article/pii/S123456','https://linkinghub.elsevier.com/retrieve/pii/S123456')]:
            item=FeedItem(source='Journal',title='A title',link=new,guid='10.1234/test')
            self.assertEqual(filter_official_duplicates([item],{'url:'+old}),[])
            self.assertIn('url:'+old,identity_tokens(link=new))
        self.assertFalse(identity_tokens(link='https://unrelated.test/document/123') & {'ieee-document:123'})

    def test_ieee_plural_authors_supports_evidence_based_deduplication(self):
        title='A sufficiently long and specific research article title on image processing'
        official=f'<rss><channel><item><title>{title}</title><link>http://ieeexplore.ieee.org/document/123</link><authors>Alice Smith;Bob Wang;</authors></item></channel></rss>'.encode()
        item=FeedItem(source='Journal',title=title,link='https://doi.org/10.1234/test',guid='10.1234/test',description='Alice Smith, Bob Wang&lt;br/&gt;Journal'.replace('&lt;','<').replace('&gt;','>'))
        identities=parse_official_feed_identity_keys(official)
        self.assertEqual(filter_official_duplicates([item],identities,official_url='https://ieeexplore.ieee.org/rss/TOC83.XML'),[])

    def test_ieee_union_retains_formal_and_year_only_early_access(self):
        formal={'DOI':'10.1234/formal','title':['Formal'], 'published':{'date-parts':[[2026,9,5]]}}
        early={'DOI':'10.1234/early','title':['Early'], 'published':{'date-parts':[[2026]]},'created':{'date-parts':[[2026,9,10]]}}
        def fetch(url,**kwargs):
            items=[formal,early] if 'from-created-date' in url else [formal]
            return json.dumps({'message':{'total-results':len(items),'items':items}}).encode()
        with patch('journal_rss_aggregator.fetch_bytes',side_effect=fetch):
            items=fetch_crossref_journal_items({'source':'Test','issn':'test','added_on':'2026-10-02','include_created':'true'},start_year=2020,end_year=2026,mailto='')
        self.assertEqual([i.guid for i in items],['10.1234/formal','10.1234/early'])
        self.assertEqual(items[1].published.date().isoformat(),'2026-09-10')
        self.assertIn('registration date',items[1].description)

    def test_every_official_journal_has_one_canonical_custom_feed(self):
        mirrors=json.loads(Path('official-feed-config.json').read_text('utf-8'))['mirrors']
        journals=[s for s in mirrors if '/streams/conf/' not in s['source_url'] and s['name']!='JMLR']
        main=[s for s in CROSSREF_JOURNALS if s.get('current_issue_only')!='true']
        self.assertEqual(len(main),71)
        self.assertEqual(len({s['issn'] for s in main}),71)
        self.assertEqual({OFFICIAL_FEED_URLS[s['issn']] for s in main},{s['source_url'] for s in journals})
        self.assertEqual(len({s['output'] for s in CROSSREF_JOURNALS}),len(CROSSREF_JOURNALS))

    def test_new_journals_use_fixed_one_month_initial_window(self):
        self.assertEqual(len(ADDITIONAL_JOURNALS),51)
        for spec in ADDITIONAL_JOURNALS:
            self.assertEqual(journal_start_date(spec,2020),'2026-09-02')
            self.assertTrue(spec['subscription_name'].endswith('_FZTX'))
            self.assertEqual(spec['date_filter'],'pub')
            if 'ieeexplore' in spec['official_url']:
                self.assertEqual(spec['include_created'],'true')
