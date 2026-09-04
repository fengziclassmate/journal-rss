"""Repair year-only feed dates while Zotero is closed, with a SQLite backup.

Only stored date references are changed, and only where the online source gives
a bare year for the same GUID in the same library. Default is a dry run.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import re
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from zotero_subscribe_conferences import feed_specs


def require_zotero_closed():
    result = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq zotero.exe', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True, check=True,
    )
    if any(row and row[0].lower() == 'zotero.exe' for row in csv.reader(io.StringIO(result.stdout))):
        raise RuntimeError('Close Zotero before running the offline date repair')


def feed_state_digest(connection):
    digest = hashlib.sha256()
    for row in connection.execute('SELECT itemID,guid,readTime,translatedTime FROM feedItems ORDER BY itemID'):
        digest.update(repr(row).encode('utf-8'))
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require_zotero_closed()
    plan = []
    with requests.Session() as session:
        for spec in feed_specs():
            response = session.get(spec['url'], timeout=90)
            response.raise_for_status()
            dates = {}
            for item in ET.fromstring(response.content).findall('./channel/item'):
                date = item.findtext('{http://purl.org/dc/elements/1.1/}date', '')
                guid = item.findtext('guid', '')
                if re.fullmatch(r'\d{4}', date) and guid.startswith('conference:'):
                    dates[guid] = date
            plan.append((spec['url'], dates))
    require_zotero_closed()
    directory = Path('F:/Zotero')
    connection = sqlite3.connect(str(directory / 'zotero.sqlite'), timeout=5)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        field_id = connection.execute("SELECT fieldID FROM fields WHERE fieldName='date'").fetchone()[0]
        updates = []
        for url, dates in plan:
            feed = connection.execute('SELECT libraryID FROM feeds WHERE url=?', (url,)).fetchone()
            if not feed:
                continue
            rows = connection.execute(
                'SELECT i.itemID,f.guid,d.valueID,v.value FROM items i JOIN feedItems f USING(itemID) '
                'JOIN itemData d USING(itemID) JOIN itemDataValues v USING(valueID) '
                'WHERE i.libraryID=? AND d.fieldID=?', (feed[0], field_id),
            )
            for item_id, guid, old_id, old in rows:
                year = dates.get(guid)
                if year:
                    value = f'{year}-00-00 {year}'
                    expanded = f'{year}-01-01 {year}-01-01 00:00:00'
                    if old == expanded:
                        updates.append((item_id, old_id, value))
        print(f'Planned date corrections: {len(updates)}', flush=True)
        if not args.apply or not updates:
            return
        require_zotero_closed()
        backup = directory / 'backups' / ('before-conference-date-repair-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite')
        backup.parent.mkdir(exist_ok=True)
        with sqlite3.connect(str(backup)) as destination:
            connection.backup(destination)
        print(f'Backup: {backup}', flush=True)
        require_zotero_closed()
        before = feed_state_digest(connection)
        count = connection.execute('SELECT COUNT(*) FROM items').fetchone()[0]
        connection.execute('BEGIN EXCLUSIVE')
        value_ids = {}
        for value in {value for _, _, value in updates}:
            connection.execute('INSERT OR IGNORE INTO itemDataValues(value) VALUES (?)', (value,))
            value_ids[value] = connection.execute('SELECT valueID FROM itemDataValues WHERE value=?', (value,)).fetchone()[0]
        connection.executemany(
            'UPDATE itemData SET valueID=? WHERE itemID=? AND fieldID=? AND valueID=?',
            [(value_ids[value], item_id, field_id, old_id) for item_id, old_id, value in updates],
        )
        assert connection.execute('SELECT COUNT(*) FROM items').fetchone()[0] == count
        assert feed_state_digest(connection) == before, 'Feed identity/read state changed'
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        connection.commit()
        print(f'Corrected {len(updates)} dates; item identities and read/translated state unchanged; quick_check=ok', flush=True)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    main()
