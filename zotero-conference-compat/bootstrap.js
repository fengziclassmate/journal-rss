var undoConferenceParserPatch;
var undoConferenceDatePatch;
var conferenceCompatScope;
var conferenceSortPatches = new Map();
function install() {}
function uninstall() {}
async function startup({rootURI, resourceURI}) {
  await Zotero.initializationPromise;
  conferenceCompatScope = {};
  Services.scriptloader.loadSubScript((rootURI || resourceURI.spec) + "walker.js", conferenceCompatScope);
  const {SAXXMLReader} = ChromeUtils.importESModule("resource://zotero/feeds/SAXXMLReader.mjs");
  undoConferenceParserPatch = conferenceCompatScope.installConferenceWalker(SAXXMLReader.prototype);
  undoConferenceDatePatch = conferenceCompatScope.installConferenceDatePrecision(Zotero.FeedItem.prototype);
  await Zotero.uiReadyPromise;
  for (const window of Zotero.getMainWindows()) await onMainWindowLoad({window});
}
async function onMainWindowLoad({window}) {
  const view = window.ZoteroPane?.itemsView;
  if (!view || !conferenceCompatScope) return;
  const prototype = Object.getPrototypeOf(view);
  if (!conferenceSortPatches.has(prototype)) {
    conferenceSortPatches.set(prototype, conferenceCompatScope.installConferenceSort(prototype));
  }
  if (view.collectionTreeRow?.isFeed?.()) await view.sort();
}
function shutdown() {
  undoConferenceParserPatch?.();
  undoConferenceParserPatch = null;
  undoConferenceDatePatch?.();
  undoConferenceDatePatch = null;
  for (const undo of conferenceSortPatches.values()) undo();
  conferenceSortPatches.clear();
  conferenceCompatScope = null;
}
