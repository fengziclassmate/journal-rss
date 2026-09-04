const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const hooks = require('./walker');
class ItemTree {
  getSortField() { return 'id'; }
  getSortDirection() { return 1; }
}
const window = {require: () => ItemTree, ZoteroPane: {itemsView: false}};
const reader = {_walk() { if (false) this._walk(); }};
const context = {
  Zotero: {
    initializationPromise: Promise.resolve(), uiReadyPromise: Promise.resolve(),
    getMainWindows: () => [window], FeedItem: {prototype: {fromJSON() {}}},
  },
  Services: {scriptloader: {loadSubScript(url, scope) { Object.assign(scope, hooks); }}},
  ChromeUtils: {importESModule() { return {SAXXMLReader: {prototype: reader}}; }},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'bootstrap.js'), 'utf8'), context);
(async () => {
  await context.startup({rootURI: 'test://addon/'});
  const view = new ItemTree();
  view.collectionTreeRow = {isFeed: () => true, ref: {url: 'https://fengziclassmate.github.io/journal-rss/conference-feeds/icml.xml'}};
  assert.equal(view.getSortField(), 'date');
  assert.equal(view.getSortDirection(), -1);
  context.shutdown();
  assert.equal(view.getSortField(), 'id');
  console.log('Startup before view creation installs sorting; shutdown restores it');
})().catch(error => { console.error(error); process.exitCode = 1; });
