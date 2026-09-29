"""Repair existing daily feed items through Zotero, without changing GUID/read state."""
import argparse
import json
import sqlite3
from pathlib import Path

from arxiv_email_daily import TITLE, day_html, load
from zotero_subscribe_conferences import ZoteroDebugger

FEED = 'https://fengziclassmate.github.io/journal-rss/arxiv-email-daily.xml'


def prepare(database, state_path, output):
    state = load(state_path, Path('unused-legacy-state.json'))
    c = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)
    try:
        rows = c.execute('''SELECT i.itemID, i.libraryID, fi.guid, fi.readTime
                            FROM items i JOIN feedItems fi USING(itemID)
                            JOIN feeds f USING(libraryID) WHERE f.url=?''', (FEED,)).fetchall()
    finally:
        c.close()
    updates, missing = [], []
    for item_id, library_id, guid, read_time in rows:
        if not guid.startswith('arxiv-email-daily:'):
            continue
        day = guid.split(':', 1)[1]
        if day not in state['days']:
            missing.append(day)
            continue
        entry = state['days'][day]
        updates.append(dict(id=item_id, library=library_id, guid=guid, readTime=read_time,
                            title=f"{TITLE} | {day} | {len(entry['papers'])} 篇",
                            url=f'{FEED.rsplit("/", 1)[0]}/arxiv-email-archive/{day}.html',
                            abstract=day_html(entry)))
    output.write_text(json.dumps(updates, ensure_ascii=False), 'utf-8')
    print(json.dumps({'planned': len(updates), 'missing_days': missing}))


def apply(payload):
    debugger = ZoteroDebugger()
    try:
        script = '''(async () => {
            const changes = await IOUtils.readJSON(PATH);
            const result = [];
            await Zotero.DB.executeTransaction(async () => {
                for (const change of changes) {
                    const item = await Zotero.Items.getAsync(change.id);
                    if (item.libraryID !== change.library || item.guid !== change.guid) {
                        throw new Error('Item identity changed; refusing to modify');
                    }
                    const feedURL = await Zotero.DB.valueQueryAsync(
                        'SELECT url FROM feeds WHERE libraryID=?', [item.libraryID]);
                    if (feedURL !== FEED) throw new Error('Wrong feed');
                    const before = await Zotero.DB.valueQueryAsync(
                        'SELECT readTime FROM feedItems WHERE itemID=?', [item.id]);
                    item.setField('title', change.title);
                    item.setField('url', change.url);
                    item.setField('abstractNote', change.abstract);
                    await item.save({skipEditCheck: true});
                    const after = await Zotero.DB.valueQueryAsync(
                        'SELECT readTime FROM feedItems WHERE itemID=?', [item.id]);
                    if (before !== after) throw new Error('Read state unexpectedly changed');
                    result.push({id: item.id, title: item.getField('title'), readTime: after});
                }
            });
            return JSON.stringify({updated: result.length, items: result});
        })()'''.replace('PATH', json.dumps(str(payload.resolve()))).replace('FEED', json.dumps(FEED))
        result = debugger.evaluate(script)
        payload.with_suffix('.result.json').write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
        print(json.dumps({'updated': result['updated']}))
    finally:
        debugger.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply(args.payload)
    else:
        prepare(args.database.resolve(), args.state, args.payload)
