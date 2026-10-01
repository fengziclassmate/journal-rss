import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from journal_rss_aggregator import FeedItem, parse_official_feed_identity_keys, filter_official_duplicates
from official_priority import reconcile


TITLE='Morphological quantification of population activity hotspots and their nonlinear relationships'
OFFICIAL=f'<rss><channel><item><title>{TITLE}</title><link>https://www.sciencedirect.com/science/article/pii/S014362282600305X?dgcid=rss_sd_all</link><description>&lt;p&gt;Author(s): Junhao Wang, Rui Li&lt;/p&gt;</description></item></channel></rss>'


class OfficialPriorityTests(unittest.TestCase):
    def paper(self, **changes):
        args=dict(source='Applied Geography',title=TITLE,link='https://doi.org/10.1016/j.apgeog.2026.104195',
                  guid='10.1016/j.apgeog.2026.104195',description='Junhao Wang, Rui Li<br/>Applied Geography<br/>DOI: 10.1016/j.apgeog.2026.104195')
        args.update(changes)
        return FeedItem(**args)

    def test_pii_and_doi_match_by_same_journal_title_and_authors(self):
        audit=[]
        self.assertEqual(filter_official_duplicates([self.paper()],parse_official_feed_identity_keys(OFFICIAL.encode()),audit,'official'),[])
        self.assertEqual(audit[0]['matched'],['same-journal:title-and-authors'])

    def test_different_authors_or_different_dois_are_not_hidden(self):
        keys=parse_official_feed_identity_keys(OFFICIAL.encode())
        paper=self.paper(description='Other Author<br/>Applied Geography')
        self.assertEqual(filter_official_duplicates([paper],keys,official_url='official'),[paper])
        raw=OFFICIAL.replace('</item>','<doi>10.1016/different</doi></item>')
        paper=self.paper()
        self.assertEqual(filter_official_duplicates([paper],parse_official_feed_identity_keys(raw.encode()),official_url='official'),[paper])

    def test_title_alone_and_unscoped_match_are_retained(self):
        paper=self.paper()
        keys=parse_official_feed_identity_keys(OFFICIAL.encode())
        self.assertEqual(filter_official_duplicates([paper],keys),[paper])
        paper=self.paper(description='')
        self.assertEqual(filter_official_duplicates([paper],keys,official_url='official'),[paper])

    def test_publish_pass_filters_existing_xml_without_changing_official(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); official=root/'official.xml'; official.write_text(OFFICIAL)
            custom=root/'custom.xml'
            custom.write_text(f'<rss><channel><item><title>{TITLE}</title><guid>10.1016/j.apgeog.2026.104195</guid><link>https://doi.org/10.1016/j.apgeog.2026.104195</link><description>Junhao Wang, Rui Li&lt;br/&gt;Applied Geography</description></item></channel></rss>')
            with patch('official_priority.CROSSREF_JOURNALS',[{'source':'Applied Geography','issn':'test','output':'custom.xml'}]), \
                 patch('official_priority.OFFICIAL_FEED_URLS',{'test':'official'}), \
                 patch('official_priority.load_official_mirror_paths',return_value={'official':official}):
                first=reconcile(root)
                second=reconcile(root)
            self.assertEqual(first[0]['removed'],1)
            self.assertEqual(second[0]['removed'],0)
            self.assertEqual(official.read_text(),OFFICIAL)
            self.assertEqual(ET.parse(custom).findall('.//item'),[])

    def test_workflow_always_reconciles_before_personal_read_filter(self):
        text=Path('.github/workflows/update-feed.yml').read_text()
        step=text.split('- name: Enforce official priority on every publication')[1].split('- name:')[0]
        self.assertNotIn('if:',step)
        self.assertLess(text.index('run: python official_priority.py'),text.index('- name: Remove previously read Zotero items'))


if __name__=='__main__': unittest.main()
