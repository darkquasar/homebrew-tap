# Normal-user native checks for Scoop and WinGet. Invoked by windows_check.ps1 through
# Windows PowerShell with normal policy, under a limited scheduled task.
# Ported from darkquasar/patronus tools/tests/package-windows-user.ps1 at
# a532c29203eb716d566cc622f0331525be1ee074 (that copy stays unchanged); the host
# architecture is now a parameter and the per-release and lifecycle flows are added.
param(
    [Parameter(Mandatory)][string]$Work,
    [ValidateSet('Register','Check','Lifecycle','Fresh')][string]$Phase = 'Check',
    [ValidateSet('amd64','arm64')][string]$Architecture = 'amd64',
    [ValidateSet('scoop','winget')][string]$Manager = 'scoop',
    [string]$Tag = 'fresh'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$pins = Get-Content "$Work/pins.json" -Raw | ConvertFrom-Json
$script:nativeRecords = @()
$recordName = if ($Phase -eq 'Fresh') { "fresh-$Manager-$Tag" } else { $Phase }
$scoopArch = @{ amd64 = '64bit'; arm64 = 'arm64' }[$Architecture]
$wingetArch = @{ amd64 = 'x64'; arm64 = 'arm64' }[$Architecture]
$appxArch = @{ amd64 = 'X64'; arm64 = 'Arm64' }[$Architecture]
$platform = "windows-$Architecture"
$app = 'patronus'

function Write-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding $false))
}

function Invoke-Native {
    param([string]$Step, [string]$File, [string[]]$Arguments, [switch]$AllowFailure)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = & $File @Arguments 2>&1 | Tee-Object -FilePath "$Work/$Step.log"
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    $text = ($lines | Out-String).Trim()
    $text | Write-Host
    $script:nativeRecords += [ordered]@{
        step = $Step; file = $File; arguments = $Arguments; exit = $code
        elapsed_seconds = $timer.Elapsed.TotalSeconds
    }
    Write-Json "$Work/native-exits-$recordName.json" $script:nativeRecords
    if ($code -ne 0 -and !$AllowFailure) { throw "Native step $Step failed (exit $code); see $Step.log." }
    return [pscustomobject]@{ Exit = $code; Text = $text }
}

function Assert-NormalUser {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
        $identity.Groups.Value -contains 'S-1-5-32-544') { throw 'Acceptance identity is an administrator.' }
    $privileges = & whoami.exe /priv
    if ($LASTEXITCODE -ne 0 -or ($privileges -join "`n") -match 'SeCreateSymbolicLinkPrivilege') {
        throw 'Normal-user check requires no symbolic-link privilege.'
    }
    $developer = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock' `
        -Name AllowDevelopmentWithoutDevLicense -ErrorAction SilentlyContinue
    if ($developer -and $developer.AllowDevelopmentWithoutDevLicense -ne 0) {
        throw 'Developer Mode is enabled; this host cannot prove the required ordinary-user alias behavior.'
    }
    $policies = @(Get-ExecutionPolicy -List)
    if ((Get-ExecutionPolicy) -ne 'RemoteSigned' -or
        @($policies | Where-Object { $_.ExecutionPolicy -in @('Bypass','Unrestricted') }).Count) {
        throw 'Acceptance requires RemoteSigned policy with no permissive override scope.'
    }
    Write-Output "Identity: $($identity.Name) $($identity.User.Value); non-admin, no symlink privilege, Developer Mode off."
    $privileges | Write-Output
}

function Register-WinGet {
    # Dependencies are from the same hash-locked official release archive, for the host architecture.
    $folder = @{ amd64 = 'x64'; arm64 = 'arm64' }[$Architecture]
    $dependencies = @(Get-ChildItem "$Work/dependencies" -Recurse -Filter '*.appx' |
        Where-Object { $_.FullName -match "[\\/]$folder[\\/]" })
    if ($dependencies.Count -lt 1) { throw "No pinned $folder WinGet runtime dependencies found." }
    $dependencies.FullName | Set-Content "$Work/appx-dependencies-$recordName.txt"
    # Disposable host: a shipped app (for example Widgets) may hold a framework the bundle updates.
    Add-AppxPackage -Path "$Work/downloads/winget.msixbundle" -DependencyPath $dependencies.FullName -ForceApplicationShutdown
    $packages = @(Get-AppxPackage -Name Microsoft.DesktopAppInstaller |
        Where-Object { $_.Architecture -eq $appxArch })
    if ($packages.Count -ne 1) { throw "Expected one registered $appxArch DesktopAppInstaller package." }
    $packages | Select-Object Name,Version,Architecture,PackageFullName |
        ConvertTo-Json | Set-Content "$Work/appx-$recordName.json"
    $exe = "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    if (!(Test-Path $exe)) { throw 'Registered user WinGet App Execution Alias is unavailable.' }
    $version = Invoke-Native -Step "winget-version-$recordName" -File $exe -Arguments @('--version')
    if ($version.Text -ne $pins.winget_version) { throw "Unexpected WinGet client: $($version.Text)" }
    return $exe
}

if ($Phase -eq 'Register') {
    Register-WinGet | Set-Content "$Work/admin-winget.txt"
    exit 0
}

if ($Phase -eq 'Fresh') {
    # Resolve from persisted registry PATH in a new process, not a reused one.
    Assert-NormalUser | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path','User')
    $result = [ordered]@{ found = $false; source = $null; target = $null; mechanism = $null; sha256 = $null; output = $null }
    $command = @(Get-Command "$app.exe" -CommandType Application -ErrorAction SilentlyContinue) | Select-Object -First 1
    if ($command) {
        $result.found = $true
        $result.source = $command.Source
        if ($Manager -eq 'scoop') {
            if ($command.Source -ne "$env:USERPROFILE\scoop\shims\$app.exe") {
                throw "Unexpected Scoop executable resolution: $($command.Source)"
            }
            $shim = Get-Content "$env:USERPROFILE\scoop\shims\$app.shim" | Where-Object { $_ -match '^path\s*=' }
            $result.target = ($shim -replace '^path\s*=\s*', '').Trim().Trim('"')
            $result.mechanism = 'scoop-shim'
        } else {
            $links = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\$app.exe"
            $packageRoot = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\"
            $registration = Get-Content "$Work/inputs/current/registration.json" -Raw | ConvertFrom-Json
            $identifier = $registration.configuration.identifiers.winget
            if ($command.Source -eq $links) {
                $result.mechanism = 'winget-link'
                $item = Get-Item $links
                $result.target = if ($item.Target) { @($item.Target)[0] } else { $links }
            } elseif ($command.Source.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase)) {
                $parts = $command.Source.Substring($packageRoot.Length).Split('\')
                if (!($parts.Count -eq 2 -and $parts[0].StartsWith("$($identifier)_", [StringComparison]::OrdinalIgnoreCase) -and
                      $parts[1] -eq "$app.exe")) { throw "Unexpected WinGet package path: $($command.Source)" }
                $result.mechanism = 'winget-package-path'
                $result.target = $command.Source
            } else { throw "Unexpected WinGet executable resolution: $($command.Source)" }
        }
        $result.sha256 = (Get-FileHash $result.target -Algorithm SHA256).Hash.ToLowerInvariant()
        $previous = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $result.output = ((& $command.Source --version 2>&1) | Out-String).Trim()
        } finally { $ErrorActionPreference = $previous }
    }
    Write-Json "$Work/fresh-$Manager-$Tag.json" $result
    exit 0
}

function Get-Fresh([string]$Name, [string]$Tag) {
    $null = Invoke-Native -Step "fresh-$Name-$Tag" -File 'powershell.exe' -Arguments @(
        '-NoProfile','-File',"$Work/user.ps1",'-Work',$Work,'-Phase','Fresh','-Architecture',$Architecture,
        '-Manager',$Name,'-Tag',$Tag)
    return Get-Content "$Work/fresh-$Name-$Tag.json" -Raw | ConvertFrom-Json
}

function Get-Expected([string]$Kind) {
    $registration = Get-Content "$Work/inputs/$Kind/registration.json" -Raw | ConvertFrom-Json
    $snapshot = Get-Content "$Work/inputs/$Kind/snapshot.json" -Raw | ConvertFrom-Json
    $asset = $registration.configuration.assets.$platform
    return [pscustomobject]@{
        Version = $snapshot.payload.identity.version
        Sha256 = $snapshot.payload.identity.assets.$asset.sha256
        Asset = $asset
        Identifier = $registration.configuration.identifiers.winget
        WinGetPath = $registration.configuration.destinations.winget.path + '/' + $snapshot.payload.identity.version
    }
}

# The resolved executable must be the host architecture's snapshot asset and report v<version>.
function Assert-Resolved($Fresh, $Expected) {
    if (!$Fresh.found) { throw "install-failed: $app does not resolve in a fresh process" }
    if ($Fresh.sha256 -ne $Expected.Sha256) {
        throw ("architecture-mismatch: resolved executable $($Fresh.target) sha256 $($Fresh.sha256) is not the " +
               "$platform snapshot asset $($Expected.Asset) $($Expected.Sha256)")
    }
    if (-not ("$($Fresh.output)".Contains("v$($Expected.Version)"))) {
        throw "version-mismatch: expected v$($Expected.Version), got: $($Fresh.output)"
    }
}

function Install-PinnedScoop {
    # Install pinned Scoop from source using the unchanged official bootstrap's
    # setup functions. Do not run Install-Scoop: it fetches mutable master refs.
    $env:SCOOP = "$env:USERPROFILE\scoop"
    # Bootstrap helpers print progress; keep it out of this function's return value.
    & {
        Set-StrictMode -Off
        $env:SCOOP_NOINSTALL = 'true'
        . "$Work/downloads/scoop-install.ps1" -ScoopDir $env:SCOOP
        $source = "$Work/scoop-source/Scoop-$($pins.scoop_commit)"
        New-Item -ItemType Directory "$env:SCOOP/apps/scoop/current" -Force | Out-Null
        Copy-Item "$source/*" "$env:SCOOP/apps/scoop/current" -Recurse
        Import-ScoopShim
        Add-ShimsDirToPath
        Add-DefaultConfig
    } | Out-Host
    $env:SCOOP_NOINSTALL = $null
    $scoop = "$env:SCOOP/apps/scoop/current/bin/scoop.ps1"
    # Use the .ps1 entry with normal policy. Upstream .cmd/.sh launchers contain
    # their own policy override; this harness never invokes them.
    $null = Invoke-Native -Step 'scoop-config' -File 'powershell.exe' -Arguments @(
        '-NoProfile','-File',$scoop,'config','aria2-enabled','false')
    $null = Invoke-Native -Step 'user-git-bytes' -File 'git.exe' -Arguments @('config','--global','core.autocrlf','false')
    New-Item -ItemType Directory "$env:SCOOP/buckets" -Force | Out-Null
    return $scoop
}

function Scoop([string]$Step, [string[]]$Arguments, [switch]$AllowFailure) {
    return Invoke-Native -Step $Step -File 'powershell.exe' -AllowFailure:$AllowFailure -Arguments (
        @('-NoProfile','-File',$script:scoop) + $Arguments)
}

function New-Bucket([string]$Kind) {
    $bucket = "$env:USERPROFILE\native-staging-bucket"
    New-Item -ItemType Directory $bucket | Out-Null
    Copy-Item "$Work/inputs/$Kind/staging/scoop/*" $bucket -Recurse
    foreach ($entry in @(
        @{ step='bucket-init'; arguments=@('-C',$bucket,'init','--initial-branch=main') },
        @{ step='bucket-add'; arguments=@('-C',$bucket,'add','bucket') },
        @{ step="bucket-commit-$Kind"; arguments=@('-C',$bucket,'-c','user.name=Native Fixture',
            '-c','user.email=fixture@example.invalid','commit','-m',"Staging bucket ($Kind)") }
    )) { $null = Invoke-Native -Step $entry.step -File 'git.exe' -Arguments $entry.arguments }
    $null = Scoop 'scoop-bucket-register' @('bucket','add','darkquasar',([uri]$bucket).AbsoluteUri)
    $localManifest = "$Work/inputs/$Kind/staging/scoop/bucket/$app.json"
    $registered = "$env:SCOOP/buckets/darkquasar/bucket/$app.json"
    if (!(Test-Path $registered) -or (Get-FileHash $registered).Hash -ne (Get-FileHash $localManifest).Hash) {
        throw "validation-failed: registered bucket manifest differs from staged bytes"
    }
    return $bucket
}

function New-Report([string]$Name) {
    $runner = Get-Content "$Work/runner.json" -Raw | ConvertFrom-Json
    $version = if ($Name -eq 'scoop') { "scoop $($pins.scoop_commit)" } else { "winget $($pins.winget_version)" }
    return [ordered]@{
        schema_version = 1; stage = 'check'; manager = $Name; platform = $platform; outcome = 'failed'; reason = ''
        runner = $runner.label; image = "$($runner.image)"; image_version = "$($runner.imageVersion)"
        manager_version = $version
        steps = [ordered]@{ validate = 'not-run'; install = 'not-run'; version = 'not-run'; uninstall = 'not-run' }
        inputs_sha256 = (Get-FileHash "$Work/inputs/current/prepare.json" -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Invoke-Step($Report, [string]$Step, [scriptblock]$Body) {
    try { & $Body; $Report.steps[$Step] = 'passed' }
    catch { $Report.steps[$Step] = 'failed'; throw }
}

function Save-Report($Report, $ErrorRecord) {
    if ($ErrorRecord) {
        $message = "$($ErrorRecord.Exception.Message)" -replace '[\x00-\x1f]', ' '
        if ($message -notmatch '^[a-z-]+: ') {
            $failed = @($Report.steps.Keys | Where-Object { $Report.steps[$_] -eq 'failed' }) | Select-Object -First 1
            $message = "$failed-failed: $message"
        }
        $Report.reason = $message.Substring(0, [Math]::Min(1000, $message.Length))
        $Report.outcome = 'failed'
    } else { $Report.outcome = 'passed' }
    Write-Json "$Work/report-check-$($Report.manager)-$platform.json" $Report
}

function Test-Scoop {
    $report = New-Report 'scoop'
    $expected = Get-Expected 'current'
    try {
        Invoke-Step $report 'validate' {
            $prepare = Get-Content "$Work/inputs/current/prepare.json" -Raw | ConvertFrom-Json
            if (!$prepare.validation.scoop.passed) { throw 'validation-failed: Scoop schema validation did not pass' }
            $null = New-Bucket 'current'
            $info = Scoop 'scoop-info' @('info',"darkquasar/$app")
            if ($info.Text -notmatch $app -or $info.Text -notmatch 'darkquasar') { throw 'validation-failed: Scoop discovery failed' }
            # This pinned Scoop prints an error then returns exit 0 for unsupported
            # architecture. Require its exact denial and absence of installed files.
            $denial = Scoop 'scoop-x86' @('install',"darkquasar/$app",'--arch','32bit','--no-update-scoop') -AllowFailure
            if ($denial.Text -notmatch "doesn't support current architecture" -or
                (Test-Path "$env:SCOOP/apps/$app") -or (Test-Path "$env:SCOOP/shims/$app.exe")) {
                throw 'validation-failed: x86 installation was not denied'
            }
        }
        Invoke-Step $report 'install' {
            $script:installedScoop = $true
            $null = Scoop 'scoop-install' @('install',"darkquasar/$app",'--arch',$scoopArch,'--no-update-scoop')
        }
        Invoke-Step $report 'version' { Assert-Resolved (Get-Fresh 'scoop' 'installed') $expected }
        Invoke-Step $report 'uninstall' {
            $null = Scoop 'scoop-uninstall' @('uninstall',$app)
            $script:installedScoop = $false
            if ((Test-Path "$env:SCOOP/shims/$app.exe") -or (Get-Fresh 'scoop' 'removed').found) {
                throw "uninstall-failed: $app still resolves after scoop uninstall"
            }
        }
        Save-Report $report $null
    } catch {
        Save-Report $report $_
        Write-Host "Scoop check failed: $($report.reason)"
    } finally {
        if ($script:installedScoop) { $null = Scoop 'scoop-cleanup-uninstall' @('uninstall',$app) -AllowFailure }
    }
}

function Test-WinGet([string]$WinGet) {
    $report = New-Report 'winget'
    $expected = Get-Expected 'current'
    try {
        Invoke-Step $report 'validate' {
            $prepare = Get-Content "$Work/inputs/current/prepare.json" -Raw | ConvertFrom-Json
            if (!$prepare.validation.winget.passed) { throw 'validation-failed: WinGet schema validation did not pass' }
            foreach ($kind in @('canonical','staging')) {
                $null = Invoke-Native -Step "winget-validate-$kind" -File $WinGet -Arguments @(
                    'validate','--manifest',"$Work/inputs/current/$kind/winget/$($expected.WinGetPath)")
            }
            $manifest = "$Work/inputs/current/staging/winget/$($expected.WinGetPath)"
            $beforePath = [Environment]::GetEnvironmentVariable('Path','User')
            $denial = Invoke-Native -Step 'winget-x86' -File $WinGet -AllowFailure -Arguments @(
                'install','--manifest',$manifest,'--architecture','x86',
                '--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
            if ($denial.Exit -eq 0 -or $denial.Text -notmatch 'No applicable installer' -or
                (Test-Path "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\$($expected.Identifier)_*") -or
                (Test-Path "$env:LOCALAPPDATA\Microsoft\WinGet\Links\$app.exe") -or
                [Environment]::GetEnvironmentVariable('Path','User') -ne $beforePath) {
                throw 'validation-failed: WinGet x86 denial failed'
            }
        }
        Invoke-Step $report 'install' {
            $script:installedWinGet = $true
            $null = Invoke-Native -Step 'winget-install' -File $WinGet -Arguments @(
                'install','--manifest',"$Work/inputs/current/staging/winget/$($expected.WinGetPath)",
                '--architecture',$wingetArch,'--accept-package-agreements','--accept-source-agreements',
                '--disable-interactivity')
        }
        Invoke-Step $report 'version' { Assert-Resolved (Get-Fresh 'winget' 'installed') $expected }
        Invoke-Step $report 'uninstall' {
            $null = Invoke-Native -Step 'winget-list' -File $WinGet -AllowFailure -Arguments @(
                'list','--id',$expected.Identifier,'--exact','--disable-interactivity')
            $null = Invoke-Native -Step 'winget-uninstall' -File $WinGet -Arguments @(
                'uninstall','--manifest',"$Work/inputs/current/staging/winget/$($expected.WinGetPath)",
                '--accept-source-agreements','--disable-interactivity')
            $script:installedWinGet = $false
            if ((Get-Fresh 'winget' 'removed').found) { throw "uninstall-failed: $app still resolves after winget uninstall" }
        }
        Save-Report $report $null
    } catch {
        Save-Report $report $_
        Write-Host "WinGet check failed: $($report.reason)"
    } finally {
        if ($script:installedWinGet) {
            $null = Invoke-Native -Step 'winget-cleanup-uninstall' -File $WinGet -AllowFailure -Arguments @(
                'uninstall','--manifest',"$Work/inputs/current/staging/winget/$($expected.WinGetPath)",
                '--accept-source-agreements','--disable-interactivity')
        }
    }
}

# T5 preservation sentinels: copy the contents of project/ and home/, never over existing files.
function Get-PreservationTargets {
    $manifest = (Get-Content "$Work/preservation/manifest.json" -Raw | ConvertFrom-Json).files
    $roots = @{ project = "$env:USERPROFILE\lifecycle-project"; home = $env:USERPROFILE }
    $targets = [ordered]@{}
    foreach ($key in $manifest.PSObject.Properties.Name) {
        $scope, $relative = $key.Split('/', 2)
        $targets[$key] = Join-Path $roots[$scope] ($relative -replace '/', '\')
    }
    return [pscustomobject]@{ Manifest = $manifest; Roots = $roots; Targets = $targets }
}

function Get-PreservationInventory {
    $p = Get-PreservationTargets
    $inventory = [ordered]@{}
    foreach ($key in $p.Targets.Keys) {
        $inventory[$key] = if (Test-Path -LiteralPath $p.Targets[$key] -PathType Leaf) {
            (Get-FileHash -LiteralPath $p.Targets[$key] -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
    }
    $scanned = @(,@('project', $p.Roots.project))
    foreach ($key in $p.Targets.Keys) {
        $scope, $relative = $key.Split('/', 2)
        if ($scope -ne 'home') { continue }
        $parts = $relative.Split('/')
        $depth = if ($parts[0] -eq '.config' -and $parts.Count -gt 2) { 2 } else { 1 }
        $scanned += ,@('home', (Join-Path $p.Roots.home ($parts[0..($depth - 1)] -join '\')))
    }
    foreach ($pair in $scanned) {
        $scope, $root = $pair
        if (!(Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($file in @(Get-ChildItem -LiteralPath $root -Recurse -Force -File)) {
            $relative = $file.FullName.Substring($p.Roots[$scope].Length).TrimStart('\') -replace '\\', '/'
            $key = "$scope/$relative"
            if (!$inventory.Contains($key)) {
                $inventory[$key] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    }
    return $inventory
}

function Test-Preservation([string]$Stage) {
    $manifest = (Get-PreservationTargets).Manifest
    $inventory = Get-PreservationInventory
    $differences = @()
    $keys = @($inventory.Keys) + @($manifest.PSObject.Properties.Name) | Sort-Object -Unique
    foreach ($key in $keys) {
        $expected = if ($manifest.PSObject.Properties.Name -contains $key) { $manifest.$key } else { $null }
        $actual = if ($inventory.Contains($key)) { $inventory[$key] } else { $null }
        if ($expected -ne $actual) { $differences += $key }
    }
    Write-Json "$Work/preservation-$Stage.json" ([ordered]@{ preserved = ($differences.Count -eq 0); differences = $differences; inventory = $inventory })
    if ($differences.Count) { throw "Preservation sentinels changed at ${Stage}: $($differences -join ', ')" }
}

function Initialize-Preservation {
    $p = Get-PreservationTargets
    foreach ($target in $p.Targets.Values) {
        if (Test-Path -LiteralPath $target) { throw "Preservation target already exists: $target" }
    }
    New-Item -ItemType Directory $p.Roots.project -Force | Out-Null
    foreach ($key in $p.Targets.Keys) {
        New-Item -ItemType Directory (Split-Path $p.Targets[$key]) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path "$Work/preservation" ($key -replace '/', '\')) -Destination $p.Targets[$key]
    }
    Test-Preservation 'seeded'
}

function Add-Stage($Stages, [string]$Name, [string]$Command, $Fresh) {
    $Stages.Add([ordered]@{ stage = $Name; command = $Command; resolution = $Fresh; preserved = $true }) | Out-Null
    Write-Json "$Work/lifecycle-stages.json" $Stages
}

function Invoke-Lifecycle([string]$WinGet) {
    $older = Get-Expected 'older'
    $current = Get-Expected 'current'
    Initialize-Preservation
    # Scoop: install the older bucket, advance the local bucket repository, update.
    $stages = New-Object Collections.ArrayList
    $bucket = New-Bucket 'older'
    $null = Scoop 'life-scoop-install' @('install',"darkquasar/$app",'--arch',$scoopArch,'--no-update-scoop')
    $fresh = Get-Fresh 'scoop' 'life-installed'; Assert-Resolved $fresh $older; Test-Preservation 'scoop-installed'
    Add-Stage $stages 'installed' "scoop install darkquasar/$app" $fresh
    Copy-Item "$Work/inputs/current/staging/scoop/bucket/$app.json" "$bucket/bucket/$app.json" -Force
    $null = Invoke-Native -Step 'bucket-advance' -File 'git.exe' -Arguments @('-C',$bucket,'-c','user.name=Native Fixture',
        '-c','user.email=fixture@example.invalid','commit','-am','Advance staging bucket to the newer candidate')
    # Keep the pinned Scoop core: hold its self-update so `scoop update` refreshes buckets only.
    $null = Scoop 'life-scoop-hold' @('config','hold_update_until','2099-12-31')
    $null = Scoop 'life-scoop-update' @('update')
    $null = Scoop 'life-scoop-update-app' @('update',$app)
    $fresh = Get-Fresh 'scoop' 'life-upgraded'; Assert-Resolved $fresh $current; Test-Preservation 'scoop-upgraded'
    Add-Stage $stages 'upgraded' "scoop update; scoop update $app" $fresh
    $null = Scoop 'life-scoop-uninstall' @('uninstall',$app)
    $fresh = Get-Fresh 'scoop' 'life-removed'
    if ($fresh.found -or (Test-Path "$env:SCOOP/shims/$app.exe")) { throw 'Scoop uninstall left the command' }
    Test-Preservation 'scoop-removed'
    Add-Stage $stages 'removed' "scoop uninstall $app" $fresh
    Write-Json "$Work/lifecycle-scoop.json" ([ordered]@{ manager = 'scoop'; platform = $platform; outcome = 'passed'
        package_shape = 'Scoop portable executable from a bucket manifest (shim), local Git bucket, pinned Scoop core held'
        versions = @($older.Version, $current.Version); stages = $stages })
    # WinGet: install the older local manifest, then upgrade with the newer one.
    $stages = New-Object Collections.ArrayList
    $oldManifest = "$Work/inputs/older/staging/winget/$($older.WinGetPath)"
    $newManifest = "$Work/inputs/current/staging/winget/$($current.WinGetPath)"
    foreach ($manifest in @($oldManifest, $newManifest)) {
        $null = Invoke-Native -Step "life-winget-validate-$(Split-Path $manifest -Leaf)" -File $WinGet -Arguments @('validate','--manifest',$manifest)
    }
    $common = @('--architecture',$wingetArch,'--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
    $null = Invoke-Native -Step 'life-winget-install' -File $WinGet -Arguments (@('install','--manifest',$oldManifest) + $common)
    $fresh = Get-Fresh 'winget' 'life-installed'; Assert-Resolved $fresh $older; Test-Preservation 'winget-installed'
    Add-Stage $stages 'installed' "winget install --manifest $oldManifest" $fresh
    $null = Invoke-Native -Step 'life-winget-upgrade' -File $WinGet -Arguments (@('upgrade','--manifest',$newManifest) + $common)
    $fresh = Get-Fresh 'winget' 'life-upgraded'; Assert-Resolved $fresh $current; Test-Preservation 'winget-upgraded'
    Add-Stage $stages 'upgraded' "winget upgrade --manifest $newManifest" $fresh
    $null = Invoke-Native -Step 'life-winget-uninstall' -File $WinGet -Arguments @(
        'uninstall','--manifest',$newManifest,'--accept-source-agreements','--disable-interactivity')
    $fresh = Get-Fresh 'winget' 'life-removed'
    if ($fresh.found) { throw 'WinGet uninstall left the command' }
    Test-Preservation 'winget-removed'
    Add-Stage $stages 'removed' "winget uninstall --manifest $newManifest" $fresh
    Write-Json "$Work/lifecycle-winget.json" ([ordered]@{ manager = 'winget'; platform = $platform; outcome = 'passed'
        package_shape = 'WinGet portable installer type from local multi-file manifests, default user scope'
        versions = @($older.Version, $current.Version); stages = $stages })
}

Start-Transcript "$Work/user.log" | Out-Null
try {
    Assert-NormalUser
    Get-ExecutionPolicy -List | Format-Table | Out-String | Write-Output
    $script:scoop = Install-PinnedScoop
    $script:installedScoop = $false
    $script:installedWinGet = $false
    $winget = $null
    if ($Phase -eq 'Check') {
        Test-Scoop
        try {
            $winget = Register-WinGet
            $policy = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppInstaller' -Name EnableLocalManifestFiles
            if ($policy.EnableLocalManifestFiles -ne 1) { throw 'Machine LocalManifestFiles policy is not effective.' }
            $null = Invoke-Native -Step 'winget-info' -File $winget -Arguments @('--info')
        } catch {
            $report = New-Report 'winget'
            $report.steps.validate = 'failed'
            Save-Report $report ([pscustomobject]@{ Exception = [Exception]::new("validation-failed: WinGet client setup failed: $($_.Exception.Message)") })
            $winget = $null
        }
        if ($winget) { Test-WinGet $winget }
        'DONE: per-release Scoop and WinGet checks finished; see report-check-*.json.' | Set-Content "$Work/DONE.txt"
    } else {
        $winget = Register-WinGet
        Invoke-Lifecycle $winget
        'PASS: Scoop and WinGet lifecycle with preservation sentinels.' | Set-Content "$Work/DONE.txt"
    }
} catch {
    $_ | Format-List * -Force | Out-String | Set-Content "$Work/failure.txt"
    throw
} finally {
    $logs = @(
        "$env:LOCALAPPDATA\Packages\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe\LocalState\DiagOutputDir",
        "$env:LOCALAPPDATA\Microsoft\WinGet\DiagOutputDir", "$env:TEMP\WinGet"
    )
    for ($i = 0; $i -lt $logs.Count; $i++) {
        if (Test-Path $logs[$i]) {
            New-Item -ItemType Directory "$Work/winget-logs/$i" -Force | Out-Null
            Copy-Item "$($logs[$i])/*" "$Work/winget-logs/$i" -Recurse -ErrorAction Continue
        }
    }
    Stop-Transcript | Out-Null
}
