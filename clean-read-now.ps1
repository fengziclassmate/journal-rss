$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$python = 'C:\Python313\python.exe'
Add-Type -AssemblyName PresentationFramework
$mutex = New-Object System.Threading.Mutex($false, 'Local\JournalRssReadCleanup')
if (-not $mutex.WaitOne(0)) {
    [System.Windows.MessageBox]::Show('Another RSS cleanup is already running.', 'RSS read cleanup') | Out-Null
    exit
}
$logDir = Join-Path 'F:\Zotero\logs' ('read-cleanup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fffffff'))
$null = New-Item -ItemType Directory -Path $logDir
$reportPath = Join-Path $logDir 'status.json'

function Invoke-PythonStep {
    param([string]$Name, [string[]]$PythonArgs)
    Write-Host "RSS cleanup: $Name"
    $outPath = Join-Path $logDir "$Name.stdout.log"
    $errPath = Join-Path $logDir "$Name.stderr.log"
    $quoted = @('-X', 'utf8') + $PythonArgs | ForEach-Object { '"' + $_ + '"' }
    $process = Start-Process -FilePath $python -ArgumentList ($quoted -join ' ') -WorkingDirectory $repo -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $outPath -RedirectStandardError $errPath
    if ($process.ExitCode -ne 0) {
        $detail = (Get-Content -LiteralPath $errPath -Encoding UTF8 -Tail 12) -join "`n"
        throw "$Name failed (exit $($process.ExitCode)).`n$detail`nLogs: $logDir"
    }
    return Get-Content -LiteralPath $outPath -Raw -Encoding UTF8
}

$wasRunning = [bool](Get-Process zotero -ErrorAction SilentlyContinue)
$started = $false
$pushedLocation = $false
try {
    $preview = Invoke-PythonStep -Name 'preview' -PythonArgs @('zotero_cleanup.py')
    $counts = $preview | ConvertFrom-Json
    $answer = [System.Windows.MessageBox]::Show("Read items: $($counts.read). Unread items: $($counts.unread). Clean read RSS items now? Unread items, My Library and retention settings will not change. Zotero will restart briefly.", 'RSS read cleanup', 'YesNo', 'Question')
    if ($answer -ne 'Yes') { return }
    Push-Location $repo
    $pushedLocation = $true
    $null = Invoke-PythonStep -Name 'read-sync' -PythonArgs @('zotero_read_sync.py', '--database', 'F:/Zotero/zotero.sqlite', '--suppression', 'read-suppression.json', '--key-file', 'F:/Zotero/journal-rss-read-filter.key', '--repo', '.', '--push')
    $expected = (Invoke-PythonStep -Name 'digest' -PythonArgs @('-c', "from rss_read_filter import suppression_digest; print(suppression_digest('read-suppression.json'))")).Trim()
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
    $debugReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($launched.HasExited) { throw 'Zotero exited during startup. Nothing deleted.' }
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect('127.0.0.1', 6011)
            $debugReady = $true
            break
        } catch { Start-Sleep -Seconds 1 } finally { $client.Dispose() }
    }
    if (-not $debugReady) { throw 'Zotero debugger did not become ready. Nothing deleted.' }
    $result = Invoke-PythonStep -Name 'apply' -PythonArgs @('zotero_cleanup.py', '--apply', '--report-file', $reportPath)
    $report = $result | ConvertFrom-Json
    [System.Windows.MessageBox]::Show("Removed: $($report.removed). Skipped: $($report.skipped). Backup: $($report.backup)", 'RSS read cleanup') | Out-Null
} catch {
    $message = $_.Exception.Message
    if (Test-Path -LiteralPath $reportPath) {
        $failure = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $message += "`nPhase: $($failure.phase). Last committed checkpoint: $($failure.checkpoint_removed). Backup: $($failure.backup)"
    }
    $message | Set-Content -LiteralPath (Join-Path $logDir 'error.txt') -Encoding UTF8
    [System.Windows.MessageBox]::Show($message, 'RSS read cleanup') | Out-Null
} finally {
    $main = Get-Process zotero -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
    if ($started) {
        if ($main) { $null = $main.CloseMainWindow() }
        if (-not $launched.WaitForExit(10000)) {
            try {
                $null = Invoke-PythonStep -Name 'shutdown' -PythonArgs @('zotero_cleanup.py', '--shutdown')
            } catch { Write-Warning $_.Exception.Message }
        }
        if (-not $launched.WaitForExit(30000)) {
            [System.Windows.MessageBox]::Show('Zotero has not exited. Close Zotero completely to stop the temporary debugger. Do not leave it running.', 'RSS cleanup needs attention', 'OK', 'Warning') | Out-Null
            if (-not $launched.WaitForExit(30000)) { Write-Error 'Temporary Zotero process still running; manual shutdown required.' }
        }
    }
    if ($wasRunning -and -not (Get-Process zotero -ErrorAction SilentlyContinue)) {
        Start-Process 'C:\Program Files\Zotero\zotero.exe'
    }
    if ($pushedLocation) { Pop-Location }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
