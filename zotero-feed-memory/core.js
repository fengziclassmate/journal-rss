/* Pure state logic, shared by the Zotero adapter and Node regression tests. */
(function (root) {
  "use strict";
  const DAY = 86400000;
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const titleKey = value => String(value || "").normalize("NFKC").replace(/\s+/g, " ").trim();
  const langKey = value => String(value || "zh").toLowerCase().replace(/_/g, "-");
  const translationKey = (title, language) => JSON.stringify([langKey(language), titleKey(title)]);
  function safeURL(value) {
    try {
      const url = new URL(value);
      if (!["https:", "http:"].includes(url.protocol)) return "";
      url.hash = "";
      for (const key of [...url.searchParams.keys()]) {
        if (/^(utm_|dgcid$|via$)/i.test(key)) url.searchParams.delete(key);
      }
      url.searchParams.sort();
      return url.href;
    } catch { return ""; }
  }
  function identifiers(item) {
    const text = [item.doi, item.guid, item.url, item.description].filter(Boolean).join(" ");
    const ids = [];
    const doi = text.match(/10\.\d{4,9}\/[^\s<>"'&]+/i)?.[0]?.replace(/[.,;]+$/, "").toLowerCase();
    if (doi) ids.push(`doi:${doi}`);
    const pii = text.match(/\bS\d{15}[\dX]\b/i)?.[0];
    if (pii) ids.push(`pii:${pii.toUpperCase()}`);
    const url = safeURL(item.url);
    if (url) ids.push(`url:${url}`);
    if (item.guid) ids.push(`guid:${JSON.stringify([item.feedURL || "", item.guid])}`);
    return [...new Set(ids)];
  }
  function emptyState(token) {
    return { version: 1, token, nextID: 1, records: {}, translations: {} };
  }
  class Memory {
    constructor(data) {
      if (!data || data.version !== 1 || typeof data.token !== "string"
          || !Number.isInteger(data.nextID) || !data.records || !data.translations
          || Array.isArray(data.records) || Array.isArray(data.translations)) {
        throw new Error("Invalid RSS memory file; refusing to overwrite it");
      }
      this.data = data;
      this.index = new Map();
      this.officialTitles = new Map();
      for (const record of Object.values(data.records)) {
        if (!record || !Array.isArray(record.ids) || !record.sources || typeof record.title !== "string") {
          throw new Error("Invalid RSS record; automatic cleanup is disabled");
        }
        for (const id of record.ids) this.index.set(id, record.key);
        this.indexOfficialTitle(record);
      }
    }
    find(item) {
      for (const id of identifiers(item)) {
        const key = this.index.get(id);
        if (key && own(this.data.records, key)) return this.data.records[key];
      }
      return null;
    }
    indexOfficialTitle(record) {
      if (!Object.values(record.sources).some(s => s.official)) return;
      const title = titleKey(record.title);
      if (!this.officialTitles.has(title)) this.officialTitles.set(title, new Set());
      this.officialTitles.get(title).add(record.key);
    }
    observe(item, now = Date.now()) {
      const ids = identifiers(item);
      if (!ids.length || !titleKey(item.title)) return null;
      let record = this.find(item);
      if (!record) {
        const key = String(this.data.nextID++);
        record = this.data.records[key] = {
          key, ids: [], title: item.title, url: safeURL(item.url), doi: item.doi || "",
          date: item.date || "", sources: {}, firstSeen: now, readAt: null,
        };
      }
      for (const id of ids) {
        if (!record.ids.includes(id)) record.ids.push(id);
        this.index.set(id, record.key);
      }
      record.title = item.title;
      if (safeURL(item.url)) record.url = safeURL(item.url);
      if (item.doi) record.doi = item.doi;
      if (item.date) record.date = item.date;
      if (item.feedURL) record.sources[item.feedURL] = {
        name: item.feedName || item.feedURL, guid: item.guid || "", lastSeen: now,
        official: !!item.official,
      };
      if (item.readAt && !record.readAt && item.readAt > (record.unreadAt || 0)) record.readAt = item.readAt;
      this.indexOfficialTitle(record);
      return record;
    }
    rememberTranslation(title, language, result) {
      if (!titleKey(title) || !String(result || "").trim()) return false;
      this.data.translations[translationKey(title, language)] = String(result).trim();
      return true;
    }
    translation(title, language) {
      return this.data.translations[translationKey(title, language)] || "";
    }
    expired(record, days, now = Date.now()) {
      return !!record?.readAt && days > 0 && now - record.readAt >= days * DAY;
    }
    archiveGUID(record) { return `urn:journal-rss:read:${this.data.token}:${record.key}`; }
    fromArchiveGUID(guid) {
      const prefix = `urn:journal-rss:read:${this.data.token}:`;
      if (!String(guid).startsWith(prefix)) return null;
      return this.data.records[String(guid).slice(prefix.length)] || null;
    }
    duplicate(record, currentFeedURL) {
      if (!record) return "";
      if (Object.entries(record.sources).some(([url, s]) => url !== currentFeedURL && s.official)) {
        return "official-id";
      }
      const key = titleKey(record.title);
      // Generic titles such as Editorial are not adequate evidence of overlap.
      if (key.length < 40) return "";
      for (const candidateKey of this.officialTitles.get(key) || []) {
        const candidate = this.data.records[candidateKey];
        if (candidate.key === record.key || titleKey(candidate.title) !== key) continue;
        const a = record.ids.find(id => id.startsWith("doi:"));
        const b = candidate.ids.find(id => id.startsWith("doi:"));
        if (a && b && a !== b) continue;
        if (Object.entries(candidate.sources).some(([url, s]) => url !== currentFeedURL && s.official)) {
          return "official-title";
        }
      }
      return "";
    }
  }
  function getExtra(extra, key) {
    const line = String(extra || "").split(/\r?\n/).find(s => s.startsWith(`${key}:`));
    return line ? line.slice(key.length + 1).trim() : "";
  }
  function setExtra(extra, key, value) {
    const lines = String(extra || "").split(/\r?\n/).filter(s => !s.startsWith(`${key}:`));
    if (value) lines.push(`${key}: ${String(value).replace(/\r?\n/g, " ")}`);
    return lines.filter(Boolean).join("\n");
  }
  const api = { DAY, Memory, emptyState, titleKey, translationKey, safeURL, identifiers, getExtra, setExtra };
  root.RSSMemoryCore = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis === "undefined" ? this : globalThis);
