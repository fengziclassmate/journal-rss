"""Publish a stable, official-first union of each journal's two collectors."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import html
import xml.etree.ElementTree as ET

from journal_rss_aggregator import (
    CROSSREF_JOURNALS, OFFICIAL_FEED_URLS, FeedItem,
    child_text, filter_official_duplicates, parse_datetime,
    parse_official_feed_identity_keys,
)
from rss_ops import write_feed, write_json, write_changed
from journal_dates import NS as DATE_NS, append_metadata, describe
from rss_read_filter import ATOM_NS, DC_NS, RDF_NS, _feed_entries, _item_tokens, _local_name

BASE = 'https://fengziclassmate.github.io/journal-rss/'
STATE = 'research-data/journal-unified-identities.json'


def journal_specs(root=Path('.')):
    mirrors = json.loads((root/'official-feed-config.json').read_text('utf-8'))['mirrors']
    by_url = {row['source_url']: row for row in mirrors}
    journals = [j for j in CROSSREF_JOURNALS if j.get('current_issue_only') != 'true']
    journals += [{'source': 'JMLR', 'output': 'jmlr.xml', 'official_url': 'https://jmlr.org/jmlr.xml'},
                 {'source': 'TMLR', 'output': 'tmlr.xml', 'official_url': ''}]
    result = []
    for journal in journals:
        official = journal.get('official_url', OFFICIAL_FEED_URLS.get(journal.get('issn'), ''))
        mirror = by_url.get(official)
        if official and not mirror:
            raise ValueError('Missing publisher mirror: ' + official)
        name = mirror['name'] if mirror else journal['source']
        name = re.sub(r'^\(csu not\)\s*', '', name, flags=re.I)
        name = re.sub(r'^tdf\s+', '', name, flags=re.I)
        output = 'journal-feeds/' + journal['output']
        result.append({'name': name, 'custom': journal['output'],
                       'official': mirror['output'] if mirror else None,
                       'official_url': official, 'url': BASE + output, 'output': output,
                       'old_urls': [BASE + journal['output']] +
                       ([mirror['mirror_url'], official] if mirror else [])})
    if len({spec['output'] for spec in result}) != len(result):
        raise ValueError('Duplicate unified journal path')
    return result


def entries(path):
    if not path.exists():
        return []
    return _feed_entries(ET.parse(path).getroot(), path)[1]


def normalize(node, source):
    """Convert RSS 1/2 and Atom to RSS 2 while retaining publisher extensions."""
    item = ET.Element('item')
    atom = _local_name(node.tag) == 'entry'
    if not atom:
        for child in node:
            value = copy.deepcopy(child)
            if value.tag.startswith('{http://purl.org/rss/1.0/}'):
                value.tag = _local_name(value.tag)
            if _local_name(value.tag) != 'source':
                item.append(value)
    else:
        for field in ('title',):
            ET.SubElement(item, field).text = child_text(node, field)
        link = next((c.get('href') for c in node if _local_name(c.tag) == 'link'
                     and c.get('rel', 'alternate') == 'alternate'), '')
        ET.SubElement(item, 'link').text = link
        ET.SubElement(item, 'guid', isPermaLink='false').text = child_text(node, 'id') or link
        body = next((c for c in node if _local_name(c.tag) == 'content'), None)
        if body is None:
            body = next((c for c in node if _local_name(c.tag) == 'summary'), None)
        if body is not None:
            ET.SubElement(item, 'description').text = (''.join(ET.tostring(c, encoding='unicode') for c in body)
                                                       if body.get('type') == 'xhtml' else ''.join(body.itertext()))
        published = child_text(node, 'published')
        if published:
            ET.SubElement(item, '{'+DC_NS+'}date').text = published
        for child in node:
            if _local_name(child.tag) == 'author':
                ET.SubElement(item, '{'+DC_NS+'}creator').text = child_text(child, 'name')
            elif not child.tag.startswith('{'+ATOM_NS+'}'):
                item.append(copy.deepcopy(child))
        updated = child_text(node, 'updated')
        if updated:
            ET.SubElement(item, '{'+BASE+'ns/dates/1}metadata_updated').text = updated
    if not child_text(item, 'guid'):
        ET.SubElement(item, 'guid', isPermaLink='false').text = node.get('{'+RDF_NS+'}about') or child_text(item, 'link')
    for child in list(item):
        value = (child.text or '').strip()
        if _local_name(child.tag) in ('date', 'pubdate') and re.fullmatch(r'\d{4}(?:-\d{2})?', value):
            item.remove(child)
            dates = {'publication': value, 'publication_source': 'publisher-feed'}
            if item.find('{'+DATE_NS+'}publication') is None:
                append_metadata(item, dates)
            body = item.find('description')
            if body is None:
                body = ET.SubElement(item, 'description')
            body.text = describe(body.text or '', dates)
    ET.SubElement(item, 'source', url=source).text = source
    return item


def identities(node):
    return {token for token in _item_tokens(node) if not token.startswith('title:')}


def matching(node, evidence):
    if not evidence:
        return False
    item = FeedItem(source='', title=child_text(node, 'title'), link=child_text(node, 'link'),
                    guid=child_text(node, 'guid'), description=child_text(node, 'description', 'encoded'))
    # RSS dc:creator and generated first-line authors share the established matcher.
    authors = ', '.join(child_text(c, 'name') or ''.join(c.itertext()) for c in node
                        if _local_name(c.tag) in ('creator', 'author'))
    if authors:
        item.description = '<p>Author(s): ' + html.escape(authors) + '</p>' + item.description
    return not filter_official_duplicates([item], evidence, official_url='same-journal')


def evidence_for(node):
    rss = ET.Element('rss')
    ET.SubElement(rss, 'channel').append(copy.deepcopy(node))
    return parse_official_feed_identity_keys(ET.tostring(rss))


def stable_guid(spec, tokens):
    primary = next((sorted(t for t in tokens if t.startswith(prefix))
                    for prefix in ('doi:', 'ieee-document:', 'elsevier-pii:', 'jmlr-paper:', 'url:', 'guid:')
                    if any(t.startswith(prefix) for t in tokens)), [])
    if not primary:
        raise ValueError('Cannot publish a journal item without an identity')
    digest = hashlib.sha256(primary[0].encode()).hexdigest()[:32]
    return 'unified:' + Path(spec['custom']).stem + ':' + digest


def union(spec, groups, aliases):
    selected = []
    by_token = {}
    by_guid = {}
    evidence = set()
    for nodes in groups:
        for node in nodes:
            tokens = identities(node)
            existing = next((by_token[token] for token in sorted(tokens) if token in by_token), None)
            if existing is None and matching(node, evidence):
                existing = next((row for row in selected if matching(node, evidence_for(row))), None)
            if existing is None:
                guid = next((aliases[token] for token in sorted(tokens) if token in aliases), None)
                guid = guid or stable_guid(spec, tokens)
                existing = by_guid.get(guid)
            if existing is None:
                for old in list(node):
                    if _local_name(old.tag) == 'guid':
                        node.remove(old)
                ET.SubElement(node, 'guid', isPermaLink='false').text = guid
                selected.append(node)
                existing = node
            guid = child_text(existing, 'guid')
            by_guid[guid] = existing
            for token in tokens | identities(existing):
                aliases[token] = guid
                by_token[token] = existing
            evidence.update(evidence_for(node))
    # Keep both collectors' identities available to the private read filter.
    alias_tag = '{'+BASE+'ns/unified/1}identity'
    by_alias = {}
    for token, guid in aliases.items():
        by_alias.setdefault(guid, set()).add(token)
    for node in selected:
        for child in list(node):
            if child.tag == alias_tag:
                node.remove(child)
        for token in sorted(by_alias.get(child_text(node, 'guid'), [])):
            ET.SubElement(node, alias_tag).text = token
    def sort_key(node):
        value = child_text(node, 'publication', 'pubDate', 'date')
        if not value:
            value = child_text(node, 'first_seen')
        parsed = parse_datetime(value)
        return parsed.timestamp() if parsed else float('-inf')
    return sorted(selected, key=sort_key, reverse=True)


def build(root=Path('.')):
    state_path = root/STATE
    state = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {}
    report = []
    specs = journal_specs(root)
    for spec in specs:
        aliases = state.setdefault(spec['output'], {})
        output = root/spec['output']
        # Seed historical GUIDs before preferring refreshed official metadata.
        previous = entries(output)
        for node in previous:
            for token in identities(node):
                aliases.setdefault(token, child_text(node, 'guid'))
        official = [normalize(n, spec['official_url']) for n in entries(root/spec['official'])] if spec['official'] else []
        custom = [normalize(n, BASE+spec['custom']) for n in entries(root/spec['custom'])]
        merged = union(spec, [official, custom, previous], aliases)
        rss = ET.Element('rss', version='2.0')
        channel = ET.SubElement(rss, 'channel')
        for key, value in [('title', spec['name']), ('link', spec['url']),
                           ('description', 'Official journal feed and custom collection, deduplicated. Publisher metadata has priority.'),
                           ('language', 'en')]:
            ET.SubElement(channel, key).text = value
        channel.extend(merged)
        write_feed(output, rss)
        report.append({'name': spec['name'], 'official': len(official), 'custom': len(custom),
                       'previous': len(previous), 'total': len(merged)})
    write_json(state_path, state)
    opml = ET.Element('opml', version='2.0')
    ET.SubElement(ET.SubElement(opml, 'head'), 'title').text = 'Unified journal RSS'
    body = ET.SubElement(opml, 'body')
    for spec in specs:
        ET.SubElement(body, 'outline', type='rss', text=spec['name'], title=spec['name'], xmlUrl=spec['url'])
    write_changed(root/'journals.opml', ET.tostring(opml, encoding='utf-8', xml_declaration=True))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(json.dumps(build(args.root), ensure_ascii=False, indent=2))
