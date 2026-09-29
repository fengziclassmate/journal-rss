$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$python = 'C:\Python313\python.exe'
Add-Type -AssemblyName PresentationFramework
$mutex = New-Object System.Threading.Mutex($false, 'Local\JournalRssReadCleanup')
if (-not $mutex.WaitOne(0)) {
    [System.Windows.MessageBox]::Show('Another RSS cleanup is already running.', 'RSS read cleanup') | Out-Null
    exit
}
$preview = & $python "$repo\zotero_cleanup.py"
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Zotero safely. Nothing deleted.' }
$counts = $preview | ConvertFrom-Json
$answer = [System.Windows.MessageBox]::Show("Read items: $($counts.read). Unread items: $($counts.unread). Clean read RSS items now? Unread items, My Library and retention settings will not change. Zotero will restart briefly.", 'RSS read cleanup', 'YesNo', 'Question')
if ($answer -ne 'Yes') { exit }
$wasRunning = [bool](Get-Process zotero -ErrorAction SilentlyContinue)
$started = $false
try {
    Push-Location $repo
    & $python zotero_read_sync.py --database F:/Zotero/zotero.sqlite --suppression read-suppression.json --key-file F:/Zotero/journal-rss-read-filter.key --repo . --push
    if ($LASTEXITCODE -ne 0) { throw 'Read-history sync failed. Nothing deleted.' }
    $expected = & $python -c "from rss_read_filter import suppression_digest; print(suppression_digest('read-suppression.json'))"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify local read history. Nothing deleted.' }
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $receipt = Invoke-RestMethod ("https://fengziclassmate.github.io/journal-rss/read-filter-status.json?t=" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
            if ($receipt.suppression_sha256 -eq $expected) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 10
    }
    if (-not $ready) { throw 'Filtering has not been published yet. Nothing deleted; retry later.' }
    $main = Get-Process zotero -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
    if ($main) {
        $null = $main.CloseMainWindow()
        if (-not $main.WaitForExit(30000)) { throw 'Close Zotero manually, then retry. Nothing deleted.' }
    }
    if (Get-Process zotero -ErrorAction SilentlyContinue) { throw 'Zotero is still running. Nothing deleted.' }
    $launched = Start-Process 'C:\Program Files\Zotero\zotero.exe' -ArgumentList '--start-debugger-server','6011' -WindowStyle Hidden -PassThru
    $started = $true
    Start-Sleep -Seconds 15
    $result = & $python zotero_cleanup.py --apply
    if ($LASTEXITCODE -ne 0) { throw 'Cleanup stopped. Review the backup and verification report.' }
    $report = $result | ConvertFrom-Json
    [System.Windows.MessageBox]::Show("Removed: $($report.removed). Skipped: $($report.skipped). Backup: $($report.backup)", 'RSS read cleanup') | Out-Null
} catch {
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'RSS read cleanup') | Out-Null
} finally {
    $main = Get-Process zotero -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
    if ($started) {
        if ($main) { $null = $main.CloseMainWindow() }
        if (-not $launched.WaitForExit(10000)) {
            & $python zotero_cleanup.py --shutdown
        }
        if (-not $launched.WaitForExit(30000)) {
            [System.Windows.MessageBox]::Show('Zotero has not exited. Close Zotero completely to stop the temporary debugger. Do not leave it running.', 'RSS cleanup needs attention', 'OK', 'Warning') | Out-Null
            if (-not $launched.WaitForExit(30000)) { Write-Error 'Temporary Zotero process still running; manual shutdown required.' }
        }
    }
    if ($wasRunning -and -not (Get-Process zotero -ErrorAction SilentlyContinue)) {
        Start-Process 'C:\Program Files\Zotero\zotero.exe'
    }
    Pop-Location
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
