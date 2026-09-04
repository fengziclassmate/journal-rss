var undoConferenceParserPatch;
function install() {}
function uninstall() {}
async function startup({rootURI, resourceURI}) {
  await Zotero.initializationPromise;
  const scope = {};
  Services.scriptloader.loadSubScript((rootURI || resourceURI.spec) + "walker.js", scope);
  const {SAXXMLReader} = ChromeUtils.importESModule("resource://zotero/feeds/SAXXMLReader.mjs");
  undoConferenceParserPatch = scope.installConferenceWalker(SAXXMLReader.prototype);
}
function shutdown() {
  undoConferenceParserPatch?.();
  undoConferenceParserPatch = null;
}
