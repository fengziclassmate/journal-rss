"""Build public operational pages, without exporting personal reading records."""
import argparse
import html
import json
from pathlib import Path
import io
import zipfile
import xml.etree.ElementTree as ET

from rss_ops import write_changed, write_json


def archive_conferences(root=Path('.')):
    paths=sorted((root/'conference-feeds').glob('*'))+[root/'conference-feeds.opml']
    if not (root/'conference-feeds.opml').exists():
        raise RuntimeError('Missing conference OPML')
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in paths:
            if path.is_file():
                info=zipfile.ZipInfo(path.relative_to(root).as_posix(),date_time=(2020,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(info,path.read_bytes())
    write_changed(root/'conference-feed-fallback.zip',stream.getvalue())


def duplicate_page(root=Path('.')):
    directory=root/'duplicate-audit'
    if not list(directory.glob('*.json')):
        write_json(directory/'initial.json',[])
    rows=[]
    for path in sorted(directory.glob('*.json')):
        for record in json.loads(path.read_text('utf-8')):
            action='已隐藏：官网已有相同标识' if record['action']=='hidden' else '仅标题相似：保留待核对'
            rows.append('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in (
                path.stem, record['title'], action, ', '.join(record['matched']),record['official_source'],record['url']))+'</tr>')
    content='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RSS 去重记录</title>
    <style>body{font:15px/1.6 system-ui;margin:24px}h1{font-size:24px}input{font:inherit;max-width:100%;width:400px;padding:8px}table{border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #ccc;overflow-wrap:anywhere}main{overflow:auto}tr[hidden]{display:none}</style>
    <h1>RSS 去重记录</h1><label>搜索 <input type="search" id="search"></label><output id="count"></output><main><table><thead><tr><th>自建源</th><th>论文</th><th>处理结果</th><th>匹配依据</th><th>官方来源</th><th>论文链接</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></main>
    <script>const rows=[...document.querySelectorAll('tbody tr')];const search=document.querySelector('#search');function filter(){let n=0;for(const row of rows){row.hidden=!row.textContent.toLowerCase().includes(search.value.trim().toLowerCase());if(!row.hidden)n++;}document.querySelector('#count').textContent=' '+n+' / '+rows.length;}search.addEventListener('input',filter);filter();</script></html>'''
    write_changed(directory/'index.html',content)


def share_publishers(root=Path('.')):
    specs=json.loads((root/'official-feed-config.json').read_text('utf-8'))['mirrors']
    opml=ET.Element('opml',version='2.0')
    ET.SubElement(ET.SubElement(opml,'head'),'title').text='Publisher original RSS sources'
    body=ET.SubElement(opml,'body')
    for spec in specs:
        ET.SubElement(body,'outline',type='rss',text=spec['name'],title=spec['name'],xmlUrl=spec['source_url'])
    write_changed(root/'official-sources.opml',ET.tostring(opml,encoding='utf-8',xml_declaration=True))
    write_changed(root/'sharing.html','''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RSS 订阅分享</title>
    <style>body{max-width:850px;margin:32px auto;padding:0 16px;font:16px/1.7 system-ui}h1{font-size:26px}li{margin:12px 0}a{color:#1555a0}</style>
    <h1>RSS 订阅分享</h1><p><a href="official-sources.opml">下载官方源订阅清单 OPML</a></p>
    <p>清单直接指向出版商，不包含作者个人的已读排除记录。部分官方源可能停更，不能保证与官网文章同步。</p>
    <h2>独立使用自建源</h2><ol><li>Fork 项目，在自己的仓库开启 Pages 和 Actions，将仓库名、配置及代码中的 Pages 地址替换为自己的地址。</li>
    <li>不要继承原作者的 read-suppression.json 内容。在自己的副本中清空它，生成独立的 HMAC 密钥并配置 RSS_READ_FILTER_KEY；本地阅读记录使用同一把新密钥。</li>
    <li>配置自己的 Zotero 数据路径。不要复制别人的数据库备份、私人文库、邮箱授权码或阅读历史。</li>
    <li>需要邮箱日报时，配置自己的 ARXIV_EMAIL_ADDRESS 和 ARXIV_EMAIL_AUTH_CODE。邮箱抓取不调用付费模型。</li>
    <li>独立生成并发布，再导入自己发布的订阅地址。不要把原作者的个人过滤地址当作全量公共源。</li></ol>
    <p><a href="https://github.com/fengziclassmate/journal-rss">项目代码</a></p></html>''')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['conferences','duplicates','sharing'])
    args=parser.parse_args()
    {'conferences':archive_conferences,'duplicates':duplicate_page,'sharing':share_publishers}[args.action]()
