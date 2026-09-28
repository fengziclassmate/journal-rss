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


TITLE = 'QQ 邮箱 arXiv 邮件全文日报'
FIELDS = ('title', 'url', 'abstract', 'authors', 'categories', 'doi', 'published')


def save(state, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(path)


def load(path, legacy):
    if path.exists():
        state = json.loads(path.read_text('utf-8'))
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
        (archive / f'{day}.html').write_text(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{TITLE} {day}</title><style>body{{max-width:1000px;margin:24px auto;padding:0 16px;'
            'font:16px/1.7 sans-serif;overflow-wrap:anywhere}article{border-bottom:1px solid #ddd;'
            'padding:12px 0}h2{font-size:18px}</style>'
            f'<h1>{TITLE} | {day}</h1>{body}</html>', 'utf-8')
        (archive / f'{day}.json').write_text(json.dumps(
            {'date': day, 'coverage': entry['coverage'], 'papers': list(entry['papers'].values())},
            ensure_ascii=False, indent=2), 'utf-8')
        links.append(f'<li><a href="{day}.html">{day} ({len(entry["papers"])} 篇)</a></li>')
    (archive / 'index.html').write_text(
        f'<!doctype html><meta charset="utf-8"><title>{TITLE}</title><h1>{TITLE}</h1><ul>'
        + ''.join(links) + '</ul>', 'utf-8')
    _write_xml(rss, output / 'arxiv-email-daily.xml')


def run(config_path, state_path, legacy_path, output, offline=False):
    config = json.loads(config_path.read_text('utf-8'))
    state = load(state_path, legacy_path)
    if not offline:
        cursor = PaperStore.empty()
        cursor.data['processed_email_hashes'] = state['processed_email_hashes']
        config['sources']['email']['since'] = config.get('email_full', {}).get('since', '2026-09-01')
        deliveries, status = fetch_arxiv_email_deliveries(cursor, config, max_emails=0)
        for delivery in deliveries:
            if add_delivery(state, delivery):
                save(state, state_path)
        state['last_fetch_status'] = status
        state['last_run'] = dt.datetime.now(UTC).isoformat()
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
