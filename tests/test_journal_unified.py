import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

from journal_unified import BASE, build, entries, identities, journal_specs, normalize, union
from rss_read_filter import DC_NS, _item_tokens, filter_feed, hash_tokens
from zotero_unify_journals import make_plan, migration_script


class UnifiedJournalTests(unittest.TestCase):
    def spec(self):
        return {'name': 'Journal', 'custom': 'journal.xml', 'official': 'official.xml',
                'official_url': 'https://publisher.test/rss', 'output': 'journal-feeds/journal.xml',
                'url': BASE+'journal-feeds/journal.xml'}

    def item(self, guid, link, title='A sufficiently long scientific paper title about remote sensing', authors='Alice Smith'):
        item = ET.Element('item')
        for key, value in [('guid', guid), ('link', link), ('title', title),
                           ('description', '<p>Author(s): '+authors+'</p>')]:
            ET.SubElement(item, key).text = value
        return item

    def test_complete_one_to_one_inventory(self):
        specs = journal_specs()
        self.assertEqual(len(specs), 73)
        self.assertEqual(sum(bool(s['official']) for s in specs), 72)
        self.assertTrue(all('_FZTX' not in s['name'] for s in specs))

    def test_official_metadata_wins_and_aliases_remember_both(self):
        official = self.item('pub', 'https://doi.org/10.1234/a')
        custom = self.item('doi:10.1234/a', 'https://doi.org/10.1234/a')
        official.find('description').text = 'Official abstract'
        result = union(self.spec(), [[official], [custom]], {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].findtext('description'), 'Official abstract')
        self.assertIn('guid:pub', _item_tokens(result[0]))
        self.assertIn('guid:doi:10.1234/a', _item_tokens(result[0]))

    def test_official_arriving_later_keeps_guid(self):
        state = {}
        custom = self.item('custom', 'https://doi.org/10.1234/a')
        first = union(self.spec(), [[custom]], state)
        official = self.item('pub', 'https://doi.org/10.1234/a')
        official.find('description').text = 'Official content'
        second = union(self.spec(), [[official], first], state)
        self.assertEqual(first[0].findtext('guid'), second[0].findtext('guid'))
        self.assertEqual(second[0].findtext('description'), 'Official content')

    def test_metadata_only_conflicting_dois_do_not_merge(self):
        first = self.item('a', 'https://test/a')
        second = self.item('b', 'https://test/b')
        ET.SubElement(first, '{'+DC_NS+'}identifier').text = '10.1234/a'
        ET.SubElement(second, '{'+DC_NS+'}identifier').text = '10.1234/b'
        self.assertEqual(len(union(self.spec(), [[first], [second]], {})), 2)

    def test_later_official_title_author_bridge_keeps_previous_guid(self):
        state = {}
        first = union(self.spec(), [[self.item('a', 'https://test/custom')]], state)
        old_guid = first[0].findtext('guid')
        second = union(self.spec(), [[self.item('b', 'https://test/publisher')], first], state)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].findtext('guid'), old_guid)

    def test_bridge_consolidates_both_groups_and_all_aliases(self):
        state = {}
        first = self.item('a', 'https://test/a', title='First distinct paper', authors='Alice')
        second = self.item('b', 'https://test/b', title='Second distinct paper', authors='Bob')
        bridge = self.item('a', 'https://test/b', title='Bridge', authors='Carol')
        result = union(self.spec(), [[first, second, bridge]], state)
        self.assertEqual(len(result), 1)
        self.assertEqual(len({state[t] for t in ('guid:a','guid:b','url:https://test/a','url:https://test/b')}), 1)

    def test_retained_official_metadata_is_not_downgraded(self):
        official = normalize(self.item('a', 'https://test/a'), self.spec()['official_url'])
        official.find('description').text = 'Publisher abstract'
        state = {}
        old = union(self.spec(), [[official]], state)
        custom = normalize(self.item('b', 'https://test/a'), BASE+self.spec()['custom'])
        result = union(self.spec(), [[custom], old], state)
        self.assertEqual(result[0].findtext('description'), 'Publisher abstract')

    def test_same_title_different_dois_not_merged(self):
        result = union(self.spec(), [[self.item('doi:10.1234/a', 'https://doi.org/10.1234/a')],
                                   [self.item('doi:10.1234/b', 'https://doi.org/10.1234/b')]], {})
        self.assertEqual(len(result), 2)

    def test_title_and_author_bridge_read_history(self):
        official = self.item('pub', 'https://publisher.test/a')
        custom = self.item('custom', 'https://doi.org/10.1234/a')
        result = union(self.spec(), [[official], [custom]], {})
        self.assertEqual(len(result), 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'feed.xml'
            rss = ET.Element('rss'); ET.SubElement(rss, 'channel').extend(result)
            ET.ElementTree(rss).write(path)
            self.assertEqual(filter_feed(path, hash_tokens({'guid:custom'}, b'test'), b'test').removed, 1)

    def test_rdf_keeps_date_and_extensions(self):
        node = ET.fromstring('<item xmlns="http://purl.org/rss/1.0/" xmlns:dc="'+DC_NS+'" '
                             'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" rdf:about="https://test/a">'
                             '<title>Paper</title><link>https://test/a</link><dc:date>2026-10-05</dc:date></item>')
        result = normalize(node, 'https://test/rss')
        self.assertEqual(result.findtext('guid'), 'https://test/a')
        self.assertEqual(result.findtext('{'+DC_NS+'}date'), '2026-10-05')
        self.assertEqual(result.findtext('title'), 'Paper')

    def test_atom_updated_is_not_publication_date(self):
        node = ET.fromstring('<entry xmlns="http://www.w3.org/2005/Atom"><id>a</id><title>Paper</title>'
                             '<link href="https://test/a"/><updated>2026-10-07T12:00:00Z</updated>'
                             '<author><name>Alice</name></author><summary type="html">&lt;p&gt;Abstract&lt;/p&gt;</summary></entry>')
        result = normalize(node, 'https://test/rss')
        self.assertIsNone(result.find('pubDate'))
        self.assertIsNone(result.find('{'+DC_NS+'}date'))
        self.assertEqual(result.findtext('description'), '<p>Abstract</p>')
        self.assertEqual(result.findtext('{'+DC_NS+'}creator'), 'Alice')

    def test_partial_publisher_date_does_not_invent_day(self):
        node = self.item('a', 'https://test/a')
        ET.SubElement(node, '{'+DC_NS+'}date').text = '2026-09'
        result = normalize(node, 'https://test/rss')
        self.assertIsNone(result.find('{'+DC_NS+'}date'))
        self.assertIn('Publication month: 2026-09', result.findtext('description'))
        self.assertEqual(result.findtext('{'+BASE+'ns/dates/1}publication'), '2026-09')

    def test_migration_plan_preserves_read_and_translation_of_duplicates(self):
        from journal_unified import STATE
        spec = self.spec(); spec['old_urls'] = ['https://test/custom', 'https://test/official']
        rows = [{'id': 1, 'library': 1, 'guid': 'official', 'readTime': '', 'translatedTime': '',
                 'data': {'title': 'Paper', 'url': 'https://doi.org/10.1234/a', 'extra': 'official'}},
                {'id': 2, 'library': 2, 'guid': 'custom', 'readTime': '2026-10-01 12:00:00', 'translatedTime': '',
                 'data': {'title': 'Paper', 'url': 'https://doi.org/10.1234/a', 'extra': 'titleTranslation: translated'}}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/STATE).parent.mkdir(parents=True); (root/STATE).write_text('{}')
            plan = make_plan(root, [{'spec':spec,'items':rows,'feeds':[{'id':1,'url':'https://test/official'},
                                                                    {'id':2,'url':'https://test/custom'}]}])
            self.assertEqual(len(plan[0]['items']), 1)
            row = plan[0]['items'][0]
            self.assertEqual(row['readTime'], '2026-10-01 12:00:00')
            self.assertIsNone(row['translatedTime'])
            self.assertIn('titleTranslation: translated', row['data']['extra'])
            self.assertEqual(len(row['members']), 2)

    def test_native_migration_verifies_before_erasing_and_skips_plugin_reimport(self):
        script = migration_script(Path('F:/backup'))
        self.assertLess(script.index('Copy verification failed'), script.index("phase:'copied'"))
        self.assertLess(script.index("phase:'copied'"), script.index('await finish(group,record)'))
        self.assertIn('IOUtils.exists(', script)
        self.assertIn('if(checkpoint){await finish(group,checkpoint);continue;}', script)
        self.assertIn('skipNotifier:true', script)
        self.assertIn("item.setField('extra',data.extra || '')", script)
        self.assertIn('strict:true', script)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the native-API mock')
    def test_native_checkpoint_resumes_after_erase_before_checkpoint_write(self):
        script = migration_script(Path('F:/backup'))
        harness = r'''
const migration=MIGRATION;
const plans=['A','B'].map((name,index)=>({spec:{name,url:'https://test/'+name},feeds:[{id:index+1,url:'https://old/'+name}],items:[]}));
let checkpoint=null,crash=true,nextID=100;
const feeds=new Map();
function feed(id,name,url){return {libraryID:id,name,url,_set(){},async saveTx(){feeds.set(id,this)},async updateUnreadCount(){},async erase(){feeds.delete(id);if(id===1 && crash){crash=false;throw new Error('simulated interruption after erase')}}};}
feeds.set(1,feed(1,'A','https://old/A'));feeds.set(2,feed(2,'B','https://old/B'));
globalThis.Zotero={initializationPromise:Promise.resolve(),DataDirectory:{dir:'F:/Zotero'},DB:{readOnly:false,async columnQueryAsync(){return []}},
 Feeds:{get:id=>feeds.get(id),getByURL:url=>[...feeds.values()].find(f=>f.url===url),async pause(){return {resume(){}}}},
 Feed:function(spec){return feed(nextID++,spec.name,spec.url)},FeedItems:{},Libraries:{userLibraryID:99},
 Date:{dateToSQL:()=>''},getActiveZoteroPane:()=>({collectionsView:{async selectLibrary(){}}})};
globalThis.IOUtils={async exists(){return checkpoint!==null},async readJSON(path){return structuredClone(path.endsWith('plan.json')?plans:checkpoint)},async writeJSON(path,data){checkpoint=structuredClone(data)}};
(async()=>{let failed=false;try{await eval(migration)}catch(error){failed=true}
 if(!failed || feeds.has(1) || checkpoint[0].phase!=='copied') throw new Error('Interruption was not simulated');
 const result=JSON.parse(await eval(migration));
 if(result.journals!==2 || result.removed!==2 || feeds.has(1) || feeds.has(2) || checkpoint.some(r=>r.phase!=='complete')) throw new Error('Resume failed');
 console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('MIGRATION', json.dumps(script))
        result = subprocess.run(['node', '-'], input=harness, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['journals'], 2)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the native-API mock')
    def test_checkpoint_never_erases_changed_source_or_copy(self):
        harness = r'''
const migration=MIGRATION;
async function check(mode){
 const data={itemType:'journalArticle',title:'Paper',extra:'cached translation'};
 const row={guid:'unified:a:abc',data,readTime:null,translatedTime:null,members:[{id:100,library:1,guid:'old',readTime:'',translatedTime:'',data}]};
 const group={spec:{name:'A',url:'https://test/A'},feeds:[{id:1,url:'https://old/A'}],items:[row]};
 const copy={id:200,libraryID:10,guid:row.guid,_feedItemReadTime:null,_feedItemTranslatedTime:null,async loadAllData(){},toJSON(){return {...data,...(mode==='copy-extra'?{extra:'new target translation'}:{})}}};
 const source={id:100,libraryID:1,guid:'old',_feedItemReadTime:mode==='source-read'?'2026-10-07 12:00:00':'',_feedItemTranslatedTime:'',async loadAllData(){},toJSON(){return {...data,...(mode==='source-extra'?{extra:'new original translation'}:{})}}};
 let erased=false;
 const feeds=new Map([[1,{libraryID:1,url:'https://old/A',async erase(){erased=true}}],[10,{libraryID:10,url:'https://test/A'}]]);
 const checkpoint=[{id:10,name:'A',url:group.spec.url,copied:[{id:200,guid:row.guid}],removed:[],phase:'copied'}];
 globalThis.Zotero={initializationPromise:Promise.resolve(),DataDirectory:{dir:'F:/Zotero'},DB:{readOnly:false},
  Feeds:{get:id=>feeds.get(id),async pause(){return {resume(){}}}},FeedItems:{async getAsyncByGUID(){return copy}},Items:{async getAsync(){return source}},
  Libraries:{userLibraryID:99},getActiveZoteroPane:()=>({collectionsView:{async selectLibrary(){}}})};
 globalThis.IOUtils={async exists(){return true},async readJSON(path){return structuredClone(path.endsWith('plan.json')?[group]:checkpoint)},async writeJSON(){}};
 let error='';try{await eval(migration)}catch(e){error=String(e)}
 if(erased || !error.includes('changed')) throw new Error('Changed originals/copies were not protected: '+mode+' '+error);
}
(async()=>{for(const mode of ['source-read','source-extra','copy-extra']) await check(mode);console.log('protected');})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('MIGRATION', json.dumps(migration_script(Path('F:/backup'))))
        result = subprocess.run(['node','-'], input=harness, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('protected', result.stdout)

    def test_repeated_build_retains_history_and_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rss = ET.Element('rss'); ET.SubElement(rss, 'channel').append(self.item('a', 'https://test/a'))
            ET.ElementTree(rss).write(root/'official.xml')
            with patch('journal_unified.journal_specs', return_value=[self.spec()]):
                build(root)
                path = root/self.spec()['output']; previous = path.read_bytes()
                build(root)
                self.assertEqual(path.read_bytes(), previous)
                ET.ElementTree(ET.fromstring('<rss><channel/></rss>')).write(root/'official.xml')
                build(root)
                self.assertEqual(len(entries(path)), 1)

    def test_workflow_union_precedes_legacy_and_read_filters(self):
        workflow = Path('.github/workflows/update-feed.yml').read_text('utf-8')
        self.assertLess(workflow.index('run: python journal_unified.py'), workflow.index('run: python official_priority.py'))
        self.assertIn('--glob "journal-feeds/*.xml"', workflow)
        self.assertIn('cp -r journal-feeds _site/', workflow)


if __name__ == '__main__':
    unittest.main()
