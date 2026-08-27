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
    -TimeoutSeconds $TimeoutSeconds
"@
$encodedCommand = [Convert]::ToBase64String(
    [Text.Encoding]::Unicode.GetBytes($wrapperCommand)
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
if (-not $process.Start()) {
    throw "Windows did not start the bounded unprivileged qPlot wrapper."
}
$outerTimeoutMilliseconds = ($TimeoutSeconds + $CleanupGraceSeconds) * 1000
if (-not $process.WaitForExit($outerTimeoutMilliseconds)) {
    # The inner wrapper is the sole owner of a kill-on-close Job handle.
    # Terminating that one process closes the handle in the kernel and
    # contains the entire standard-user child tree without enumerating it.
    $process.Kill()
    [void] $process.WaitForExit(5000)
    Publish-PersistedChildLogs
    throw (
        "The unprivileged qPlot wrapper exceeded its bounded " +
        "$($TimeoutSeconds + $CleanupGraceSeconds)-second lifetime."
    )
}
if ($process.ExitCode -ne 0) {
    Publish-PersistedChildLogs
}
exit $process.ExitCode
