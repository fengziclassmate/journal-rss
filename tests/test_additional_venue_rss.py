from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import additional_venue_rss as agent

class AdditionalVenueTests(unittest.TestCase):
    def test_tacl_inline_case_does_not_split_words(self):
        raw=b'''<collection id="2026.tacl"><volume id="1"><meta><year>2026</year></meta><paper id="2"><title><fixed-case>A</fixed-case>gent Memory</title><author><first>Xin</first><last>Li</last></author><abstract>Tool calling</abstract><doi>10.1234/test</doi></paper></volume></collection>'''
        row=agent.parse_tacl(raw)[0]
        self.assertEqual(row['title'],'Agent Memory')
        self.assertEqual(row['authors'],['Xin Li'])
        self.assertEqual(row['dates']['publication'],'2026')

    def test_aamas_excludes_presented_journal_papers_and_demos(self):
        raw=b'''<a href="#R">Research Paper Track</a><a href="#J">JAAMAS Track</a><a name="R"></a><p><a href="../pdfs/a.pdf">LLM Agent Planning</a><font face="Times New Roman">Xin Li <i>(University)</i></font></p><a name="J"></a><p><a href="../pdfs/b.pdf">Agent journal presentation</a></p>'''
        rows=agent.parse_aamas(raw,'https://ifaamas.org/Proceedings/aamas2026/forms/contents.htm',2026)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['authors'],['Xin Li'])
        self.assertTrue(rows[0]['url'].endswith('/aamas2026/pdfs/a.pdf'))

    def test_colm_only_uses_indexed_papers(self):
        raw=b'''<a href="/admin/login">Login</a><div class="paper-content"><a href="/paper_files/paper/2026/hash/a-Abstract-Conference.html">Agent Planning</a><span class="paper-authors">Xin Li, Nan Zhao</span></div>'''
        rows=agent.parse_colm(raw,'https://proceedings.colm.cc/paper_files/paper/2026',2026)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['title'],'Agent Planning')

    def test_repeated_updates_keep_guid_and_history(self):
        source=agent.SOURCES[0]
        row=dict(id='paper-one',title='Agent Planning',url='https://example.org/paper',authors=[],abstract='',doi='',dates={'publication':'2026'})
        non_agent=dict(row,id='paper-two',title='Representation learning',url='https://example.org/other')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder)
            with patch.object(agent,'SOURCES',[source]),patch.object(agent,'collect_colm',return_value=[row,row,non_agent]):
                agent.run(root)
                first=(root/source['output']).read_bytes()
                agent.run(root)
                second=(root/source['output']).read_bytes()
                agent.run(root,cached=True)
                third=(root/source['output']).read_bytes()
            self.assertEqual(first,second)
            self.assertEqual(second,third)
            tree=ET.fromstring(first)
            self.assertEqual(len(tree.findall('./channel/item')),2)
            self.assertIn('Representation learning',[n.findtext('title') for n in tree.findall('./channel/item')])
            self.assertIsNone(tree.find('./channel/item/pubDate'))

    def test_outputs_use_existing_filter_and_publication_paths(self):
        self.assertEqual(len({row['output'] for row in agent.SOURCES}),5)
        for row in agent.SOURCES:
            self.assertIn(Path(row['output']).parent.as_posix(),['conference-feeds','journal-feeds'])

if __name__=='__main__':unittest.main(verbosity=2)
