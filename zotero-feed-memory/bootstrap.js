var rssMemoryContext;
function install() {}
function uninstall() {}
async function startup({ rootURI, resourceURI }) {
  await Zotero.initializationPromise;
  const root = rootURI || resourceURI.spec;
  rssMemoryContext = { Zotero, Services, IOUtils, PathUtils, URL };
  Services.scriptloader.loadSubScript(root + "core.js", rssMemoryContext);
  Services.scriptloader.loadSubScript(root + "addon.js", rssMemoryContext);
  await rssMemoryContext.RSSMemoryAddon.start();
}
function onMainWindowLoad({ window }) { rssMemoryContext?.RSSMemoryAddon.addMenu(window); }
function onMainWindowUnload({ window }) { window.document.getElementById("journal-rss-memory-menu")?.remove(); }
async function shutdown() {
  await rssMemoryContext?.RSSMemoryAddon.stop();
  rssMemoryContext = null;
}
