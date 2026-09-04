// A DOM walk with bounded call-stack use, even for tens of thousands of siblings.
function walkIteratively() {
  const walker = this._walker;
  let depth = 0;
  while (true) {
    const node = walker.currentNode;
    if (node.nodeType === 1) {
      this.contentHandler.startElement(node.namespaceURI, node.localName, "", node.attributes);
      if (walker.firstChild()) {
        depth++;
        continue;
      }
      this.contentHandler.endElement(node.namespaceURI, node.localName, "");
    } else if (node.nodeType === 3 || node.nodeType === 4) {
      this.contentHandler.characters(node.data);
    } else if (node.nodeType === 7) {
      this.contentHandler.processingInstruction(node.target, node.data);
    }
    while (!walker.nextSibling()) {
      if (depth === 0) return;
      walker.parentNode();
      depth--;
      const parent = walker.currentNode;
      this.contentHandler.endElement(parent.namespaceURI, parent.localName, "");
    }
  }
}

function installConferenceWalker(prototype) {
  const original = prototype._walk;
  // A future Zotero version may already replace the recursive implementation.
  if (typeof original !== "function" || !/this\._walk\s*\(/.test(original.toString())) {
    return () => {};
  }
  const prefix = "https://fengziclassmate.github.io/journal-rss/conference-feeds/";
  const replacement = function () {
    const url = this.baseURI?.href || this.baseURI?.spec || String(this.baseURI || "");
    return url.startsWith(prefix)
      ? walkIteratively.call(this)
      : original.apply(this, arguments);
  };
  prototype._walk = replacement;
  return () => {
    if (prototype._walk === replacement) prototype._walk = original;
  };
}

function installConferenceSort(prototype) {
  const originals = new Map();
  const isConference = view => {
    const row = view.collectionTreeRow;
    return row?.isFeed?.() && row.ref?.url?.startsWith(
      "https://fengziclassmate.github.io/journal-rss/conference-feeds/"
    );
  };
  for (const [name, value] of [["getSortField", "date"], ["getSortDirection", -1]]) {
    const original = prototype[name];
    if (typeof original !== "function") continue;
    const replacement = function () {
      return isConference(this) ? value : original.apply(this, arguments);
    };
    originals.set(name, {original, replacement});
    prototype[name] = replacement;
  }
  return () => {
    for (const [name, {original, replacement}] of originals) {
      if (prototype[name] === replacement) prototype[name] = original;
    }
  };
}

function installConferenceDatePrecision(prototype) {
  const original = prototype.fromJSON;
  const replacement = function (json) {
    const date = json.date;
    const partial = this.guid?.startsWith("conference:") && /^\d{4}(?:-\d{2})?$/.test(date || "");
    const result = original.apply(this, arguments);
    // FeedItem.fromJSON expands ISO years to January 1; keep source precision.
    if (partial) this.setField("date", date);
    return result;
  };
  prototype.fromJSON = replacement;
  return () => {
    if (prototype.fromJSON === replacement) prototype.fromJSON = original;
  };
}

if (typeof module !== "undefined") module.exports = {walkIteratively, installConferenceWalker, installConferenceSort, installConferenceDatePrecision};
