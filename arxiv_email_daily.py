"""Unfiltered daily arXiv email records. No model, ranking or paid API calls."""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from research_rss import PaperStore, UTC, _rss_root, _write_xml, fetch_arxiv_email_deliveries
from rss_ops import write_json, write_changed, write_feed
from rss_health import record


TITLE = 'QQ 邮箱 arXiv 邮件全文日报'
FIELDS = ('title', 'url', 'abstract', 'authors', 'categories', 'doi', 'published')


def save(state, path):
    path = Path(path)
    directory = path.with_suffix('')
    days = {}
    for day, entry in state['days'].items():
        dt.date.fromisoformat(day)
        write_json(directory / f'{day}.json', entry)
        days[day] = f'{day}.json'
    manifest = {k:v for k,v in state.items() if k not in ('days','version')}
    write_json(path, dict(manifest, version=2, day_files=days))


def load(path, legacy):
    if path.exists():
        state = json.loads(path.read_text('utf-8'))
        if state.get('version') == 2:
            days = {}
            for day, filename in state.pop('day_files').items():
                dt.date.fromisoformat(day)
                if filename != f'{day}.json':
                    raise ValueError('Invalid email state shard path')
                days[day] = json.loads((path.with_suffix('') / filename).read_text('utf-8'))
            return dict(state, version=1, days=days)
        if state.get('version') != 1:
            raise ValueError('Unsupported email daily state')
        return state
    state = {'version': 1, 'days': {}, 'processed_email_hashes': []}
    if legacy.exists():
        old = PaperStore.load(legacy)
        for key, paper in old.data['papers'].items():
            for day in paper.get('email_days', []):
                entry = state['days'].setdefault(day, {'papers': {}, 'messages': [], 'coverage': 'legacy-cache'})
                entry['papers'][key] = {name: paper.get(name) for name in FIELDS}
    # AI-era processed flags are deliberately not reused: retrieve original emails.
    return state


def add_delivery(state, delivery):
    if delivery.message_hash in state['processed_email_hashes']:
        return False
    if not delivery.papers:
        print('[warning] email contains no parsed papers; retained for a free retry')
        return False
    entry = state['days'].setdefault(delivery.received_day, {'papers': {}, 'messages': [], 'coverage': 'email'})
    if entry['coverage'] == 'legacy-cache':
        entry['papers'] = {}
    for paper in delivery.papers:
        aliases = paper.aliases()
        key = next((a for a in aliases if a.startswith('arxiv:')), '')
        key = key or next((a for a in aliases if a.startswith('doi:')), '')
        key = key or hashlib.sha256(paper.url.encode()).hexdigest()
        entry['papers'][key] = {
            'title': paper.title, 'url': paper.url, 'abstract': paper.abstract,
            'authors': paper.authors, 'categories': paper.categories, 'doi': paper.doi,
            'published': paper.published.isoformat() if paper.published else '',
        }
    entry['messages'].append(delivery.message_hash)
    entry['coverage'] = 'email'
    state['processed_email_hashes'].append(delivery.message_hash)
    return True


def day_html(entry):
    parts = [f"<p>当日邮件论文共 {len(entry['papers'])} 篇，按论文标识去重，未做筛选。</p>"]
    if entry['coverage'] == 'legacy-cache':
        parts.append('<p>历史缓存记录，尚未通过原始邮件核验完整性。</p>')
    for paper in entry['papers'].values():
        parts.append(f'<article><h2><a href="{html.escape(paper["url"], quote=True)}">'
                     f'{html.escape(paper["title"])}</a></h2>')
        parts.append('<p>' + html.escape(', '.join(paper.get('authors') or [])) + '</p>')
        parts.append('<p>' + html.escape(', '.join(paper.get('categories') or [])) + '</p>')
        parts.append('<p>' + html.escape(paper.get('abstract') or '邮件中未提供摘要。').replace('\n', '<br>') + '</p>')
        parts.append('</article>')
    return ''.join(parts)


def write_outputs(state, output, base_url):
    feed_url = base_url.rstrip('/') + '/arxiv-email-daily.xml'
    rss, channel = _rss_root(TITLE, feed_url, '按邮件接收日期归档全部论文标题、作者、摘要与链接，不使用 AI。', 'zh-CN')
    archive = output / 'arxiv-email-archive'
    archive.mkdir(parents=True, exist_ok=True)
    links = []
    for day, entry in sorted(state['days'].items(), reverse=True):
        date = dt.date.fromisoformat(day)
        body = day_html(entry)
        item = ET.SubElement(channel, 'item')
        ET.SubElement(item, 'title').text = f"{TITLE} | {day} | {len(entry['papers'])} 篇"
        ET.SubElement(item, 'link').text = f'{base_url}/arxiv-email-archive/{day}.html'
        ET.SubElement(item, 'guid', isPermaLink='false').text = f'arxiv-email-daily:{day}'
        ET.SubElement(item, 'description').text = body
        published = dt.datetime.combine(date, dt.time(), dt.timezone(dt.timedelta(hours=8)))
        ET.SubElement(item, 'pubDate').text = email.utils.format_datetime(published)
        write_changed(archive / f'{day}.html',
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{TITLE} {day}</title><style>body{{max-width:1000px;margin:24px auto;padding:0 16px;'
            'font:16px/1.7 sans-serif;overflow-wrap:anywhere}article{border-bottom:1px solid #ddd;'
            'padding:12px 0}h2{font-size:18px}</style>'
            f'<h1>{TITLE} | {day}</h1>'
            '<link rel="stylesheet" href="../assets/email-reader.css">'
            '<form id="filters" role="search"><label>搜索 <input id="search" type="search"></label>'
            '<label>类别 <select id="category"><option value="">全部类别</option></select></label>'
            '<label><input id="abstracts" type="checkbox">展开摘要</label>'
            '<output id="count" aria-live="polite"></output></form>'
            '<details id="directory"><summary>论文目录</summary><nav id="toc"></nav></details>'
            f'<main id="papers">{body}</main><script src="../assets/email-reader.js" defer></script></html>')
        write_changed(archive / f'{day}.json', json.dumps(
            {'date': day, 'coverage': entry['coverage'], 'papers': list(entry['papers'].values())},
            ensure_ascii=False, indent=2))
        links.append(f'<li><a href="{day}.html">{day} ({len(entry["papers"])} 篇)</a></li>')
    write_changed(archive / 'index.html',
        f'<!doctype html><meta charset="utf-8"><title>{TITLE}</title><h1>{TITLE}</h1><ul>'
        + ''.join(links) + '</ul>')
    write_feed(output / 'arxiv-email-daily.xml', rss)


def run(config_path, state_path, legacy_path, output, offline=False):
    config = json.loads(config_path.read_text('utf-8'))
    state = load(state_path, legacy_path)
    if not offline:
        cursor = PaperStore.empty()
        cursor.data['processed_email_hashes'] = state['processed_email_hashes']
        config['sources']['email']['since'] = config.get('email_full', {}).get('since', '2026-09-01')
        try:
            deliveries, status = fetch_arxiv_email_deliveries(cursor, config, max_emails=0)
        except Exception as error:
            record('QQ arXiv 邮件', 'failed', detail=type(error).__name__)
            raise
        accepted = 0
        for delivery in deliveries:
            if add_delivery(state, delivery):
                save(state, state_path)
                accepted += 1
        state['last_fetch_status'] = status
        state['last_run'] = dt.datetime.now(UTC).isoformat()
        record('QQ arXiv 邮件', 'ok' if status.startswith('ok:') and accepted == len(deliveries) else 'partial',
               count=sum(len(day['papers']) for day in state['days'].values()),
               identities=[f'{day}:{key}' for day,entry in state['days'].items() for key in entry['papers']],
               detail=f'新邮件 {len(deliveries)} 封，成功解析 {accepted} 封；{status}')
    save(state, state_path)
    write_outputs(state, output, config['base_url'])
    print(f"[info] unfiltered email days={len(state['days'])}; paid API calls=0")
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('research-config.json'))
    parser.add_argument('--state', type=Path, default=Path('research-data/arxiv-email-full-state.json'))
    parser.add_argument('--legacy-state', type=Path, default=Path('research-data/arxiv-email-state.json'))
    parser.add_argument('--output-dir', type=Path, default=Path('.'))
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.config, args.state, args.legacy_state, args.output_dir, args.offline))
