var undoConferenceParserPatch;
var undoConferenceDatePatch;
var undoRSSRefreshPatch;
var conferenceCompatScope;
var conferenceSortPatches = new Map();
function install() {}
function uninstall() {}
async function startup({rootURI, resourceURI}) {
  await Zotero.initializationPromise;
  conferenceCompatScope = {};
  Services.scriptloader.loadSubScript((rootURI || resourceURI.spec) + "walker.js", conferenceCompatScope);
  Services.scriptloader.loadSubScript((rootURI || resourceURI.spec) + "refresh.js", conferenceCompatScope);
  const {SAXXMLReader} = ChromeUtils.importESModule("resource://zotero/feeds/SAXXMLReader.mjs");
  undoConferenceParserPatch = conferenceCompatScope.installConferenceWalker(SAXXMLReader.prototype);
  undoConferenceDatePatch = conferenceCompatScope.installConferenceDatePrecision(Zotero.FeedItem.prototype);
  const {FeedProcessor} = ChromeUtils.importESModule("resource://zotero/feeds/FeedProcessor.mjs");
  const cacheDir = PathUtils.join(Zotero.DataDirectory.dir, "rss-refresh-cache");
  const cachePath = id => PathUtils.join(cacheDir, `${id}.json`);
  undoRSSRefreshPatch = conferenceCompatScope.installRSSRefreshCache({
    feedPrototype: Zotero.Feed.prototype,
    readerPrototype: Zotero.FeedReader.prototype,
    readerHost: Zotero,
    processorPrototype: FeedProcessor.prototype,
    now: () => Date.now(),
    generation: id => Services.prefs.getStringPref(`extensions.zotero.rssRefreshGeneration.${id}`, ""),
    advanceGeneration: async id => {
      const key = `extensions.zotero.rssRefreshGeneration.${id}`;
      const value = Services.uuid.generateUUID().toString();
      Services.prefs.setStringPref(key, value);
      const file = Services.dirsvc.get("ProfD", Ci.nsIFile).clone();
      file.append("prefs.js");
      // Explicit-file save is blocking; verify the sentinel before importing.
      Services.prefs.savePrefFile(file);
      const saved = await IOUtils.read(file.path);
      const sentinel = Array.from(`user_pref(${JSON.stringify(key)}, ${JSON.stringify(value)});`, c => c.charCodeAt(0));
      // Legacy profiles can contain non-UTF-8 values; our sentinel is ASCII.
      if (!saved.some((byte, index) => byte === sentinel[0]
        && sentinel.every((value, offset) => saved[index + offset] === value))) {
        throw new Error("RSS generation was not persisted");
      }
    },
    getReceipt: async url => (await Zotero.HTTP.request("GET", url, {
      responseType: "json", timeout: 15000, headers: {"Cache-Control": "no-cache"},
    })).response,
    read: id => IOUtils.readJSON(cachePath(id)),
    hasCache: id => IOUtils.exists(cachePath(id)),
    write: async (id, value) => {
      await IOUtils.makeDirectory(cacheDir, {ignoreExisting: true});
      await IOUtils.writeJSON(cachePath(id), value, {tmpPath: cachePath(id) + ".tmp"});
    },
    remove: id => IOUtils.remove(cachePath(id), {ignoreAbsent: true}),
    isMissing: error => error.name === "NotFoundError",
    prepare: feed => feed.waitForDataLoad("item"),
    rows: id => Zotero.DB.queryAsync(
      "SELECT itemID, guid FROM feedItems JOIN items USING(itemID) WHERE libraryID=?", [id]
    ),
    allPresent: async guids => {
      for (let start = 0; start < guids.length; start += 200) {
        const batch = guids.slice(start, start + 200);
        const count = await Zotero.DB.valueQueryAsync(
          "SELECT COUNT(*) FROM feedItems WHERE guid IN (" + batch.map(() => "?").join(",") + ")", batch
        );
        if (count !== batch.length) return false;
      }
      return true;
    },
    hash: buffer => {
      const bytes = new Uint8Array(buffer);
      const hasher = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
      hasher.init(hasher.SHA256);
      hasher.update(bytes, bytes.length);
      return Array.from(hasher.finish(false), c => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
    },
    defer: () => Zotero.Promise.defer(),
    sqlNow: () => Zotero.Date.dateToSQL(new Date(), true),
    status: feed => Zotero.Notifier.trigger("statusChanged", "feed", feed.id),
    log: error => Zotero.debug("RSS refresh cache fallback: " + error),
  });
  Zotero.__rssRefreshStats = undoRSSRefreshPatch.stats;
  await Zotero.uiReadyPromise;
  for (const window of Zotero.getMainWindows()) await onMainWindowLoad({window});
}
async function onMainWindowLoad({window}) {
  if (!conferenceCompatScope || typeof window.require !== "function") return;
  // The window hook can run before its first item-tree instance is assigned.
  const prototype = window.require("zotero/itemTree").prototype;
  if (!conferenceSortPatches.has(prototype)) {
    conferenceSortPatches.set(prototype, conferenceCompatScope.installConferenceSort(prototype));
  }
  const view = window.ZoteroPane?.itemsView;
  if (view?.collectionTreeRow?.isFeed?.()) await view.sort();
}
function shutdown() {
  undoRSSRefreshPatch?.();
  undoRSSRefreshPatch = null;
  delete Zotero.__rssRefreshStats;
  undoConferenceParserPatch?.();
  undoConferenceParserPatch = null;
  undoConferenceDatePatch?.();
  undoConferenceDatePatch = null;
  for (const undo of conferenceSortPatches.values()) undo();
  conferenceSortPatches.clear();
  conferenceCompatScope = null;
}
