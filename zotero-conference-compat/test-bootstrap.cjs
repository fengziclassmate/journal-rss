const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const hooks = require('./walker');
const refresh = require('./refresh');
const manifest = require('./manifest.json');
assert.equal(manifest.version, '1.1.2');
assert.equal(manifest.applications.zotero.strict_min_version, '9.0');
assert.equal(manifest.applications.zotero.strict_max_version, '10.0.*');
class ItemTree {
  getSortField() { return 'id'; }
  getSortDirection() { return 1; }
}
const window = {require: () => ItemTree, ZoteroPane: {itemsView: false}};
const reader = {_walk() { if (false) this._walk(); }};
const nativeUpdate = async function () {};
const feedPrototype = {_updateFeed: nativeUpdate};
const process = async function () {};
const response = function () {};
const parse = function () {};
const processorPrototype = {onResponseAvailable: response, parseAsync: parse};
const Reader = function () {};
Reader.prototype = {process};
const context = {
  PathUtils: {join: (...parts) => parts.join('/')},
  IOUtils: {},
  Zotero: {
    initializationPromise: Promise.resolve(), uiReadyPromise: Promise.resolve(),
    getMainWindows: () => [window], FeedItem: {prototype: {fromJSON() {}}},
    Feed: {prototype: feedPrototype}, FeedReader: Reader,
    DataDirectory: {dir: 'F:/Zotero'},
  },
  Services: {scriptloader: {loadSubScript(url, scope) { Object.assign(scope, url.endsWith('refresh.js') ? refresh : hooks); }}},
  ChromeUtils: {importESModule() { return {SAXXMLReader: {prototype: reader}, FeedProcessor: {prototype: processorPrototype}}; }},
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'bootstrap.js'), 'utf8'), context);
(async () => {
  await context.startup({rootURI: 'test://addon/'});
  const view = new ItemTree();
  view.collectionTreeRow = {isFeed: () => true, ref: {url: 'https://fengziclassmate.github.io/journal-rss/conference-feeds/icml.xml'}};
  assert.equal(view.getSortField(), 'date');
  assert.equal(view.getSortDirection(), -1);
  assert.notEqual(feedPrototype._updateFeed, nativeUpdate);
  assert.notEqual(context.Zotero.FeedReader, Reader);
  assert.notEqual(processorPrototype.parseAsync, parse);
  assert.equal(context.Zotero.__rssRefreshStats.skipped, 0);
  context.shutdown();
  assert.equal(view.getSortField(), 'id');
  assert.equal(feedPrototype._updateFeed, nativeUpdate);
  assert.equal(context.Zotero.FeedReader, Reader);
  assert.equal(processorPrototype.parseAsync, parse);
  assert.equal(processorPrototype.onResponseAvailable, response);
  assert.equal(Reader.prototype.process, process);
  assert.equal(context.Zotero.__rssRefreshStats, undefined);
  console.log('Startup before view creation installs sorting; shutdown restores it');
})().catch(error => { console.error(error); process.exitCode = 1; });
