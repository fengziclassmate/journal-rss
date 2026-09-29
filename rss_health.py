"""Persist collection evidence separately from filtering and publication."""
import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from rss_ops import write_json, write_changed, write_feed

BASE = 'https://fengziclassmate.github.io/journal-rss'


def record(source, status, count=None, *, identities=None, detail='', root=None):
    folder = root or os.environ.get('RSS_HEALTH_DIR')
    if not folder:
        return
    path = Path(folder) / (hashlib.sha256(source.encode()).hexdigest()[:20] + '.json')
    old = json.loads(path.read_text('utf-8')) if path.exists() else {}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    value = dict(old, source=source, status=status, last_attempt=now, detail=detail)
    if count is not None:
        value['count'] = count
    if status == 'ok':
        value['last_success'] = now
        value['drop_warning'] = count is not None and old.get('successful_count', 0) >= 20 and count < old['successful_count'] * .5
        if count is not None:
            value['successful_count'] = count
        if identities is not None:
            hashes = sorted({hashlib.sha256(str(key).encode()).hexdigest() for key in identities})
            value['new_count'] = len(set(hashes) - set(old['identities'])) if 'identities' in old else None
            value['identities'] = sorted(set(hashes) | set(old.get('identities', [])))
    else:
        value['new_count'] = None
    write_json(path, value)


def render(root=Path('.')):
    root = Path(root)
    now = dt.datetime.now(dt.timezone.utc)
    day = now.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    states = [json.loads(p.read_text('utf-8')) for p in sorted((root/'rss-health-state').glob('*.json'))]
    if not states:
        initial = {'source':'监控初始化','status':'unverified','last_attempt':None,
                   'detail':'等待首次采集；既有文件不等于本次采集成功'}
        write_json(root/'rss-health-state/initial.json',initial)
        states = [initial]
    elif len(states)>1:
        states = [item for item in states if item['source']!='监控初始化']
    lines = []
    for item in sorted(states, key=lambda x: x['source']):
        status = {'ok': '成功', 'failed': '失败，未确认新数据', 'preserved': '采集失败，保留旧数据',
                  'fallback': '降级来源', 'partial': '部分失败', 'running': '运行未完成', 'unverified': '完整性待核验'}.get(item['status'], item['status'])
        if item.get('drop_warning'):
            status += '；数量下降超过 50%'
        last = item.get('last_success')
        if last and (now - dt.datetime.fromisoformat(last)).total_seconds() > 72*3600:
            status += '；超过 72 小时未确认成功'
        new = item.get('new_count')
        label = '新增数未核验' if new is None else f'新增 {new}'
        lines.append('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in (
            item['source'], status, item.get('count', '-'), label,
            item.get('last_attempt') or '尚无记录', last or '尚无记录', item.get('detail', ''))) + '</tr>')
    body = '<table><thead><tr><th>来源</th><th>状态</th><th>采集数</th><th>变化</th><th>最近尝试 UTC</th><th>最近成功 UTC</th><th>详情</th></tr></thead><tbody>' + ''.join(lines) + '</tbody></table>'
    if not states:
        body = '<p>尚无采集证据；发布成功不代表采集成功。</p>'
    directory = root/'rss-health'
    write_json(directory/f'{day}.json', {'date':day, 'sources':[{k:v for k,v in row.items() if k != 'identities'} for row in states]})
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RSS 更新健康日报</title><style>body{font:15px/1.6 system-ui;margin:24px}td,th{padding:8px;text-align:left;border-bottom:1px solid #ccc}main{overflow:auto}</style><h1>RSS 更新健康日报</h1><main>' + body + '</main></html>'
    write_changed(directory/f'{day}.html', page)
    write_changed(directory/'index.html', page)
    rss=ET.Element('rss',version='2.0'); channel=ET.SubElement(rss,'channel')
    for tag,text in [('title','RSS 更新健康日报'),('link',BASE+'/rss-health/'),('description','采集状态与异常，不调用 AI')]:
        ET.SubElement(channel,tag).text=text
    for path in sorted(directory.glob('????-??-??.json'),reverse=True)[:60]:
        entry=json.loads(path.read_text('utf-8')); item=ET.SubElement(channel,'item')
        ET.SubElement(item,'title').text='RSS 更新健康日报 | '+entry['date']
        ET.SubElement(item,'guid',isPermaLink='false').text='rss-health:'+entry['date']
        ET.SubElement(item,'link').text=BASE+'/rss-health/'+entry['date']+'.html'
        ET.SubElement(item,'description').text=body if path.stem==day else '查看当日采集状态与异常记录。'
        ET.SubElement(item,'pubDate').text=email.utils.format_datetime(dt.datetime.fromisoformat(entry['date']).replace(tzinfo=dt.timezone(dt.timedelta(hours=8))))
    write_feed(root/'rss-health.xml',rss)


def guard_sizes(root):
    paths = list(root.glob('*.xml')) + list(root.glob('research-data/**/*.json')) + list(root.glob('*.zip'))
    oversized=[]
    warnings=[]
    for path in paths:
        size=path.stat().st_size
        if size >= 50*1024*1024:
            warnings.append(f'{path.name}: {size/1024/1024:.1f} MiB')
            print(f'::warning file={path}::File is {size/1024/1024:.1f} MiB; split storage before 100 MiB')
        if size >= 99*1024*1024:
            oversized.append(str(path))
    record('文件体积', 'partial' if warnings else 'ok', detail='；'.join(warnings) or '已检查：没有单文件超过 50 MiB',root=root/'rss-health-state')
    if oversized:
        raise RuntimeError('Refusing an oversized Git commit: '+', '.join(oversized))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['render','guard','run'])
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if args.action=='render': render()
    elif args.action=='guard': guard_sizes(Path('.'))
    else:
        group,*command=args.command
        if command and command[0]=='--': command.pop(0)
        os.environ['RSS_HEALTH_DIR']='rss-health-state'
        record('任务:'+group,'running')
        result=subprocess.run(command)
        record('任务:'+group,'ok' if result.returncode==0 else 'failed',detail='仅表示进程退出状态，具体来源见各条记录')
        sys.exit(result.returncode)
