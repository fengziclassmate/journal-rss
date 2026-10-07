// Only successful imports of an exact downloaded payload can create a checkpoint.
function installRSSRefreshCache(env) {
  const base = "https://fengziclassmate.github.io/journal-rss/";
  const active = new Map();
  const running = new Map();
  const checkpoints = new Map();
  const disabledLibraries = new Set();
  const processorContexts = new WeakMap();
  const readerContexts = new WeakMap();
  let constructing = null;
  const stats = {skipped: 0, full: 0, receipts: 0, failures: 0};
  let receiptPromise;
  let receiptExpires = 0;
  let fullTail = Promise.resolve();
  let stopped = false;
  const originalUpdate = env.feedPrototype._updateFeed;
  const originalProcess = env.readerPrototype.process;
  const originalResponse = env.processorPrototype.onResponseAvailable;
  const originalParse = env.processorPrototype.parseAsync;
  const OriginalReader = env.readerHost.FeedReader;
  const pathFor = url => typeof url === "string" && url.startsWith(base)
    && /^[a-z0-9_./-]+\.xml$/i.test(url.slice(base.length)) ? url.slice(base.length) : null;
  const keyFor = feed => feed.libraryID;
  const warn = error => { stats.failures++; stats.lastFailure = String(error); env.log(error); };

  async function receipt() {
    if (!receiptPromise || env.now() >= receiptExpires) {
      receiptExpires = env.now() + 30000;
      stats.receipts++;
      receiptPromise = env.getReceipt(base + "read-filter-status.json").then(value => {
        if (value?.version !== 2 || !value.feeds) throw new Error("Invalid RSS receipt");
        return value.feeds;
      }).catch(error => { warn(error); return null; });
    }
    return receiptPromise;
  }

  function valid(record, feed) {
    return !disabledLibraries.has(feed.libraryID) && record?.version === 1 && record.url === feed.url
      && record.libraryID === feed.libraryID && /^[a-f0-9]{64}$/.test(record.hash)
      && Array.isArray(record.remote) && record.remote.every(x => typeof x === "string")
      && Array.isArray(record.local) && record.local.every(x => typeof x === "string")
      && Array.isArray(record.foreign) && record.foreign.every(x => typeof x === "string")
      && (record.generation || "") === env.generation(feed.libraryID)
      && (record.lastUpdate === undefined || record.lastUpdate === feed.lastUpdate);
  }

  async function checkpoint(feed) {
    const key = keyFor(feed);
    if (!checkpoints.has(key)) {
      try {
        const value = await env.read(feed.libraryID);
        checkpoints.set(key, valid(value, feed) ? value : null);
      } catch (error) {
        checkpoints.set(key, null);
        if (!env.isMissing(error)) warn(error);
      }
    }
    const value = checkpoints.get(key);
    return valid(value, feed) ? value : null;
  }

  async function invalidate(feed) {
    checkpoints.set(keyFor(feed), null);
    try { await env.remove(feed.libraryID); } catch (error) { warn(error); }
  }

  const Reader = function (url) {
    const context = active.get(url);
    const previous = constructing;
    constructing = context || null;
    if (context) context.readers++;
    try {
      const reader = Reflect.construct(OriginalReader, [...arguments]);
      if (context) readerContexts.set(reader, context);
      return reader;
    } finally { constructing = previous; }
  };
  Object.setPrototypeOf(Reader, OriginalReader);
  Reader.prototype = OriginalReader.prototype;
  const parse = function () {
    if (constructing) processorContexts.set(this, constructing);
    return originalParse.apply(this, arguments);
  };

  const response = function (result) {
    const context = processorContexts.get(this);
    if (context) {
      context.responses++;
      // A concurrent independent reader of the same URL invalidates this checkpoint.
      if (context.responses === 1) {
        try {
          context.hash = result.clone().arrayBuffer().then(env.hash).catch(error => {
            warn(error);
            return null;
          });
        } catch (error) { warn(error); }
      }
    }
    return originalResponse.apply(this, arguments);
  };

  const process = async function () {
    const context = readerContexts.get(this);
    const result = await originalProcess.apply(this, arguments);
    if (context && Array.isArray(this._feedItems)) {
      // Capture identity before Feed._updateFeed deletes guid from parsed JSON.
      const values = await Promise.all(this._feedItems.map(item => item.promise));
      if (!context.remote) context.remote = [...new Set(values.filter(Boolean).map(item => item.guid))];
    }
    return result;
  };

  async function unchanged(feed, record, rows) {
    const protectedGUIDs = new Set(record.remote);
    const protectedIDs = new Set(rows.filter(row => protectedGUIDs.has(row.guid)).map(row => row.itemID));
    const deferred = env.defer();
    feed._updating = deferred.promise;
    try {
      await env.status(feed);
      feed._set("_feedLastCheckError", null);
      // Preserve Zotero's rule: expiry only removes entries absent from this RSS.
      await feed.clearExpiredItems(protectedIDs);
      feed._set("_feedLastCheck", env.sqlNow());
      await feed.saveTx();
      await feed.updateUnreadCount();
      stats.skipped++;
    } finally {
      feed._updating = false;
      deferred.resolve();
      await env.status(feed);
    }
  }

  async function update(feed, args) {
    const key = keyFor(feed);
    const path = pathFor(feed.url);
    if (stopped || !path) return originalUpdate.apply(feed, args);
    if (feed._updating) return feed._updating;
    try {
      const [hashes, record] = await Promise.all([receipt(), checkpoint(feed)]);
      if (stopped) return originalUpdate.apply(feed, args);
      if (feed._updating) return feed._updating;
      if (record && hashes?.[path] === record.hash) {
        const rows = await env.rows(feed.libraryID);
        const present = new Set(rows.map(row => row.guid));
        // A user-deleted/import-missing entry must still be handled by the native importer.
        if (record.local.every(guid => present.has(guid)) && await env.allPresent(record.foreign)
          && valid(record, feed)) {
          if (stopped) return originalUpdate.apply(feed, args);
          if (feed._updating) return feed._updating;
          await unchanged(feed, record, rows);
          return;
        }
      }
    } catch (error) {
      warn(error);
    }
    // Large native imports write long transactions; do not run them concurrently.
    const pending = fullTail.then(() => full(feed, args), () => full(feed, args));
    fullTail = pending.catch(() => {});
    return pending;
  }

  async function full(feed, args) {
    if (stopped) return originalUpdate.apply(feed, args);
    if (feed._updating) return feed._updating;
    const key = keyFor(feed);
    stats.full++;
    try { await env.advanceGeneration(feed.libraryID); }
    catch (error) { disabledLibraries.add(feed.libraryID); warn(error); }
    await invalidate(feed);
    if (stopped) return originalUpdate.apply(feed, args);
    if (feed._updating) return feed._updating;
    const context = {url: feed.url, responses: 0, readers: 0, remote: null, hash: null};
    // A second library pointing at the same URL falls back without caching either import.
    const previous = active.get(feed.url);
    if (previous) previous.responses++;
    else active.set(feed.url, context);
    const captureValid = () => !stopped && !disabledLibraries.has(feed.libraryID) && !previous && !feed.lastCheckError
      && feed.url === context.url && active.get(context.url) === context
      && context.responses === 1 && context.readers === 1;
    try {
      await env.prepare(feed);
      const importing = originalUpdate.apply(feed, args);
      context.updating = feed._updating;
      const result = await importing;
      try {
        const hash = await context.hash;
        if (hash && captureValid() && context.remote?.every(guid => typeof guid === "string")) {
          const remote = new Set(context.remote);
          const rows = await env.rows(feed.libraryID);
          const local = rows.filter(row => remote.has(row.guid)).map(row => row.guid);
          const present = new Set(local);
          const foreign = [...remote].filter(guid => !present.has(guid));
          if (await env.allPresent(foreign) && captureValid()) {
            const record = {
              version: 1, libraryID: feed.libraryID, url: feed.url, hash,
              generation: env.generation(feed.libraryID),
              lastUpdate: feed.lastUpdate,
              remote: [...remote], local, foreign,
            };
            await env.write(feed.libraryID, record);
            if (captureValid()) checkpoints.set(key, record);
            else await invalidate(feed);
          }
        }
      } catch (error) { await invalidate(feed); warn(error); }
      return result;
    } catch (error) {
      await invalidate(feed);
      if (context.updating && feed._updating === context.updating) {
        feed._updating = false;
        feed._set("_feedLastCheckError", String(error));
        await env.status(feed);
      }
      throw error;
    } finally {
      if (active.get(context.url) === context) active.delete(context.url);
    }
  }

  const replacement = function () {
    if (stopped) return originalUpdate.apply(this, arguments);
    if (!pathFor(this.url)) {
      const feed = this, args = arguments;
      return (async () => {
        try {
          if (!checkpoints.has(keyFor(feed)) && !env.generation(feed.libraryID)
            && !await env.hasCache(feed.libraryID)) return originalUpdate.apply(feed, args);
        } catch (error) { warn(error); return originalUpdate.apply(feed, args); }
        try { await env.advanceGeneration(feed.libraryID); }
        catch (error) { disabledLibraries.add(feed.libraryID); warn(error); }
        await invalidate(feed);
        return originalUpdate.apply(feed, args);
      })();
    }
    const key = keyFor(this);
    if (running.has(key)) return running.get(key);
    const promise = update(this, arguments).finally(() => running.delete(key));
    running.set(key, promise);
    return promise;
  };
  env.feedPrototype._updateFeed = replacement;
  env.readerPrototype.process = process;
  env.processorPrototype.onResponseAvailable = response;
  env.processorPrototype.parseAsync = parse;
  env.readerHost.FeedReader = Reader;
  const undo = () => {
    stopped = true;
    if (env.feedPrototype._updateFeed === replacement) env.feedPrototype._updateFeed = originalUpdate;
    if (env.readerPrototype.process === process) env.readerPrototype.process = originalProcess;
    if (env.processorPrototype.onResponseAvailable === response) env.processorPrototype.onResponseAvailable = originalResponse;
    if (env.processorPrototype.parseAsync === parse) env.processorPrototype.parseAsync = originalParse;
    if (env.readerHost.FeedReader === Reader) env.readerHost.FeedReader = OriginalReader;
    checkpoints.clear();
  };
  undo.stats = stats;
  return undo;
}

if (typeof module !== "undefined") module.exports = {installRSSRefreshCache};
