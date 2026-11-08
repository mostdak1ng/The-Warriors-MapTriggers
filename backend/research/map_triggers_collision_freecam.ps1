# Primary launcher implementation for MapTriggers_Collision_Freecam.cmd.
[CmdletBinding()]
param(
    [Alias("Map")]
    [string]$VolumeSet = "",
    [switch]$KeepVolumeSet,
    [switch]$IncludeAuxiliary,
    [switch]$NoAutoDetect,
    [switch]$CollisionToggle,
    [switch]$ExtendedCollisionView,
    [double]$Duration = 0,
    [double]$Speed = 8.0,
    [double]$MouseSensitivity = 0.15,
    [ValidateRange(0.0, 0.999999)]
    [double]$WorldTimeScale = 0.0001,
    [switch]$StartWorldPaused,
    [double]$CollisionRadius = 35.0,
    [ValidateRange(1, 44)]
    [int]$CollisionLimit = 44,
    [double]$CollisionUpdateHz = 8.0,
    [string]$HumanWatchPrefix = "",
    [double]$HumanWatchHz = 10.0,
    [switch]$TraceLoopTiming,
    [switch]$PostBeginCameraOverride,
    [int]$Port = 28011,
    [int]$CameraStatePort = 28012,
    [ValidateRange(0, 65535)]
    [int]$GangStatePort = 0,
    [ValidateRange(0.5, 20.0)]
    [double]$GangTelemetryHz = 5.0,
    [ValidateRange(0, 4000)]
    [int]$OverlayStatusXOffset = 0,
    [string]$CatalogFile = "",
    [string]$Iso = "",
    [string]$SessionDirectory = "",
    [string]$PythonExecutable = "",
    [string]$PythonwExecutable = "",
    [switch]$Controller,
    [switch]$CaptureKeyboardPad,
    [switch]$ReturnToFollowCamera,
    [switch]$NonInteractive,
    [ValidateSet("freecam", "maptriggers")]
    [string]$Profile = "maptriggers"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
trap { Write-Host ("ERROR: " + $_.Exception.Message); exit 1 }
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
if ($PostBeginCameraOverride) {
    throw "This test branch uses the native transform setter; the old diagnostic cannot be combined."
}
if ($StartWorldPaused -and -not $CollisionToggle) {
    throw "-StartWorldPaused requires the combined collision launcher."
}
if ($ExtendedCollisionView -and -not $CollisionToggle) {
    throw "-ExtendedCollisionView requires the combined collision launcher."
}

$workspaceDir = Split-Path -Parent $PSScriptRoot
$catalogPath = Join-Path $PSScriptRoot "lua_volume_catalog.json"
if ($CatalogFile) { $catalogPath = [IO.Path]::GetFullPath($CatalogFile) }
$catalogDirectory = Split-Path -Parent $catalogPath
if (-not $SessionDirectory) { $SessionDirectory = $PSScriptRoot }
$SessionDirectory = [IO.Path]::GetFullPath($SessionDirectory)
$null = [IO.Directory]::CreateDirectory($SessionDirectory)
$stopPath = Join-Path $SessionDirectory "stop.flag"
$controlStatePath = Join-Path $SessionDirectory "controls.json"
$mapRequestPath = Join-Path $SessionDirectory "map_request.json"
$mapResponsePath = Join-Path $SessionDirectory "map_response.txt"
$overlayReadyPath = Join-Path $SessionDirectory "overlay_ready.flag"
$overlayErrorPath = Join-Path $SessionDirectory "overlay.stderr.txt"
$ownerProcess = $null
if ($env:TW_MAPTRIGGERS_PARENT_PID) {
    try {
        $ownerProcess = [Diagnostics.Process]::GetProcessById([int]$env:TW_MAPTRIGGERS_PARENT_PID)
        $null = $ownerProcess.Handle
    }
    catch { Set-Content -LiteralPath $stopPath -Value "launcher exited" }
}
$statePath = Join-Path $PSScriptRoot (
    "persistent_freecam_state_{0}.json" -f $PID
)
$readyPath = Join-Path $PSScriptRoot (
    "persistent_freecam_ready_{0}.flag" -f $PID
)
$python = if ($PythonExecutable) { $PythonExecutable } else { (Get-Command python.exe -ErrorAction Stop).Source }
$pythonw = if ($PythonwExecutable) { $PythonwExecutable } else { (Get-Command pythonw.exe -ErrorAction Stop).Source }
$statePath = Join-Path $SessionDirectory "resume.json"
$readyPath = Join-Path $SessionDirectory "ready.flag"
$transitionExitCode = 75
$rendererInstalled = $false
$rendererCleanupAttempted = $false
$rendererAbandoned = $false
$overlayProcess = $null
$codePointer = $null
$dataPointer = $null
$kindVisibilityPointer = $null
$flagRenderStatePointer = $null
$volumeSelectionPointer = $null
$volumeCount = 0
$volumeOrder = ""
$customDataset = $null
$displayName = $null
$worldId = $null
$sessionIndex = 0
$startTime = Get-Date
$worldTimePrearmed = $false
$worldTimeOriginalBits = $null
$uiPanelsStopPath = Join-Path $PSScriptRoot "game_ui_panels_stop_$PID.flag"
$uiPanelsStatePath = Join-Path $PSScriptRoot "game_ui_panels_state_$PID.json"
$visibilityStatePath = Join-Path $PSScriptRoot "game_ui_panels_state_$PID.visibility.json"
$uiPanelsStatePath = Join-Path $SessionDirectory "panels.json"
$uiPanelsStopPath = $stopPath
$visibilityStatePath = Join-Path $SessionDirectory "panels.visibility.json"

function Convert-ToInvariantText([double]$Value) {
    return $Value.ToString(
        [System.Globalization.CultureInfo]::InvariantCulture
    )
}

function Convert-ToProcessArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Restore-PrearmedWorldTime {
    if (-not $script:worldTimePrearmed) {
        return $true
    }
    $savedErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $restoreOutput = @(
            & $python "-B" ".\research\world_time_control.py" `
                "--port" $Port `
                "--restore-bits" $script:worldTimeOriginalBits 2>&1
        )
        $restoreExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorAction
    }
    $restoreOutput | ForEach-Object { Write-Host $_ }
    if ($restoreExitCode -eq 0) {
        $script:worldTimePrearmed = $false
        $script:worldTimeOriginalBits = $null
        return $true
    }
    return $false
}

function Select-VolumeEntry(
    $Catalog,
    [string]$Requested,
    [bool]$ShowAuxiliary
) {
    $available = @(
        $Catalog.entries | Where-Object {
            $ShowAuxiliary -or $_.category -ne "auxiliary"
        }
    )
    if ($available.Count -eq 0) {
        throw "The volume catalog contains no selectable maps."
    }

    if ([string]::IsNullOrWhiteSpace($Requested) -and $NonInteractive) {
        $requestId = [guid]::NewGuid().ToString("N")
        Remove-Item -LiteralPath $mapResponsePath -Force -ErrorAction SilentlyContinue
        $request = @{ request_id = $requestId; choices = @($available | Select-Object id, display_name) }
        $requestTemp = $mapRequestPath + ".tmp"
        $request | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $requestTemp -Encoding UTF8
        Move-Item -LiteralPath $requestTemp -Destination $mapRequestPath -Force
        Write-Host "MAP_SELECTION_REQUIRED: choose the loaded map in the launcher."
        try {
            while ($true) {
                if ($null -ne $ownerProcess -and $ownerProcess.HasExited) { Set-Content -LiteralPath $stopPath -Value "launcher exited" }
                if (Test-Path -LiteralPath $stopPath) { return $null }
                if (Test-Path -LiteralPath $mapResponsePath) {
                    $response = Get-Content -Raw -LiteralPath $mapResponsePath | ConvertFrom-Json
                    Remove-Item -LiteralPath $mapResponsePath -Force
                    if ($response.request_id -ceq $requestId) {
                        $Requested = $response.map_id
                        break
                    }
                }
                Start-Sleep -Milliseconds 100
            }
        }
        finally {
            Remove-Item -LiteralPath $mapRequestPath, $mapResponsePath -Force -ErrorAction SilentlyContinue
        }
    }
    if ([string]::IsNullOrWhiteSpace($Requested)) {
        Write-Host ""
        Write-Host "MAP VOLUME SETS" -ForegroundColor Cyan
        $lastCategory = ""
        for ($index = 0; $index -lt $available.Count; $index++) {
            $item = $available[$index]
            if ($item.category -ne $lastCategory) {
                $lastCategory = $item.category
                Write-Host ""
                Write-Host (
                    $lastCategory.ToUpperInvariant()
                ) -ForegroundColor DarkCyan
            }
            Write-Host (
                "  {0,2}. {1} [{2} boxes]" -f
                ($index + 1), $item.display_name, $item.volume_count
            )
        }
        Write-Host ""
        $Requested = Read-Host "Choose a number"
    }

    $choice = 0
    if ([int]::TryParse($Requested, [ref]$choice)) {
        if ($choice -ge 1 -and $choice -le $available.Count) {
            return $available[$choice - 1]
        }
    }

    $matches = @(
        $Catalog.entries | Where-Object {
            $_.id -ieq $Requested -or
            $_.level_id -ieq $Requested -or
            $_.entry.ToString() -eq $Requested -or
            $_.mission_name -ieq $Requested
        }
    )
    if ($matches.Count -eq 1) {
        return $matches[0]
    }
    if ($matches.Count -gt 1) {
        throw "The requested map name is ambiguous: $Requested"
    }
    throw "Unknown map or selection: $Requested"
}

function Get-DetectedVolumeSet {
    $scanPlans = @(
        [pscustomobject]@{
            Label = "fast active-script window"
            Start = "0x00B00000"
            End = "0x00E00000"
        },
        [pscustomobject]@{
            Label = "complete EE heap fallback"
            Start = "0x00700000"
            End = "0x02000000"
        }
    )
    for ($attempt = 0; $attempt -lt $scanPlans.Count; $attempt++) {
        $scanPlan = $scanPlans[$attempt]
        Write-Host (
            "Detecting the loaded map from stable EE memory " +
            "({0}, pass {1}/{2})..." -f
            $scanPlan.Label, ($attempt + 1), $scanPlans.Count
        ) -ForegroundColor Cyan
        # Windows PowerShell 5.1 turns native stderr into ErrorRecords. Keep
        # the complete diagnostic and exit code instead of terminating at the
        # first traceback line under the launcher's ErrorActionPreference=Stop.
        $savedErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $detectionOutput = @(
                & $python "-B" ".\research\detect_current_map.py" `
                    "--catalog" $catalogPath `
                    "--iso" $Iso `
                    "--port" $Port `
                    "--start" $scanPlan.Start `
                    "--end" $scanPlan.End `
                    "--top" 3 `
                    "--json" 2>&1
            )
            $detectionExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorAction
        }
        if ($detectionExitCode -eq 0) {
            try {
                $detection = (
                    $detectionOutput -join "`n"
                ) | ConvertFrom-Json
                if ($null -ne $detection.detected) {
                    Write-Host (
                        "Auto-detected: " +
                        $detection.detected.display_name +
                        " [" + $detection.detected.all_name_hits + "/" +
                        $detection.detected.all_name_total + " names]"
                    ) -ForegroundColor Green
                    return [string]$detection.detected.id
                }
                $bestCandidate = @($detection.results)[0]
                if ($null -ne $bestCandidate) {
                    Write-Host (
                        (
                            "No confident match in {0}: {1}. Best: {2} " +
                            "[{3}/{4} unique names]."
                        ) -f
                        $scanPlan.Label,
                        $detection.confidence_reason,
                        $bestCandidate.display_name,
                        $bestCandidate.distinctive_hits,
                        $bestCandidate.distinctive_total
                    ) -ForegroundColor DarkYellow
                }
            }
            catch {
                Write-Warning "Could not parse map detection: $_"
            }
        }
        else {
            Write-Warning (
                "Automatic detection failed: " +
                ($detectionOutput -join " ")
            )
        }
        if ($attempt -lt ($scanPlans.Count - 1)) {
            Start-Sleep -Seconds 1
        }
    }
    return ""
}

function Stop-VolumeOverlay {
    if ($null -eq $script:overlayProcess) {
        return
    }
    try {
        $script:overlayProcess.Refresh()
        if (-not $script:overlayProcess.HasExited) {
            Stop-Process -Id $script:overlayProcess.Id
            Wait-Process `
                -Id $script:overlayProcess.Id `
                -ErrorAction SilentlyContinue
        }
    }
    catch {
        Write-Warning "Could not close the transparent panel: $_"
    }
    finally {
        $script:overlayProcess = $null
    }
}

function Invoke-RendererDisarm {
    if (-not $script:rendererInstalled) {
        return $true
    }
    Write-Host (
        "Disarming renderer hooks without touching the EE heap..."
    ) -ForegroundColor Cyan
    $savedErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($CollisionToggle) {
            $disarmOutput = @(
                & $python "-B" ".\research\simultaneous_debug_renderer.py" `
                    $script:customDataset `
                    "--port" $Port `
                    "--cleanup-code-pointer" $script:codePointer `
                    "--disarm-only" 2>&1
            )
        }
        else {
            $disarmOutput = @(
                & $python "-B" ".\research\map_volumes_probe.py" `
                    $script:customDataset `
                    "--port" $Port `
                    "--cleanup-code-pointer" $script:codePointer `
                    "--disarm-only" 2>&1
            )
        }
        $disarmExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorAction
    }
    $disarmOutput | ForEach-Object { Write-Host $_ }
    return $disarmExitCode -eq 0
}

function Invoke-RendererCleanup([bool]$AllowReclaimed) {
    do {
        $script:cleanupRetrySafe = $false
        $cleaned = Invoke-RendererCleanupOnce $AllowReclaimed
    } while ($script:cleanupRetrySafe)
    return $cleaned
}

function Invoke-RendererCleanupOnce([bool]$AllowReclaimed) {
    if (-not $script:rendererInstalled) {
        return $true
    }
    $env:TW_MAPTRIGGERS_CANCEL_FILE = Join-Path $SessionDirectory "exit_now.flag"
    $cleanupWaitPath = Join-Path $SessionDirectory "cleanup_wait.flag"
    Set-Content -LiteralPath $cleanupWaitPath -Value "retiring"
    $cleanupArguments = @(
        $script:customDataset,
        "--port", $Port.ToString(),
        "--cleanup-code-pointer", $script:codePointer,
        "--cleanup-data-pointer", $script:dataPointer
    )
    if ($AllowReclaimed) {
        $cleanupArguments += "--cleanup-if-owned"
    }
    $savedErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($CollisionToggle) {
            $cleanupOutput = @(
                & $python "-B" `
                    ".\research\simultaneous_debug_renderer.py" `
                    @cleanupArguments 2>&1 | ForEach-Object { Write-Host $_; $_ }
            )
        }
        else {
            $cleanupOutput = @(
                & $python "-B" `
                    ".\research\map_volumes_probe.py" `
                    @cleanupArguments 2>&1 | ForEach-Object { Write-Host $_; $_ }
            )
        }
        $cleanupExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorAction
    }
    Remove-Item -LiteralPath $cleanupWaitPath -Force -ErrorAction SilentlyContinue
    if ($cleanupExitCode -eq 77) {
        Write-Host "Close the game's pause menu or resume PCSX2 to finish exiting"
        $script:cleanupRetrySafe = $true
        return $false
    }
    if ($cleanupExitCode -eq 79 -or $cleanupExitCode -eq 80) {
        $script:rendererAbandoned = $true
        if ($cleanupExitCode -eq 79) {
            Write-Warning "Hooks disarmed; heap blocks or allocator scratch retained. Restart the game before another session."
        }
        return $true
    }
    $script:rendererCleanupAttempted = $cleanupExitCode -eq 78
    if ($cleanupExitCode -ne 0 -and $cleanupExitCode -ne 78) {
        $script:rendererAbandoned = $true
        Write-Warning "Renderer cleanup stopped before release could be confirmed. Restart the game before another session."
    }
    $cleaned = $cleanupExitCode -eq 0
    if ($cleaned) {
        $script:rendererCleanupAttempted = $false
        $script:rendererInstalled = $false
        $script:codePointer = $null
        $script:dataPointer = $null
        $script:kindVisibilityPointer = $null
        $script:flagRenderStatePointer = $null
        $script:volumeSelectionPointer = $null
        $script:volumeCount = 0
        $script:volumeOrder = ""
    }
    return $cleaned
}

function Wait-ForStableWorld(
    [double]$Timeout = 0,
    [bool]$RequireFollowCamera = $false,
    [bool]$AllowUserStop = $false
) {
    $arguments = @(
        ".\research\world_lifecycle.py",
        "--port", $Port.ToString(),
        "--stable-seconds", "1",
        "--require-heap-callsite"
    )
    if ($Timeout -gt 0) {
        $arguments += @(
            "--timeout",
            (Convert-ToInvariantText $Timeout)
        )
    }
    if ($RequireFollowCamera) {
        $arguments += "--require-follow-camera"
    }
    if ($AllowUserStop) {
        $arguments += @(
            "--stop-file", $uiPanelsStopPath,
            "--ui-panels-state", $uiPanelsStatePath
        )
    }
    $savedErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Stream notices and O toggle status while the world is unavailable.
        & $python "-B" @arguments 2>&1 | ForEach-Object { Write-Host $_ }
        $waitExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorAction
    }
    if ($waitExitCode -eq 79) { $script:rendererAbandoned = $true }
    return $waitExitCode -eq 0
}

Push-Location $workspaceDir
$env:TW_MAPTRIGGERS_CANCEL_FILE = $stopPath
try {
    if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
        throw "Missing volume catalog: $catalogPath"
    }
    $catalog = Get-Content -Raw -LiteralPath $catalogPath | ConvertFrom-Json
    $requestedSet = $VolumeSet
    $initialRequestedSet = $VolumeSet

    # The menu (level100) has no player. Keep this independent of the
    # gameplay renderer so O works during the initial wait and transitions.
    if ((Test-Path -LiteralPath $uiPanelsStopPath) -and -not $NonInteractive) {
        Remove-Item -LiteralPath $uiPanelsStopPath -Force
    }
    Set-Content -LiteralPath $uiPanelsStatePath -Value '{"hidden":false,"error":""}' -Encoding ASCII
    foreach ($statusFile in @($uiPanelsStatePath, $visibilityStatePath)) {
        $pendingStatusFile = [System.IO.Path]::ChangeExtension($statusFile, "tmp")
        if (Test-Path -LiteralPath $pendingStatusFile) {
            Remove-Item -LiteralPath $pendingStatusFile -Force
        }
    }
    Set-Content -LiteralPath $visibilityStatePath -Value '{"light":false,"no_fog":false,"range_factor":1,"no_pvs":false,"active":false,"error":""}' -Encoding ASCII
    Write-Host "  O: hide game 2D panels (OFF). Available in menus too. F10: exit." -ForegroundColor Cyan

    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { break }
        $rendererInstalled = $false
        $codePointer = $null
        $dataPointer = $null
        $kindVisibilityPointer = $null
        $flagRenderStatePointer = $null
        $volumeSelectionPointer = $null
        $volumeCount = 0
        $volumeOrder = ""

        Write-Host ""
        Write-Host (
            "Waiting for a complete stable gameplay world..."
        ) -ForegroundColor Cyan
        if (
            -not (
                Wait-ForStableWorld `
                    -RequireFollowCamera ($sessionIndex -gt 0) `
                    -AllowUserStop $true
            )
        ) {
            if (Test-Path -LiteralPath $uiPanelsStopPath) {
                break
            }
            throw "Gameplay world wait failed."
        }

        if (
            ($sessionIndex -gt 0 -and -not $KeepVolumeSet) -or
            (
                [string]::IsNullOrWhiteSpace($requestedSet) -and
                -not $NoAutoDetect
            )
        ) {
            $requestedSet = Get-DetectedVolumeSet
            if ([string]::IsNullOrWhiteSpace($requestedSet)) {
                Write-Warning (
                    "Automatic detection remained inconclusive; " +
                    "showing the manual menu."
                )
            }
        }

        if (
            -not [string]::IsNullOrWhiteSpace($requestedSet) -and
            (Test-Path -LiteralPath $requestedSet -PathType Leaf)
        ) {
            $customDataset = (Resolve-Path -LiteralPath $requestedSet).Path
            $displayName = [IO.Path]::GetFileNameWithoutExtension(
                $customDataset
            )
            $worldId = "custom:" + $customDataset
        }
        else {
            $selected = Select-VolumeEntry `
                $catalog `
                $requestedSet `
                ([bool]$IncludeAuxiliary)
            if (Test-Path -LiteralPath $stopPath) { break }
            $customDataset = Join-Path $catalogDirectory $selected.dataset
            $displayName = $selected.display_name
            $worldId = $selected.id
        }
        if (-not (Test-Path -LiteralPath $customDataset -PathType Leaf)) {
            throw "Missing volume dataset: $customDataset"
        }

        Write-Host ""
        Write-Host "Selected: $displayName" -ForegroundColor Green
        $savedErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            if ($CollisionToggle) {
                Write-Host (
                    "Installing simultaneous map triggers and terrain..."
                ) -ForegroundColor Cyan
                $installWorldTimeArguments = @()
                $extendedRendererArguments = @("--object-obb-support")
                if ($ExtendedCollisionView) {
                    $extendedRendererArguments += "--collision-xray"
                }
                if ($StartWorldPaused -and $sessionIndex -eq 0) {
                    Write-Host (
                        "Resume PCSX2 now. Allocation runs at normal speed; " +
                        "the world switches to the requested pause as soon " +
                        "as the renderer hook verifies."
                    ) -ForegroundColor Yellow
                    $installWorldTimeArguments = @(
                        "--start-world-time-scale",
                        (Convert-ToInvariantText $WorldTimeScale)
                    )
                }
                $installOutput = @(
                    & $python "-B" ".\research\simultaneous_debug_renderer.py" `
                        $customDataset `
                        "--port" $Port `
                        "--collision-radius" (
                            Convert-ToInvariantText $CollisionRadius
                        ) `
                        "--collision-limit" $CollisionLimit `
                        "--install-only" `
                        @extendedRendererArguments `
                        @installWorldTimeArguments 2>&1 | ForEach-Object { Write-Host $_; $_ }
                )
            }
            else {
                Write-Host (
                    "Installing the complete extracted volume set..."
                ) -ForegroundColor Cyan
                $installOutput = @(
                    & $python "-B" ".\research\map_volumes_probe.py" `
                        $customDataset `
                        "--port" $Port `
                        "--install-only" 2>&1 | ForEach-Object { Write-Host $_; $_ }
                )
            }
            $installExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorAction
        }
        $joinedOutput = $installOutput -join "`n"
        $worldTimeMatch = [regex]::Match(
            $joinedOutput,
            "WORLD_TIME_ORIGINAL_BITS=(0x[0-9A-Fa-f]+)"
        )
        if ($worldTimeMatch.Success) {
            $worldTimeOriginalBits = $worldTimeMatch.Groups[1].Value
            $worldTimePrearmed = $true
        }
        if ($installExitCode -eq 77) {
            Write-Host "Waiting for a complete stable gameplay world..."
            continue
        }
        if ($installExitCode -eq 79) { $rendererAbandoned = $true; break }
        if ($installExitCode -ne 0) {
            throw "Volume renderer installation failed (exit $installExitCode)."
        }
        if (
            $StartWorldPaused -and
            $sessionIndex -eq 0 -and
            -not $worldTimePrearmed
        ) {
            throw "Renderer did not return world-time pause ownership."
        }
        $codeMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_CODE_POINTER=(0x[0-9A-Fa-f]+)"
        )
        $dataMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_DATA_POINTER=(0x[0-9A-Fa-f]+)"
        )
        $kindVisibilityMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_KIND_VISIBILITY_POINTER=(0x[0-9A-Fa-f]+)"
        )
        $flagRenderStateMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_FLAG_RENDER_STATE_POINTER=(0x[0-9A-Fa-f]+)"
        )
        $volumeSelectionMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_VOLUME_SELECTION_POINTER=(0x[0-9A-Fa-f]+)"
        )
        $volumeCountMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_VOLUME_COUNT=([0-9]+)"
        )
        $volumeOrderMatch = [regex]::Match(
            $joinedOutput,
            "ACTIVE_VOLUME_ORDER=([0-9,]+)"
        )
        if (
            -not $codeMatch.Success -or
            -not $dataMatch.Success -or
            -not $kindVisibilityMatch.Success -or
            (
                $CollisionToggle -and
                (
                    -not $flagRenderStateMatch.Success -or
                    -not $volumeSelectionMatch.Success -or
                    -not $volumeCountMatch.Success -or
                    -not $volumeOrderMatch.Success
                )
            )
        ) {
            throw (
                "The renderer did not return its lifecycle pointers."
            )
        }
        $codePointer = $codeMatch.Groups[1].Value
        $dataPointer = $dataMatch.Groups[1].Value
        $kindVisibilityPointer = $kindVisibilityMatch.Groups[1].Value
        if ($CollisionToggle) {
            $flagRenderStatePointer = (
                $flagRenderStateMatch.Groups[1].Value
            )
            $volumeSelectionPointer = (
                $volumeSelectionMatch.Groups[1].Value
            )
            $volumeCount = [int]$volumeCountMatch.Groups[1].Value
            $volumeOrder = $volumeOrderMatch.Groups[1].Value
        }
        $rendererInstalled = $true

        $remainingDuration = 0.0
        if ($Duration -gt 0) {
            $elapsed = ((Get-Date) - $startTime).TotalSeconds
            $remainingDuration = [Math]::Max(0.01, $Duration - $elapsed)
        }
        $overlayDuration = if ($remainingDuration -gt 0) {
            $remainingDuration + 5.0
        }
        else {
            0.0
        }
        $overlayArguments = @(
            "-B",
            ".\research\trigger_overlay.py",
            (Convert-ToProcessArgument $customDataset),
            "--duration", (Convert-ToInvariantText $overlayDuration),
            "--labels-only",
            "--camera-state-port", $CameraStatePort.ToString(),
            "--camera-volume-panel",
            "--status-x-offset", $OverlayStatusXOffset.ToString(),
            "--ui-panels-state", (Convert-ToProcessArgument $uiPanelsStatePath)
        )
        $overlayArguments += @("--control-state", (Convert-ToProcessArgument $controlStatePath), "--profile", $Profile, "--ready-file", (Convert-ToProcessArgument $overlayReadyPath))
        if ($CollisionToggle) {
            $overlayArguments += @(
                "--native-flag-geometry",
                "--volume-order", $volumeOrder
            )
        }

        Write-Host ""
        Write-Host (
            "PERSISTENT MAP DEBUG FREECAM ACTIVE: $displayName"
        ) -ForegroundColor Green
        Write-Host "  W/A/S/D  move       Q/E  down/up"
        Write-Host "  Shift    fast       Ctrl precision"
        Write-Host "  Mouse    look       R    show/hide full overlay"
        Write-Host "  O        hide/show the game's 2D panels (default OFF)"
        Write-Host "  Z        exploration light   X remove fog   F5 range 1x/2x/4x/8x"
        Write-Host "  F11      all loaded sectors / automatic sector visibility"
        Write-Host "  F        trigger flags / all flags / off"
        if ($CollisionToggle) {
            Write-Host "  N        next trigger (Shift+N previous)"
            Write-Host "  M        show only the selected trigger"
            Write-Host "  0-3      show/hide trigger kinds"
            Write-Host "  4-9      show/hide terrain categories"
            Write-Host "  C        climb diagnostics (static + climbable Cars)"
            Write-Host (
                "  '        show/hide all terrain"
            ) -ForegroundColor Magenta
            Write-Host "  V        freecam / normal camera"
            Write-Host (
                "  P        pause/release world (camera stays live)"
            ) -ForegroundColor Yellow
            Write-Host "  F8       attach/release player"
            Write-Host "  Y        player air stand on/off"
            Write-Host "  G        god mode"
            Write-Host "  T        player physics bounds"
            if ($ExtendedCollisionView) {
                Write-Host (
                    "  B        nearby live object OBBs (up to 24)"
                ) -ForegroundColor Magenta
                Write-Host (
                    "  XRAY     triggers/terrain ignore scene depth"
                ) -ForegroundColor Magenta
            }
        }
        else {
            Write-Host "  0-9      show/hide every volume of that kind"
        }
        Write-Host (
            "  F10      finish safely and clean everything"
        ) -ForegroundColor Yellow
        Write-Host (
            "  Loading screens automatically suspend and rebuild the tool."
        ) -ForegroundColor Cyan

        $freecamArguments = @(
            "-B",
            ".\research\freecam_probe.py",
            "--duration", (Convert-ToInvariantText $remainingDuration),
            "--speed", (Convert-ToInvariantText $Speed),
            "--mouse-sensitivity", (
                Convert-ToInvariantText $MouseSensitivity
            ),
            "--port", $Port.ToString(),
            "--broadcast-port", $CameraStatePort.ToString(),
            "--kind-visibility-address", $kindVisibilityPointer,
            "--lifecycle-renderer-code-address", $codePointer,
            "--world-id", $worldId,
            "--transition-state", $statePath,
            "--ready-file", $readyPath,
            "--transition-stable-seconds", "8",
            "--ui-panels-state", $uiPanelsStatePath
        )
        $freecamArguments += @("--stop-file", $stopPath, "--control-state", $controlStatePath, "--profile", $Profile)
        if ($Controller) { $freecamArguments += "--controller" }
        if ($CaptureKeyboardPad) { $freecamArguments += "--capture-keyboard-pad" }
        if ($ReturnToFollowCamera) { $freecamArguments += "--return-to-follow-camera" }
        if ($sessionIndex -gt 0 -and (Test-Path -LiteralPath $statePath)) {
            $freecamArguments += @("--resume-state", $statePath)
        }
        if ($CollisionToggle) {
            $freecamArguments += @(
                "--simultaneous-code-address", $codePointer,
                "--native-camera-code-address", $codePointer,
                "--simultaneous-data-address", $dataPointer,
                "--flag-render-state-address", $flagRenderStatePointer,
                "--volume-selection-address", $volumeSelectionPointer,
                "--volume-count", $volumeCount.ToString(),
                "--collision-radius", (
                    Convert-ToInvariantText $CollisionRadius
                ),
                "--collision-limit", $CollisionLimit.ToString(),
                "--collision-update-hz", (
                    Convert-ToInvariantText $CollisionUpdateHz
                ),
                "--carry-player",
                "--start-player-released",
                "--player-god-mode",
                "--player-no-target",
                "--player-air-stand-toggle",
                "--camera-mode-toggle",
                "--player-debug-toggle",
                "--climb-object-support",
                "--world-time-toggle",
                "--world-time-scale", (
                    Convert-ToInvariantText $WorldTimeScale
                )
            )
            if ($ExtendedCollisionView) {
                $freecamArguments += "--object-debug-toggle"
            }
        }
        if ($worldTimePrearmed) {
            $freecamArguments += @(
                "--start-world-time-slowed",
                "--world-time-original-bits", $worldTimeOriginalBits
            )
        }
        if ($TraceLoopTiming) {
            $freecamArguments += "--trace-loop-timing"
        }
        if ($GangStatePort -gt 0) {
            $freecamArguments += @(
                "--gang-broadcast-port", $GangStatePort.ToString(),
                "--gang-telemetry-hz", (
                    Convert-ToInvariantText $GangTelemetryHz
                )
            )
        }
        if (-not [string]::IsNullOrWhiteSpace($HumanWatchPrefix)) {
            $freecamArguments += @(
                "--human-watch-prefix", $HumanWatchPrefix,
                "--human-watch-hz", (Convert-ToInvariantText $HumanWatchHz)
            )
        }
        if ($PostBeginCameraOverride) {
            $freecamArguments += @(
                "--camera-override-code-address", $codePointer,
                "--trace-camera-race"
            )
        }

        if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
            Remove-Item -LiteralPath $readyPath -Force
        }
        $freecamProcessArguments = @(
            $freecamArguments | ForEach-Object {
                Convert-ToProcessArgument ([string]$_)
            }
        )
        $freecamProcess = Start-Process `
            -FilePath $python `
            -ArgumentList $freecamProcessArguments `
            -WorkingDirectory $workspaceDir `
            -NoNewWindow `
            -PassThru
        # Windows PowerShell 5.1 can lose ExitCode on a Start-Process object
        # unless its native handle is materialized before the child exits.
        $null = $freecamProcess.Handle
        $readyDeadline = (Get-Date).AddSeconds(60)
        while (
            -not (Test-Path -LiteralPath $readyPath -PathType Leaf)
        ) {
            $freecamProcess.Refresh()
            if ($freecamProcess.HasExited) {
                break
            }
            if ((Get-Date) -ge $readyDeadline) {
                Write-Warning (
                    "Freecam initialization has not reported ready after " +
                    "60 seconds; starting the panel without the handshake."
                )
                break
            }
            Start-Sleep -Milliseconds 50
        }
        $freecamProcess.Refresh()
        if (-not $freecamProcess.HasExited) {
            if (Test-Path -LiteralPath $overlayReadyPath) { Remove-Item -LiteralPath $overlayReadyPath -Force }
            if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
                Remove-Item -LiteralPath $readyPath -Force
            }
            $overlayProcess = Start-Process `
                -FilePath $pythonw `
                -ArgumentList $overlayArguments `
                -WorkingDirectory $workspaceDir `
                -WindowStyle Hidden `
                -RedirectStandardError $overlayErrorPath `
                -PassThru
            $overlayDeadline = (Get-Date).AddSeconds(5)
            while (-not (Test-Path -LiteralPath $overlayReadyPath) -and -not $overlayProcess.HasExited -and (Get-Date) -lt $overlayDeadline) {
                Start-Sleep -Milliseconds 50
                $overlayProcess.Refresh()
            }
            $overlayProcess.Refresh()
            if ($overlayProcess.HasExited) {
                Write-Warning (
                    "The transparent volume panel exited during startup; " +
                    "freecam and native geometry remain active."
                )
            }
            if (-not (Test-Path -LiteralPath $overlayReadyPath) -or $overlayProcess.HasExited) {
                Set-Content -LiteralPath $stopPath -Value "overlay failed"
                $freecamProcess.WaitForExit()
                throw "The overlay did not initialize. Review $overlayErrorPath. Native cleanup was requested."
            }
        }
        # Wait unconditionally: the child may have exited during the ready
        # handshake, in which case the branch above is skipped.
        while (-not $freecamProcess.WaitForExit(100)) {
            if ($null -ne $ownerProcess -and $ownerProcess.HasExited) { Set-Content -LiteralPath $stopPath -Value "launcher exited" }
        }
        $freecamExitCode = $freecamProcess.ExitCode
        if ($null -eq $freecamExitCode) {
            throw "Freecam finished but its process exit code was unavailable."
        }
        Stop-VolumeOverlay
        if (
            $worldTimePrearmed -and
            -not (Restore-PrearmedWorldTime)
        ) {
            throw (
                "CRITICAL: the prearmed world timestep could not be " +
                "restored. Do not create a save state; restart the game."
            )
        }

        if ($freecamExitCode -eq $transitionExitCode) {
            Write-Host ""
            Write-Host (
                "Sustained gameplay detected after the transition. " +
                "Releasing the old-world blocks now that the allocator " +
                "is safe..."
            ) -ForegroundColor Magenta
            if (-not (Invoke-RendererCleanup $true)) {
                throw "Delayed transition cleanup failed."
            }
            $sessionIndex += 1
            $requestedSet = if ($KeepVolumeSet) {
                $initialRequestedSet
            }
            else {
                ""
            }
            continue
        }
        if ($freecamExitCode -ne 0) {
            throw "Freecam exited with code $freecamExitCode."
        }

        if (-not (Invoke-RendererCleanup $false)) {
            throw "Renderer cleanup failed after F10."
        }
        break
    }
}
finally {
    Stop-VolumeOverlay
    foreach ($uiPath in @($uiPanelsStopPath, $uiPanelsStatePath, $visibilityStatePath)) {
        if (Test-Path -LiteralPath $uiPath) {
            Remove-Item -LiteralPath $uiPath -Force -ErrorAction Continue
        }
        $pendingStatusFile = [System.IO.Path]::ChangeExtension($uiPath, "tmp")
        if (Test-Path -LiteralPath $pendingStatusFile) {
            Remove-Item -LiteralPath $pendingStatusFile -Force -ErrorAction Continue
        }
    }
    if (
        $worldTimePrearmed -and
        -not (Restore-PrearmedWorldTime)
    ) {
        Write-Warning (
            "CRITICAL: native world time is still overridden. " +
            "Do not create a save state; restart the game."
        )
    }
    if ($rendererInstalled -and -not $rendererAbandoned) {
        $disarmed = Invoke-RendererDisarm
        if ($disarmed) {
            if ($rendererCleanupAttempted) {
                throw "CRITICAL: renderer cleanup did not confirm completion. Hooks disarmed; heap release will not be retried. Restart the game before retrying."
            }
            Write-Host (
                "Waiting for a stable world before any heap cleanup..."
            ) -ForegroundColor Cyan
            if (Wait-ForStableWorld) {
                if (-not (Invoke-RendererCleanup $true)) {
                    Write-Error (
                        "CRITICAL: renderer blocks could not be cleaned. " +
                        "Do not create a save state; restart the game."
                    )
                }
            }
            else {
                Write-Warning (
                    "The game is no longer reachable. Its EE RAM is gone, " +
                    "so no in-game cleanup remains."
                )
            }
        }
        else {
            Write-Error (
                "CRITICAL: renderer hooks could not be disarmed. " +
                "Do not create a save state; restart the game."
            )
        }
    }
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        Remove-Item -LiteralPath $statePath -Force
    }
    if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
        Remove-Item -LiteralPath $readyPath -Force
    }
    Pop-Location
}

if ($rendererAbandoned) { exit 79 }
Write-Host "Persistent map debug freecam closed cleanly." -ForegroundColor Green
