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
    [int] $TimeoutSeconds = 420
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;

public static class QPlotActionsConsoleForwarder
{
    public static readonly DataReceivedEventHandler Output = ForwardOutput;
    public static readonly DataReceivedEventHandler Error = ForwardError;

    private static void ForwardOutput(object sender, DataReceivedEventArgs eventArgs)
    {
        if (eventArgs.Data != null)
        {
            Console.Out.WriteLine(eventArgs.Data);
        }
    }

    private static void ForwardError(object sender, DataReceivedEventArgs eventArgs)
    {
        if (eventArgs.Data != null)
        {
            Console.Error.WriteLine(eventArgs.Data);
        }
    }
}
'@

function Assert-PathWithin {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,

        [Parameter(Mandatory = $true)]
        [string] $Parent
    )

    $relative = [System.IO.Path]::GetRelativePath($Parent, $Path)
    if (
        [System.IO.Path]::IsPathRooted($relative) -or
        $relative -eq ".." -or
        $relative.StartsWith("..\", [System.StringComparison]::Ordinal) -or
        $relative.StartsWith("../", [System.StringComparison]::Ordinal)
    ) {
        throw "Refusing to grant an unprivileged account access outside '$Parent': '$Path'."
    }
}

function Invoke-IcaclsChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments
    )

    $output = & icacls.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls.exe failed with exit code $LASTEXITCODE.`n$($output -join [Environment]::NewLine)"
    }
}

if ($env:RUNNER_OS -ne "Windows") {
    throw "This helper is only supported on GitHub-hosted Windows runners."
}
if (-not $env:GITHUB_ACTIONS -or -not $env:GITHUB_WORKSPACE -or -not $env:RUNNER_TEMP) {
    throw "This helper requires the GitHub Actions workspace and runner environment."
}

$workspace = (Resolve-Path -LiteralPath $env:GITHUB_WORKSPACE).Path
$workingPath = (Resolve-Path -LiteralPath $WorkingDirectory).Path
Assert-PathWithin -Path $workingPath -Parent $workspace

$resolvedExecutable = (Resolve-Path -LiteralPath $FilePath).Path
$runnerTemp = (Resolve-Path -LiteralPath $env:RUNNER_TEMP).Path
$unprivilegedTemp = if ($env:QPLOT_UNPRIVILEGED_TEMP) {
    [System.IO.Path]::GetFullPath($env:QPLOT_UNPRIVILEGED_TEMP)
} else {
    Join-Path $runnerTemp "qplot-standard-user"
}
Assert-PathWithin -Path $unprivilegedTemp -Parent $runnerTemp
[void] (New-Item -ItemType Directory -Force -Path $unprivilegedTemp)
$unprivilegedTemp = (Resolve-Path -LiteralPath $unprivilegedTemp).Path

$userName = "qplotci$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$plainPassword = "Qp1!$([Guid]::NewGuid().ToString('N'))"
$securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
$plainPassword = $null
$userCreated = $false
$grantedPaths = [System.Collections.Generic.List[string]]::new()
$aclIdentity = $null
$childExitCode = $null
$primaryError = $null
$process = $null
$outputHandler = $null
$errorHandler = $null

try {
    $localUser = New-LocalUser `
        -Name $userName `
        -Password $securePassword `
        -AccountNeverExpires `
        -PasswordNeverExpires `
        -UserMayNotChangePassword `
        -Description "Disposable qPlot CI standard account"
    $userCreated = $true

    $usersGroup = Get-LocalGroup -SID (
        [System.Security.Principal.SecurityIdentifier] "S-1-5-32-545"
    )
    $userGroupSids = @(
        Get-LocalGroupMember -Group $usersGroup |
            ForEach-Object { $_.SID.Value }
    )
    if ($userGroupSids -notcontains $localUser.SID.Value) {
        Add-LocalGroupMember -Group $usersGroup -Member $localUser
    }

    $administratorsGroup = Get-LocalGroup -SID (
        [System.Security.Principal.SecurityIdentifier] "S-1-5-32-544"
    )
    $administratorSids = @(
        Get-LocalGroupMember -Group $administratorsGroup |
            ForEach-Object { $_.SID.Value }
    )
    if ($administratorSids -contains $localUser.SID.Value) {
        throw "The disposable qPlot CI account unexpectedly belongs to Administrators."
    }

    $aclIdentity = "*$($localUser.SID.Value)"
    foreach ($grantPath in @($workspace, $unprivilegedTemp)) {
        # Record before icacls runs so a partial recursive grant is also
        # removed if icacls reports an error partway through the tree.
        $grantedPaths.Add($grantPath)
        # Give the disposable account effective access to every existing
        # object.  Inheritance flags apply to directories, not leaf files, so
        # this must be a separate recursive pass without (OI)/(CI).
        Invoke-IcaclsChecked -Arguments @(
            $grantPath,
            "/grant:r",
            "${aclIdentity}:M",
            "/T",
            "/Q"
        )
        # Add inheritance on the root after /grant:r so files and directories
        # created by the child process receive the same access automatically.
        Invoke-IcaclsChecked -Arguments @(
            $grantPath,
            "/grant",
            "${aclIdentity}:(OI)(CI)M",
            "/Q"
        )
    }

    $homePath = Join-Path $unprivilegedTemp "home"
    $roamingPath = Join-Path $homePath "AppData\Roaming"
    $localAppDataPath = Join-Path $homePath "AppData\Local"
    [void] (New-Item -ItemType Directory -Force -Path $roamingPath)
    [void] (New-Item -ItemType Directory -Force -Path $localAppDataPath)

    # Git rejects a repository owned by the elevated runner when it is read
    # by the standard account. Trust only this checked-out workspace through
    # the disposable account's protected global configuration.
    $gitConfigPath = Join-Path $homePath ".gitconfig"
    $gitWorkspace = $workspace.Replace("\", "/").Replace('"', '\"')
    [System.IO.File]::WriteAllText(
        $gitConfigPath,
        "[safe]`n`tdirectory = `"$gitWorkspace`"`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $resolvedExecutable
    $startInfo.WorkingDirectory = $workingPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UserName = $userName
    $startInfo.Domain = $env:COMPUTERNAME
    $startInfo.Password = $securePassword
    $startInfo.LoadUserProfile = $true

    foreach ($argument in $ArgumentList) {
        [void] $startInfo.ArgumentList.Add($argument)
    }

    $startInfo.Environment.Clear()
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $startInfo.Environment[[string] $entry.Key] = [string] $entry.Value
    }
    $startInfo.Environment["USERNAME"] = $userName
    $startInfo.Environment["USERDOMAIN"] = $env:COMPUTERNAME
    $startInfo.Environment["USERPROFILE"] = $homePath
    $startInfo.Environment["HOME"] = $homePath
    $startInfo.Environment["APPDATA"] = $roamingPath
    $startInfo.Environment["LOCALAPPDATA"] = $localAppDataPath
    $startInfo.Environment["TEMP"] = $unprivilegedTemp
    $startInfo.Environment["TMP"] = $unprivilegedTemp
    $startInfo.Environment["TMPDIR"] = $unprivilegedTemp
    $startInfo.Environment["RUNNER_TEMP"] = $unprivilegedTemp
    $startInfo.Environment["PIP_CACHE_DIR"] = Join-Path $unprivilegedTemp "pip-cache"
    $startInfo.Environment["MPLCONFIGDIR"] = Join-Path $unprivilegedTemp "matplotlib"
    $startInfo.Environment["QPLOT_UNPRIVILEGED_TEMP"] = $unprivilegedTemp

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Windows did not start the unprivileged qPlot CI process."
    }

    # Forward complete lines as they arrive, but never make direct-process
    # completion depend on pipe EOF. A failed subprocess regression can leave
    # a descendant holding an inherited pipe after pytest has already exited;
    # ReadToEndAsync would then hide the traceback until the Actions timeout.
    $outputHandler = [QPlotActionsConsoleForwarder]::Output
    $errorHandler = [QPlotActionsConsoleForwarder]::Error
    $process.add_OutputDataReceived($outputHandler)
    $process.add_ErrorDataReceived($errorHandler)
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    # Use only the timed overload here. The parameterless overload also waits
    # for asynchronous output handlers to observe pipe EOF, which may never
    # arrive when a failed subprocess regression leaves a descendant holding
    # an inherited stdout or stderr handle after pytest itself has exited.
    $processDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while (-not $process.WaitForExit(250)) {
        # Poll only the direct pytest process; diagnostics keep streaming via
        # the DataReceived handlers above.
        if ([DateTime]::UtcNow -ge $processDeadline) {
            try {
                $process.Kill($true)
                [void] $process.WaitForExit(5000)
            } catch {
                Write-Warning "Could not terminate timed-out child tree: $_"
            }
            throw (
                "The unprivileged qPlot CI process exceeded its " +
                "$TimeoutSeconds-second direct-process deadline."
            )
        }
    }
    Start-Sleep -Milliseconds 250
    $childExitCode = $process.ExitCode
} catch {
    $primaryError = $_
} finally {
    if ($userCreated) {
        for ($index = $grantedPaths.Count - 1; $index -ge 0; $index--) {
            try {
                Invoke-IcaclsChecked -Arguments @(
                    $grantedPaths[$index],
                    "/remove:g",
                    $aclIdentity,
                    "/T",
                    "/C",
                    "/Q"
                )
            } catch {
                Write-Warning "Could not remove the qPlot CI ACL: $_"
            }
        }
        try {
            Remove-LocalUser -Name $userName
        } catch {
            Write-Warning "Could not remove the disposable qPlot CI account: $_"
        }
    }
    if ($null -ne $process) {
        if ($null -ne $outputHandler) {
            try {
                $process.CancelOutputRead()
            } catch {
                Write-Warning "Could not stop reading child stdout: $_"
            }
            $process.remove_OutputDataReceived($outputHandler)
        }
        if ($null -ne $errorHandler) {
            try {
                $process.CancelErrorRead()
            } catch {
                Write-Warning "Could not stop reading child stderr: $_"
            }
            $process.remove_ErrorDataReceived($errorHandler)
        }
        $process.Dispose()
    }
    if ($null -ne $securePassword) {
        $securePassword.Dispose()
    }
}

if ($null -ne $primaryError) {
    Write-Error $primaryError
    exit 1
}
if ($null -eq $childExitCode) {
    Write-Error "The unprivileged qPlot CI process did not return an exit code."
    exit 1
}
exit $childExitCode
