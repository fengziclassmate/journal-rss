"""Prepare a disposable Zotero profile. Does not read or change the user's profile."""
import json
import time
from pathlib import Path

root = Path(__file__).resolve().parent
work = root / '.smoke' / f'automation-{time.time_ns()}'
profile = work / 'profile'
data = work / 'data'
profile.mkdir(parents=True, exist_ok=True)
data.mkdir(parents=True, exist_ok=True)
preferences = {
 'extensions.zotero.dataDir': str(data), 'extensions.zotero.useDataDir': True,
 'extensions.zotero.firstRun': False, 'extensions.zotero.firstRun2': False,
 'extensions.zotero.sync.autoSync': False, 'extensions.zotero.httpServer.port': 23129,
 'extensions.zotero.httpServer.enabled': True, 'extensions.autoDisableScopes': 0,
 'extensions.enabledScopes': 15, 'extensions.startupScanScopes': 15,
 'extensions.logging.enabled': True, 'app.update.enabled': False,
 'extensions.zotero.automaticScraperUpdates': False,
 'extensions.zotero.retractions.enabled': False,
 'extensions.zotero.streaming.enabled': False,
 'devtools.debugger.remote-enabled': True, 'devtools.debugger.prompt-connection': False,
 'devtools.chrome.enabled': True,
 'extensions.update.enabled': False, 'toolkit.telemetry.reportingpolicy.firstRun': False,
}
(profile / 'user.js').write_text(''.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});\n' for k,v in preferences.items()), encoding='utf-8')
(root / '.smoke' / 'current.json').write_text(json.dumps({'work':str(work)}), encoding='utf-8')
print(profile)
