# Build Windows installers for ALL plugins — or a subset by target name.
#
# Reads the per-plugin metadata CMake emits (build\dmse_plugins\<Target>.json), builds
# each <Target>_All (Release) and compiles the shared installer.iss with the right
# /D defines (product name, plugin dir, target, per-product GUID, version).
#
# Usage (from the repo root, after `cmake -B build` with MSVC):
#   powershell -ExecutionPolicy Bypass -File packaging\windows\make_installers.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\windows\make_installers.ps1 StyloPoly SubC
#
# Requires Inno Setup 6 (ISCC on PATH, or set $env:ISCC to its full path).

param([string[]] $Targets)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Resolve-Path (Join-Path $scriptDir "..\..")
$buildDir  = if ($env:BUILD_DIR) { $env:BUILD_DIR } else { Join-Path $repoRoot "build" }
$metaDir   = Join-Path $buildDir "dmse_plugins"
$iscc      = if ($env:ISCC) { $env:ISCC } else { "ISCC" }

$metas = Get-ChildItem -Path $metaDir -Filter *.json -ErrorAction SilentlyContinue
if (-not $metas) {
    Write-Error "No plugin metadata in $metaDir - configure first: cmake -B $buildDir"
}

$packaged = @()
foreach ($file in $metas) {
    $m = Get-Content $file.FullName -Raw | ConvertFrom-Json
    if ($Targets -and ($Targets -notcontains $m.target)) { continue }

    Write-Host ""
    Write-Host ("=" * 64)
    Write-Host "  $($m.target) - $($m.product) $($m.version) ($($m.bundleId))"
    Write-Host ("=" * 64)

    cmake --build $buildDir --target "$($m.target)_All" --config Release
    if ($LASTEXITCODE -ne 0) { Write-Error "build failed for $($m.target)" }

    & $iscc `
        "/DMyName=$($m.product)" `
        "/DMyDir=$($m.dir)" `
        "/DMyTarget=$($m.target)" `
        "/DMyAppGuid=$($m.windowsAppGuid)" `
        "/DMyVersion=$($m.version)" `
        (Join-Path $scriptDir "installer.iss")
    if ($LASTEXITCODE -ne 0) { Write-Error "ISCC failed for $($m.target)" }

    $packaged += "$($m.product)-$($m.version)"
}

Write-Host ""
if (-not $packaged) {
    Write-Host "Nothing matched ($Targets). Known targets:"
    $metas | ForEach-Object { Write-Host "  $($_.BaseName)" }
    exit 1
}
Write-Host "Done - $($packaged.Count) installer(s): $($packaged -join ', ')"
