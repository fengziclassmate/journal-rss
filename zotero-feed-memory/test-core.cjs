const { test } = require('node:test');
const assert = require('node:assert/strict');
const C = require('./core.js');
const item = overrides => ({ title: 'A detailed study of spatial accessibility in urban systems',
  url: 'https://doi.org/10.1016/j.test.2026.123', guid: '10.1016/j.test.2026.123',
  feedURL: 'https://fengziclassmate.github.io/journal-rss/ceus.xml', ...overrides });
const memory = () => new C.Memory(C.emptyState('test-token'));

test('translation survives JSON roundtrip and is separated by target language and original title', () => {
  const m = memory();
  m.rememberTranslation(item().title, 'zh', 'translated');
  const restored = new C.Memory(JSON.parse(JSON.stringify(m.data)));
  assert.equal(restored.translation(item().title, 'zh'), 'translated');
  assert.equal(restored.translation(item().title, 'de'), '');
  assert.equal(restored.translation('Revised title', 'zh'), '');
  assert.equal(restored.rememberTranslation('title', 'zh', ''), false);
});
test('read ledger survives item deletion and a changed feed GUID', () => {
  const m = memory();
  const record = m.observe(item({ readAt: 1000 }));
  const restored = new C.Memory(JSON.parse(JSON.stringify(m.data)));
  const reappeared = restored.observe(item({ guid: 'different-guid' }));
  assert.equal(record.key, reappeared.key);
  assert.equal(reappeared.readAt, 1000);
  assert.equal(restored.expired(reappeared, 30, 1000 + 29 * C.DAY), false);
  assert.equal(restored.expired(reappeared, 30, 1000 + 30 * C.DAY), true);
});
test('DOI duplicate across official and custom sources, independent of tracking parameters', () => {
  const m = memory();
  const custom = m.observe(item());
  m.observe(item({ feedURL: 'https://rss.sciencedirect.com/publication/science/01989715', official: true }));
  assert.equal(m.duplicate(custom, item().feedURL), 'official-id');
  assert.equal(C.safeURL('https://x.test/p?a=1&dgcid=rss_sd_all'), 'https://x.test/p?a=1');
});
test('same title without shared identifier is labeled as title match, not confirmed DOI duplicate', () => {
  const m = memory();
  const custom = m.observe(item());
  m.observe(item({ guid: 'pii-1', url: 'https://www.sciencedirect.com/science/article/pii/S0198971526000906',
    feedURL: 'https://rss.sciencedirect.com/publication/science/01989715', official: true }));
  assert.equal(m.duplicate(custom, item().feedURL), 'official-title');
});
test('different DOIs and generic titles are never treated as proven duplicates', () => {
  const m = memory();
  const custom = m.observe(item());
  m.observe(item({ url: 'https://doi.org/10.1016/another', guid: '10.1016/another', official: true, feedURL: 'https://official.test/rss' }));
  assert.equal(m.duplicate(custom, item().feedURL), '');
  const a = m.observe(item({ title: 'Editorial', guid: 'ed1', url: 'https://a.test/ed' }));
  m.observe(item({ title: 'Editorial', guid: 'ed2', url: 'https://b.test/ed', official: true, feedURL: 'https://b.test/rss' }));
  assert.equal(m.duplicate(a, item().feedURL), '');
});
test('archive uses its own GUID namespace and keeps private state out of source identifiers', () => {
  const m = memory(); const record = m.observe(item());
  assert.notEqual(m.archiveGUID(record), item().guid);
  assert.equal(m.fromArchiveGUID(m.archiveGUID(record)), record);
  assert.equal(m.fromArchiveGUID(item().guid), null);
});
test('updating translation retains unrelated Extra fields', () => {
  const extra = C.setExtra('DOI: 10.1/a\ntitleTranslation: old\nSome other text', 'titleTranslation', 'new');
  assert.equal(C.getExtra(extra, 'titleTranslation'), 'new');
  assert.match(extra, /Some other text/);
  assert.match(extra, /DOI: 10.1\/a/);
});
test('malformed cache fails closed', () => {
  assert.throws(() => new C.Memory({ version: 2 }));
  assert.throws(() => new C.Memory({ ...C.emptyState('x'), records: { x: {} } }));
});
test('marking unread does not restore an older read timestamp from another subscription', () => {
  const m = memory();
  const r = m.observe(item({readAt:1000}));
  r.readAt=null; r.unreadAt=2000;
  m.observe(item({readAt:1000}));
  assert.equal(r.readAt,null);
  m.observe(item({readAt:3000}));
  assert.equal(r.readAt,3000);
});
