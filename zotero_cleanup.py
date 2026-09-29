"""One-shot read-item cleanup, gated on a published suppression receipt."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.request
import tempfile

from rss_ops import write_json
from zotero_read_sync import create_database_snapshot
from zotero_subscribe_conferences import ZoteroDebugger
from rss_read_filter import hash_tokens, identity_tokens, validate_suppression, suppression_payload_digest

BASE='https://fengziclassmate.github.io/journal-rss/'


def inspect(database):
    connection=sqlite3.connect(database)
    connection.row_factory=sqlite3.Row
    try:
        targets=[dict(row) for row in connection.execute('''SELECT fi.itemID AS id,fi.guid,
            fi.readTime,i.libraryID AS library,f.url FROM feedItems fi
            JOIN items i USING(itemID) JOIN feeds f USING(libraryID) WHERE fi.readTime IS NOT NULL''')]
        return {'targets':targets,
                'unread': [list(row) for row in connection.execute('SELECT itemID,guid FROM feedItems WHERE readTime IS NULL ORDER BY itemID')],
                'library': [list(row) for row in connection.execute('SELECT itemID,libraryID,key FROM items WHERE libraryID NOT IN (SELECT libraryID FROM feeds) ORDER BY itemID')],
                'settings': [list(row) for row in connection.execute('SELECT libraryID,refreshInterval,cleanupReadAfter,cleanupUnreadAfter FROM feeds ORDER BY libraryID')]}
    finally:
        connection.close()


def eligible(plan, receipt, suppression, key=None):
    payload=json.loads(suppression.read_text('utf-8'))
    hashes=validate_suppression(payload,key)
    expected=suppression_payload_digest(payload)
    if receipt.get('suppression_sha256') != expected:
        raise RuntimeError('Published filter receipt is not current. No items may be deleted.')
    paths=set(receipt.get('feeds',{}))
    accepted=[]; skipped=[]
    for row in plan['targets']:
        path=row['url'][len(BASE):] if row['url'].startswith(BASE) else None
        remembered = bool(hash_tokens(identity_tokens(guid=row['guid']),key) & hashes)
        (accepted if path in paths and remembered else skipped).append(row)
    return accepted,skipped


def verify_published_feeds(targets, receipt, fetch=None):
    def download(path):
        request=urllib.request.Request(BASE+path+'?cleanup='+dt.datetime.now().strftime('%Y%m%d%H%M%S'),
                                       headers={'Cache-Control':'no-cache'})
        with urllib.request.urlopen(request,timeout=60) as response:
            return response.read()
    fetch=fetch or download
    for path in sorted({row['url'][len(BASE):] for row in targets}):
        if hashlib.sha256(fetch(path)).hexdigest()!=receipt['feeds'][path]:
            raise RuntimeError('Published feed does not match the filter receipt: '+path)


def fetch_receipt():
    url=BASE+'read-filter-status.json?verify='+dt.datetime.now().strftime('%Y%m%d%H%M%S')
    with urllib.request.urlopen(url,timeout=40) as response:
        return json.load(response)


def apply(plan, folder):
    write_json(folder/'plan.json',plan)
    debugger=ZoteroDebugger()
    try:
        script='''(async()=>{
          if (Zotero.DataDirectory.dir.replaceAll('\\\\','/').toLowerCase() !== 'f:/zotero') throw new Error('Wrong database');
          const databases=await Zotero.DB.queryAsync('PRAGMA database_list');
          const main=databases.find(row=>row.name==='main');
          if(!main || main.file.replaceAll('\\\\','/').toLowerCase()!=='f:/zotero/zotero.sqlite') throw new Error('Backup database differs from active database');
          const plan=await IOUtils.readJSON(PATH);
          const pause=await Zotero.Feeds.pause();
          const removed=[],skipped=[];
          try {
            for(let start=0;start<plan.targets.length;start+=50){
              await Zotero.DB.executeTransaction(async()=>{
                for(const expected of plan.targets.slice(start,start+50)){
                  const row=await Zotero.DB.rowQueryAsync('SELECT fi.guid,fi.readTime,i.libraryID FROM feedItems fi JOIN items i USING(itemID) JOIN feeds f USING(libraryID) WHERE fi.itemID=?',[expected.id]);
                  if(!row || !row.readTime || row.readTime!==expected.readTime || row.guid!==expected.guid || row.libraryID!==expected.library){skipped.push(expected.id);continue;}
                  const item=await Zotero.FeedItems.getAsync(expected.id);
                  await item.erase();
                  removed.push(expected.id);
                }
              });
              await IOUtils.writeJSON(RESULT,{removed,skipped});
            }
            return JSON.stringify({removed,skipped});
          } finally {pause.resume();}
        })()'''.replace('PATH',json.dumps(str((folder/'plan.json').resolve()))).replace('RESULT',json.dumps(str((folder/'result.json').resolve())))
        return debugger.evaluate(script)
    finally:
        debugger.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path('F:/Zotero/zotero.sqlite'))
    parser.add_argument('--suppression',type=Path,default=Path(__file__).parent/'read-suppression.json')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--shutdown',action='store_true')
    parser.add_argument('--key-file',type=Path,default=Path('F:/Zotero/journal-rss-read-filter.key'))
    args=parser.parse_args()
    if args.shutdown:
        debugger=ZoteroDebugger()
        try:
            print(json.dumps(debugger.evaluate("(async()=>{setTimeout(()=>Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit),500);return JSON.stringify({requested:true});})()")))
        finally:
            debugger.close()
        return
    if not args.apply:
        with tempfile.TemporaryDirectory() as temp:
            plan=inspect(create_database_snapshot(args.database,Path(temp)))
        print(json.dumps({'read':len(plan['targets']),'unread':len(plan['unread']),'action':'preview only'}))
        return
    if args.database.resolve() != Path('F:/Zotero/zotero.sqlite').resolve():
        raise ValueError('Cleanup is bound to the actual F:/Zotero/zotero.sqlite database')
    receipt=fetch_receipt()
    folder=args.database.parent/('read-cleanup-'+dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    snapshot=create_database_snapshot(args.database,folder)
    plan=inspect(snapshot)
    key=args.key_file.read_text('ascii').strip().encode('ascii')
    plan['targets'],skipped=eligible(plan,receipt,args.suppression,key)
    verify_published_feeds(plan['targets'],receipt)
    write_json(folder/'unmanaged-feeds-skipped.json',skipped)
    result=apply(plan,folder)
    with tempfile.TemporaryDirectory() as temp:
        after=inspect(create_database_snapshot(args.database,Path(temp)))
    missing_unread=set(map(tuple,plan['unread']))-set(map(tuple,after['unread']))
    missing_library=set(map(tuple,plan['library']))-set(map(tuple,after['library']))
    checks={'unread_preserved':not missing_unread,'library_preserved':not missing_library,
            'settings_unchanged':plan['settings']==after['settings']}
    write_json(folder/'verification.json',checks)
    if not all(checks.values()):
        raise RuntimeError('Verification requires attention; keep backup: '+str(folder))
    print(json.dumps({'removed':len(result['removed']),'skipped':len(skipped)+len(result['skipped']),'backup':str(folder),**checks}))


if __name__=='__main__': main()
