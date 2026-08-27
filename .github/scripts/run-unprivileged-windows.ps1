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
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

public sealed class QPlotProcessJob : IDisposable
{
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;
    private const int JobObjectExtendedLimitInformationClass = 9;
    private readonly object sync = new object();
    private IntPtr handle;
    private ManualResetEvent deadlineCancelled;
    private Thread deadlineThread;
    private bool timedOut;

    public QPlotProcessJob()
    {
        handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        var information = new JobObjectExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags =
            JobObjectLimitKillOnJobClose;
        if (!SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformationClass,
            ref information,
            (uint)Marshal.SizeOf<JobObjectExtendedLimitInformation>()))
        {
            int error = Marshal.GetLastWin32Error();
            Dispose();
            throw new Win32Exception(error);
        }
    }

    public void Assign(Process process)
    {
        if (!AssignProcessToJobObject(handle, process.Handle))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }

    public bool TimedOut
    {
        get
        {
            lock (sync)
            {
                return timedOut;
            }
        }
    }

    public void ArmTimeout(int timeoutMilliseconds)
    {
        if (timeoutMilliseconds < 1)
        {
            throw new ArgumentOutOfRangeException(nameof(timeoutMilliseconds));
        }
        lock (sync)
        {
            if (handle == IntPtr.Zero)
            {
                throw new ObjectDisposedException(nameof(QPlotProcessJob));
            }
            if (deadlineThread != null)
            {
                throw new InvalidOperationException("The job deadline is already armed.");
            }
            deadlineCancelled = new ManualResetEvent(false);
            deadlineThread = new Thread(WaitForDeadline);
            deadlineThread.IsBackground = true;
            deadlineThread.Name = "qplot-Windows-test-deadline";
            deadlineThread.Start(timeoutMilliseconds);
        }
    }

    private void WaitForDeadline(object state)
    {
        int timeoutMilliseconds = (int)state;
        ManualResetEvent cancelled = deadlineCancelled;
        if (cancelled.WaitOne(timeoutMilliseconds))
        {
            return;
        }
        lock (sync)
        {
            if (handle == IntPtr.Zero)
            {
                return;
            }
            timedOut = true;
            IntPtr ownedHandle = handle;
            handle = IntPtr.Zero;
            CloseHandle(ownedHandle);
        }
    }

    public void Dispose()
    {
        lock (sync)
        {
            if (handle == IntPtr.Zero)
            {
                return;
            }
            if (deadlineCancelled != null)
            {
                deadlineCancelled.Set();
            }
            IntPtr ownedHandle = handle;
            handle = IntPtr.Zero;
            if (!CloseHandle(ownedHandle))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
        GC.SuppressFinalize(this);
    }

    ~QPlotProcessJob()
    {
        if (handle != IntPtr.Zero)
        {
            CloseHandle(handle);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(
        IntPtr jobAttributes,
        string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        ref JobObjectExtendedLimitInformation information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(
        IntPtr job,
        IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
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

function Publish-ChildLog {
    param(
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [System.IO.TextWriter] $Destination
    )

    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return
    }
    try {
        $Destination.Write([System.IO.File]::ReadAllText($Path))
    } catch {
        Write-Warning "Could not publish child diagnostics from '$Path': $_"
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
$processStarted = $false
$processJob = $null
$processContained = $false
$stdoutPath = $null
$stderrPath = $null

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

    $stdoutPath = Join-Path $unprivilegedTemp "child-stdout.txt"
    $stderrPath = Join-Path $unprivilegedTemp "child-stderr.txt"
    $pythonArguments = ConvertTo-Json -Compress -InputObject @($ArgumentList)
    $pythonBootstrap = @'
import json
import os
import runpy
import sys

stdout_path = os.environ["QPLOT_CI_STDOUT_PATH"]
stderr_path = os.environ["QPLOT_CI_STDERR_PATH"]
stdout_file = open(stdout_path, "w", encoding="utf-8", buffering=1)
stderr_file = open(stderr_path, "w", encoding="utf-8", buffering=1)
os.dup2(stdout_file.fileno(), 1, inheritable=True)
os.dup2(stderr_file.fileno(), 2, inheritable=True)
sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
sys.stderr = open(2, "w", encoding="utf-8", buffering=1, closefd=False)
sys.path.insert(0, os.getcwd())

arguments = json.loads(os.environ["QPLOT_CI_PYTHON_ARGUMENTS"])
if not arguments:
    raise SystemExit("The unprivileged Python command has no target.")
if arguments[0] == "-m":
    if len(arguments) < 2:
        raise SystemExit("The unprivileged Python -m command has no module.")
    target = arguments[1]
    sys.argv = [target, *arguments[2:]]
    runpy.run_module(target, run_name="__main__", alter_sys=False)
else:
    target = arguments[0]
    sys.argv = [target, *arguments[1:]]
    runpy.run_path(target, run_name="__main__")
'@
    $bootstrapPath = Join-Path $unprivilegedTemp "run-python-command.py"
    [System.IO.File]::WriteAllText(
        $bootstrapPath,
        $pythonBootstrap,
        [System.Text.UTF8Encoding]::new($false)
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $resolvedExecutable
    $startInfo.WorkingDirectory = $workingPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $false
    $startInfo.RedirectStandardError = $false
    $startInfo.UserName = $userName
    $startInfo.Domain = $env:COMPUTERNAME
    $startInfo.Password = $securePassword
    $startInfo.LoadUserProfile = $true

    [void] $startInfo.ArgumentList.Add($bootstrapPath)

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
    $startInfo.Environment["QPLOT_CI_STDOUT_PATH"] = $stdoutPath
    $startInfo.Environment["QPLOT_CI_STDERR_PATH"] = $stderrPath
    $startInfo.Environment["QPLOT_CI_PYTHON_ARGUMENTS"] = $pythonArguments
    $startInfo.Environment["PYTHONUNBUFFERED"] = "1"

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $processJob = [QPlotProcessJob]::new()
    if (-not $process.Start()) {
        throw "Windows did not start the unprivileged qPlot CI process."
    }
    $processStarted = $true
    $processJob.Assign($process)
    $processContained = $true
    $processJob.ArmTimeout($TimeoutSeconds * 1000)

    # Poll only the direct Python process. Its bootstrap redirected fd 1/2 to
    # regular files, so descendants cannot create an anonymous-pipe EOF wait.
    while (-not $process.WaitForExit(250)) {
    }
    $childExitCode = $process.ExitCode
    if ($processJob.TimedOut) {
        throw (
            "The unprivileged qPlot CI process exceeded its " +
            "$TimeoutSeconds-second direct-process deadline."
        )
    }
} catch {
    $primaryError = $_
} finally {
    # Closing the job handle kills every remaining descendant and therefore
    # closes inherited output handles before reader and account cleanup.
    if ($null -ne $processJob) {
        try {
            $processJob.Dispose()
        } catch {
            Write-Warning "Could not close the unprivileged process job: $_"
        }
    }
    if (-not $processContained -and $processStarted -and $null -ne $process) {
        try {
            if (-not $process.HasExited) {
                $process.Kill()
                [void] $process.WaitForExit(5000)
            }
        } catch {
            Write-Warning "Could not terminate the uncontained child: $_"
        }
    }
    Publish-ChildLog -Path $stdoutPath -Destination ([Console]::Out)
    Publish-ChildLog -Path $stderrPath -Destination ([Console]::Error)
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
