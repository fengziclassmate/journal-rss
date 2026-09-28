import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

from arxiv_email_daily import add_delivery, run, write_outputs
from research_rss import (EmailDelivery, Paper, PaperStore, fetch_arxiv_email_deliveries,
                          _write_email_only_outputs, build_daily_record)


class EmailDailyTests(unittest.TestCase):
    def state(self):
        return {'version':1,'days':{},'processed_email_hashes':[]}

    def paper(self, index, abstract='Original abstract'):
        return Paper('arxiv-email',f'2609.{index:05d}',f'Paper {index}',
                     f'https://arxiv.org/abs/2609.{index:05d}',abstract=abstract,authors=['A'],categories=['cs.CV'])

    def test_all_papers_and_full_abstracts_without_scores(self):
        state=self.state()
        papers=[self.paper(i) for i in range(1001)]
        papers[-1].abstract='start ' + 'long abstract ' * 1500 + ' END'
        add_delivery(state,EmailDelivery('2026-09-28','one',papers))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_outputs(state,root,'https://example.test')
            items=ET.parse(root/'arxiv-email-daily.xml').findall('./channel/item')
            self.assertEqual(len(items),1)
            self.assertIn('| 1001 ',items[0].findtext('title'))
            self.assertEqual(items[0].findtext('guid'),'arxiv-email-daily:2026-09-28')
            body=items[0].findtext('description')
            self.assertEqual(body.count('<article>'),1001)
            self.assertIn(' END',body)
            self.assertNotIn('/100',body)
            self.assertNotIn('DeepSeek',body)
            archive=json.loads((root/'arxiv-email-archive/2026-09-28.json').read_text('utf-8'))
            self.assertEqual(len(archive['papers']),1001)
            self.assertTrue(items[0].findtext('link').endswith('/arxiv-email-archive/2026-09-28.html'))

    def test_same_day_dedup_and_stable_repeated_fetch(self):
        state=self.state()
        first=EmailDelivery('2026-09-28','one',[self.paper(1)])
        self.assertTrue(add_delivery(state,first))
        self.assertFalse(add_delivery(state,first))
        add_delivery(state,EmailDelivery('2026-09-28','two',[self.paper(1),self.paper(2)]))
        self.assertEqual(len(state['days']['2026-09-28']['papers']),2)

    def test_later_version_does_not_rewrite_prior_day(self):
        state=self.state()
        add_delivery(state,EmailDelivery('2026-09-27','one',[self.paper(1,'first')]))
        add_delivery(state,EmailDelivery('2026-09-28','two',[self.paper(1,'second')]))
        self.assertEqual(state['days']['2026-09-27']['papers']['arxiv:2609.00001']['abstract'],'first')

    def test_no_library_or_llm_calls_and_scan_not_limited_to_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            config=root/'config.json'
            config.write_text(json.dumps({'base_url':'https://example.test','sources':{'email':{}}}))
            deliveries=[EmailDelivery('2026-09-28',str(i),[self.paper(i)]) for i in range(11)]
            with patch('arxiv_email_daily.fetch_arxiv_email_deliveries',return_value=(deliveries,'ok:11')) as fetch, \
                 patch('research_rss.analyze_with_deepseek',side_effect=AssertionError('paid call')), \
                 patch('research_rss.prepare_library',side_effect=AssertionError('library read')):
                run(config,root/'state.json',root/'missing.json',root)
            self.assertEqual(fetch.call_args.kwargs['max_emails'],0)
            self.assertEqual(len(json.loads((root/'state.json').read_text('utf-8'))['days']['2026-09-28']['papers']),11)

    def test_unsafe_html_is_escaped_and_days_sorted_newest_first(self):
        state=self.state()
        add_delivery(state,EmailDelivery('2026-09-27','one',[self.paper(1,'<script>bad</script>')]))
        add_delivery(state,EmailDelivery('2026-09-28','two',[self.paper(2)]))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            write_outputs(state,root,'https://example.test')
            items=ET.parse(root/'arxiv-email-daily.xml').findall('./channel/item')
            self.assertEqual(items[0].findtext('guid'),'arxiv-email-daily:2026-09-28')
            self.assertNotIn('<script>',items[1].findtext('description'))

    def test_empty_parse_is_not_marked_complete(self):
        state=self.state()
        self.assertFalse(add_delivery(state,EmailDelivery('2026-09-28','bad',[])))
        self.assertNotIn('bad',state['processed_email_hashes'])

    @patch.dict(os.environ, {'ARXIV_EMAIL_ADDRESS':'test@example.test','ARXIV_EMAIL_AUTH_CODE':'test'})
    def test_mailbox_unlimited_scan_and_received_date_filter(self):
        connection=Mock()
        connection.search.return_value=('OK',[b'1 2 3 4 5 6 7 8 9 10 11'])
        connection.fetch.side_effect=lambda number, query: ('OK',[(b'RFC822',
            b'Message-ID: <' + number + b'>\n\nbody')])
        with patch('research_rss.imaplib.IMAP4_SSL',return_value=connection), \
             patch('research_rss.parse_arxiv_email',return_value=[self.paper(1)]):
            deliveries,_=fetch_arxiv_email_deliveries(PaperStore.empty(),
                {'sources':{'email':{'since':'2026-09-01'}}},max_emails=0)
        self.assertEqual(len(deliveries),11)
        self.assertIn('SINCE 01-Sep-2026',connection.search.call_args.args[1])

    def test_production_workflow_has_no_model_credentials(self):
        workflow=Path('.github/workflows/update-feed.yml').read_text('utf-8')
        self.assertNotIn('DEEPSEEK_API_KEY',workflow)
        self.assertNotIn('ZOTERO_LIBRARY_KEY',workflow)
        self.assertIn('python arxiv_email_daily.py',workflow)

    @patch.dict(os.environ, {'ARXIV_EMAIL_ADDRESS':'test@example.test','ARXIV_EMAIL_AUTH_CODE':'test'})
    def test_mailbox_filters_old_results_even_when_server_ignores_since(self):
        connection=Mock()
        connection.search.return_value=('OK',[b'1'])
        connection.fetch.return_value=('OK',[(b'1 (INTERNALDATE "20-Aug-2026 09:00:00 +0800")',
            b'Message-ID: <old>\n\nbody')])
        with patch('research_rss.imaplib.IMAP4_SSL',return_value=connection), \
             patch('research_rss.parse_arxiv_email') as parse:
            deliveries,_=fetch_arxiv_email_deliveries(PaperStore.empty(),
                {'sources':{'email':{'since':'2026-09-01'}}},max_emails=0)
        self.assertEqual(deliveries,[])
        parse.assert_not_called()

    def test_legacy_selected_count_does_not_link_to_full_analysis(self):
        store=PaperStore.empty()
        selected=store.upsert(self.paper(1))
        store.upsert(self.paper(2))
        store.data['digests']['2026-09-28']=build_daily_record(
            store,'2026-09-28',[selected],{},guid_prefix='arxiv-email-daily')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            _write_email_only_outputs(store,root,'https://example.test')
            item=ET.parse(root/'arxiv-email-daily.xml').find('./channel/item')
            self.assertIn('| 1 ',item.findtext('title'))
            self.assertTrue(item.findtext('link').endswith('/arxiv-email-archive/2026-09-28.html'))
            linked=json.loads((root/'arxiv-email-archive/2026-09-28.json').read_text('utf-8'))
            self.assertEqual(len(linked['papers']),1)


if __name__ == '__main__':
    unittest.main()
