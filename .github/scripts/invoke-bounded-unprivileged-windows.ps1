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
    [int] $CleanupGraceSeconds = 120,

    [ValidateRange(0, 3600)]
    [int] $OuterTimeoutSeconds = 0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:RUNNER_OS -ne "Windows") {
    throw "This helper is only supported on GitHub-hosted Windows runners."
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Threading;

public sealed class QPlotOuterDeadline : IDisposable
{
    private readonly ManualResetEvent cancelled = new ManualResetEvent(false);
    private readonly IntPtr processHandle;
    private readonly Thread thread;

    public QPlotOuterDeadline(IntPtr processHandle, int timeoutMilliseconds)
    {
        if (processHandle == IntPtr.Zero)
        {
            throw new ArgumentException(
                "The nested wrapper process handle is invalid.",
                nameof(processHandle)
            );
        }
        if (timeoutMilliseconds < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(timeoutMilliseconds));
        }
        this.processHandle = processHandle;
        thread = new Thread(WaitForDeadline);
        thread.IsBackground = true;
        thread.Name = "qplot-Windows-CI-outer-deadline";
        thread.Start(timeoutMilliseconds);
    }

    private void WaitForDeadline(object state)
    {
        int timeoutMilliseconds = (int)state;
        if (cancelled.WaitOne(timeoutMilliseconds))
        {
            return;
        }
        Console.Error.WriteLine(
            "The bounded unprivileged qPlot wrapper reached its outer deadline."
        );
        if (!TerminateProcess(processHandle, 1))
        {
            Console.Error.WriteLine(
                "Native termination of the nested wrapper returned Windows error " +
                Marshal.GetLastWin32Error() + "."
            );
        }
        Environment.Exit(1);
    }

    public void Dispose()
    {
        cancelled.Set();
        GC.SuppressFinalize(this);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(
        IntPtr processHandle,
        uint exitCode
    );
}
'@

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

    $maximumBytes = 32768
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        $length = [int] [Math]::Min($stream.Length, $maximumBytes)
        if ($stream.Length -gt $length) {
            $Destination.WriteLine(
                "[qPlot diagnostics truncated to the final $length bytes]"
            )
        }
        [void] $stream.Seek(-$length, [System.IO.SeekOrigin]::End)
        $buffer = [byte[]]::new($length)
        $totalRead = 0
        while ($totalRead -lt $length) {
            $read = $stream.Read($buffer, $totalRead, $length - $totalRead)
            if ($read -eq 0) {
                break
            }
            $totalRead += $read
        }
        $Destination.Write(
            [System.Text.Encoding]::UTF8.GetString($buffer, 0, $totalRead)
        )
    } catch {
        Write-Warning "Could not publish bounded child diagnostics from '$Path': $_"
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
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
$phaseLogPath = if ($env:QPLOT_CI_PHASE_LOG_PATH) {
    [System.IO.Path]::GetFullPath($env:QPLOT_CI_PHASE_LOG_PATH)
} else {
    Join-Path `
        $phaseLogRoot `
        "qplot-bounded-wrapper-$([Guid]::NewGuid().ToString('N')).txt"
}
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
if ($env:QPLOT_CI_OUTER_WRAPPER_PID_PATH) {
    [System.IO.File]::WriteAllText(
        $env:QPLOT_CI_OUTER_WRAPPER_PID_PATH,
        [string] $process.Id,
        [System.Text.UTF8Encoding]::new($false)
    )
}
$outerLifetimeSeconds = if ($OuterTimeoutSeconds -gt 0) {
    $OuterTimeoutSeconds
} else {
    $TimeoutSeconds + $CleanupGraceSeconds
}
$outerDeadline = [QPlotOuterDeadline]::new(
    $process.Handle,
    $outerLifetimeSeconds * 1000
)
Write-Host (
    "The bounded unprivileged wrapper started with a " +
    "$outerLifetimeSeconds-second outer deadline."
)
while (-not $process.WaitForExit(250)) {
    # Process observation is deliberately subordinate to the independent
    # compiled deadline thread. If this call, PowerShell, or populated-Job
    # cleanup blocks, that thread exits this outer process without unwinding.
}
$outerDeadline.Dispose()
$wrapperExitCode = $process.ExitCode
if ($wrapperExitCode -ne 0) {
    Publish-PersistedLog -Path $phaseLogPath -Destination ([Console]::Out)
    Publish-PersistedChildLogs
}
exit $wrapperExitCode
