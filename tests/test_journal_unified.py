import json
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
        self.assertLess(script.index('Copy verification failed'), script.index('.erase()'))
        self.assertIn('skipNotifier:true', script)
        self.assertIn("item.setField('extra',data.extra || '')", script)
        self.assertIn('strict:true', script)

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
