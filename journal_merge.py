"""Publish one canonical custom feed per journal, including its current issue."""
import argparse
import datetime as dt
from pathlib import Path

from journal_rss_aggregator import (
    CROSSREF_JOURNALS, UTC, doi_identities, read_existing_feed_items, write_rss,
)


def merge_items(*groups):
    seen = set()
    result = []
    for group in groups:
        for item in group:
            keys = doi_identities(item.guid, item.link)
            keys.update('id:' + value.strip().lower() for value in (item.guid, item.link) if value)
            if keys & seen:
                seen.update(keys)
                continue
            seen.update(keys)
            result.append(item)
    return sorted(result, key=lambda item: item.published or dt.datetime(1900, 1, 1, tzinfo=UTC), reverse=True)


def journal_pairs():
    main = {j['issn']: j for j in CROSSREF_JOURNALS if j.get('current_issue_only') != 'true'}
    return [(main[j['issn']], j) for j in CROSSREF_JOURNALS if j.get('current_issue_only') == 'true']


def merge_all(root):
    results = []
    for main, current in journal_pairs():
        path = root / main['output']
        if not path.exists():
            raise RuntimeError(f'Missing canonical feed: {path}')
        original = read_existing_feed_items(path)
        merged = merge_items(original, read_existing_feed_items(root / current['output']))
        title = main['feed_title'].replace(' Early Access', '')
        write_rss(merged, path, feed_title=title, feed_link=main['feed_link'],
                  feed_description='Journal updates and current formal issue articles. Publisher duplicates and previously read items are filtered before publication.',
                  max_items=max(1, len(merged)), prefix_item_titles=False, feed_language='en')
        results.append({'feed':main['output'], 'added':len(merged)-len(original), 'total':len(merged)})
    return results


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(json.dumps(merge_all(args.root), indent=2))
