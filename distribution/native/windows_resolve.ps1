# Command resolution and version checks shared by windows_user.ps1 and its regression
# test (test_windows_resolve.ps1). Windows PowerShell 5.1 compatible. Nothing here runs a
# program unless the caller names the exact SHA-256 it expects to run.

function Test-VersionToken([string]$Output, [string]$Version) {
    # `v<version>` as a whole token: v2.7.1 matches; v2.7.10, v2.7.1-rc1 and v2.7.0 do not.
    return "$Output".Contains("v$Version")
}

function Resolve-NativeCommand {
    # Resolve one command name the way a fresh process does with the current $env:Path.
    # With -ExpectedSha, the resolved executable runs (`--version`, 60 s bound) only when its
    # SHA-256 equals that digest. Without it the probe is resolve-only and never starts a process.
    param(
        [Parameter(Mandatory)][ValidateSet('scoop','winget')][string]$Manager,
        [Parameter(Mandatory)][string]$Command,
        [string]$Identifier = '',
        [string]$ExpectedSha = '',
        [Parameter(Mandatory)][string]$OutputBase
    )
    $result = [ordered]@{ found = $false; source = $null; target = $null; mechanism = $null; sha256 = $null
                          executed = $false; output = $null }
    $resolved = @(Get-Command "$Command.exe" -CommandType Application -ErrorAction SilentlyContinue) | Select-Object -First 1
    if (!$resolved) { return $result }
    $result.found = $true
    $result.source = $resolved.Source
    if ($Manager -eq 'scoop') {
        $shims = Join-Path $env:USERPROFILE 'scoop\shims'
        if ($resolved.Source -ne (Join-Path $shims "$Command.exe")) {
            $result.mechanism = 'unexpected'
            $result.target = $resolved.Source
        } else {
            $shim = Get-Content (Join-Path $shims "$Command.shim") -ErrorAction SilentlyContinue |
                Where-Object { $_ -match '^path\s*=' }
            $result.target = if ($shim) { ($shim -replace '^path\s*=\s*', '').Trim().Trim('"') } else { $resolved.Source }
            $result.mechanism = 'scoop-shim'
        }
    } else {
        $links = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\$Command.exe"
        $packageRoot = (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') + '\'
        if ($resolved.Source -eq $links) {
            $result.mechanism = 'winget-link'
            $item = Get-Item $links
            $result.target = if ($item.Target) { @($item.Target)[0] } else { $links }
        } elseif ($resolved.Source.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $parts = $resolved.Source.Substring($packageRoot.Length).Split('\')
            $valid = $parts.Count -eq 2 -and $parts[1] -eq "$Command.exe" -and
                     $parts[0].StartsWith("$($Identifier)_", [StringComparison]::OrdinalIgnoreCase)
            $result.mechanism = if ($valid) { 'winget-package-path' } else { 'unexpected' }
            $result.target = $resolved.Source
        } else {
            $result.mechanism = 'unexpected'
            $result.target = $resolved.Source
        }
    }
    if (Test-Path -LiteralPath $result.target -PathType Leaf) {
        $result.sha256 = (Get-FileHash -LiteralPath $result.target -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    # Only the host architecture's snapshot asset ever runs: a foreign or leftover executable
    # is recorded, never started (it could also block a non-interactive session on a dialog).
    if ((!$ExpectedSha -or $result.sha256 -eq $ExpectedSha) -and $result.mechanism -ne 'unexpected') {
        $process = Start-Process -FilePath $resolved.Source -ArgumentList '--version' -NoNewWindow -PassThru `
            -RedirectStandardOutput "$OutputBase.out" -RedirectStandardError "$OutputBase.err"
        if (!$process.WaitForExit(60000)) { $process.Kill(); throw "$Command --version exceeded 60 seconds" }
        $result.executed = $true
        $result.output = ((Get-Content "$OutputBase.out", "$OutputBase.err" -ErrorAction SilentlyContinue) | Out-String).Trim()
    }
    return $result
}
