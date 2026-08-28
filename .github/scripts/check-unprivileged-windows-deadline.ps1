[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $FilePath,

    [string] $WorkingDirectory = $env:GITHUB_WORKSPACE
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:RUNNER_OS -ne "Windows") {
    throw "This probe is only supported on GitHub-hosted Windows runners."
}

function ConvertTo-SingleQuotedPowerShellLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    return "'" + $Value.Replace("'", "''") + "'"
}

$wrapper = Join-Path $PSScriptRoot "invoke-bounded-unprivileged-windows.ps1"
$wrapperLiteral = ConvertTo-SingleQuotedPowerShellLiteral $wrapper
$fileLiteral = ConvertTo-SingleQuotedPowerShellLiteral $FilePath
$workingLiteral = ConvertTo-SingleQuotedPowerShellLiteral $WorkingDirectory
$probeCommand = @"
& $wrapperLiteral ``
    -FilePath $fileLiteral ``
    -ArgumentList @('-c', 'import time; time.sleep(30)') ``
    -WorkingDirectory $workingLiteral ``
    -TimeoutSeconds 30 ``
    -CleanupGraceSeconds 10 ``
    -OuterTimeoutSeconds 2
"@
$encodedCommand = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($probeCommand)
)

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = (Get-Command pwsh).Source
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
[void] $startInfo.ArgumentList.Add("-NoLogo")
[void] $startInfo.ArgumentList.Add("-NoProfile")
[void] $startInfo.ArgumentList.Add("-NonInteractive")
[void] $startInfo.ArgumentList.Add("-EncodedCommand")
[void] $startInfo.ArgumentList.Add($encodedCommand)

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
if (-not $process.Start()) {
    throw "Windows did not start the unprivileged deadline probe."
}
if (-not $process.WaitForExit(60000)) {
    $process.Kill($true)
    [void] $process.WaitForExit(5000)
    throw "The unprivileged Windows deadline probe exceeded 60 seconds."
}
$stopwatch.Stop()
if ($process.ExitCode -eq 0) {
    throw "The deliberately sleeping deadline probe unexpectedly succeeded."
}
Write-Host (
    "The unprivileged Windows deadline probe returned exit code " +
    "$($process.ExitCode) in $([Math]::Round($stopwatch.Elapsed.TotalSeconds, 2)) seconds."
)
