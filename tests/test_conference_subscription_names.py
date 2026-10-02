import unittest
from zotero_subscribe_conferences import feed_specs


class ConferenceSubscriptionNamesTests(unittest.TestCase):
    def test_unumbered_names_and_original_urls(self):
        specs = feed_specs()
        self.assertEqual(len(specs), 36)
        self.assertEqual(len({spec['url'] for spec in specs}), 36)
        self.assertTrue(all(spec['name'].startswith('Conf ') for spec in specs))
        self.assertTrue(all('|' not in spec['name'] for spec in specs))
        names = {spec['name'] for spec in specs}
        self.assertTrue({'Conf NeurIPS', 'Conf ICML', 'Conf Daily'} <= names)
        self.assertTrue(specs[-1]['url'].endswith('/top-conference-daily.xml'))
