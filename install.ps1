[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update", "status", "open", "uninstall")]
    [string]$Action = "install",
    [switch]$InstallScoutSkill,
    [string]$Currency,
    [string]$UsdRate,
    [switch]$UsdOnly,
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

if ($PurgeData -and $Action -ne "uninstall") { Fail "-PurgeData is valid only with uninstall." 2 }
if ($InstallScoutSkill -and $Action -notin @("install", "update")) { Fail "-InstallScoutSkill is valid only with install or update." 2 }
if ($UsdOnly -and (-not [string]::IsNullOrWhiteSpace($Currency) -or -not [string]::IsNullOrWhiteSpace($UsdRate))) { Fail "-UsdOnly cannot be combined with -Currency or -UsdRate." 2 }
if (([string]::IsNullOrWhiteSpace($Currency)) -xor ([string]::IsNullOrWhiteSpace($UsdRate))) { Fail "-Currency and -UsdRate are required together." 2 }
if (($UsdOnly -or $Currency) -and $Action -notin @("install", "update")) { Fail "Currency options are valid only with install or update." 2 }

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
$ScoutSkillRoot = $null
$CopilotSkillRoot = $null
if ($Action -eq "uninstall" -or $InstallScoutSkill) {
    $ScoutSkillRoot = [IO.Path]::GetFullPath((Env-OrDefault "SCOUT_COST_SKILL_DIR" (Join-Path $ProfileRoot ".scout\m-skills\cost")))
    $CopilotSkillRoot = [IO.Path]::GetFullPath((Env-OrDefault "COPILOT_COST_SKILL_DIR" (Join-Path $ProfileRoot ".copilot\m-skills\cost")))
}
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

function Test-SkillPathContainsReparsePoint([string]$PathValue) {
    Assert-BelowProfile $PathValue
    $relative = $PathValue.Substring($ProfileRoot.Length).TrimStart('\')
    $current = $ProfileRoot
    foreach ($part in $relative.Split([char]'\', [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        try {
            if (-not (Test-Path -LiteralPath $current -ErrorAction Stop)) { continue }
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        } catch {
            return $true
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $true }
    }
    return $false
}

function Test-PathsOverlapLexically([string]$Left, [string]$Right) {
    $a = $Left.TrimEnd('\')
    $b = $Right.TrimEnd('\')
    return ($a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
        $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $b.StartsWith($a + '\', [StringComparison]::OrdinalIgnoreCase))
}

function Assert-SkillRootsDoNotOverlapCoreDeletionPaths {
    $coreDeletionPaths = @(
        $Launcher, $LauncherMarker,
        (Join-Path $InstallRoot "src"), (Join-Path $InstallRoot "templates"),
        (Join-Path $InstallRoot "skills"), (Join-Path $InstallRoot "config.example.json")
    )
    if ($PurgeData) {
        $coreDeletionPaths += @($InstallRoot, $ConfigRoot, $ConfigPath)
    }
    foreach ($skillRoot in @($ScoutSkillRoot, $CopilotSkillRoot)) {
        Assert-BelowProfile $skillRoot
        foreach ($corePath in $coreDeletionPaths) {
            if (Test-PathsOverlapLexically $skillRoot $corePath) {
                Fail "Unsafe managed paths: skill roots must not overlap core deletion paths."
            }
        }
    }
}

function Assert-ManagedRoots([string[]]$Roots) {
    foreach ($pathValue in $Roots) {
        $managed = $pathValue.TrimEnd('\')
        $project = $ProjectRoot.TrimEnd('\')
        if ($managed.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
            $managed.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase) -or
            $project.StartsWith($managed + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Fail "Unsafe managed path: managed paths must not overlap the source package."
        }
        Assert-NoReparsePoint $pathValue
    }
    for ($left = 0; $left -lt $Roots.Count; $left++) {
        for ($right = $left + 1; $right -lt $Roots.Count; $right++) {
            $a = $Roots[$left].TrimEnd('\')
            $b = $Roots[$right].TrimEnd('\')
            if ($a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or
                $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase) -or
                $b.StartsWith($a + '\', [StringComparison]::OrdinalIgnoreCase)) {
                Fail "Unsafe managed paths: managed roots must be pairwise disjoint."
            }
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

function Assert-DeletionTreeSafe([string]$PathValue) {
    if (-not (Test-Path -LiteralPath $PathValue)) { return }
    Assert-NoReparsePoint $PathValue
    $pending = New-Object System.Collections.Generic.Stack[string]
    $pending.Push($PathValue)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $item = Get-Item -LiteralPath $current -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail "Refusing managed path containing a reparse point."
        }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
                $pending.Push($child.FullName)
            }
        }
    }
}

function Assert-DeletionFileSafe([string]$PathValue) {
    if (-not (Test-Path -LiteralPath $PathValue)) { return }
    Assert-DeletionTreeSafe $PathValue
    if ((Get-Item -LiteralPath $PathValue -Force).PSIsContainer) {
        Fail "Refusing to remove a directory through the file manifest."
    }
}

function Get-OwnedSkillDeletionRoots {
    $owned = New-Object System.Collections.Generic.List[string]
    foreach ($root in @($ScoutSkillRoot, $CopilotSkillRoot)) {
        Assert-BelowProfile $root
        if (Test-SkillPathContainsReparsePoint $root) { continue }
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $item = Get-Item -LiteralPath $root -Force
        if (-not $item.PSIsContainer) { continue }
        $marker = Join-Path $root $OwnerMarkerName
        if (Test-Path -LiteralPath $marker -PathType Leaf) { $owned.Add($root) | Out-Null }
    }
    return $owned.ToArray()
}

function Assert-UninstallPreflight([string[]]$OwnedSkillRoots) {
    $uninstallRoots = @($InstallRoot, $BinRoot, $ConfigRoot) + $OwnedSkillRoots
    Assert-ManagedRoots $uninstallRoots
    Assert-OwnedDirectoryOrAbsent $InstallRoot
    Assert-OwnedDirectoryOrAbsent $ConfigRoot
    Assert-LauncherOwnedOrAbsent
    foreach ($root in $OwnedSkillRoots) { Assert-OwnedDirectoryOrAbsent $root }

    if (Test-Path -LiteralPath $LauncherMarker -PathType Leaf) {
        Assert-DeletionFileSafe $Launcher
        Assert-DeletionFileSafe $LauncherMarker
    }
    if (Test-Path -LiteralPath (Join-Path $InstallRoot $OwnerMarkerName) -PathType Leaf) {
        foreach ($name in @("src", "templates", "skills")) {
            Assert-DeletionTreeSafe (Join-Path $InstallRoot $name)
        }
        Assert-DeletionFileSafe (Join-Path $InstallRoot "config.example.json")
    }
    foreach ($root in $OwnedSkillRoots) { Assert-DeletionTreeSafe $root }
    if ($PurgeData) {
        foreach ($name in @("history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm", "dashboard.html", "hmac-secret", "refresh.log", "billing-snapshot.json", $OwnerMarkerName)) {
            Assert-DeletionFileSafe (Join-Path $InstallRoot $name)
        }
        Assert-DeletionFileSafe $ConfigPath
        Assert-DeletionFileSafe (Join-Path $ConfigRoot $OwnerMarkerName)
    }
}

if ($Action -in @("install", "update")) {
    $InstallRoots = @($InstallRoot, $BinRoot, $ConfigRoot)
    if ($InstallScoutSkill) { $InstallRoots += @($ScoutSkillRoot, $CopilotSkillRoot) }
    Assert-ManagedRoots $InstallRoots
    Assert-OwnedDirectoryOrAbsent $InstallRoot
    Assert-OwnedDirectoryOrAbsent $ConfigRoot
    Assert-LauncherOwnedOrAbsent
    if ($InstallScoutSkill) {
        Assert-OwnedDirectoryOrAbsent $ScoutSkillRoot
        Assert-OwnedDirectoryOrAbsent $CopilotSkillRoot
    }
} elseif ($Action -in @("status", "open")) {
    Assert-ManagedRoots @($BinRoot)
    Assert-LauncherOwnedOrAbsent
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
    $ConfigWasPresent = Test-Path -LiteralPath $ConfigPath
    $SelectedCurrency = $Currency
    $SelectedRate = $UsdRate
    $SelectedUsdOnly = $UsdOnly
    if (-not $ConfigWasPresent) {
        if (-not $SelectedUsdOnly -and [string]::IsNullOrWhiteSpace($SelectedCurrency) -and -not [Console]::IsInputRedirected) {
            $SelectedCurrency = Read-Host "Optional secondary currency code (press Enter for USD only, for example NOK or EUR)"
            if ([string]::IsNullOrWhiteSpace($SelectedCurrency)) { $SelectedUsdOnly = $true }
            else { $SelectedRate = Read-Host "Manual rate (1 USD equals how many $SelectedCurrency?)" }
        }
        if (-not $SelectedUsdOnly -and -not [string]::IsNullOrWhiteSpace($SelectedCurrency)) {
            $SelectedCurrency = $SelectedCurrency.ToUpperInvariant()
            if ($SelectedCurrency -notmatch '^[A-Z]{3}$' -or $SelectedCurrency -eq "USD") {
                Fail "Currency code must be three letters other than USD."
            }
            [decimal]$ParsedRate = 0
            $ValidRate = [decimal]::TryParse($SelectedRate, [Globalization.NumberStyles]::Number, [Globalization.CultureInfo]::InvariantCulture, [ref]$ParsedRate)
            if (-not $ValidRate -or $ParsedRate -le 0) { Fail "Currency rate must be a positive finite number." }
        }
    }
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

    if (-not $ConfigWasPresent) {
        $PreviousPythonPath = $env:PYTHONPATH
        try {
            $env:PYTHONPATH = Join-Path $InstallRoot "src"
            if ($SelectedUsdOnly -or [string]::IsNullOrWhiteSpace($SelectedCurrency)) {
                & $Python -m scout_usage_tracker --config $ConfigPath configure-currency --usd-only | Out-Null
            } else {
                & $Python -m scout_usage_tracker --config $ConfigPath configure-currency --code $SelectedCurrency --usd-rate $SelectedRate | Out-Null
            }
            if ($LASTEXITCODE -ne 0) { Fail "Currency configuration failed." }
        } finally {
            $env:PYTHONPATH = $PreviousPythonPath
        }
    }

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

function Uninstall-Program {
    Assert-SkillRootsDoNotOverlapCoreDeletionPaths
    $ownedSkillRoots = @(Get-OwnedSkillDeletionRoots)
    Assert-UninstallPreflight $ownedSkillRoots
    if (Test-Path -LiteralPath $LauncherMarker -PathType Leaf) {
        Remove-FileIfPresent $Launcher
        Remove-FileIfPresent $LauncherMarker
    }
    foreach ($root in $ownedSkillRoots) { Remove-ProgramTree $root }
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
