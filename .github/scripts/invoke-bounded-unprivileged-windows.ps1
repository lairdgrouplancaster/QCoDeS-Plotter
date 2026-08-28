[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $FilePath,

    [Parameter(Mandatory = $true)]
    [AllowEmptyCollection()]
    [string[]] $ArgumentList,

    [string] $WorkingDirectory = $env:GITHUB_WORKSPACE,

    [ValidateRange(1, 3600)]
    [int] $TimeoutSeconds = 420,

    [ValidateRange(1, 600)]
    [int] $CleanupGraceSeconds = 120
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:RUNNER_OS -ne "Windows") {
    throw "This helper is only supported on GitHub-hosted Windows runners."
}

function ConvertTo-SingleQuotedPowerShellLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Value
    )

    return "'" + $Value.Replace("'", "''") + "'"
}

function Publish-PersistedLog {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,

        [Parameter(Mandatory = $true)]
        [System.IO.TextWriter] $Destination
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    try {
        $Destination.Write([System.IO.File]::ReadAllText($Path))
    } catch {
        Write-Warning "Could not publish bounded child diagnostics from '$Path': $_"
    }
}

function Publish-PersistedChildLogs {
    if (-not $env:QPLOT_UNPRIVILEGED_TEMP) {
        return
    }
    Publish-PersistedLog `
        -Path (Join-Path $env:QPLOT_UNPRIVILEGED_TEMP "child-stdout.txt") `
        -Destination ([Console]::Out)
    Publish-PersistedLog `
        -Path (Join-Path $env:QPLOT_UNPRIVILEGED_TEMP "child-stderr.txt") `
        -Destination ([Console]::Error)
}

$wrapper = Join-Path $PSScriptRoot "run-unprivileged-windows.ps1"
$wrapperLiteral = ConvertTo-SingleQuotedPowerShellLiteral $wrapper
$fileLiteral = ConvertTo-SingleQuotedPowerShellLiteral $FilePath
$workingLiteral = ConvertTo-SingleQuotedPowerShellLiteral $WorkingDirectory
$phaseLogRoot = if ($env:RUNNER_TEMP) {
    $env:RUNNER_TEMP
} else {
    [IO.Path]::GetTempPath()
}
$phaseLogPath = Join-Path `
    $phaseLogRoot `
    "qplot-bounded-wrapper-$([Guid]::NewGuid().ToString('N')).txt"
$phaseLogLiteral = ConvertTo-SingleQuotedPowerShellLiteral $phaseLogPath
$argumentLiterals = @(
    $ArgumentList | ForEach-Object {
        ConvertTo-SingleQuotedPowerShellLiteral $_
    }
)
$argumentExpression = "@(" + ($argumentLiterals -join ", ") + ")"
$wrapperCommand = @"
& $wrapperLiteral ``
    -FilePath $fileLiteral ``
    -ArgumentList $argumentExpression ``
    -WorkingDirectory $workingLiteral ``
    -TimeoutSeconds $TimeoutSeconds *> $phaseLogLiteral
"@
$encodedCommand = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($wrapperCommand)
)

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = (Get-Command pwsh).Source
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
[void] $startInfo.ArgumentList.Add("-NoLogo")
[void] $startInfo.ArgumentList.Add("-NoProfile")
[void] $startInfo.ArgumentList.Add("-NonInteractive")
[void] $startInfo.ArgumentList.Add("-EncodedCommand")
[void] $startInfo.ArgumentList.Add($encodedCommand)

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) {
    throw "Windows did not start the bounded unprivileged qPlot wrapper."
}
$outerLifetimeSeconds = $TimeoutSeconds + $CleanupGraceSeconds
$outerDeadline = [DateTime]::UtcNow.AddSeconds($outerLifetimeSeconds)
Write-Host (
    "The bounded unprivileged wrapper started with a " +
    "$outerLifetimeSeconds-second outer deadline."
)
while (-not $process.WaitForExit(250)) {
    if ([DateTime]::UtcNow -lt $outerDeadline) {
        continue
    }
    Write-Host "The bounded unprivileged wrapper reached its outer deadline."
    # The inner wrapper is the sole owner of a kill-on-close Job handle.
    # Terminating that one process closes the handle in the kernel and
    # contains the entire standard-user child tree without enumerating it.
    $process.Kill()
    Write-Host "The bounded unprivileged wrapper termination call returned."
    [void] $process.WaitForExit(5000)
    Publish-PersistedLog -Path $phaseLogPath -Destination ([Console]::Out)
    Publish-PersistedChildLogs
    Write-Error (
        "The unprivileged qPlot wrapper exceeded its bounded " +
        "$outerLifetimeSeconds-second lifetime."
    )
    # Do not re-enter Process.Dispose(), PowerShell unwinding, or asynchronous
    # EOF accounting here. A native cleanup utility may still own the private
    # redirected pipe's write end; the OS process boundary closes our read end
    # without waiting and, crucially, no descendant owns an Actions pipe.
    [Environment]::Exit(1)
}
$wrapperExitCode = $process.ExitCode
if ($wrapperExitCode -ne 0) {
    Publish-PersistedLog -Path $phaseLogPath -Destination ([Console]::Out)
    Publish-PersistedChildLogs
}
exit $wrapperExitCode
