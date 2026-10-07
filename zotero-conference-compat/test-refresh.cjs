const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const {installRSSRefreshCache} = require("./refresh.js");
const base = "https://fengziclassmate.github.io/journal-rss/";
const sha = body => crypto.createHash("sha256").update(body).digest("hex");

function fixture() {
  let now = 0;
  let body = "first payload";
  let remote = ["a", "b"];
  let rows = [{itemID: 1, guid: "a"}, {itemID: 2, guid: "b"}, {itemID: 3, guid: "old"}];
  const records = new Map();
  const generations = new Map();
  const calls = {native: 0, receipts: 0, expiry: [], writes: 0, generations: 0, removes: 0};
  const processorPrototype = {onResponseAvailable() {}, parseAsync() {}};
  const readerPrototype = {async process() {}};
  const readerHost = {FeedReader: function(url) {
    const processor = Object.create(processorPrototype);
    processor.parseAsync();
    processor.onResponseAvailable({url, clone() { return {async arrayBuffer() { return Buffer.from(body); }}; }});
    this._url = url;
    this._feedItems = remote.map(guid => ({guid})).concat(null).map(value => ({promise: Promise.resolve(value)}));
  }};
  readerHost.FeedReader.prototype = readerPrototype;
  const feedPrototype = {
    async _updateFeed() {
      calls.native++;
      if (env.nativeHook) await env.nativeHook.call(this);
      const reader = new readerHost.FeedReader(this.url);
      await reader.process();
      const values = (await Promise.all(reader._feedItems.map(item => item.promise))).filter(Boolean);
      for (const value of values) delete value.guid;
      for (const guid of remote) if (!rows.some(row => row.guid === guid)) rows.push({itemID: rows.length + 10, guid});
      this.lastCheckError = this.fail ? "Import failed" : null;
    },
  };
  const env = {
    feedPrototype, readerPrototype, readerHost, processorPrototype,
    now: () => now,
    generation: id => generations.get(id) || "",
    advanceGeneration: id => { calls.generations++; generations.set(id, String(Number(generations.get(id) || 0) + 1)); },
    getReceipt: async () => {
      calls.receipts++;
      if (env.failReceipt) throw new Error("Offline");
      return {version: 2, feeds: {"conference-feeds/icml.xml": env.receiptHash || sha(body)}};
    },
    read: async id => records.get(id),
    hasCache: async id => records.has(id),
    write: async (id, value) => {
      if (env.failWrite) throw new Error("Read-only cache directory");
      calls.writes++;
      records.set(id, value);
    },
    remove: async id => { calls.removes++; if (env.failRemove) throw new Error("Read-only cache"); records.delete(id); },
    isMissing: () => false,
    prepare: async () => {},
    rows: async () => rows,
    allPresent: async guids => guids.every(guid => rows.some(row => row.guid === guid)),
    hash: async bytes => sha(bytes),
    defer: () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; },
    sqlNow: () => "2026-10-07 04:00:00",
    status: async () => {},
    log: () => {},
  };
  const original = feedPrototype._updateFeed;
  const undo = installRSSRefreshCache(env);
  const feed = Object.assign(Object.create(feedPrototype), {
    libraryID: 42, url: base + "conference-feeds/icml.xml",
    _set(name, value) { this[name] = value; },
    async clearExpiredItems(ids) { calls.expiry.push([...ids]); },
    async saveTx() {}, async updateUnreadCount() {},
  });
  return {feed, calls, records, env, undo, original, rows,
    advance() { now += 31000; },
    change(value, guids) { body = value; if (guids) remote = guids; },
  };
}

(async () => {
  const f = fixture();
  await f.feed._updateFeed();
  assert.equal(f.calls.native, 1);
  assert.equal(f.records.get(42).hash, sha("first payload"));
  assert.deepEqual(f.records.get(42).remote, ["a", "b"]);
  await f.feed._updateFeed();
  assert.equal(f.calls.native, 1, "unchanged skips parser/import");
  assert.deepEqual(f.calls.expiry, [[1, 2]], "only remote members protected from expiry");
  assert.equal(f.feed._feedLastCheck, "2026-10-07 04:00:00");
  assert.equal(f.calls.receipts, 1, "receipt shared");
  assert.equal(f.feed._updating, false);
  f.advance(); f.change("new payload", ["a", "c"]);
  await f.feed._updateFeed();
  assert.equal(f.calls.native, 2, "changed hash runs full importer");
  f.rows.splice(f.rows.findIndex(row => row.guid === "c"), 1);
  await f.feed._updateFeed();
  assert.equal(f.calls.native, 3, "manual deletion triggers importer");
  f.advance(); f.env.failReceipt = true;
  await f.feed._updateFeed(); await f.feed._updateFeed();
  assert.equal(f.calls.native, 5, "offline receipt uses native importer");
  assert.equal(f.calls.receipts, 3, "failed receipt doesn't cause request storm");
  f.undo();
  assert.equal(f.env.feedPrototype._updateFeed, f.original);

  const g = fixture();
  await Promise.all([g.feed._updateFeed(), g.feed._updateFeed()]);
  assert.equal(g.calls.native, 1, "same feed coalesces concurrent refreshes");
  g.advance(); g.change("bad import"); g.feed.fail = true;
  await g.feed._updateFeed();
  assert.equal(g.records.has(42), false, "failed native processing invalidates checkpoint");
  g.feed.fail = false; g.env.failWrite = true;
  await g.feed._updateFeed();
  assert.equal(g.calls.native, 3, "cache write failure doesn't fail successful update");
  g.undo();

  const h = fixture();
  h.feed.url = "https://example.com/official.xml";
  await h.feed._updateFeed(); await h.feed._updateFeed();
  assert.equal(h.calls.native, 2); assert.equal(h.calls.receipts, 0);
  assert.equal(h.calls.generations, 0); assert.equal(h.calls.removes, 0);
  h.undo();

  const i = fixture();
  i.records.set(42, {version: 1, libraryID: 99, url: i.feed.url, hash: sha("first payload"), remote: [], local: [], foreign: []});
  await i.feed._updateFeed();
  assert.equal(i.calls.native, 1, "different library never shares checkpoint");
  i.undo();

  const j = fixture();
  await j.feed._updateFeed();
  j.feed.clearExpiredItems = async () => { throw new Error("Cleanup failed"); };
  await j.feed._updateFeed();
  assert.equal(j.calls.native, 2, "fast-path error falls back");
  assert.equal(j.feed._updating, false, "failure releases native updating flag");
  j.undo();

  const k = fixture();
  await k.feed._updateFeed();
  k.env.allPresent = async () => false;
  await k.feed._updateFeed();
  assert.equal(k.calls.native, 2, "cross-feed GUID loss forces native import");
  k.undo();

  const l = fixture();
  await l.feed._updateFeed();
  l.advance(); l.change("B"); l.env.hash = async () => null;
  await l.feed._updateFeed();
  assert.equal(l.records.has(42), false, "unprovable replacement removes old disk checkpoint");
  l.advance(); l.change("first payload");
  await l.feed._updateFeed();
  assert.equal(l.calls.native, 3, "A/B/A hash failure cannot reuse A");
  l.undo();

  const m = fixture();
  await m.feed._updateFeed();
  m.feed.url = base + "journal-feeds/other.xml";
  await m.feed._updateFeed();
  m.feed.url = base + "conference-feeds/icml.xml";
  await m.feed._updateFeed();
  assert.equal(m.calls.native, 3, "A/B/A URL cannot reuse stale A metadata");
  m.undo();

  const n = fixture();
  await n.feed._updateFeed();
  n.advance();
  let release;
  n.env.getReceipt = () => new Promise(r => { release = r; });
  const pending = n.feed._updateFeed();
  await Promise.resolve(); await Promise.resolve();
  n.undo();
  release({version:2, feeds:{"conference-feeds/icml.xml":sha("first payload")}});
  await pending;
  assert.equal(n.calls.native, 2, "disable during request restores native behavior");
  assert.equal(n.calls.expiry.length, 0);

  const o = fixture();
  let releaseHash;
  o.env.hash = () => new Promise(r => { releaseHash = r; });
  const importing = o.feed._updateFeed();
  while (!releaseHash) await Promise.resolve();
  const extra = new o.env.readerHost.FeedReader(o.feed.url);
  await extra.process();
  // Both response hashes resolve together, but capture has multiple readers.
  o.env.hash = async bytes => sha(bytes);
  releaseHash(sha("first payload"));
  await importing;
  assert.equal(o.records.has(42), false, "late independent reader invalidates capture");
  o.undo();

  const p = fixture();
  p.change("downloaded B"); p.env.receiptHash = sha("receipt A");
  await p.feed._updateFeed(); await p.feed._updateFeed();
  assert.equal(p.calls.native, 2, "deployment race must use actual response hash");
  p.advance(); p.env.receiptHash = sha("downloaded B");
  await p.feed._updateFeed();
  assert.equal(p.calls.native, 2, "matching receipt after deployment can skip");
  p.undo();

  const q = fixture();
  await q.feed._updateFeed();
  q.advance(); q.change("B"); q.env.failWrite = true; q.env.failRemove = true;
  await q.feed._updateFeed();
  q.undo();
  q.advance(); q.change("first payload");
  const reinstall = installRSSRefreshCache(q.env);
  await q.feed._updateFeed();
  assert.equal(q.calls.native, 3, "durable generation rejects stale file after failed deletion and restart");
  reinstall();

  const r = fixture();
  r.env.nativeHook = async function () {
    this._updating = new Promise(() => {});
    throw new Error("Transaction timeout");
  };
  await assert.rejects(r.feed._updateFeed(), /Transaction timeout/);
  assert.equal(r.feed._updating, false, "failed native import releases orphaned status");
  r.env.nativeHook = null;
  await r.feed._updateFeed();
  assert.equal(r.calls.native, 2, "failed feed can retry");
  r.undo();

  const s = fixture();
  let concurrent = 0, maximum = 0;
  s.env.nativeHook = async () => {
    maximum = Math.max(maximum, ++concurrent);
    await new Promise(resolve => setTimeout(resolve, 10));
    concurrent--;
  };
  const second = Object.assign(Object.create(s.env.feedPrototype), s.feed, {libraryID:43});
  await Promise.all([s.feed._updateFeed(), second._updateFeed()]);
  assert.equal(maximum, 1, "large managed imports serialize native transactions");
  s.undo();

  const t = fixture();
  t.env.advanceGeneration = () => { throw new Error("Preference save failed"); };
  await t.feed._updateFeed(); await t.feed._updateFeed();
  assert.equal(t.calls.native, 2, "generation storage failure preserves native fallback");
  assert.equal(t.records.has(42), false, "failed persistence never produces a checkpoint");
  t.undo();

  const u = fixture();
  u.feed.url = "https://example.com/official.xml";
  u.env.hasCache = async () => { throw new Error("Cache path inaccessible"); };
  await u.feed._updateFeed();
  assert.equal(u.calls.native, 1, "unrelated cache existence error preserves native fallback");
  u.undo();
  console.log("RSS refresh cache tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
