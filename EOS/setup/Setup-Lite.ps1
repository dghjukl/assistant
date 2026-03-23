# ============================================================
#  EOS - Lite Setup  (Setup-Lite.ps1)
#  Downloads everything EXCEPT models larger than 1 GB.
#  Total download: ~3 GB instead of ~16 GB.
#
#  HOW TO RUN:
#    Right-click this file -> "Run with PowerShell"
#    OR open PowerShell and type:  .\Setup-Lite.ps1
#
#  What this script downloads:
#    * llama.cpp server binaries (from ggml-org/llama.cpp)
#    * Piper TTS voice engine
#    * LFM2.5 thinking model               (~805 MB)
#    * LFM2 tool-calling model             (~805 MB)
#    * Whisper STT model                   (~253 MB)
#    * Piper TTS voice (Amy)               (~61 MB)
#    * Python packages (requirements.txt)
#
#  The creativity model is NOT downloaded automatically.
#  Place any instruct GGUF of your choice in models\creativity\
#  to enable creativity mode.
#
#  What you need to add manually after this script:
#    * Primary model (Qwen3-8B or similar, >1 GB)
#    * Vision model (optional, only for vision mode)
#
#  Want everything including large models? Run Setup-Full.ps1.
# ============================================================

#Requires -Version 5.1
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

$Root    = $PSScriptRoot
$Divider = "  " + ("=" * 56)

function Write-Banner($text) {
    Write-Host ""
    Write-Host $Divider -ForegroundColor Cyan
    Write-Host "    $text" -ForegroundColor Cyan
    Write-Host $Divider -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step($text) {
    Write-Host ""
    Write-Host "  -- $text" -ForegroundColor Yellow
}

function Write-OK($text)      { Write-Host "    [OK]      $text" -ForegroundColor Green     }
function Write-Skip($text)    { Write-Host "    [SKIP]    $text" -ForegroundColor DarkGreen  }
function Write-Warn($text)    { Write-Host "    [NOTE]    $text" -ForegroundColor DarkYellow }
function Write-Problem($text) { Write-Host "    [MISSING] $text" -ForegroundColor Red        }


# -- Download helper ----------------------------------------------------------

function Download-File {
    param(
        [string]$Url,
        [string]$Dest,
        [string]$Label,
        [long]  $ExpectedMB = 0
    )

    if (Test-Path $Dest) {
        $sz = (Get-Item $Dest).Length
        if ($sz -gt 1024) {
            Write-Skip "$Label - already present"
            return
        }
        Remove-Item $Dest -Force
    }

    $parent = Split-Path $Dest -Parent
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $sizeHint = if ($ExpectedMB -gt 0) { " (~${ExpectedMB} MB)" } else { "" }
    Write-Host "    Downloading $Label$sizeHint ..." -ForegroundColor White

    $ok = $false
    try {
        Import-Module BitsTransfer -ErrorAction Stop
        Start-BitsTransfer -Source $Url -Destination $Dest -DisplayName "EOS: $Label"
        $ok = $true
    } catch { }

    if (-not $ok) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
            $ok = $true
        } catch {
            Write-Problem "Failed to download $Label"
            Write-Host "    URL: $Url" -ForegroundColor Gray
            Write-Host "    Please download manually and place at: $Dest" -ForegroundColor Gray
            return
        }
    }

    $mb = [math]::Round((Get-Item $Dest).Length / 1MB, 1)
    Write-OK "$Label - ${mb} MB saved"
}


function Expand-To {
    param([string]$ZipPath, [string]$DestDir)
    if (-not (Test-Path $DestDir)) {
        New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    }
    Expand-Archive -Path $ZipPath -DestinationPath $DestDir -Force
}


# -----------------------------------------------------------------------------
Write-Banner "EOS  |  Lite Setup  (small models only)"
Write-Host "  This downloads ~3 GB of supporting files." -ForegroundColor White
Write-Host "  The primary model (Qwen3-8B, ~6.3 GB) must be added manually." -ForegroundColor Yellow
Write-Host "  Downloads are skipped if the file already exists." -ForegroundColor Gray


# -- 1. Python check ----------------------------------------------------------
Write-Step "Checking Python"

$pyOK = $false
try {
    $pyver = (python --version 2>&1).ToString().Trim()
    if ($pyver -match "Python (3\.\d+)") {
        $minor = [int]($Matches[1].Split(".")[1])
        if ($minor -ge 10) {
            Write-OK "Python found: $pyver"
            $pyOK = $true
        } else {
            Write-Problem "Python $pyver is too old. EOS needs Python 3.10 or newer."
        }
    }
} catch {
    Write-Problem "Python not found."
}

if (-not $pyOK) {
    Write-Host ""
    Write-Host "  Please install Python 3.11 from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "  IMPORTANT: Check 'Add Python to PATH' during installation." -ForegroundColor Yellow
    Write-Host "  After installing Python, re-run this script." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}


# -- 2. GPU check -------------------------------------------------------------
Write-Step "Checking for NVIDIA GPU"

$hasGPU = $false
try {
    $nvOut = nvidia-smi --query-gpu=name --format=csv,noheader 2>&1
    if ($LASTEXITCODE -eq 0 -and $nvOut -notmatch "error") {
        $gpuName = ($nvOut -split "`n")[0].Trim()
        Write-OK "NVIDIA GPU: $gpuName - GPU acceleration enabled"
        $hasGPU = $true
    }
} catch { }

if (-not $hasGPU) {
    Write-Warn "No NVIDIA GPU detected - CPU-only llama.cpp will be installed"
}


# -- 3. Create directory structure --------------------------------------------
Write-Step "Creating directory structure"

$dirs = @(
    "data",
    "data\memory_store",
    "models\primary",
    "models\stt",
    "models\thinking",
    "models\tool",
    "models\tts",
    "models\vision",
    "models\creativity",
    "AI personal files"
)

foreach ($d in $dirs) {
    $path = Join-Path $Root $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Write-OK "Created: $d"
    }
}
Write-OK "All directories ready"


# -- 4. Download llama.cpp server binaries ------------------------------------
Write-Step "Downloading llama.cpp server binaries"

$cpuExe    = Join-Path $Root "llama-CPU\llama-server.exe"
$gpuExe    = Join-Path $Root "llama-b8149-bin-win-cuda-13.1-x64\llama-server.exe"
$cpuNeeded = -not (Test-Path $cpuExe)
$gpuNeeded = $hasGPU -and -not (Test-Path $gpuExe)

if (-not $cpuNeeded -and -not $gpuNeeded) {
    Write-Skip "llama.cpp binaries - already installed"
} else {
    Write-Host "    (Fetching latest release from github.com/ggml-org/llama.cpp ...)" -ForegroundColor Gray
    try {
        $headers = @{ "User-Agent" = "EOS-Installer/1.0"; "Accept" = "application/vnd.github.v3+json" }
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" -Headers $headers
        $relTag  = $release.tag_name
        Write-Host "    Latest release: $relTag" -ForegroundColor Gray

        $tmpZip = "$env:TEMP\llama_cpu.zip"
        $tmpDir = "$env:TEMP\llama_extract"

        # CPU build
        if ($cpuNeeded) {
            $cpuAsset = $release.assets |
                Where-Object { $_.name -match "bin-win-avx2-x64\.zip$" } |
                Select-Object -First 1
            if (-not $cpuAsset) {
                $cpuAsset = $release.assets |
                    Where-Object { $_.name -match "win.*x64.*\.zip" -and $_.name -notmatch "cuda" } |
                    Select-Object -First 1
            }

            if ($cpuAsset) {
                Download-File $cpuAsset.browser_download_url $tmpZip "llama.cpp CPU build ($($cpuAsset.name))" 50

                if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
                Expand-To $tmpZip $tmpDir

                $serverExe = Get-ChildItem $tmpDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
                if ($serverExe) {
                    $cpuDest = Join-Path $Root "llama-CPU"
                    New-Item -ItemType Directory -Force -Path $cpuDest | Out-Null
                    Get-ChildItem $serverExe.DirectoryName | Copy-Item -Destination $cpuDest -Force
                    Write-OK "llama-server (CPU) -> llama-CPU\"
                }

                Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
                Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
            } else {
                Write-Problem "Could not find CPU build - check https://github.com/ggml-org/llama.cpp/releases"
            }
        } else {
            Write-Skip "llama.cpp CPU build - already installed"
        }

        # GPU (CUDA) build
        if ($gpuNeeded) {
            $cudaAsset = $release.assets |
                Where-Object { $_.name -match "bin-win-cuda.*x64.*\.zip$" } |
                Sort-Object { $_.name } | Select-Object -Last 1

            if ($cudaAsset) {
                $tmpZip2 = "$env:TEMP\llama_gpu.zip"
                Download-File $cudaAsset.browser_download_url $tmpZip2 "llama.cpp CUDA build ($($cudaAsset.name))" 200

                if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
                Expand-To $tmpZip2 $tmpDir

                $serverExe2 = Get-ChildItem $tmpDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
                if ($serverExe2) {
                    $gpuDest = Join-Path $Root "llama-b8149-bin-win-cuda-13.1-x64"
                    New-Item -ItemType Directory -Force -Path $gpuDest | Out-Null
                    Get-ChildItem $serverExe2.DirectoryName | Copy-Item -Destination $gpuDest -Force
                    Write-OK "llama-server (GPU/CUDA) -> llama-b8149-bin-win-cuda-13.1-x64\"
                }

                Remove-Item $tmpZip2 -Force -ErrorAction SilentlyContinue
                Remove-Item $tmpDir  -Recurse -Force -ErrorAction SilentlyContinue
            } else {
                Write-Warn "No CUDA build found - GPU will fall back to CPU binary"
            }
        } elseif ($hasGPU) {
            Write-Skip "llama.cpp GPU/CUDA build - already installed"
        }

    } catch {
        Write-Problem "Could not fetch llama.cpp release: $_"
        Write-Host "    Download from: https://github.com/ggml-org/llama.cpp/releases" -ForegroundColor Gray
    }
}


# -- 5. Download Piper TTS engine ---------------------------------------------
Write-Step "Downloading Piper TTS engine"

if (Test-Path (Join-Path $Root "Piper\piper\piper.exe")) {
    Write-Skip "Piper TTS engine - already installed"
} else {
    try {
        $headers  = @{ "User-Agent" = "EOS-Installer/1.0"; "Accept" = "application/vnd.github.v3+json" }
        $piperRel = Invoke-RestMethod -Uri "https://api.github.com/repos/rhasspy/piper/releases/latest" -Headers $headers

        $piperAsset = $piperRel.assets |
            Where-Object { $_.name -match "piper_windows_amd64\.zip$" } |
            Select-Object -First 1

        if ($piperAsset) {
            $piperZip = "$env:TEMP\piper_win.zip"
            $piperOut = Join-Path $Root "Piper"
            Download-File $piperAsset.browser_download_url $piperZip "Piper TTS engine" 10
            New-Item -ItemType Directory -Force -Path $piperOut | Out-Null
            Expand-To $piperZip $piperOut
            if (Test-Path (Join-Path $Root "Piper\piper\piper.exe")) {
                Write-OK "Piper TTS -> Piper\piper\piper.exe"
            } else {
                Write-Warn "Piper extracted - check Piper\ folder structure"
            }
            Remove-Item $piperZip -Force -ErrorAction SilentlyContinue
        } else {
            Write-Problem "Could not find Windows Piper release"
        }
    } catch {
        Write-Problem "Could not fetch Piper release: $_"
    }
}


# -- 6. Download small AI models (~3 GB) --------------------------------------
Write-Step "Downloading small AI models  (~2 GB total)"
Write-Host "    Skipping the primary model (Qwen3-8B) - see manual steps below." -ForegroundColor DarkYellow

function Ensure-ModelDir {
    param([string]$Dir, [string]$Filename, [string]$Url, [string]$Label, [long]$MB)
    $dest     = Join-Path $Root "$Dir\$Filename"
    $full     = Join-Path $Root $Dir
    $existing = Get-ChildItem $full -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existing) {
        Write-Skip "$Label - $($existing.Name) already present"
        return
    }
    Download-File $Url $dest $Label $MB
}

# Thinking worker
Write-Host ""
Write-Host "    [1/4] Thinking model" -ForegroundColor White
Ensure-ModelDir "models\thinking" "LFM2.5-1.2B-Thinking-Q5_K_M.gguf" "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Thinking-GGUF/resolve/main/LFM2.5-1.2B-Thinking-Q5_K_M.gguf" "LFM2.5-1.2B-Thinking-Q5_K_M" 820

# Tool router
Write-Host "    [2/4] Tool-calling model" -ForegroundColor White
Ensure-ModelDir "models\tool" "LFM2-1.2B-Tool-Q5_K_M.gguf" "https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF/resolve/main/LFM2-1.2B-Tool-Q5_K_M.gguf" "LFM2-1.2B-Tool-Q5_K_M" 820

# Whisper STT
Write-Host "    [3/4] Speech-to-text model (Whisper)" -ForegroundColor White
Download-File "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q8_0.bin" (Join-Path $Root "models\stt\ggml-small.en-q8_0.bin") "ggml-small.en-q8_0 (Whisper STT)" 255

# Piper TTS Amy voice
Write-Host "    [4/4] Text-to-speech voice (Piper Amy)" -ForegroundColor White
Download-File "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx" (Join-Path $Root "models\tts\en_US-amy-medium.onnx") "en_US-amy-medium.onnx (Piper TTS voice)" 63
Download-File "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json" (Join-Path $Root "models\tts\en_US-amy-medium.onnx.json") "en_US-amy-medium.onnx.json (voice config)" 1


# -- 7. Install Python packages ------------------------------------------------
Write-Step "Installing Python packages"

$reqFile = Join-Path $Root "requirements.txt"
if (Test-Path $reqFile) {
    try {
        python -m pip install --upgrade pip --quiet
        python -m pip install -r $reqFile --no-warn-script-location
        Write-OK "Python packages installed"
    } catch {
        Write-Problem "pip install error: $_"
        Write-Host "    Try manually: python -m pip install -r requirements.txt" -ForegroundColor Gray
    }
} else {
    Write-Problem "requirements.txt not found"
}


# -- 7b. Verify bundled embedding model ----------------------------------------
Write-Step "Checking bundled embedding model (all-MiniLM-L6-v2)"
$embedPath = Join-Path $Root "models\embedding\all-MiniLM-L6-v2"
if (Test-Path $embedPath) {
    Write-OK "Embedding model found at models\embedding\all-MiniLM-L6-v2"
} else {
    Write-Warn "Embedding model not found at models\embedding\all-MiniLM-L6-v2 — memory retrieval will be disabled until it is placed there."
}


# -- 8. Final verification -----------------------------------------------------
Write-Step "Verification"
Write-Host ""

$checks = [ordered]@{
    "Thinking model (.gguf in models\thinking)" = @{ type="dir_gguf"; path="models\thinking"                   }
    "Tool model (.gguf in models\tool)"         = @{ type="dir_gguf"; path="models\tool"                       }
    "Whisper STT"                               = @{ type="file";     path="models\stt\ggml-small.en-q8_0.bin" }
    "Piper TTS voice"                           = @{ type="file";     path="models\tts\en_US-amy-medium.onnx"  }
    "Piper binary"                              = @{ type="file";     path="Piper\piper\piper.exe"              }
    "llama-server (CPU)"                        = @{ type="file";     path="llama-CPU\llama-server.exe"         }
}
if ($hasGPU) {
    $checks["llama-server (GPU)"] = @{ type="file"; path="llama-b8149-bin-win-cuda-13.1-x64\llama-server.exe" }
}

$allGood = $true
foreach ($name in $checks.Keys) {
    $item = $checks[$name]
    $full = Join-Path $Root $item.path
    $ok   = if ($item.type -eq "dir_gguf") {
                ($null -ne (Get-ChildItem (Join-Path $Root $item.path) -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1))
            } else {
                Test-Path $full
            }
    if ($ok) { Write-OK $name } else { Write-Problem $name; $allGood = $false }
}

# Primary model - not downloaded by this script
$primaryHas = ($null -ne (Get-ChildItem (Join-Path $Root "models\primary") -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1))
if ($primaryHas) {
    Write-OK "Primary model found in models\primary\"
} else {
    Write-Problem "Primary model MISSING - EOS cannot start without it (see instructions below)"
    $allGood = $false
}

# Creativity (user-supplied, optional)
$creOK = ($null -ne (Get-ChildItem (Join-Path $Root "models\creativity") -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1))
if ($creOK) {
    Write-OK "Creativity model found in models\creativity\ - creativity mode available"
} else {
    Write-Warn "No creativity model - place any instruct GGUF in models\creativity\ to enable creativity mode"
}


# -- 9. Manual steps for primary model ----------------------------------------
Write-Host ""
Write-Host $Divider -ForegroundColor Yellow
Write-Host "  MANUAL STEP REQUIRED - Primary Model" -ForegroundColor Yellow
Write-Host $Divider -ForegroundColor Yellow
Write-Host ""
Write-Host "  EOS needs a primary language model to run. Download one of these:" -ForegroundColor White
Write-Host ""
Write-Host "  Recommended (what this project uses):" -ForegroundColor White
Write-Host "    Qwen3-8B-Q6_K.gguf (~6.3 GB)" -ForegroundColor Gray
Write-Host "    https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q6_K.gguf" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Smaller alternative (less RAM, decent quality):" -ForegroundColor White
Write-Host "    Qwen3-4B-Q6_K.gguf (~3.0 GB)" -ForegroundColor Gray
Write-Host "    https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q6_K.gguf" -ForegroundColor Cyan
Write-Host ""
Write-Host "  After downloading, place the .gguf file in:" -ForegroundColor White
Write-Host "    $(Join-Path $Root 'models\primary\')" -ForegroundColor White
Write-Host ""
Write-Host "  The filename does not matter - just drop any single .gguf in that folder." -ForegroundColor Gray
Write-Host $Divider -ForegroundColor Yellow
Write-Host ""
Write-Host "  Optional: Vision model (for Start Vision Mode.bat only)" -ForegroundColor DarkGray
Write-Host "    Download both files below and place them in models\vision\" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    Main model  (~1.93 GB):" -ForegroundColor DarkGray
Write-Host "    https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf" -ForegroundColor Cyan
Write-Host ""
Write-Host "    Vision projector / mmproj  (~1.34 GB):" -ForegroundColor DarkGray
Write-Host "    https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf" -ForegroundColor Cyan
Write-Host ""

Write-Host "  Optional integrations (manual setup):" -ForegroundColor DarkGray
Write-Host "    Discord: create 'AI personal files\Discord.txt' with your bot token" -ForegroundColor DarkGray
Write-Host "    Google:  place client_secret_*.json in 'AI personal files\'" -ForegroundColor DarkGray


# -- Done ----------------------------------------------------------------------
Write-Host ""
Write-Host $Divider -ForegroundColor Cyan

if ($allGood) {
    Write-Host "  Setup complete. EOS assets are in place." -ForegroundColor Green
} else {
    Write-Host "  Setup finished with some missing or optional items (see above)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Post-setup machine assessment:" -ForegroundColor White
try {
    python -m runtime.windows_deployment --root $Root --config (Join-Path $Root 'config.json')
} catch {
    Write-Warn "Automatic launch assessment failed. Run: python verify.py"
}
Write-Host ""
Write-Host "  Recommended next steps:" -ForegroundColor White
Write-Host "    1. Run: python verify.py" -ForegroundColor Gray
Write-Host "    2. Run: launchers\Launch EOS.bat" -ForegroundColor Gray
Write-Host "       The launcher will detect the supported machine tier and pre-select a safe profile." -ForegroundColor DarkGray
Write-Host "    3. If the launcher reports a degraded or CPU-only tier, that is a supported mode, not a failure." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Direct launchers (advanced/manual control):" -ForegroundColor White
Write-Host "    launchers\start-minimal.bat    - start only the main model" -ForegroundColor Gray
Write-Host "    launchers\start-standard.bat   - recommended default bundle" -ForegroundColor Gray
Write-Host "    launchers\start-full.bat       - enable every installed helper" -ForegroundColor Gray
Write-Host "    start-eos.bat                   - bootstrap WebUI after backends are running" -ForegroundColor Gray
Write-Host ""
Write-Host "  Web interface: http://127.0.0.1:7860/" -ForegroundColor Cyan
Write-Host "  Admin panel:    http://127.0.0.1:7860/admin" -ForegroundColor Cyan
Write-Host $Divider -ForegroundColor Cyan
Write-Host ""
Read-Host "  Press Enter to close"
