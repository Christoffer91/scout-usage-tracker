[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update", "status", "open", "uninstall")]
    [string]$Action = "install",
    [switch]$InstallScoutSkill,
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"
$OwnerMarkerName = ".scout-usage-tracker-owned"

function Get-CanonicalPath([string]$PathValue) {
    $full = [IO.Path]::GetFullPath($PathValue)
    $missing = New-Object System.Collections.Generic.List[string]
    $cursor = $full
    while (-not (Test-Path -LiteralPath $cursor)) {
        $leaf = Split-Path -Leaf $cursor
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrEmpty($leaf) -or $parent -eq $cursor) { break }
        $missing.Insert(0, $leaf)
        $cursor = $parent
    }
    if (Test-Path -LiteralPath $cursor) {
        $cursor = (Get-Item -LiteralPath $cursor -Force).FullName
    }
    foreach ($leaf in $missing) { $cursor = Join-Path $cursor $leaf }
    return [IO.Path]::GetFullPath($cursor)
}

$ProjectRoot = Get-CanonicalPath $PSScriptRoot

function Fail([string]$Message, [int]$Code = 1) {
    [Console]::Error.WriteLine($Message)
    exit $Code
}

function Env-OrDefault([string]$Name, [string]$DefaultValue) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $DefaultValue }
    return $value
}

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    Fail "USERPROFILE must identify the current user profile."
}
$ProfileRoot = (Get-CanonicalPath $env:USERPROFILE).TrimEnd('\')
if ([string]::IsNullOrWhiteSpace($ProfileRoot) -or $ProfileRoot -eq [IO.Path]::GetPathRoot($ProfileRoot)) {
    Fail "Unsafe USERPROFILE: expected a non-root directory."
}

$InstallRoot = Get-CanonicalPath (Env-OrDefault "SCOUT_USAGE_INSTALL_ROOT" (Join-Path $ProfileRoot ".local\share\scout-usage-tracker"))
$BinRoot = Get-CanonicalPath (Env-OrDefault "SCOUT_USAGE_BIN_DIR" (Join-Path $ProfileRoot ".local\bin"))
$ConfigRoot = Get-CanonicalPath (Env-OrDefault "SCOUT_USAGE_CONFIG_DIR" (Join-Path $ProfileRoot ".config\scout-usage-tracker"))
$ScoutSkillRoot = Get-CanonicalPath (Env-OrDefault "SCOUT_COST_SKILL_DIR" (Join-Path $ProfileRoot ".scout\m-skills\cost"))
$CopilotSkillRoot = Get-CanonicalPath (Env-OrDefault "COPILOT_COST_SKILL_DIR" (Join-Path $ProfileRoot ".copilot\m-skills\cost"))
$Launcher = Join-Path $BinRoot "scout-usage.cmd"
$LauncherMarker = Join-Path $BinRoot "scout-usage.cmd.scout-usage-tracker-owned"
$ConfigPath = Join-Path $ConfigRoot "config.json"

function Assert-BelowProfile([string]$PathValue) {
    $prefix = $ProfileRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $PathValue.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        Fail "Unsafe managed path: every managed path must be strictly below USERPROFILE."
    }
}

function Assert-NoReparsePoint([string]$PathValue) {
    Assert-BelowProfile $PathValue
    $relative = $PathValue.Substring($ProfileRoot.Length).TrimStart('\')
    $current = $ProfileRoot
    foreach ($part in $relative.Split([char]'\', [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail "Refusing managed path containing a reparse point."
            }
        }
    }
}

$ManagedRoots = @($InstallRoot, $BinRoot, $ConfigRoot, $ScoutSkillRoot, $CopilotSkillRoot)
foreach ($pathValue in $ManagedRoots) {
    $managed = $pathValue.TrimEnd('\')
    $project = $ProjectRoot.TrimEnd('\')
    if ($managed.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
        $managed.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $project.StartsWith($managed + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Fail "Unsafe managed path: managed paths must not overlap the source package."
    }
}
foreach ($pathValue in $ManagedRoots) { Assert-NoReparsePoint $pathValue }
for ($left = 0; $left -lt $ManagedRoots.Count; $left++) {
    for ($right = $left + 1; $right -lt $ManagedRoots.Count; $right++) {
        $a = $ManagedRoots[$left].TrimEnd('\')
        $b = $ManagedRoots[$right].TrimEnd('\')
        if ($a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
            $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $b.StartsWith($a + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Fail "Unsafe managed paths: managed roots must be pairwise disjoint."
        }
    }
}

function Assert-OwnedDirectoryOrAbsent([string]$Root) {
    if (Test-Path -LiteralPath $Root) {
        $item = Get-Item -LiteralPath $Root -Force
        if (-not $item.PSIsContainer) { Fail "Refusing a non-directory managed root." }
        $marker = Join-Path $Root $OwnerMarkerName
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
            Fail "Refusing a pre-existing unowned managed directory."
        }
        Assert-NoReparsePoint $marker
    }
}

function Assert-LauncherOwnedOrAbsent {
    if (Test-Path -LiteralPath $Launcher) {
        if (-not (Test-Path -LiteralPath $LauncherMarker -PathType Leaf)) {
            Fail "Refusing to overwrite or remove an unowned launcher."
        }
        Assert-NoReparsePoint $Launcher
        Assert-NoReparsePoint $LauncherMarker
    }
}

Assert-OwnedDirectoryOrAbsent $InstallRoot
Assert-OwnedDirectoryOrAbsent $ConfigRoot
Assert-LauncherOwnedOrAbsent
if ($InstallScoutSkill -or $Action -eq "uninstall") {
    Assert-OwnedDirectoryOrAbsent $ScoutSkillRoot
    Assert-OwnedDirectoryOrAbsent $CopilotSkillRoot
}

function Resolve-Python {
    $configured = [Environment]::GetEnvironmentVariable("SCOUT_USAGE_PYTHON")
    $candidate = $null
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        $candidate = [IO.Path]::GetFullPath($configured)
    } else {
        foreach ($name in @("python.exe", "python3.exe")) {
            $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
            if ($null -ne $command) {
                $candidate = [IO.Path]::GetFullPath($command.Source)
                break
            }
        }
    }
    if ($null -eq $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        Fail "Python 3.10 or newer with sqlite3 is required."
    }
    try {
        $probe = & $candidate -c "import sqlite3,sys; print(sys.executable); print(sqlite3.sqlite_version); raise SystemExit(0 if sys.version_info >= (3,10) else 1)" 2>$null
        if ($LASTEXITCODE -ne 0 -or $probe.Count -lt 2) { Fail "Python 3.10 or newer with sqlite3 is required." }
        $resolved = [IO.Path]::GetFullPath([string]$probe[0])
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { Fail "Python executable could not be resolved safely." }
        return $resolved
    } catch {
        Fail "Python 3.10 or newer with sqlite3 is required."
    }
}

function Write-Utf8([string]$PathValue, [string]$Content) {
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($PathValue, $Content, $encoding)
}

function New-OwnedDirectory([string]$Root) {
    [IO.Directory]::CreateDirectory($Root) | Out-Null
    Write-Utf8 (Join-Path $Root $OwnerMarkerName) "owned by Scout Usage Tracker`r`n"
}

function Remove-ProgramTree([string]$PathValue) {
    if (Test-Path -LiteralPath $PathValue) {
        Assert-NoReparsePoint $PathValue
        Remove-Item -LiteralPath $PathValue -Recurse -Force
    }
}

function Install-Skill([string]$Target, [string]$Label) {
    Assert-OwnedDirectoryOrAbsent $Target
    Remove-ProgramTree $Target
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Target)) | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "skills\cost") -Destination $Target -Recurse
    Write-Utf8 (Join-Path $Target $OwnerMarkerName) "owned by Scout Usage Tracker`r`n"
    Write-Output "Installed $Label /cost skill."
}

function Install-Program {
    $Python = Resolve-Python
    New-OwnedDirectory $InstallRoot
    New-OwnedDirectory $ConfigRoot
    [IO.Directory]::CreateDirectory($BinRoot) | Out-Null

    foreach ($name in @("src", "templates", "skills")) {
        Remove-ProgramTree (Join-Path $InstallRoot $name)
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $name) -Destination (Join-Path $InstallRoot $name) -Recurse
    }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "config.example.json") -Destination (Join-Path $InstallRoot "config.example.json") -Force
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot "config.example.json") -Destination $ConfigPath
    }

    $batchPython = $Python.Replace("%", "%%")
    $batchSource = (Join-Path $InstallRoot "src").Replace("%", "%%")
    $batchConfig = $ConfigPath.Replace("%", "%%")
    $launcherText = "@echo off`r`nsetlocal`r`nset `"PYTHONPATH=$batchSource`"`r`n`"$batchPython`" -m scout_usage_tracker --config `"$batchConfig`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    Write-Utf8 $Launcher $launcherText
    Write-Utf8 $LauncherMarker "owned by Scout Usage Tracker`r`n"

    if ($InstallScoutSkill) {
        Install-Skill $ScoutSkillRoot "Scout"
        Install-Skill $CopilotSkillRoot "Copilot-compatible"
    }
    Write-Output "Installed Scout Usage Tracker."
}

function Remove-FileIfPresent([string]$PathValue) {
    if (Test-Path -LiteralPath $PathValue) {
        Assert-NoReparsePoint $PathValue
        $item = Get-Item -LiteralPath $PathValue -Force
        if ($item.PSIsContainer) { Fail "Refusing to remove a directory through the file manifest." }
        Remove-Item -LiteralPath $PathValue -Force
    }
}

function Remove-OwnedSkill([string]$Target) {
    $marker = Join-Path $Target $OwnerMarkerName
    if (Test-Path -LiteralPath $marker -PathType Leaf) { Remove-ProgramTree $Target }
}

function Uninstall-Program {
    if (Test-Path -LiteralPath $LauncherMarker -PathType Leaf) {
        Remove-FileIfPresent $Launcher
        Remove-FileIfPresent $LauncherMarker
    }
    Remove-OwnedSkill $ScoutSkillRoot
    Remove-OwnedSkill $CopilotSkillRoot
    if (Test-Path -LiteralPath (Join-Path $InstallRoot $OwnerMarkerName) -PathType Leaf) {
        foreach ($name in @("src", "templates", "skills")) { Remove-ProgramTree (Join-Path $InstallRoot $name) }
        Remove-FileIfPresent (Join-Path $InstallRoot "config.example.json")
    }
    if ($PurgeData) {
        foreach ($name in @("history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm", "dashboard.html", "hmac-secret", "refresh.log", "billing-snapshot.json", $OwnerMarkerName)) {
            Remove-FileIfPresent (Join-Path $InstallRoot $name)
        }
        Remove-FileIfPresent $ConfigPath
        Remove-FileIfPresent (Join-Path $ConfigRoot $OwnerMarkerName)
        foreach ($root in @($InstallRoot, $ConfigRoot)) {
            if (Test-Path -LiteralPath $root) {
                try { [IO.Directory]::Delete($root, $false) } catch [IO.IOException] { }
            }
        }
        Write-Output "Uninstalled program and removed enumerated tracker-owned data; unrelated files were preserved."
    } else {
        Write-Output "Uninstalled program; preserved config, history, secret, dashboard, logs, and ownership markers."
    }
}

if ($PurgeData -and $Action -ne "uninstall") { Fail "-PurgeData is valid only with uninstall." 2 }
if ($InstallScoutSkill -and $Action -notin @("install", "update")) { Fail "-InstallScoutSkill is valid only with install or update." 2 }

switch ($Action) {
    "install" { Install-Program }
    "update" { Install-Program }
    "uninstall" { Uninstall-Program }
    "status" {
        if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) { Fail "Scout Usage Tracker is not installed." }
        & $Launcher status
        exit $LASTEXITCODE
    }
    "open" {
        if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) { Fail "Scout Usage Tracker is not installed." }
        & $Launcher open
        exit $LASTEXITCODE
    }
}
