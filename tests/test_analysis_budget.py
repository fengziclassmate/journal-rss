import copy
import json
import os
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis_budget import Budget, prepare_budget
from research_rss import (Paper, PaperStore, analysis_complete, analyze_with_deepseek,
                          migrate_analysis, restore_legacy_analysis, EmailDelivery, run_email_only)


class Backend:
    def __init__(self):
        self.data = {'version':1, 'days':{}, 'papers':{}, 'requests':[]}
        self.fail = False
    def load(self):
        return copy.deepcopy(self.data)
    def save(self, data):
        if self.fail:
            raise RuntimeError('checkpoint unavailable')
        self.data = copy.deepcopy(data)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.settings = {'daily_requests':2, 'daily_reserved_units':10000, 'max_attempts_per_paper':1}
        self.budget = Budget(self.settings, self.backend)
        self.store = PaperStore.empty()
        self.key = self.store.upsert(Paper('test','one','Paper','https://example.test/one'), now='2026-09-29')
        self.record = self.store.data['papers'][self.key]
        self.record['billing_eligible'] = True
        self.config = {'api_safety':self.settings, '_budget':self.budget, 'llm':{'batch_size':1}}

    def test_restart_does_not_reset_daily_budget(self):
        for key in ['a','b']:
            self.assertTrue(self.budget.reserve([key], b'body', 10))
            self.budget.finish([], 'failed')
        other = Budget(self.settings,self.backend)
        self.assertFalse(other.reserve(['c'],b'body',10))

    def test_unknown_interrupted_request_cannot_repeat(self):
        self.budget.reserve([self.key],b'body',10)
        other = Budget(self.settings,self.backend)
        self.assertFalse(other.eligible(self.record))

    def test_input_output_quota_is_reserved_before_call(self):
        self.assertFalse(self.budget.reserve([self.key], b'body', 10000))
        self.assertTrue(self.budget.eligible(self.record))

    def test_profile_change_does_not_invalidate_paid_analysis(self):
        self.record['analysis_hash']='old-model-or-library-hash'
        self.assertTrue(analysis_complete(self.record, {'api_safety':self.settings,'_library_version':'new'}))

    def test_legacy_translation_is_migrated_without_api(self):
        archived=dict(title='Paper',title_zh='translated',summary_zh='summary',score=80)
        restore_legacy_analysis(self.record,archived,self.config)
        self.assertTrue(analysis_complete(self.record,self.config))
        self.assertTrue(self.record['content_hash'])

    def test_legacy_title_mismatch_is_not_trusted(self):
        restore_legacy_analysis(self.record,dict(title='Other',title_zh='x',summary_zh='y'),self.config)
        self.assertFalse(self.record['analysis_hash'])

    def test_old_unprocessed_records_do_not_become_billable(self):
        self.record.pop('billing_eligible')
        migrate_analysis(self.store,self.config)
        self.assertFalse(self.budget.eligible(self.record))

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'test'})
    def test_checkpoint_failure_prevents_api_call(self):
        self.backend.fail=True
        with patch('research_rss._request') as request:
            with self.assertRaisesRegex(RuntimeError,'checkpoint'):
                analyze_with_deepseek(self.store,[self.key],self.config)
        request.assert_not_called()

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'test'})
    def test_success_is_reusable_after_publish_failure(self):
        response={'id':'request-123','usage':{'prompt_tokens':12,'completion_tokens':20},
                  'choices':[{'finish_reason':'stop','message':{'content':json.dumps([
                      {'id':self.key,'relevance_score':80,'title_zh':'translated','summary_zh':'summary'}])}}]}
        with patch('research_rss._request',return_value=json.dumps(response).encode()) as request:
            analyze_with_deepseek(self.store,[self.key],self.config)
        request.assert_called_once()
        other=Budget(self.settings,self.backend)
        self.record['analysis_hash']=''
        other.restore([self.record])
        self.assertTrue(self.record['analysis_hash'])
        self.assertEqual(self.backend.data['requests'][0]['usage']['prompt_tokens'],12)
        self.assertEqual(self.backend.data['requests'][0]['response_id'],'request-123')

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'test'})
    def test_invalid_json_cannot_loop_on_retry(self):
        response={'id':'truncated','usage':{'completion_tokens':4096},
                  'choices':[{'finish_reason':'length','message':{'content':'[broken'}}]}
        with patch('research_rss._request',return_value=json.dumps(response).encode()) as request:
            analyze_with_deepseek(self.store,[self.key],self.config)
            self.config['_budget']=Budget(self.settings,self.backend)
            analyze_with_deepseek(self.store,[self.key],self.config)
        self.assertEqual(request.call_count,1)
        self.assertEqual(self.backend.data['requests'][0]['finish_reason'],'length')

    def test_queues_survive_restart_and_do_not_mix_email_with_crossref(self):
        self.budget.queue([self.record],'research')
        other=Budget(self.settings,self.backend)
        email=PaperStore.empty()
        other.hydrate(email,'email')
        self.assertFalse(email.data['papers'])
        other.hydrate(email,'research')
        self.assertIn(self.key,email.data['papers'])

    def test_paid_gate_requires_both_configuration_and_environment(self):
        config={'api_safety':{'paid_enabled':True}}
        with patch.dict(os.environ,{'RSS_PAID_ANALYSIS_ALLOWED':'false'}):
            prepare_budget(config)
        self.assertTrue(config['_paid_disabled'])
        with patch('research_rss._request') as request:
            self.assertEqual(analyze_with_deepseek(self.store,[self.key],config),'disabled:cost-safety')
        request.assert_not_called()

    def test_historical_email_is_not_enrolled_and_paid_off_stays_off(self):
        old=Paper('arxiv-email','2609.00001','Old','https://arxiv.org/abs/2609.00001')
        new=Paper('arxiv-email','2609.00002','New','https://arxiv.org/abs/2609.00002')
        deliveries=[EmailDelivery('2026-09-28','old',[old]),EmailDelivery('2026-09-29','new',[new])]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            config=root/'config.json'
            config.write_text(json.dumps({'base_url':'https://example.test',
                'api_safety':dict(self.settings,paid_enabled=False,new_papers_from='2026-09-29')}))
            with patch('research_rss.fetch_arxiv_email_deliveries',return_value=(deliveries,'ok:2')), \
                 patch('research_rss._request') as request:
                run_email_only(config,root/'state.json',root,now=dt.datetime(2026,9,29,tzinfo=dt.timezone.utc))
            request.assert_not_called()
            state=PaperStore.load(root/'state.json')
            self.assertNotIn('arxiv:2609.00001',state.data['papers'])
            self.assertTrue(state.data['papers']['arxiv:2609.00002']['billing_eligible'])


if __name__ == '__main__':
    unittest.main()
