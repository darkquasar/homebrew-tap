# Regression for windows_resolve.ps1 (Windows PowerShell 5.1, disposable Windows runner).
# T7R-04: removal probes resolve only; a leftover executable never starts, so its
# execution marker is never created. T7R-05: the version check is an exact token.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. "$PSScriptRoot/windows_resolve.ps1"
$script:failures = @()
function Check([string]$Name, [bool]$Condition) {
    if ($Condition) { Write-Output "PASS $Name" } else { Write-Output "FAIL $Name"; $script:failures += $Name }
}

Check 'version: accepts patronus version v2.7.1' (Test-VersionToken 'patronus version v2.7.1' '2.7.1')
Check 'version: accepts a trailing CRLF' (Test-VersionToken "patronus version v2.7.1`r`n" '2.7.1')
foreach ($output in @('patronus version v2.7.10', 'patronus version v2.7.1-rc1', 'patronus version v2.7.0',
                      'patronus version v2.7.1+build', 'patronus version v12.7.1', 'patronus version 2.7.1', '')) {
    Check "version: rejects '$output'" (-not (Test-VersionToken $output '2.7.1'))
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('resolve-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $root | Out-Null
$env:PROBE_MARKER = Join-Path $root 'executed.marker'
$probe = Join-Path $root 'probe.exe'
Add-Type -OutputAssembly $probe -OutputType ConsoleApplication -TypeDefinition @'
public static class Probe {
    public static void Main(string[] args) {
        System.IO.File.WriteAllText(System.Environment.GetEnvironmentVariable("PROBE_MARKER"), "executed");
        System.Console.WriteLine("leftover version v2.7.1");
    }
}
'@
$sha = (Get-FileHash $probe -Algorithm SHA256).Hash.ToLowerInvariant()
$env:USERPROFILE = Join-Path $root 'home'
$env:LOCALAPPDATA = Join-Path $root 'local'
$shims = Join-Path $env:USERPROFILE 'scoop\shims'
$apps = Join-Path $env:USERPROFILE 'scoop\apps\leftover\current'
$package = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\darkquasar.Leftover_Microsoft.Winget.Source_8wekyb3d8bbwe'
New-Item -ItemType Directory $shims, $apps, $package -Force | Out-Null
Copy-Item $probe (Join-Path $shims 'leftover.exe')
Copy-Item $probe (Join-Path $apps 'leftover-windows-amd64.exe')
"path = `"$apps\leftover-windows-amd64.exe`"" | Set-Content (Join-Path $shims 'leftover.shim')
Copy-Item $probe (Join-Path $package 'leftover.exe')

$cases = @(
    @{ manager = 'scoop'; path = $shims },
    @{ manager = 'winget'; path = $package }
)
foreach ($case in $cases) {
    $env:Path = $case.path
    $base = Join-Path $root "probe-$($case.manager)"
    foreach ($expected in @('', ('0' * 64))) {
        $label = if ($expected) { 'a different expected digest' } else { 'no expected digest (removal probe)' }
        $result = Resolve-NativeCommand -Manager $case.manager -Command 'leftover' -Identifier 'darkquasar.Leftover' `
            -ExpectedSha $expected -OutputBase $base
        Check "$($case.manager): leftover is found with $label" ([bool]$result.found)
        Check "$($case.manager): leftover is not executed with $label" (-not $result.executed)
        Check "$($case.manager): no execution marker with $label" (-not (Test-Path $env:PROBE_MARKER))
    }
    # Positive control: the probe does create the marker when the exact expected digest runs it.
    $result = Resolve-NativeCommand -Manager $case.manager -Command 'leftover' -Identifier 'darkquasar.Leftover' `
        -ExpectedSha $sha -OutputBase $base
    Check "$($case.manager): control run with the exact digest executes" ([bool]$result.executed -and (Test-Path $env:PROBE_MARKER))
    Remove-Item $env:PROBE_MARKER -ErrorAction SilentlyContinue
}

if ($script:failures.Count) { Write-Output "FAILED: $($script:failures.Count)"; exit 1 }
Write-Output 'OK: all resolve and version checks passed'
exit 0
