import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from zotero_cleanup import cleanup_script, run_cleanup
from zotero_read_sync import choose_readable_database


class CleanupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(database=self.root/'zotero.sqlite',
                                    key_file=self.root/'key', suppression=self.root/'suppression',
                                    report_file=self.root/'report.json')
        self.args.key_file.write_text('test', encoding='ascii')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows launcher')
    def test_launcher_persists_stderr_and_keeps_stdout_parseable(self):
        launcher = Path(__file__).resolve().parents[1]/'clean-read-now.ps1'
        script = r'''
$ErrorActionPreference='Stop'
$repo=REPO
$python=PYTHON
$logDir=LOGDIR
$ast=[System.Management.Automation.Language.Parser]::ParseFile(LAUNCHER,[ref]$null,[ref]$null)
$function=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Invoke-PythonStep'},$true)
Invoke-Expression $function.Extent.Text
$result=Invoke-PythonStep -Name 'ok' -PythonArgs @('-c', "print('JSON-ONLY')")
if($result.Trim() -ne 'JSON-ONLY'){throw 'stdout mixed with logs'}
$caught=$false
try {Invoke-PythonStep -Name 'failure' -PythonArgs @('-c', "raise RuntimeError('TEST-NATIVE-ERROR')")}
catch {$caught=$_.Exception.Message.Contains('TEST-NATIVE-ERROR')}
if(-not $caught){throw 'native failure was not surfaced'}
if(-not (Get-Content (Join-Path $logDir 'failure.stderr.log') -Raw).Contains('TEST-NATIVE-ERROR')){throw 'missing stderr'}
'''
        for name, value in [('REPO',launcher.parent),('PYTHON',sys.executable),
                            ('LOGDIR',self.root),('LAUNCHER',launcher)]:
            script=script.replace(name,"'"+str(value).replace("'","''")+"'")
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',script],
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_network_error_is_saved_before_any_deletion(self):
        with patch('zotero_cleanup.check_ready'), \
             patch('zotero_cleanup.fetch_receipt', side_effect=OSError('network failed')), \
             patch('zotero_cleanup.apply') as apply:
            with self.assertRaises(OSError):
                run_cleanup(self.args)
        apply.assert_not_called()
        report = json.loads(self.args.report_file.read_text())
        self.assertEqual(report['phase'], 'receipt')
        self.assertEqual(report['error'], 'network failed')
        self.assertIn('OSError', (Path(report['backup'])/'error.log').read_text())

    def test_native_error_keeps_checkpoint_and_checks_protected_items(self):
        plan = {'targets':[{'id':1}], 'unread':[[2,'unread']],
                'library':[[3,1,'key']], 'settings':[[2,1440,1,999]]}

        def fail_native(plan, folder):
            (folder/'result.json').write_text(json.dumps({'removed':[1],'skipped':[]}))
            raise RuntimeError('native failure')

        with patch('zotero_cleanup.check_ready'), \
             patch('zotero_cleanup.fetch_receipt',return_value={}), \
             patch('zotero_cleanup.create_database_snapshot',return_value=self.args.database), \
             patch('zotero_cleanup.inspect',return_value=plan), \
             patch('zotero_cleanup.eligible',return_value=(plan['targets'],[])), \
             patch('zotero_cleanup.verify_published_feeds'), \
             patch('zotero_cleanup.apply',side_effect=fail_native):
            with self.assertRaisesRegex(RuntimeError,'native failure'):
                run_cleanup(self.args)
        report = json.loads(self.args.report_file.read_text())
        self.assertEqual(report['phase'],'apply')
        self.assertEqual(report['checkpoint_removed'],1)
        self.assertTrue(all(report['observed_checks'].values()))

    def test_startup_failure_blocks_backup_and_deletion(self):
        with patch('zotero_cleanup.check_ready',side_effect=RuntimeError('readonly database')), \
             patch('zotero_cleanup.create_database_snapshot') as snapshot, \
             patch('zotero_cleanup.apply') as apply:
            with self.assertRaisesRegex(RuntimeError,'readonly'):
                run_cleanup(self.args)
        snapshot.assert_not_called()
        apply.assert_not_called()
        report=json.loads(self.args.report_file.read_text())
        self.assertEqual(report['phase'],'startup')
        self.assertEqual(report['checkpoint_removed'],0)

    def test_export_uses_copy_without_opening_live_sqlite(self):
        with patch('zotero_read_sync.create_database_snapshot',return_value=self.root/'copy.sqlite') as snapshot, \
             patch('zotero_read_sync._database_is_readable') as live:
            result=choose_readable_database(self.args.database,self.root/'copy')
        self.assertEqual(result,self.root/'copy.sqlite')
        snapshot.assert_called_once_with(self.args.database,self.root/'copy')
        live.assert_not_called()

    def test_export_snapshot_failure_never_falls_back_to_live_or_old_backup(self):
        with patch('zotero_read_sync.create_database_snapshot',side_effect=RuntimeError('copy failed')), \
             patch('zotero_read_sync._database_is_readable') as live:
            with self.assertRaisesRegex(RuntimeError,'copy failed'):
                choose_readable_database(self.args.database,self.root/'copy')
        live.assert_not_called()

    def test_javascript_rollback_does_not_report_uncommitted_deletions(self):
        script = cleanup_script(self.root)
        harness = r'''
const assert=require('node:assert/strict');
const saved={}; let resumed=false, leftFeed=false, notifications=0;
const targets=Array.from({length:52},(_,i)=>({id:i+1,guid:'g'+(i+1),readTime:'read',library:2}));
global.IOUtils={readJSON:async()=>({targets}),writeJSON:async(path,value)=>{saved[path]=JSON.parse(JSON.stringify(value));}};
global.Zotero={initializationPromise:Promise.resolve(),DataDirectory:{dir:'f:/zotero'},
  Libraries:{userLibraryID:1},
  getMainWindow:()=>({ZoteroPane:{getCollectionTreeRow:()=>({isFeed:()=>false,isFeeds:()=>true}),
    collectionsView:{selectLibrary:async(id)=>{assert.equal(id,1);leftFeed=true;return true;}}}}),
  Notifier:{Queue:class {},commit:async()=>{notifications++;}},
  Feeds:{pause:async()=>({resume:()=>{resumed=true;}})},
  DB:{queryAsync:async()=>[{name:'main',file:'f:/zotero/zotero.sqlite'}],
    executeTransaction:async(callback)=>callback(),
    rowQueryAsync:async(sql,[id])=>({guid:'g'+id,readTime:'read',libraryID:2})},
  FeedItems:{getAsync:async(id)=>({erase:async(options)=>{assert.ok(leftFeed);assert.ok(options.notifierQueue);if(id===52)throw new Error('simulated failure');}})}};
(async()=>{
  await assert.rejects(()=>eval(SCRIPT),/simulated failure/);
  const error=Object.entries(saved).find(([path])=>path.endsWith('zotero-error.json'))[1];
  const checkpoint=Object.entries(saved).find(([path])=>path.endsWith('result.json'))[1];
  assert.equal(error.committedRemoved.length,50);
  assert.equal(checkpoint.removed.length,50);
  assert.equal(error.itemID,52);
  assert.equal(error.batchStart,50);
  assert.equal(notifications,1);
  assert.ok(resumed);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace('SCRIPT',json.dumps(script))
        result = subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == '__main__':
    unittest.main()
