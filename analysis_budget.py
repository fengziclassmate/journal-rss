"""Durable, fail-closed API journal, independent of feed publishing."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.request


class GitHubJournal:
    branch = 'rss-analysis-ledger'
    path = 'analysis-ledger.enc'

    def __init__(self):
        from cryptography.fernet import Fernet
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.token = os.environ['GH_TOKEN']
        self.cipher = Fernet(os.environ['ZOTERO_LIBRARY_KEY'].encode())
        self.sha = None
        try:
            self.api(f'git/ref/heads/{self.branch}')
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            head = self.api('git/ref/heads/main')['object']['sha']
            self.api('git/refs', {'ref': f'refs/heads/{self.branch}', 'sha': head}, 'POST')

    def api(self, path, body=None, method='GET'):
        request = urllib.request.Request(
            f'https://api.github.com/repos/{self.repo}/{path}',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def load(self):
        try:
            item = self.api(f'contents/{self.path}?ref={self.branch}')
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {'version': 1, 'days': {}, 'papers': {}, 'requests': []}
            raise
        self.sha = item['sha']
        if item.get('content'):
            encrypted = base64.b64decode(item['content'])
        else:
            blob = self.api(f"git/blobs/{item['sha']}")
            encrypted = base64.b64decode(blob['content'])
        data = json.loads(self.cipher.decrypt(encrypted))
        if data.get('version') != 1:
            raise ValueError('Unknown billing journal version')
        return data

    def save(self, data):
        encrypted = self.cipher.encrypt(json.dumps(data, ensure_ascii=False).encode())
        body = {'message': 'Checkpoint API budget and results', 'branch': self.branch,
                'content': base64.b64encode(encrypted).decode()}
        if self.sha:
            body['sha'] = self.sha
        # A conflict or network error must stop paid processing, not retry blindly.
        result = self.api(f'contents/{self.path}', body, 'PUT')
        self.sha = result['content']['sha']


class Budget:
    def __init__(self, settings, backend):
        self.settings = settings
        self.backend = backend
        self.data = backend.load()
        self.day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        self.active = None

    def restore(self, records):
        for record in records:
            cached = self.data['papers'].get(record['key'], {}).get('result')
            if cached and not record.get('analysis_hash'):
                record.update(cached)

    def hydrate(self, store, stream):
        for key, entry in self.data['papers'].items():
            record = entry.get('queued', {}).get(stream)
            if record and key not in store.data['papers']:
                store.data['papers'][key] = dict(record)
        self.restore(store.data['papers'].values())

    def queue(self, records, stream):
        changed = False
        for record in records:
            if record.get('billing_eligible') and not record.get('analysis_hash'):
                queued = self.data['papers'].setdefault(record['key'], {}).setdefault('queued', {})
                if stream not in queued:
                    queued[stream] = {k: v for k, v in record.items() if not k.startswith('_')}
                    changed = True
        if changed:
            self.backend.save(self.data)

    def eligible(self, record):
        attempts = self.data['papers'].get(record['key'], {}).get('attempts', 0)
        return (not record.get('analysis_hash')
                and record.get('billing_eligible', False)
                and attempts < int(self.settings.get('max_attempts_per_paper', 1)))

    def reserve(self, keys, body, output_limit):
        if self.active is not None:
            raise RuntimeError('Previous paid request has not been checkpointed')
        self.day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        day = self.data['days'].setdefault(self.day, {'requests': 0, 'reserved_units': 0})
        # UTF-8 request bytes plus output token allowance is a conservative workload
        # quota, not a claim about provider billing or exact tokenizer counts.
        units = len(body) + output_limit
        if (day['requests'] >= int(self.settings['daily_requests']) or
                day['reserved_units'] + units > int(self.settings['daily_reserved_units'])):
            return False
        for key in keys:
            if self.data['papers'].get(key, {}).get('attempts', 0) >= int(self.settings.get('max_attempts_per_paper', 1)):
                raise RuntimeError('Attempt limit exceeded')
        day['requests'] += 1
        day['reserved_units'] += units
        for key in keys:
            entry = self.data['papers'].setdefault(key, {})
            entry['attempts'] = entry.get('attempts', 0) + 1
        self.active = {'day': self.day, 'keys': keys, 'reserved_units': units,
                       'status': 'reserved', 'usage': {}, 'response_id': None}
        self.data['requests'].append(self.active)
        self.backend.save(self.data)
        print(f"[info] paid-api reserved day={self.day} requests={day['requests']} units={day['reserved_units']}")
        return True

    def response(self, response):
        self.active.update(usage=response.get('usage', {}), response_id=response.get('id'),
                           finish_reason=response.get('choices', [{}])[0].get('finish_reason'))

    def finish(self, records, status):
        fields = ('score', 'reason', 'tags', 'title_zh', 'summary_zh', 'insight_zh',
                  'analysis_hash', 'content_hash')
        for record in records:
            if record.get('analysis_hash'):
                self.data['papers'][record['key']]['result'] = {
                    name: record.get(name) for name in fields}
        self.active['status'] = status
        self.backend.save(self.data)
        print(f"[info] paid-api checkpoint status={status} usage={json.dumps(self.active['usage'])}")
        self.active = None


def prepare_budget(config):
    settings = config.get('api_safety')
    if not settings:
        return
    if not settings.get('paid_enabled') or os.environ.get('RSS_PAID_ANALYSIS_ALLOWED') != 'true':
        config['_paid_disabled'] = True
        return
    config['_budget'] = Budget(settings, GitHubJournal())
