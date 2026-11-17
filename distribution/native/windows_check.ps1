# Scoop and WinGet native checks on one disposable GitHub-hosted Windows runner.
# Setup runs as the runner administrator; every manager operation runs as a separate
# normal user through windows_user.ps1. No credentials are held.
# Ported from darkquasar/patronus tools/tests/package-windows-check.ps1 at
# a532c29203eb716d566cc622f0331525be1ee074 (that copy stays unchanged): the host
# architecture is detected, fixtures come from prepare.py, and results are reports.
param(
    [Parameter(Mandatory)][string]$Inputs,   # prepare.py trees: current/ (and older/ for lifecycle)
    [Parameter(Mandatory)][string]$Out,
    [ValidateSet('check','lifecycle')][string]$Mode = 'check',
    [Parameter(Mandatory)][string]$RunnerLabel
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tapRoot = (Resolve-Path "$PSScriptRoot/../..").Path
$pins = Get-Content "$PSScriptRoot/pins.json" -Raw | ConvertFrom-Json
if (Test-Path $Out) { throw "Evidence directory already exists: $Out" }
New-Item -ItemType Directory $Out | Out-Null
Start-Transcript -Path "$Out/setup.log" | Out-Null
$servers = @()
$taskRegistered = $false
$taskName = 'patronus-native'
$timer = [Diagnostics.Stopwatch]::StartNew()
$phaseTimes = [ordered]@{}
$osArch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$architecture = @{ X64 = 'amd64'; Arm64 = 'arm64' }[$osArch]
$platform = "windows-$architecture"
$prepare = Get-Content "$Inputs/current/prepare.json" -Raw | ConvertFrom-Json
$expect = $prepare.expect_failure

function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding $false))
}

function Start-Loopback([string]$Directory, [string]$Url, [string]$Name) {
    $port = ([uri]$Url).Port
    $server = Start-Process (Get-Command python).Source -ArgumentList @(
        '-m', 'http.server', "$port", '--bind', '127.0.0.1', '--directory', $Directory
    ) -PassThru -RedirectStandardOutput "$Out/http-$Name.log" -RedirectStandardError "$Out/http-$Name-error.log"
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-WebRequest "$Url/" -UseBasicParsing | Out-Null; return $server }
        catch { Start-Sleep -Milliseconds 500 }
    }
    throw "Local fixture server unavailable: $Url"
}

function Test-Verdict($Report) {
    if (!$expect) { return $Report.outcome -eq 'passed' }
    $prefix = @{ arch = 'architecture-mismatch:'; version = 'version-mismatch:' }[$expect]
    return $Report.outcome -eq 'failed' -and "$($Report.reason)".StartsWith($prefix)
}

$failure = $null
try {
    if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows') {
        throw 'Run only on a disposable GitHub-hosted Windows runner.'
    }
    if (!$architecture) { throw "Unsupported Windows architecture: $osArch" }
    if ($Mode -eq 'lifecycle' -and !(Test-Path "$Inputs/older/prepare.json")) { throw 'Lifecycle needs older inputs.' }
    Copy-Item "$PSScriptRoot/pins.json" "$Out/pins.json"
    New-Item -ItemType Directory "$Out/inputs" | Out-Null
    Copy-Item "$Inputs/*" "$Out/inputs" -Recurse
    Copy-Item "$tapRoot/distribution/tests/native/preservation" "$Out/preservation" -Recurse
    $identity = [ordered]@{
        label = $RunnerLabel; image = $env:ImageOS; imageVersion = $env:ImageVersion
        os = [Environment]::OSVersion.VersionString; osArchitecture = $osArch
        processArchitecture = [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
        platform = $platform; mode = $Mode; expectFailure = $expect
        githubSHA = $env:GITHUB_SHA; run = $env:GITHUB_RUN_ID; attempt = $env:GITHUB_RUN_ATTEMPT
        powerShell = $PSVersionTable.PSVersion.ToString(); python = (& python --version); git = (& git --version)
    }
    Write-Json "$Out/runner.json" $identity
    $phaseTimes['prepared_at_seconds'] = $timer.Elapsed.TotalSeconds
    New-Item -ItemType Directory "$Out/downloads" | Out-Null
    foreach ($download in $pins.downloads.PSObject.Properties) {
        $path = "$Out/downloads/$($download.Name)"
        Invoke-WebRequest -Uri $download.Value.url -OutFile $path -UseBasicParsing
        if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $download.Value.sha256) {
            throw "Dependency checksum mismatch: $($download.Name)"
        }
    }
    Expand-Archive "$Out/downloads/scoop.zip" "$Out/scoop-source"
    Expand-Archive "$Out/downloads/winget-dependencies.zip" "$Out/dependencies"
    Get-ChildItem "$Out/dependencies" -Recurse -File | Select-Object -ExpandProperty FullName | Set-Content "$Out/dependencies.txt"
    $phaseTimes['downloaded_at_seconds'] = $timer.Elapsed.TotalSeconds
    # Only normal RemoteSigned checks; no execution-policy command-line override.
    Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
    & powershell.exe -NoProfile -Command 'Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force'
    if ($LASTEXITCODE -ne 0) { throw 'Could not set the normal Windows PowerShell execution policy.' }
    Copy-Item "$PSScriptRoot/windows_user.ps1" "$Out/user.ps1"
    & powershell.exe -NoProfile -File "$Out/user.ps1" -Work $Out -Phase Register -Architecture $architecture
    if ($LASTEXITCODE -ne 0) { throw 'Administrator WinGet registration failed.' }
    $winget = (Get-Content "$Out/admin-winget.txt" -Raw).Trim()
    & $winget --version
    if ($LASTEXITCODE -ne 0) { throw 'Pinned WinGet client unavailable.' }
    # WinGet admin settings are per SID. Machine policy reaches the normal user.
    $policy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppInstaller'
    New-Item $policy -Force | Out-Null
    New-ItemProperty $policy -Name EnableLocalManifestFiles -Value 1 -PropertyType DWord -Force | Out-Null
    # Hosted images enable Developer Mode; ordinary-user acceptance requires it off.
    $developerPolicy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
    New-Item $developerPolicy -Force | Out-Null
    New-ItemProperty $developerPolicy -Name AllowDevelopmentWithoutDevLicense `
        -Value 0 -PropertyType DWord -Force | Out-Null
    Get-ItemProperty $developerPolicy -Name AllowDevelopmentWithoutDevLicense |
        Select-Object AllowDevelopmentWithoutDevLicense | ConvertTo-Json | Set-Content "$Out/developer-mode.json"
    $phaseTimes['registered_at_seconds'] = $timer.Elapsed.TotalSeconds
    foreach ($kind in @('current','older')) {
        if (!(Test-Path "$Out/inputs/$kind/prepare.json")) { continue }
        $loopback = (Get-Content "$Out/inputs/$kind/prepare.json" -Raw | ConvertFrom-Json).loopback
        if ($loopback) { $servers += Start-Loopback "$Out/inputs/$kind/assets" $loopback $kind }
    }
    $name = 'patronusNative'
    if (Get-LocalUser $name -ErrorAction SilentlyContinue) { throw 'Test user already exists.' }
    $password = [guid]::NewGuid().ToString('N') + '!a9Z'
    $secure = ConvertTo-SecureString $password -AsPlainText -Force
    $user = New-LocalUser -Name $name -Password $secure -Description 'Disposable native-check normal user'
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    Add-LocalGroupMember -Group $usersGroup -Member $user
    # Password-based tasks need this normal logon right; no administrator or
    # symlink privilege is granted. Preserve all existing assignees and denials.
    & secedit.exe /export /cfg "$Out/logon-before.inf" /areas USER_RIGHTS /log "$Out/logon-export.log"
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing batch-logon rights.' }
    $rights = Get-Content "$Out/logon-before.inf"
    $sid = $user.SID.Value
    $deny = @($rights | Where-Object { $_ -match '^SeDenyBatchLogonRight\s*=' }) -join ''
    foreach ($deniedSid in @($sid,'S-1-5-32-545','S-1-1-0','S-1-5-11')) {
        if ($deny -match [regex]::Escape($deniedSid)) { throw 'An explicit batch-logon denial applies; refusing to change it.' }
    }
    $assignment = @($rights | Where-Object { $_ -match '^SeBatchLogonRight\s*=' })
    $existing = if ($assignment.Count) { ($assignment[0] -split '=',2)[1].Trim() } else { '' }
    $assignees = @($existing -split ',' | Where-Object { $_ }) + "*$sid"
    @('[Unicode]','Unicode=yes','[Version]','signature="$CHICAGO$"','Revision=1',
        '[Privilege Rights]',('SeBatchLogonRight = ' + ($assignees -join ','))) |
        Set-Content "$Out/logon-grant.inf" -Encoding Unicode
    & secedit.exe /configure /db "$Out/logon-policy.sdb" /cfg "$Out/logon-grant.inf" `
        /areas USER_RIGHTS /log "$Out/logon-configure.log"
    if ($LASTEXITCODE -ne 0) { throw 'Could not assign the test account its batch-logon right.' }
    & icacls.exe $Out /grant '*S-1-5-32-545:(OI)(CI)M' /T /Q
    if ($LASTEXITCODE -ne 0) { throw 'Could not grant test-user access to staging evidence.' }
    $phase = @{ check = 'Check'; lifecycle = 'Lifecycle' }[$Mode]
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
        '-NoProfile -File "{0}\user.ps1" -Work "{0}" -Phase {1} -Architecture {2}' -f $Out, $phase, $architecture)
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 25) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    & wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable task startup diagnostics.' }
    $taskStarted = Get-Date
    Register-ScheduledTask -TaskName $taskName -Action $action -Settings $settings `
        -User "$env:COMPUTERNAME\$name" -Password $password -RunLevel Limited | Out-Null
    $taskRegistered = $true
    Export-ScheduledTask -TaskName $taskName | Set-Content "$Out/task-definition.txt"
    $password = $null
    Start-ScheduledTask $taskName
    $deadline = (Get-Date).AddMinutes(26)
    $startupDeadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 2
        $task = Get-ScheduledTask $taskName
        $info = Get-ScheduledTaskInfo $taskName
        if ($info.LastRunTime -lt $taskStarted.AddSeconds(-2) -and (Get-Date) -gt $startupDeadline) {
            throw "Test-user task never started (state $($task.State), result $($info.LastTaskResult))."
        }
    } while (($task.State -eq 'Running' -or $info.LastRunTime -lt $taskStarted.AddSeconds(-2)) -and (Get-Date) -lt $deadline)
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask $taskName
        for ($i = 0; $i -lt 20 -and (Get-ScheduledTask $taskName).State -eq 'Running'; $i++) { Start-Sleep -Milliseconds 500 }
        throw 'Native user task exceeded its time bound; task was stopped.'
    }
    $info | Format-List | Out-String | Set-Content "$Out/task.txt"
    if (Test-Path "$Out/user.log") { Get-Content "$Out/user.log" }
    if ($info.LastTaskResult -ne 0 -or !(Test-Path "$Out/DONE.txt")) {
        throw "Native user task failed (result $($info.LastTaskResult)); inspect user.log and failure.txt."
    }
} catch {
    $failure = "$($_.Exception.Message)" -replace '[\x00-\x1f]', ' '
    Write-Host "Harness failure: $failure"
} finally {
    if ($taskRegistered) {
        if ((Get-ScheduledTask $taskName).State -in @('Running','Queued')) { Stop-ScheduledTask $taskName }
        Get-ScheduledTaskInfo $taskName | Format-List | Out-String | Set-Content "$Out/task.txt"
        $finalTask = Get-ScheduledTask $taskName
        [ordered]@{
            task = $finalTask.TaskName; state = $finalTask.State.ToString()
            user = $finalTask.Principal.UserId; logonType = $finalTask.Principal.LogonType.ToString()
            runLevel = $finalTask.Principal.RunLevel.ToString()
        } | ConvertTo-Json | Set-Content "$Out/task-state.json"
        Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-TaskScheduler/Operational'; StartTime = $taskStarted.AddSeconds(-5)
        } -ErrorAction SilentlyContinue | Where-Object { $_.Message -match $taskName } |
            Select-Object TimeCreated,Id,LevelDisplayName,Message |
            ConvertTo-Json -Depth 4 | Set-Content "$Out/task-events.json"
    }
    $phaseTimes['finished_at_seconds'] = $timer.Elapsed.TotalSeconds
    $phaseTimes | ConvertTo-Json | Set-Content "$Out/phase-times.json"
    foreach ($server in $servers) { if (!$server.HasExited) { Stop-Process -Id $server.Id } }
    Stop-Transcript | Out-Null
}

if ($Mode -eq 'lifecycle') {
    if ($failure) { Write-Host "Lifecycle failed: $failure"; exit 1 }
    Get-Content "$Out/lifecycle-scoop.json", "$Out/lifecycle-winget.json"
    exit 0
}
# A manager without a report failed in setup: record it so the summary sees a failure, not silence.
$ok = $true
foreach ($manager in @('scoop','winget')) {
    $path = "$Out/report-check-$manager-$platform.json"
    if (!(Test-Path $path)) {
        $runner = Get-Content "$Out/runner.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
        $reason = "harness-failed: $failure"
        Write-Json $path ([ordered]@{
            schema_version = 1; stage = 'check'; manager = $manager; platform = $platform; outcome = 'failed'
            reason = $reason.Substring(0, [Math]::Min(1000, $reason.Length)); runner = $RunnerLabel
            image = "$env:ImageOS"; image_version = "$env:ImageVersion"
            manager_version = $(if ($manager -eq 'scoop') { "scoop $($pins.scoop_commit)" } else { "winget $($pins.winget_version)" })
            steps = [ordered]@{ validate = 'not-run'; install = 'not-run'; version = 'not-run'; uninstall = 'not-run' }
            inputs_sha256 = (Get-FileHash "$Inputs/current/prepare.json" -Algorithm SHA256).Hash.ToLowerInvariant()
        })
    }
    $report = Get-Content $path -Raw | ConvertFrom-Json
    Write-Host "$manager ${platform}: $($report.outcome) $($report.reason)"
    if (!(Test-Verdict $report)) { $ok = $false }
}
if (!$ok) { exit 1 }
exit 0
