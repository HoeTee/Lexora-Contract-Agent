param(
    [string]$Tag = "southernbanker/lexora:latest",
    [string]$KeyFile,
    [string]$VmpToolsDir
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$temporaryKey = $null

try {
    if (-not $VmpToolsDir) {
        $VmpToolsDir = $env:VMP_TOOLS_DIR
    }
    if (-not $VmpToolsDir) {
        throw "VMP tools directory is required. Pass -VmpToolsDir or set VMP_TOOLS_DIR."
    }
    if (-not (Test-Path -LiteralPath $VmpToolsDir -PathType Container)) {
        throw "VMP tools directory does not exist: $VmpToolsDir. Pass -VmpToolsDir or set VMP_TOOLS_DIR."
    }
    $resolvedVmpToolsDir = (Resolve-Path -LiteralPath $VmpToolsDir).Path
    $requiredVmpFiles = @(
        "vmprotect_con",
        "libVMProtectSDK64.so",
        "libjitterentropy.so.3",
        "VMProtectLicense.ini",
        "sdk\VMProtectSDK.h"
    )
    foreach ($relativePath in $requiredVmpFiles) {
        $requiredPath = Join-Path $resolvedVmpToolsDir $relativePath
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Required VMP tool file is missing: $requiredPath"
        }
    }

    if ($KeyFile) {
        $resolvedKey = (Resolve-Path -LiteralPath $KeyFile).Path
    }
    else {
        $temporaryKey = Join-Path ([System.IO.Path]::GetTempPath()) ("lexora-source-" + [guid]::NewGuid().ToString("N") + ".key")
        $key = New-Object byte[] 32
        $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($key)
        }
        finally {
            $random.Dispose()
        }
        [System.IO.File]::WriteAllBytes($temporaryKey, $key)
        $resolvedKey = $temporaryKey
    }

    $keyLength = (Get-Item -LiteralPath $resolvedKey).Length
    if ($keyLength -ne 32) {
        throw "AES key must contain exactly 32 bytes; received $keyLength bytes."
    }

    docker build `
        --build-context "vmp_tools=$resolvedVmpToolsDir" `
        --secret "id=source_key,src=$resolvedKey" `
        --tag $Tag `
        $projectRoot

    if ($LASTEXITCODE -ne 0) {
        throw "Docker build failed with exit code $LASTEXITCODE."
    }

    Write-Output "Built protected image: $Tag"
}
finally {
    if ($temporaryKey -and (Test-Path -LiteralPath $temporaryKey)) {
        Remove-Item -LiteralPath $temporaryKey -Force
    }
}
