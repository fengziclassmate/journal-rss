"""Complete venue subscriptions for five sources absent from the existing feeds."""
import argparse
import datetime as dt
import email.utils
import html
import json
from pathlib import Path
import re
import sys
import urllib.error
from urllib.parse import urlencode, urljoin, urlsplit, unquote
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from journal_dates import append_metadata, crossref_dates, describe
from official_feed_proxy import fetch_bytes
from rss_health import record
from rss_ops import write_feed, write_json

BASE='https://fengziclassmate.github.io/journal-rss/'
SOURCES=[
    dict(slug='colm',name='Conf COLM',kind='colm',url='https://proceedings.colm.cc/',output='conference-feeds/colm.xml'),
    dict(slug='aamas',name='Conf AAMAS',kind='aamas',url='https://ifaamas.org/Proceedings/',output='conference-feeds/aamas.xml'),
    dict(slug='tacl',name='TACL',kind='tacl',url='https://aclanthology.org/venues/tacl/',output='journal-feeds/tacl.xml'),
    dict(slug='jaamas',name='JAAMAS',kind='crossref',issn='1387-2532',url='https://link.springer.com/journal/10458',output='journal-feeds/jaamas.xml'),
    dict(slug='kbs',name='Knowledge-Based Systems',kind='crossref',issn='0950-7051',url='https://www.sciencedirect.com/journal/knowledge-based-systems',output='journal-feeds/knowledge-based-systems.xml'),
]
STATE=Path('research-data/additional-venues-state.json')


def text(value):
    return ' '.join(value.split())


def download(url):
    return fetch_bytes(url,attempts=2,timeout=30)


def parse_colm(raw,url,year):
    soup=BeautifulSoup(raw,'html.parser')
    rows=[]
    for node in soup.select('.paper-content'):
        link=node.select_one('a[href*="-Abstract-"]')
        if link is None:continue
        authors=node.select_one('.paper-authors')
        paper_url=urljoin(url,link['href'])
        rows.append(dict(id=paper_url,title=text(link.get_text()),url=paper_url,
                         authors=[text(authors.get_text())] if authors else [],abstract='',doi='',
                         dates={'publication':str(year),'publication_source':'official proceedings'}))
    if not rows:raise ValueError('COLM proceedings markup contains no paper records')
    return rows


def collect_colm(source):
    root=BeautifulSoup(download(source['url']),'html.parser')
    volumes={urljoin(source['url'],a['href']):int(match.group(1))
             for a in root.select('a[href]')
             if (match:=re.search(r'/paper_files/paper/(20\d{2})/?$',a['href'])) and int(match.group(1))>=2025}
    if not volumes:raise ValueError('COLM proceedings index contains no supported volumes')
    return [row for url,year in sorted(volumes.items()) for row in parse_colm(download(url),url,year)]


def parse_aamas(raw,url,year):
    soup=BeautifulSoup(raw,'html.parser')
    labels={'researchpapertrack','researchpapers','fullpapers','maintrack'}
    sections={unquote(urlsplit(a['href']).fragment)
              for a in soup.select('a[href^="#"]') if urlsplit(a['href']).fragment}
    toc=next((a for a in soup.select('a[href^="#"]')
              if re.sub(r'\W','',a.get_text()).lower() in labels),None)
    if toc is None:raise ValueError('AAMAS main research track not found')
    section=unquote(urlsplit(toc['href']).fragment)
    marker=soup.find('a',attrs={'name':section})
    if marker is None:raise ValueError('AAMAS research section anchor not found')
    rows=[]
    for a in marker.find_all_next('a'):
        if (a.get('name') in sections or a.get('id') in sections):break
        href=a.get('href','')
        if not href.lower().endswith('.pdf'):continue
        paper_url=urljoin(url,href)
        author_font=a.parent.select_one('font[face="Times New Roman"]')
        authors=[]
        if author_font is not None:
            for affiliation in author_font.select('i'):affiliation.decompose()
            authors=[text(name) for name in author_font.stripped_strings if text(name)]
        rows.append(dict(id=paper_url,title=text(a.get_text()),url=paper_url,authors=authors,
                         abstract='',doi='',dates={'publication':str(year),'publication_source':'official main research track'}))
    if not rows:raise ValueError('AAMAS main track contains no paper records')
    return rows


def collect_aamas(source):
    rows=[]
    for year in range(2025,dt.datetime.now(dt.timezone.utc).year+1):
        url=source['url']+f'aamas{year}/forms/contents.htm'
        try:raw=download(url)
        except urllib.error.HTTPError as error:
            if error.code!=404:raise
            print(f'AAMAS {year}: official proceedings not available (HTTP 404)',file=sys.stderr)
            continue
        rows.extend(parse_aamas(raw,url,year))
    if not rows:raise ValueError('No AAMAS proceedings available')
    return rows


def parse_tacl(raw):
    root=ET.fromstring(raw)
    rows=[]
    for volume in root.findall('volume'):
        year=volume.findtext('meta/year')
        for paper in volume.findall('paper'):
            if paper.get('id')=='0':continue
            ident=f"{root.attrib['id']}-{volume.attrib['id']}.{paper.attrib['id']}"
            title=''.join(paper.find('title').itertext())
            abstract=paper.find('abstract')
            rows.append(dict(id=ident,title=text(title),url=f'https://aclanthology.org/{ident}/',
                             authors=[text(' '.join(author.itertext())) for author in paper.findall('author')],
                             abstract=text(''.join(abstract.itertext())) if abstract is not None else '',
                             doi=paper.findtext('doi','').lower(),
                             dates={'publication':year,'publication_source':'ACL Anthology volume'}))
    if not rows:raise ValueError('TACL volume contains no paper records')
    return rows


def collect_tacl(source):
    rows=[]
    for year in range(2025,dt.datetime.now(dt.timezone.utc).year+1):
        url=f'https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{year}.tacl.xml'
        try:raw=download(url)
        except urllib.error.HTTPError as error:
            if error.code!=404:raise
            print(f'TACL {year}: official volume not available (HTTP 404)',file=sys.stderr)
            continue
        rows.extend(parse_tacl(raw))
    if not rows:raise ValueError('No TACL volumes available')
    return rows


def collect_crossref(source):
    rows={}
    cursor='*'
    seen=set()
    total=None
    while True:
        query=urlencode({'filter':'from-pub-date:2025-01-01,type:journal-article',
                         'rows':1000,'cursor':cursor,
                         'select':'DOI,title,URL,author,abstract,published,published-online,published-print,created'})
        url=f"https://api.crossref.org/journals/{source['issn']}/works?{query}"
        payload=json.loads(download(url))
        if payload['status']!='ok':raise ValueError('Crossref returned an error response')
        message=payload['message']
        if total is None:total=message['total-results']
        old_count=len(rows)
        for work in message['items']:
            ident=work['DOI'].lower()
            title=work['title'][0]
            rows[ident]=dict(id=ident,title=text(BeautifulSoup(title,'html.parser').get_text()),
                            url=work['URL'],doi=ident,
                            authors=[' '.join(filter(None,[a.get('given',''),a.get('family','')])) for a in work.get('author',[])],
                            abstract=text(BeautifulSoup(work.get('abstract',''),'html.parser').get_text()),
                            dates=crossref_dates(work))
        if len(rows)>=total:return list(rows.values())
        seen.add(cursor)
        cursor=message['next-cursor']
        if len(rows)==old_count or cursor in seen:raise ValueError('Crossref pagination did not advance')


def build_feed(source,rows,root):
    rss=ET.Element('rss',version='2.0');channel=ET.SubElement(rss,'channel')
    for key,value in [('title',source['name']),('link',source['url']),('language','en'),
                      ('description','Complete source records since 2025, without topic filtering. Official main research tracks are used for conferences; publication precision is preserved.')]:
        ET.SubElement(channel,key).text=value
    for ident,row in sorted(rows.items(),key=lambda pair:(pair[1]['dates'].get('publication',''),pair[1]['first_seen']),reverse=True):
        node=ET.SubElement(channel,'item')
        ET.SubElement(node,'title').text=row['title']
        ET.SubElement(node,'link').text=row['url']
        ET.SubElement(node,'guid',isPermaLink='false').text='venue:'+source['slug']+':'+ident
        for name in row['authors']:ET.SubElement(node,'{http://purl.org/dc/elements/1.1/}creator').text=name
        if row['doi']:ET.SubElement(node,'{http://prismstandard.org/namespaces/basic/2.0/}doi').text=row['doi']
        ET.SubElement(node,'category').text=source['name']
        dates=dict(row['dates'],first_seen=row['first_seen'])
        body='<p>Authors: '+html.escape(', '.join(row['authors']))+'</p>'
        if row['abstract']:body+='<p>'+html.escape(row['abstract'])+'</p>'
        ET.SubElement(node,'description').text=describe(body,dates)
        append_metadata(node,dates)
        publication=dates.get('publication','')
        if len(publication)==10:
            ET.SubElement(node,'pubDate').text=email.utils.format_datetime(dt.datetime.fromisoformat(publication).replace(tzinfo=dt.timezone.utc))
        ET.SubElement(node,'source',url=source['url']).text=source['name']
    write_feed(root/source['output'],rss)


def run(root,cached=False):
    state_path=root/STATE
    state=json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
    failures=[]
    if not cached:
        collectors={'colm':collect_colm,'aamas':collect_aamas,'tacl':collect_tacl,'crossref':collect_crossref}
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        for source in SOURCES:
            try:
                fresh=collectors[source['kind']](source)
                selected=state.setdefault(source['slug'],{})
                for row in fresh:
                    first_seen=selected.get(row['id'],{}).get('first_seen',now)
                    selected[row['id']]=dict(row,first_seen=first_seen)
                write_json(state_path,state)
                record(source['output'],'ok',len(selected),identities=selected,
                       detail=f"Complete venue collection without topic filtering; checked {len(fresh)} source records")
            except (OSError,ValueError,ET.ParseError) as error:
                record(source['output'],'failed',detail=str(error))
                print(f"{source['name']}: {type(error).__name__}: {error}",file=sys.stderr)
                failures.append(source['name'])
    for source in SOURCES:
        if source['slug'] in state:
            build_feed(source,state[source['slug']],root)
            print(f"{source['name']}: {len(state[source['slug']])} papers")
        elif cached:raise ValueError('Missing saved records: '+source['slug'])
    if failures:raise RuntimeError('Source collection failed; existing records retained: '+', '.join(failures))
    return {source['slug']:len(state[source['slug']]) for source in SOURCES}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('.'))
    parser.add_argument('--cached',action='store_true')
    args=parser.parse_args()
    run(args.root,args.cached)
