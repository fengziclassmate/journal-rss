const assert = require('node:assert/strict');
const {walkIteratively, installConferenceWalker, installConferenceSort} = require('./walker');

function element(name, children = []) {
  const node = {nodeType: 1, namespaceURI: 'test', localName: name, attributes: [], children};
  children.forEach((child, i) => {
    child.parent = node;
    child.next = children[i + 1];
  });
  return node;
}
function walker(root) {
  return {
    currentNode: root,
    firstChild() { const n = this.currentNode.children?.[0]; if (n) this.currentNode = n; return n; },
    nextSibling() { const n = this.currentNode.next; if (n) this.currentNode = n; return n; },
    parentNode() { const n = this.currentNode.parent; if (n) this.currentNode = n; return n; },
  };
}
const events = [];
const handler = {
  startElement(ns, name) { events.push('start:' + name); },
  endElement(ns, name) { events.push('end:' + name); },
  characters(text) { events.push('text:' + text); },
  processingInstruction(target, data) { events.push('pi:' + target + ':' + data); },
};
const root = element('root', [
  element('entry', [{nodeType: 3, data: 'text'}, {nodeType: 4, data: '<body>'}]),
  {nodeType: 8}, {nodeType: 7, target: 'x', data: 'y'}, element('empty'),
]);
walkIteratively.call({_walker: walker(root), contentHandler: handler});
assert.deepEqual(events, ['start:root', 'start:entry', 'text:text', 'text:<body>', 'end:entry', 'pi:x:y', 'start:empty', 'end:empty', 'end:root']);

let starts = 0, ends = 0;
const huge = element('rss', [element('channel', Array.from({length: 30000}, () => element('item')))]);
walkIteratively.call({_walker: walker(huge), contentHandler: {startElement() { starts++; }, endElement() { ends++; }}});
assert.equal(starts, 30002);
assert.equal(ends, starts);

let forwarded = 0;
const original = function () { if (false) this._walk(); forwarded++; };
const prototype = {_walk: original};
const undo = installConferenceWalker(prototype);
prototype._walk.call({baseURI: new URL('https://other.test/feed')});
assert.equal(forwarded, 1);
undo();
assert.equal(prototype._walk, original);
const modern = {_walk() {}};
const modernWalk = modern._walk;
installConferenceWalker(modern)();
assert.equal(modern._walk, modernWalk);
console.log('SAX events, 30,000 entries, unrelated feeds, shutdown and future-version guard passed');
const proto = {getSortField() { return 'id'; }, getSortDirection() { return 1; }};
const view = Object.create(proto);
view.collectionTreeRow = {isFeed: () => true, ref: {url: 'https://fengziclassmate.github.io/journal-rss/conference-feeds/icml.xml'}};
const undoSort = installConferenceSort(proto);
assert.equal(view.getSortField(), 'date');
assert.equal(view.getSortDirection(), -1);
view.collectionTreeRow.ref.url = 'https://other.test/feed';
assert.equal(view.getSortField(), 'id');
assert.equal(view.getSortDirection(), 1);
undoSort();
assert.equal(view.getSortField(), 'id');
console.log('Conference-only newest-first display and restoration passed');
