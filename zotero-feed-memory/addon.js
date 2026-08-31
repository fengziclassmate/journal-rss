/* global Zotero, Services, IOUtils, PathUtils, RSSMemoryCore */
var RSSMemoryAddon = (() => {
  "use strict";
  const C = RSSMemoryCore;
  const ID = "journal-rss-memory@fengziclassmate";
  const PREF = "extensions.zotero.journalRSSMemory.enabled";
  const OWN_HOST = "fengziclassmate.github.io";
  const officialHosts = new Set(["rss.sciencedirect.com", "www.tandfonline.com", "ieeexplore.ieee.org", "www.dqxxkx.cn", "www.ygxb.ac.cn", "ch.whu.edu.cn"]);
  let memory, path, alive = true, observer, column, originalUpdate, updateWrapper, originalJSON, jsonWrapper;
  let running = false, pending = false, started = false, archive, endpointPath, endpoint, serviceBusy = false;
  let lastError = "", translatedCount = 0, lastOfficialCheck = 0, officialChecking = false;
  let writeQueue = Promise.resolve();
  const status = new Map(), priorRead = new Map(), retries = new Map();
  const pref = key => Zotero.Prefs.get(`ZoteroPDFTranslate.${key}`);
  const target = () => pref("targetLanguage") || "zh";
  const enabled = () => Zotero.Prefs.get(PREF, true) === true;
  const log = error => { lastError = String(error); Zotero.logError(error); };
  const field = (item, name) => { try { return item.getField(name) || ""; } catch { return ""; } };
  const isOfficial = url => { try { return officialHosts.has(new URL(url).hostname); } catch { return false; } };
  const isOwn = url => { try { const u = new URL(url); return u.hostname === OWN_HOST && u.pathname.startsWith('/journal-rss/'); } catch { return false; } };
  const isArchive = feed => !!feed && feed.url === archiveURL();
  function archiveURL() {
    const port = Zotero.Prefs.get("httpServer.port") || 23119;
    return `http://127.0.0.1:${port}/journal-rss-memory/${memory.data.token}/read.xml`;
  }
  function meta(item, feed) {
    const time = item._feedItemReadTime;
    return {
      title: field(item, "title"), doi: field(item, "DOI"), url: field(item, "url"),
      description: field(item, "abstractNote"), date: field(item, "date"), guid: item.guid,
      feedURL: feed.url, feedName: feed.name, official: isOfficial(feed.url),
      readAt: time ? Date.parse(time.replace(" ", "T") + "Z") || Date.now() : null,
    };
  }
  function flush() {
    const snapshot = JSON.stringify(memory.data);
    const next = writeQueue.catch(() => {}).then(async () => {
      await IOUtils.writeUTF8(path, snapshot, { tmpPath: path + ".tmp" });
    });
    writeQueue = next;
    return next;
  }
  function capture(item, feed) {
    const record = memory.observe(meta(item, feed));
    const extra = field(item, "extra");
    const text = C.getExtra(extra, "titleTranslation");
    const lang = C.getExtra(extra, "rssMemoryLanguage") || target();
    if (text) memory.rememberTranslation(field(item, "title"), lang, text);
    return record;
  }
  async function itemsIn(feed) {
    await feed.waitForDataLoad?.("item");
    return Zotero.FeedItems.getAll(feed.id, false, false, false);
  }
  async function captureFeed(feed) {
    if (isArchive(feed)) return;
    for (const item of await itemsIn(feed)) {
      if (!item.isFeedItem) continue;
      capture(item, feed);
    }
    await flush();
  }
  function xml(value) {
    return String(value || "").replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function archiveXML() {
    const records = Object.values(memory.data.records).filter(r => r.readAt).sort((a, b) => b.readAt - a.readAt);
    const entries = records.map(r => {
      const translation = memory.translation(r.title, target());
      const description = [translation, r.doi, ...Object.values(r.sources).map(s => s.name)].filter(Boolean).join(" | ");
      return `<item><title>${xml(r.title)}</title><link>${xml(r.url)}</link>`
        + `<guid isPermaLink="false">${xml(memory.archiveGUID(r))}</guid>`
        + `<description>${xml(description)}</description><pubDate>${new Date(r.readAt).toUTCString()}</pubDate></item>`;
    }).join("");
    return `<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>RSS 已读归档</title>`
      + `<link>${xml(archiveURL())}</link><description>Local read history</description>${entries}</channel></rss>`;
  }
  async function ensureArchive() {
    archive = Zotero.Feeds.getAll().find(isArchive);
    if (!archive) {
      archive = new Zotero.Feed({ name: "RSS 已读归档（本机）", url: archiveURL(),
        refreshInterval: 1440, cleanupReadAfter: 365000, cleanupUnreadAfter: 365000 });
      await archive.saveTx();
    }
    return archive;
  }
  async function saveTranslation(item, text, language) {
    let extra = C.setExtra(field(item, "extra"), "titleTranslation", text);
    extra = C.setExtra(extra, "rssMemoryLanguage", language);
    if (extra !== field(item, "extra")) {
      item.setField("extra", extra);
      await item.saveTx();
    }
  }
  async function syncArchive() {
    await ensureArchive();
    for (const item of await itemsIn(archive)) {
      const record = memory.fromArchiveGUID(item.guid);
      if (record && !record.readAt) await Zotero.FeedItems.erase([item.id]);
    }
    // No network extraction, notes, attachments or PDFs are copied.
    for (const r of Object.values(memory.data.records)) {
      if (!r.readAt) continue;
      const guid = memory.archiveGUID(r);
      let item = await Zotero.FeedItems.getAsyncByGUID(guid);
      if (!item) {
        item = new Zotero.FeedItem("journalArticle");
        item.libraryID = archive.id;
        item.guid = guid;
      }
      item.setField("title", r.title);
      item.setField("url", r.url);
      if (r.doi) item.setField("DOI", r.doi);
      if (r.date) item.setField("date", r.date);
      item.isRead = true;
      const text = memory.translation(r.title, target());
      let extra = C.setExtra(field(item, "extra"), "rssReadAt", new Date(r.readAt).toISOString());
      extra = C.setExtra(extra, "rssSources", Object.values(r.sources).map(s => s.name).join("; "));
      if (text) {
        extra = C.setExtra(extra, "titleTranslation", text);
        extra = C.setExtra(extra, "rssMemoryLanguage", target());
      }
      item.setField("extra", extra);
      if (item.hasChanged()) await item.saveTx();
    }
    await archive.updateUnreadCount();
  }
  async function scan() {
    const feeds = Zotero.Feeds.getAll().filter(f => !isArchive(f));
    const snapshots = [];
    for (const feed of feeds) {
      if (feed.updating) { pending = true; continue; }
      for (const item of await itemsIn(feed)) {
        if (!item.isFeedItem) continue;
        const record = capture(item, feed);
        if (record) snapshots.push({ item, feed, record });
      }
    }
    await flush();
    await syncArchive();
    for (const { item, feed, record } of snapshots) {
      if (!alive || !enabled()) return;
      const overlap = memory.duplicate(record, feed.url);
      status.set(item.id, overlap === "official-id" ? "曾见官方（标识一致）"
        : overlap === "official-title" ? "官方同题（待核对）" : "");
      const text = memory.translation(field(item, "title"), target());
      if (text) await saveTranslation(item, text, target());
      if (record.readAt && !item.isRead) {
        item.isRead = true;
        await item.saveTx();
      } else if (!record.readAt && record.unreadAt && item.isRead) {
        item.isRead = false;
        await item.saveTx();
      }
      priorRead.set(item.id, item.isRead);
      // Verify both durable memory and archive copy before removing an expired feed item.
      if (memory.expired(record, feed.cleanupReadAfter || 30)
          && await Zotero.FeedItems.getAsyncByGUID(memory.archiveGUID(record))) {
        await Zotero.FeedItems.erase([item.id]);
        status.delete(item.id);
        priorRead.delete(item.id);
      }
    }
    for (const feed of feeds) await feed.updateUnreadCount();
    for (const win of Zotero.getMainWindows()) win.ZoteroPane?.itemsView?.refresh();
    return snapshots;
  }
  async function translateOne(item) {
    const title = field(item, "title"), language = target();
    if (!title || memory.translation(title, language)) return;
    const letters = (title.match(/[A-Za-z]/g) || []).length;
    const han = (title.match(/[\u3400-\u9fff]/g) || []).length;
    if (language.startsWith("zh") && han > letters) return;
    const key = C.translationKey(title, language);
    if ((retries.get(key) || 0) > Date.now()) return;
    if (!Zotero.PDFTranslate?.api?.translate) { lastError = "Translate for Zotero 未启用"; return; }
    if (serviceBusy) return;
    serviceBusy = true;
    const request = Promise.resolve().then(() => Zotero.PDFTranslate.api.translate(title, {
      pluginID: ID, langfrom: pref("sourceLanguage") || "en", langto: language,
    })).finally(() => { serviceBusy = false; });
    const result = await Promise.race([request, Zotero.Promise.delay(45000).then(() => {
      throw new Error("翻译请求超时；未完成的请求结束前不再发送新请求");
    })]);
    if (!alive || !enabled()) return;
    if (result.status !== "success" || !String(result.result || "").trim()) {
      retries.set(key, Date.now() + 3600000);
      lastError = "标题翻译失败，1 小时后重试；检查原翻译插件的服务设置";
      return;
    }
    memory.rememberTranslation(title, language, result.result);
    await flush();
    const current = await Zotero.Items.getAsync(item.id);
    if (current && C.titleKey(field(current, "title")) === C.titleKey(title)) {
      await saveTranslation(current, result.result, language);
    }
    translatedCount++;
  }
  async function pump() {
    if (running || !alive || !enabled() || !memory) return;
    running = true;
    try {
      pending = false;
      const snapshots = await scan() || [];
      const seen = new Set();
      // A bounded serial batch avoids flooding the user's configured translation service.
      let count = 0;
      for (const { item } of snapshots) {
        if (!alive || !enabled() || count >= 20) break;
        const title = field(item, "title");
        if (memory.translation(title, target()) || seen.has(C.titleKey(title))) continue;
        if (!await Zotero.Items.getAsync(item.id)) continue;
        seen.add(C.titleKey(title));
        try { await translateOne(item); } catch (error) { log(error); retries.set(C.translationKey(title, target()), Date.now() + 3600000); }
        count++;
        await Zotero.Promise.delay(1500);
      }
      await flush();
    } catch (error) { log(error); }
    finally { running = false; }
  }
  function schedule() {
    if (pending || !enabled()) return;
    pending = true;
    Zotero.Promise.delay(1000).then(() => { if (alive) return pump(); }).catch(log);
  }
  async function officialRefresh() {
    if (officialChecking || Date.now() - lastOfficialCheck < 6 * 3600000) return;
    officialChecking = true;
    try {
      const journals = new Map([
        ["pattern-recognition", "00313203"], ["scs", "22106707"], ["asc", "15684946"],
        ["applied-geography", "01436228"], ["habitat-international", "01973975"],
        ["information-fusion", "15662535"], ["cities", "02642751"], ["ceus", "01989715"],
      ]);
      const needed = new Set();
      for (const feed of Zotero.Feeds.getAll()) {
        if (!isOwn(feed.url)) continue;
        const name = new URL(feed.url).pathname.split('/').pop().replace(/-current-issue\.xml$/, '').replace(/\.xml$/, '');
        if (journals.has(name)) needed.add(name);
      }
      for (const name of needed) {
        if (!alive || !enabled()) break;
        const url = `https://rss.sciencedirect.com/publication/science/${journals.get(name)}`;
        const reader = new Zotero.FeedReader(url);
        try {
          await reader.process();
          const iterator = new reader.ItemIterator();
          let data;
          while ((data = await iterator.next().value)) {
            memory.observe({ title: data.title, guid: data.guid, url: data.url, doi: data.DOI,
              description: data.abstractNote, date: data.date, feedURL: url, feedName: name + ' official RSS', official: true });
          }
        } catch (error) { Zotero.debug(`Journal RSS Memory: official comparison unavailable for ${name}`); }
        finally { reader.terminate(); }
      }
      lastOfficialCheck = Date.now();
      await flush();
      schedule();
    } finally { officialChecking = false; }
  }
  function installHooks() {
    originalUpdate = Zotero.Feed.prototype._updateFeed;
    updateWrapper = async function (...args) {
      if (!enabled() || !alive) return originalUpdate.apply(this, args);
      if (isArchive(this)) return syncArchive();
      await captureFeed(this);
      const result = await originalUpdate.apply(this, args);
      schedule();
      return result;
    };
    Zotero.Feed.prototype._updateFeed = updateWrapper;
    originalJSON = Zotero.FeedItem.prototype.fromJSON;
    jsonWrapper = function (data) {
      if (!enabled() || !alive) return originalJSON.call(this, data);
      const feed = Zotero.Feeds.get(this.libraryID);
      if (!feed || isArchive(feed)) return originalJSON.call(this, data);
      capture(this, feed);
      const text = memory.translation(data.title || field(this, "title"), target());
      if (text) {
        data = { ...data, extra: C.setExtra(data.extra, "titleTranslation", text) };
        data.extra = C.setExtra(data.extra, "rssMemoryLanguage", target());
      }
      return originalJSON.call(this, data);
    };
    Zotero.FeedItem.prototype.fromJSON = jsonWrapper;
    observer = Zotero.Notifier.registerObserver({ notify(event, type, ids) {
      if (!enabled() || !alive) return;
      if (type === "item" && ["add", "modify"].includes(event)) {
        for (const item of Zotero.Items.get(ids)) {
          if (!item?.isFeedItem) continue;
          const feed = Zotero.Feeds.get(item.libraryID);
          if (!feed || isArchive(feed)) continue;
          const record = memory.find(meta(item, feed));
          if (record && priorRead.get(item.id) === true && !item.isRead) {
            record.readAt = null;
            record.unreadAt = Date.now();
          }
          capture(item, feed);
          priorRead.set(item.id, item.isRead);
        }
        flush().catch(log);
      }
      if (type === "feed" || type === "item") schedule();
    } }, ["item", "feed"], ID);
  }
  function alert(text) { Services.prompt.alert(Zotero.getMainWindow(), "RSS 记忆", text); }
  async function enable() {
    if (!memory) { alert("缓存文件无法读取。为保护原有记录，功能未启用。"); return; }
    Zotero.Prefs.set(PREF, true, true);
    await ensureArchive();
    schedule();
    officialRefresh().catch(log);
  }
  async function restoreSelected() {
    if (running) { alert("正在处理订阅，请稍后重试。"); return; }
    running = true;
    try {
      for (const selected of Zotero.getActiveZoteroPane().getSelectedItems()) {
        if (!selected.isFeedItem) continue;
        const r = memory.fromArchiveGUID(selected.guid);
        if (!r) continue;
        const feeds = Zotero.Feeds.getAll();
        const source = Object.entries(r.sources).find(([url]) => feeds.some(f => f.url === url));
        if (!source) { alert("原订阅已移除。归档保持不变，请先重新添加原订阅。"); continue; }
        const [url, info] = source;
        const feed = feeds.find(f => f.url === url);
        let item = await Zotero.FeedItems.getAsyncByGUID(info.guid);
        if (!item) {
          item = new Zotero.FeedItem("journalArticle");
          item.libraryID = feed.id;
          item.guid = info.guid;
          item.setField("title", r.title);
          item.setField("url", r.url);
          if (r.doi) item.setField("DOI", r.doi);
          if (r.date) item.setField("date", r.date);
        }
        const oldReadAt = r.readAt;
        r.readAt = null;
        r.unreadAt = Date.now();
        try {
          await flush();
          item.isRead = false;
          await item.saveTx();
        } catch (error) { r.readAt = oldReadAt; await flush(); throw error; }
        priorRead.set(item.id, false);
        await Zotero.FeedItems.erase([selected.id]);
        await feed.updateUnreadCount();
      }
      if (archive) await archive.updateUnreadCount();
    } finally { running = false; schedule(); }
  }
  function addMenu(win) {
    if (win.document.getElementById("journal-rss-memory-menu")) return;
    const doc = win.document, menu = doc.createXULElement("menu"), popup = doc.createXULElement("menupopup");
    menu.id = "journal-rss-memory-menu";
    menu.setAttribute("label", "RSS 记忆");
    menu.appendChild(popup);
    for (const [label, action] of [
      ["启用自动翻译、缓存和已读归档", enable],
      ["立即检查订阅", () => { schedule(); officialRefresh().catch(log); }],
      ["将所选归档放回原订阅的未读列表", restoreSelected],
      ["暂停（保留缓存和归档）", () => Zotero.Prefs.set(PREF, false, true)],
      ["状态与缓存位置", () => alert(`状态：${enabled() ? '启用' : '暂停'}\n缓存译文：${Object.keys(memory?.data.translations || {}).length}\n已读记录：${Object.values(memory?.data.records || {}).filter(r => r.readAt).length}\n本次翻译：${translatedCount}\n缓存：${path || ''}\n最近问题：${lastError || '无'}`)],
    ]) {
      const item = doc.createXULElement("menuitem");
      item.setAttribute("label", label);
      item.addEventListener("command", () => Promise.resolve().then(action).catch(log));
      popup.appendChild(item);
    }
    doc.getElementById("menu_ToolsPopup")?.appendChild(menu);
  }
  async function start() {
    if (started) return;
    started = true;
    Zotero.JournalRSSMemory = { enable, runOnce: pump, getState: () => memory?.data };
    const folder = PathUtils.join(Zotero.DataDirectory.dir, "journal-rss-memory");
    path = PathUtils.join(folder, "state.json");
    try {
      await IOUtils.makeDirectory(folder, { ignoreExisting: true });
      const data = await IOUtils.exists(path) ? await IOUtils.readJSON(path)
        : C.emptyState(Zotero.Utilities.randomString(32));
      memory = new C.Memory(data);
      await flush();
      endpointPath = new URL(archiveURL()).pathname;
      endpoint = function () {};
      endpoint.prototype = {
        supportedMethods: ["GET"], supportedDataTypes: ["*"],
        allowRequestsFromUnsafeWebContent: true,
        init(request) {
          if (request.headers.origin || !alive || !enabled()) return [403, "text/plain", "Unavailable"];
          return [200, "application/rss+xml; charset=utf-8", archiveXML()];
        },
      };
      Zotero.Server.Endpoints[endpointPath] = endpoint;
      installHooks();
      column = Zotero.ItemTreeManager.registerColumn({ dataKey: "rssOverlap", label: "RSS 来源标记", pluginID: ID,
        dataProvider: item => status.get(item.id) || "", enabledTreeIDs: ["main"],
        zoteroPersist: ["width", "hidden", "sortDirection"] });
    } catch (error) { log(error); Zotero.Prefs.set(PREF, false, true); }
    for (const win of Zotero.getMainWindows()) addMenu(win);
    (async () => {
      while (alive) {
        if (enabled() && memory) {
          await pump();
          officialRefresh().catch(log);
        }
        await Zotero.Promise.delay(60000);
      }
    })().catch(log);
  }
  async function stop() {
    alive = false;
    if (observer) Zotero.Notifier.unregisterObserver(observer);
    if (Zotero.Feed.prototype._updateFeed === updateWrapper) Zotero.Feed.prototype._updateFeed = originalUpdate;
    if (Zotero.FeedItem.prototype.fromJSON === jsonWrapper) Zotero.FeedItem.prototype.fromJSON = originalJSON;
    if (column) Zotero.ItemTreeManager.unregisterColumn(column);
    if (endpointPath && Zotero.Server.Endpoints[endpointPath] === endpoint) delete Zotero.Server.Endpoints[endpointPath];
    for (const win of Zotero.getMainWindows()) win.document.getElementById("journal-rss-memory-menu")?.remove();
    if (memory) await flush();
    delete Zotero.JournalRSSMemory;
  }
  return { start, stop, addMenu };
})();
