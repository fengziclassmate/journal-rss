"""Migrate paired journal subscriptions through Zotero's native API, with checkpoints."""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from journal_unified import BASE, STATE, identities, journal_specs, union
from rss_ops import write_json
from zotero_subscribe_conferences import ZoteroDebugger


def export_script(specs):
    return '''(async()=>{
await Zotero.initializationPromise;
if(Zotero.DataDirectory.dir.replaceAll('\\\\','/').toLowerCase()!=='f:/zotero') throw new Error('Wrong data directory');
if(Zotero.DB.readOnly) throw new Error('Database is read-only');
globalThis.__journalUnionPause ||= await Zotero.Feeds.pause();
const specs=SPECS, result=[];
for(const spec of specs){
  const feeds=Zotero.Feeds.getAll().filter(f=>[spec.url,...spec.old_urls].includes(f.url));
  const rows=[];
  for(const feed of feeds){
    const ids=await Zotero.DB.columnQueryAsync('SELECT itemID FROM items WHERE libraryID=?',[feed.libraryID]);
    for(const id of ids){
      const item=await Zotero.Items.getAsync(id);
      if(!item.isFeedItem) throw new Error('Unexpected non-feed item');
      await item.loadAllData();
      if(item.getAttachments().length || item.getNotes().length) throw new Error('Feed item has children; manual migration required');
      rows.push({id,library:feed.libraryID,guid:item.guid,readTime:item._feedItemReadTime,
                 translatedTime:item._feedItemTranslatedTime,data:item.toJSON()});
    }
  }
  result.push({spec,feeds:feeds.map(f=>({id:f.libraryID,url:f.url,name:f.name})),items:rows});
}
return JSON.stringify(result);
})()'''.replace('SPECS', json.dumps(specs))


def item_node(row):
    data = row['data']
    node = ET.Element('item')
    for key, value in [('guid', row['guid']), ('link', data.get('url', '')),
                       ('title', data.get('title', '')), ('description', data.get('abstractNote', '')),
                       ('{http://purl.org/dc/elements/1.1/}identifier', data.get('DOI', ''))]:
        ET.SubElement(node, key).text = value
    for creator in data.get('creators', []):
        ET.SubElement(node, '{http://purl.org/dc/elements/1.1/}creator').text = (
            creator.get('name') or ' '.join(filter(None, [creator.get('firstName'), creator.get('lastName')]))
        )
    return node


def make_plan(root, exported):
    state = json.loads((root/STATE).read_text('utf-8'))
    plan = []
    for row in exported:
        spec = row['spec']
        aliases = state.setdefault(spec['output'], {})
        # Official metadata first; items not currently in any live RSS still migrate.
        urls = {feed['id']: feed['url'] for feed in row['feeds']}
        ordered = sorted(row['items'], key=lambda item: urls[item['library']] not in spec['old_urls'][1:])
        union(spec, [[item_node(item) for item in ordered]], aliases)
        groups = {}
        for item in ordered:
            guids = {aliases[token] for token in identities(item_node(item)) if token in aliases}
            if len(guids) != 1:
                raise ValueError('Ambiguous migration identity: ' + item['guid'])
            groups.setdefault(guids.pop(), []).append(item)
        items = []
        for guid, members in groups.items():
            selected = next((item for item in members if item['translatedTime']), members[0])
            data = dict(selected['data'])
            extras = list(dict.fromkeys(item['data'].get('extra', '') for item in members if item['data'].get('extra')))
            if extras:
                data['extra'] = '\n\n'.join(extras)
            items.append({'guid': guid, 'data': data, 'members': members,
                          'readTime': min((item['readTime'] for item in members if item['readTime']), default=None),
                          'translatedTime': max((item['translatedTime'] for item in members if item['translatedTime']), default=None)})
        plan.append({'spec': spec, 'feeds': row['feeds'], 'items': items})
    write_json(root/STATE, state)
    return plan


def migration_script(folder):
    return '''(async()=>{
await Zotero.initializationPromise;
if(Zotero.DataDirectory.dir.replaceAll('\\\\','/').toLowerCase()!=='f:/zotero' || Zotero.DB.readOnly) throw new Error('Unsafe database');
const plan=await IOUtils.readJSON(PLAN), result=[];
globalThis.__journalUnionPause ||= await Zotero.Feeds.pause();
const pane=Zotero.getActiveZoteroPane();
await pane.collectionsView.selectLibrary(Zotero.Libraries.userLibraryID);
try{
for(const group of plan){
  const spec=group.spec;
  // Abort if the user or another task changed any source after the export.
  for(const old of group.feeds){
    const feed=Zotero.Feeds.get(old.id);
    if(!feed || feed.url!==old.url) throw new Error('Subscription changed since export');
    const ids=await Zotero.DB.columnQueryAsync('SELECT itemID FROM items WHERE libraryID=?',[old.id]);
    const expected=group.items.flatMap(row=>row.members.filter(m=>m.library===old.id).map(m=>m.id));
    if(JSON.stringify(ids.sort((a,b)=>a-b))!==JSON.stringify(expected.sort((a,b)=>a-b))) throw new Error('Source items changed; re-export');
  }
  for(const row of group.items){
    for(const member of row.members){
      const source=await Zotero.Items.getAsync(member.id);
      await source.loadAllData();
      if(source.guid!==member.guid || source._feedItemReadTime!==member.readTime ||
         source._feedItemTranslatedTime!==member.translatedTime || JSON.stringify(source.toJSON())!==JSON.stringify(member.data)) {
        throw new Error('Item changed since export: '+member.id);
      }
    }
  }
  let target=Zotero.Feeds.getByURL(spec.url);
  if(!target){
    target=new Zotero.Feed({name:spec.name,url:spec.url,refreshInterval:1440,cleanupReadAfter:1,cleanupUnreadAfter:999});
    target._set('_feedLastCheck',Zotero.Date.dateToSQL(new Date(),true));
    await target.saveTx({skipSelect:true});
  }
  target.name=spec.name; target.refreshInterval=1440; target.cleanupReadAfter=1; target.cleanupUnreadAfter=999;
  await target.saveTx({skipSelect:true});
  const copied=[];
  for(const row of group.items){
    let item=await Zotero.FeedItems.getAsyncByGUID(row.guid);
    if(item && item.libraryID!==target.libraryID) throw new Error('Unified GUID belongs to another library');
    const data={...row.data};
    for(const field of ['key','version','collections','relations']) delete data[field];
    if(!item){
      item=new Zotero.FeedItem(data.itemType,{guid:row.guid});
      item.libraryID=target.libraryID;
    }
    // Avoid partial-date coercion and Extra extraction/reordering during a lossless copy.
    Zotero.Item.prototype.fromJSON.call(item,data,{strict:true});
    item.setField('extra',data.extra || '');
    item._feedItemReadTime=row.readTime;
    item._feedItemTranslatedTime=row.translatedTime;
    item._changed.feedItemData=true;
    // Do not retrigger plugin "new item" processing for an existing, translated item.
    await item.saveTx({skipSelect:true,skipDateModifiedUpdate:true,skipNotifier:true});
    await item.loadAllData();
    const saved=item.toJSON();
    for(const [field,value] of Object.entries(row.data)){
      if(['key','version','collections','relations','dateModified'].includes(field)) continue;
      if(JSON.stringify(saved[field])!==JSON.stringify(value)) throw new Error('Copy verification failed: '+field+' / '+row.guid);
    }
    if((item._feedItemReadTime || null)!==row.readTime || (item._feedItemTranslatedTime || null)!==row.translatedTime) throw new Error('Reading state not preserved');
    copied.push({id:item.id,guid:item.guid,sourceIDs:row.members.map(m=>m.id),readTime:row.readTime,translatedTime:row.translatedTime});
  }
  // Originals are removed only after every copy in this journal is verified.
  const removed=[];
  for(const old of group.feeds){
    if(old.id!==target.libraryID){await Zotero.Feeds.get(old.id).erase();removed.push(old.id);}
  }
  await target.updateUnreadCount();
  result.push({name:spec.name,id:target.libraryID,url:spec.url,copied,removed});
  await IOUtils.writeJSON(RESULT,result);
}
}finally{
  if(globalThis.__journalUnionPause){globalThis.__journalUnionPause.resume();delete globalThis.__journalUnionPause;}
}
return JSON.stringify({journals:result.length,copied:result.reduce((n,r)=>n+r.copied.length,0),removed:result.reduce((n,r)=>n+r.removed.length,0)});
})()'''.replace('PLAN', json.dumps(str(folder/'plan.json'))).replace('RESULT', json.dumps(str(folder/'native-result.json')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export', 'plan', 'migrate'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    args.folder.mkdir(parents=True, exist_ok=True)
    if args.action == 'plan':
        plan = make_plan(root, json.loads((args.folder/'export.json').read_text('utf-8')))
        write_json(args.folder/'plan.json', plan)
        print(json.dumps({'journals': len(plan), 'sourceItems': sum(len(i['members']) for g in plan for i in g['items']),
                          'uniqueItems': sum(len(g['items']) for g in plan)}))
        return
    debugger = ZoteroDebugger()
    try:
        if args.action == 'export':
            value = debugger.evaluate(export_script(journal_specs(root)))
            write_json(args.folder/'export.json', value)
            print(json.dumps({'journals': len(value), 'subscriptions': sum(len(r['feeds']) for r in value),
                              'items': sum(len(r['items']) for r in value)}))
        else:
            print(json.dumps(debugger.evaluate(migration_script(args.folder))))
    finally:
        debugger.close()


if __name__ == '__main__':
    main()
