/* Runs exclusively in the disposable smoke profile prepared by smoke-setup.py. */
function install() {}
function uninstall() {}
function shutdown() {}
async function startup() {
  await Zotero.initializationPromise;
  const output = PathUtils.join(Zotero.DataDirectory.dir, "smoke-result.json");
  const results = [];
  const check = (condition, name) => { results.push({ name, pass: !!condition }); if (!condition) throw new Error(name); };
  try {
    check(Zotero.DataDirectory.dir.includes(".smoke"), "isolated data directory");
    for (let i = 0; i < 50 && !Zotero.JournalRSSMemory; i++) await Zotero.Promise.delay(200);
    check(!!Zotero.JournalRSSMemory, "packaged add-on starts in Zotero 9");
    check(!!Zotero.getMainWindow().document.getElementById('journal-rss-memory-menu'), 'RSS menu installed in main window');
    Zotero.Prefs.set("journalRSSMemory.enabled", true);
    Zotero.Prefs.set("ZoteroPDFTranslate.targetLanguage", "zh");
    let calls = 0;
    Zotero.PDFTranslate = { api: { async translate() { calls++; return { status: 'success', result: '测试译文' }; } } };
    const feed = new Zotero.Feed({ name: 'Smoke custom feed', url: 'https://example.invalid/smoke.xml',
      refreshInterval: 100000, cleanupReadAfter: 30, cleanupUnreadAfter: 90 });
    await feed.saveTx();
    for(let i=0;i<25;i++) {
      const chinese=new Zotero.FeedItem('journalArticle');
      chinese.libraryID=feed.id; chinese.guid='smoke-zh-'+i;
      chinese.setField('title','城市空间信息研究方法 '+i);
      chinese.setField('url','https://example.invalid/chinese/'+i);
      await chinese.saveTx();
    }
    let item = new Zotero.FeedItem('journalArticle');
    item.libraryID = feed.id;
    item.guid = '10.1016/smoke2026';
    item.setField('title', 'A sufficiently long article title about geographical modelling in cities');
    item.setField('url', 'https://doi.org/10.1016/smoke2026');
    await item.saveTx();
    await Zotero.JournalRSSMemory.runOnce();
    // Startup background work may already own the serial worker.
    for(let i=0;i<100 && !item.getField('extra').includes('测试译文');i++) await Zotero.Promise.delay(100);
    check(item.getField('extra').includes('测试译文'), 'English title translated despite 25 preceding Chinese titles');
    check(calls === 1, 'one service request');
    item.fromJSON({ itemType:'journalArticle', title:item.getField('title'), url:item.getField('url'), date:'2026-08-31' });
    await item.saveTx();
    check(item.getField('extra').includes('测试译文'), 'translation survives real Zotero fromJSON refresh');
    const state = Zotero.JournalRSSMemory.getState();
    const record = Object.values(state.records).find(r=>r.ids.includes('doi:10.1016/smoke2026'));
    check(!!record, 'persistent record indexed by DOI');
    record.readAt = Date.now() - 31*86400000;
    item.isRead = true;
    await item.saveTx();
    await Zotero.Promise.delay(4000);
    await Zotero.JournalRSSMemory.runOnce();
    const archivedGUID = `urn:journal-rss:read:${state.token}:${record.key}`;
    const archived = await Zotero.FeedItems.getAsyncByGUID(archivedGUID);
    check(!!archived && archived.isRead, 'read article copied to separate feed');
    check(archived.getAttachments().length === 0, 'archive has no PDFs or attachments');
    check(!await Zotero.FeedItems.getAsyncByGUID('10.1016/smoke2026'), 'expired original removed only after archive exists');
    item = new Zotero.FeedItem('journalArticle'); item.libraryID=feed.id; item.guid='10.1016/smoke2026';
    item.setField('title',record.title); item.setField('url',record.url); await item.saveTx();
    await Zotero.Promise.delay(4000);
    await Zotero.JournalRSSMemory.runOnce();
    check(!await Zotero.FeedItems.getAsyncByGUID('10.1016/smoke2026'), 'reintroduced article does not remain in source feed');
    check(calls === 1, 'no second translation after reintroduction');
    const saved = await IOUtils.readJSON(PathUtils.join(Zotero.DataDirectory.dir,'journal-rss-memory','state.json'));
    check(Object.keys(saved.translations).length === 1, 'translation persisted to disk');
    const archiveFeed = Zotero.Feeds.getAll().find(f=>f.name.includes('已读归档'));
    const reader = new Zotero.FeedReader(archiveFeed.url);
    try {
      await reader.process(); const data = await new reader.ItemIterator().next().value;
      check(data.guid === archivedGUID, 'local archive RSS parses through Zotero FeedReader');
    } finally { reader.terminate(); }
    await Zotero.Promise.delay(2000);
    const pane=Zotero.getActiveZoteroPane();
    const getSelected=pane.getSelectedItems;
    pane.getSelectedItems=()=>[archived];
    try {
      const command=Zotero.getMainWindow().document.querySelector('#journal-rss-memory-menu menuitem[label="将所选归档放回原订阅的未读列表"]');
      command.dispatchEvent(new (Zotero.getMainWindow().Event)('command'));
      await Zotero.Promise.delay(4000);
      const returned=await Zotero.FeedItems.getAsyncByGUID('10.1016/smoke2026');
      check(returned && !returned.isRead,'archive can be returned to source as unread');
      check(record.readAt === null,'explicit reread resets read memory');
    } finally { pane.getSelectedItems=getSelected; }
    await IOUtils.writeJSON(output,{ success:true, results });
  } catch(error) {
    await IOUtils.writeJSON(output,{ success:false, results, error:String(error), stack:error.stack });
  } finally {
    Services.startup.quit(Components.interfaces.nsIAppStartup.eForceQuit);
  }
}
