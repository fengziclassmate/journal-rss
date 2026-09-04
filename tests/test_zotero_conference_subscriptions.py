import json
import shutil
import subprocess
import unittest

from zotero_subscribe_conferences import feed_specs, installation_script


class SubscriptionTests(unittest.TestCase):
    def test_specs_have_36_unique_urls(self):
        specs = feed_specs()
        self.assertEqual(len(specs), 36)
        self.assertEqual(len({s["url"] for s in specs}), 36)
        self.assertTrue(specs[-1]["url"].endswith("/top-conference-daily.xml"))

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the Zotero API mock")
    def test_registration_is_idempotent_and_preserves_existing_feeds(self):
        mock = r"""
const assert = require('node:assert/strict');
const feeds = new Map([['https://example.test/old.xml', {name: 'Old', libraryID: 1}]]);
let resumeCount = 0;
global.Zotero = {
  DataDirectory: {dir: 'F:\\Zotero'},
  Date: {dateToSQL: date => date.toISOString()},
  Feeds: {
    pause: async () => ({resume: () => resumeCount++}),
    getByURL: url => feeds.get(url),
    getAll: () => [...feeds.values()]
  },
  Feed: class {
    constructor(params) { Object.assign(this, params); }
    _set(key, value) { this[key] = value; }
    async saveTx() {
      this.libraryID ??= feeds.size + 1;
      feeds.set(this.url, this);
    }
  }
};
(async () => {
  const register = () => SCRIPT;
  const first = JSON.parse(await register());
  assert.equal(first.created.length, 36);
  assert.deepEqual(first.errors, []);
  const checks = [...feeds.values()].slice(1).map(f => f._feedLastCheck);
  const second = JSON.parse(await register());
  assert.equal(second.created.length, 0);
  assert.equal(second.updated.length, 36);
  assert.equal(feeds.size, 37);
  assert.equal(resumeCount, 2);
  assert.equal(feeds.get('https://example.test/old.xml').name, 'Old');
  assert.deepEqual([...feeds.values()].slice(1).map(f => f._feedLastCheck), checks);
  assert.equal(new Set(checks).size, 36);
  assert.ok([...feeds.values()].slice(1).every(f => f.refreshInterval === 1440));
  Zotero.DataDirectory.dir = 'D:\\Other';
  await assert.rejects(register, /Unexpected Zotero data directory/);
  console.log('registration, idempotence, stagger and directory guard passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("SCRIPT", installation_script(feed_specs()))
        result = subprocess.run(["node", "-e", mock], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
