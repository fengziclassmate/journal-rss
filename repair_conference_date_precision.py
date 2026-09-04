"""One-time source-backed date precision repair via Zotero's item API.

Defaults to a dry run. Requires the local debugger and the compatibility add-on.
Only dates of existing conference feed items are touched; no entries are recreated.
"""
import argparse
import json
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from zotero_subscribe_conferences import ZoteroDebugger, feed_specs


SCRIPT = """(async()=>{
if (Zotero.DataDirectory.dir.replaceAll('\\\\', '/').toLowerCase() !== 'f:/zotero') {
  throw new Error('Unexpected data directory');
}
const plan = await IOUtils.readJSON(PLAN_PATH);
const feed = Zotero.Feeds.getByURL(plan.url);
if (!feed) throw new Error('Subscription not registered: '+plan.url);
if (feed._updating) await feed._updating;
await feed.waitForDataLoad('item');
const rows = await Zotero.DB.queryAsync(
  'SELECT itemID,guid FROM feedItems WHERE itemID IN (SELECT itemID FROM items WHERE libraryID=?)',
  [feed.libraryID]
);
const byGUID = new Map(rows.map(row=>[row.guid,row.itemID]));
const changes=[];
for (const [guid,date] of plan.dates) {
  if (!/^\\d{4}(?:-\\d{2})?$/.test(date)) throw new Error('Not a partial date');
  const id=byGUID.get(guid);
  if (!id) continue;
  const item=Zotero.Items.get(id);
  if (item.getField('date')!==date) changes.push({item,date});
}
if (APPLY) {
  for(let start=0; start<changes.length; start+=100) {
    await Zotero.DB.executeTransaction(async()=>{
      for(const {item,date} of changes.slice(start,start+100)) {
        item.setField('date',date);
        await item.save({skipNotifier:true,skipDateModifiedUpdate:true});
      }
    });
    await Zotero.Promise.delay(1);
  }
}
return JSON.stringify({libraryID:feed.libraryID,matched:plan.dates.length,changed:changes.length,applied:APPLY});
})()"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    client=ZoteroDebugger()
    session=requests.Session()
    try:
        with tempfile.TemporaryDirectory(prefix='conference-date-repair-') as temp:
            path=Path(temp)/'plan.json'
            for spec in feed_specs():
                response=session.get(spec['url'],timeout=90)
                response.raise_for_status()
                items=ET.fromstring(response.content).findall('./channel/item')
                dates=[]
                for item in items:
                    date=item.findtext('{http://purl.org/dc/elements/1.1/}date','')
                    guid=item.findtext('guid','')
                    if guid.startswith('conference:') and re.fullmatch(r'\d{4}(?:-\d{2})?',date):
                        dates.append((guid,date))
                path.write_text(json.dumps({'url':spec['url'],'dates':dates}),encoding='utf-8')
                script=SCRIPT.replace('PLAN_PATH',json.dumps(str(path))).replace('APPLY',str(args.apply).lower())
                print(json.dumps(client.evaluate(script,timeout_seconds=1800)),flush=True)
            if args.apply:
                print(client.evaluate("""(async()=>{
const view=Zotero.getMainWindow().ZoteroPane.itemsView;
await view.sort();view.invalidate();
return JSON.stringify({refreshed:true});
})()"""),flush=True)
    finally:
        session.close()
        client.close()


if __name__=='__main__':
    main()
