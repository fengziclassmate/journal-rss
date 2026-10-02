"""Apply publisher priority to every journal XML before every deployment."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from journal_rss_aggregator import (CROSSREF_JOURNALS, OFFICIAL_FEED_URLS, FeedItem,
    child_text, load_official_mirror_paths, load_official_seen, save_official_seen,
    parse_official_feed_identity_keys, filter_official_duplicates)
from rss_read_filter import _feed_entries
from rss_ops import write_feed, write_json


def reconcile(root):
    history_path = root/'official-feed-seen.json'
    history = load_official_seen(history_path)
    mirrors = load_official_mirror_paths(root/'official-feed-config.json')
    for url, path in mirrors.items():
        if path.exists():
            history.setdefault(url, set()).update(parse_official_feed_identity_keys(path.read_bytes()))
    save_official_seen(history_path, history)
    results = []
    journals = [*CROSSREF_JOURNALS, {'source': 'JMLR', 'output': 'jmlr.xml',
                                  'official_url': 'https://jmlr.org/jmlr.xml'}]
    for journal in journals:
        path = root/journal['output']
        if not path.exists():
            continue
        url = journal.get('official_url') or OFFICIAL_FEED_URLS[journal['issn']]
        tree = ET.parse(path)
        parent, entries = _feed_entries(tree.getroot(), path)
        items = [FeedItem(source=journal['source'], title=child_text(node,'title'),
                         link=child_text(node,'link'), guid=child_text(node,'guid','id'),
                         description=child_text(node,'description','encoded')) for node in entries]
        audit = []
        kept = filter_official_duplicates(items, history.get(url,set()), audit, url)
        keep_ids = {id(item) for item in kept}
        for node, item in zip(entries, items):
            if id(item) not in keep_ids:
                parent.remove(node)
        removed = len(items)-len(kept)
        if removed:
            write_feed(path, tree.getroot())
        # Keep previous removal evidence when an already-filtered feed is republished.
        audit_path = root/'duplicate-audit'/f"{journal['output']}.json"
        old = json.loads(audit_path.read_text('utf-8')) if audit_path.exists() else []
        merged = {row['guid']: row for row in old}
        merged.update({row['guid']: row for row in audit})
        write_json(audit_path, list(merged.values()))
        results.append({'feed':journal['output'], 'removed':removed, 'kept':len(kept)})
    return results


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('.'))
    args=parser.parse_args()
    print(json.dumps(reconcile(args.root),ensure_ascii=False,indent=2))
