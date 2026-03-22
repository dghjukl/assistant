# ============================================================
#  EOS - Full Setup  (Setup-Full.ps1)
#  Downloads and installs EVERYTHING needed to run EOS,
#  including all AI models (~13 GB total download).
#
#  HOW TO RUN:
#    Right-click this file -> "Run with PowerShell"
#    OR open PowerShell and type:  .\Setup-Full.ps1
#
#  What this script downloads:
#    * llama.cpp server binaries (from ggml-org/llama.cpp)
#    * Piper TTS voice engine
#    * Qwen3-8B primary model              (~6.3 GB)
#    * LFM2.5 thinking model               (~805 MB)
#    * LFM2 tool-calling model             (~805 MB)
#    * Whisper STT model                   (~253 MB)
#    * Piper TTS voice (Amy)               (~61 MB)
#    * Qwen2.5-VL-3B vision model + mmproj (~3.3 GB)
#    * Python packages (requirements.txt)
#
#  The creativity model is NOT downloaded automatically.
#  Place any instruct GGUF of your choice in models\creativity\
#  to enable creativity mode.
#
#  Need the smaller version? Run Setup-Lite.ps1 instead.
#  (Setup-Lite skips the large LLMs and vision model.)
# ============================================================

#Requires -Version 5.1
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force
$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # speeds up Invoke-WebRequest

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

function Write-OK($text)      { Write-Host "    [OK]      $text" -ForegroundColor Green    }
function Write-Skip($text)    { Write-Host "    [SKIP]    $text" -ForegroundColor DarkGreen }
function Write-Warn($text)    { Write-Host "    [NOTE]    $text" -ForegroundColor DarkYellow }
function Write-Problem($text) { Write-Host "    [MISSING] $text" -ForegroundColor Red }


# -- Download helper ----------------------------------------------------------
# Uses BITS (Background Intelligent Transfer Service) which shows a proper
# Windows progress bar and can resume interrupted downloads.
# Falls back to Invoke-WebRequest if BITS is unavailable.

function Download-File {
    param(
        [string]$Url,
        [string]$Dest,
        [string]$Label,
        [long]  $ExpectedMB = 0
    )

    # Skip if file already exists and has non-zero size
    if (Test-Path $Dest) {
        $sz = (Get-Item $Dest).Length
        if ($sz -gt 1024) {
            Write-Skip "$Label - already present"
            return
        }
        # File exists but is suspiciously small - re-download
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
    } catch {
        # BITS unavailable or failed - fall back
    }

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


# -- Extract ZIP helper --------------------------------------------------------

function Expand-To {
    param([string]$ZipPath, [string]$DestDir)
    if (-not (Test-Path $DestDir)) {
        New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    }
    Expand-Archive -Path $ZipPath -DestinationPath $DestDir -Force
}


# -----------------------------------------------------------------------------
Write-Banner "EOS  |  Full Setup"
Write-Host "  This will download approximately 13 GB of files." -ForegroundColor White
Write-Host "  Downloads are skipped if the file already exists." -ForegroundColor Gray
Write-Host "  You can safely re-run this script at any time." -ForegroundColor Gray


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
    Write-Warn "No NVIDIA GPU detected - will install CPU-only llama.cpp"
    Write-Host "    EOS will run on CPU only (slower but fully functional)." -ForegroundColor Gray
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
        $headers  = @{ "User-Agent" = "EOS-Installer/1.0"; "Accept" = "application/vnd.github.v3+json" }
        $apiUrl   = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
        $release  = Invoke-RestMethod -Uri $apiUrl -Headers $headers
        $relTag   = $release.tag_name
        Write-Host "    Latest release: $relTag" -ForegroundColor Gray

        $tmpZip = "$env:TEMP\llama_cpu.zip"
        $tmpDir = "$env:TEMP\llama_extract"

        # -- CPU build --------------------------------------------------------
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

                $serverExe = Get-ChildItem $tmpDir -Filter "llama-server.exe" -Recurse |
                             Select-Object -First 1
                if ($serverExe) {
                    $cpuDest = Join-Path $Root "llama-CPU"
                    New-Item -ItemType Directory -Force -Path $cpuDest | Out-Null
                    Get-ChildItem $serverExe.DirectoryName |
                        Copy-Item -Destination $cpuDest -Force
                    Write-OK "llama-server (CPU) -> llama-CPU\"
                }

                Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
                Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
            } else {
                Write-Problem "Could not find CPU build in latest release - check https://github.com/ggml-org/llama.cpp/releases"
            }
        } else {
            Write-Skip "llama.cpp CPU build - already installed"
        }

        # -- GPU (CUDA) build -------------------------------------------------
        if ($gpuNeeded) {
            $cudaAsset = $release.assets |
                Where-Object { $_.name -match "bin-win-cuda.*x64.*\.zip$" } |
                Sort-Object { $_.name } | Select-Object -Last 1

            if ($cudaAsset) {
                $tmpZip2 = "$env:TEMP\llama_gpu.zip"
                Download-File $cudaAsset.browser_download_url $tmpZip2 "llama.cpp CUDA build ($($cudaAsset.name))" 200

                if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
                Expand-To $tmpZip2 $tmpDir

                $serverExe2 = Get-ChildItem $tmpDir -Filter "llama-server.exe" -Recurse |
                              Select-Object -First 1
                if ($serverExe2) {
                    $gpuDest = Join-Path $Root "llama-b8149-bin-win-cuda-13.1-x64"
                    New-Item -ItemType Directory -Force -Path $gpuDest | Out-Null
                    Get-ChildItem $serverExe2.DirectoryName |
                        Copy-Item -Destination $gpuDest -Force
                    Write-OK "llama-server (GPU/CUDA) -> llama-b8149-bin-win-cuda-13.1-x64\"
                }

                Remove-Item $tmpZip2 -Force -ErrorAction SilentlyContinue
                Remove-Item $tmpDir  -Recurse -Force -ErrorAction SilentlyContinue
            } else {
                Write-Warn "No CUDA build found in latest release. GPU acceleration will fall back to CPU binary."
            }
        } elseif ($hasGPU) {
            Write-Skip "llama.cpp GPU/CUDA build - already installed"
        }

    } catch {
        Write-Problem "Could not fetch llama.cpp release: $_"
        Write-Host "    Download manually from: https://github.com/ggml-org/llama.cpp/releases" -ForegroundColor Gray
        Write-Host "    Place llama-server.exe in: llama-CPU\" -ForegroundColor Gray
        if ($hasGPU) {
            Write-Host "    And in: llama-b8149-bin-win-cuda-13.1-x64\" -ForegroundColor Gray
        }
    }
}


# -- 5. Download Piper TTS engine ---------------------------------------------
Write-Step "Downloading Piper TTS engine"

if (Test-Path (Join-Path $Root "Piper\piper\piper.exe")) {
    Write-Skip "Piper TTS engine - already installed"
} else {
    try {
        $headers  = @{ "User-Agent" = "EOS-Installer/1.0"; "Accept" = "application/vnd.github.v3+json" }
        $piperApi = "https://api.github.com/repos/rhasspy/piper/releases/latest"
        $piperRel = Invoke-RestMethod -Uri $piperApi -Headers $headers

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
                Write-Warn "Piper extracted but piper.exe location may differ - check Piper\ folder"
            }

            Remove-Item $piperZip -Force -ErrorAction SilentlyContinue
        } else {
            Write-Problem "Could not find Windows Piper release"
            Write-Host "    Download from: https://github.com/rhasspy/piper/releases" -ForegroundColor Gray
            Write-Host "    Extract so that Piper\piper\piper.exe exists" -ForegroundColor Gray
        }
    } catch {
        Write-Problem "Could not fetch Piper release: $_"
    }
}


# -- 6. Download AI models -----------------------------------------------------
Write-Step "Downloading AI models  (this is the big part - ~13 GB total)"
Write-Host "    Each download is skipped if the file already exists." -ForegroundColor Gray

# Helper: download a model only if its folder doesn't already contain a .gguf
function Ensure-ModelDir {
    param([string]$Dir, [string]$Filename, [string]$Url, [string]$Label, [long]$MB)
    $dest = Join-Path $Root "$Dir\$Filename"
    $full = Join-Path $Root $Dir
    $existing = Get-ChildItem $full -Filter "*.gguf" -File -ErrorAction SilentlyContinue |
                Select-Object -First 1
    if ($existing) {
        Write-Skip "$Label - $($existing.Name) already in $Dir\"
        return
    }
    Download-File $Url $dest $Label $MB
}

# -- Primary model: Qwen3-8B-Q6_K (~6.3 GB) -----------------------------------
Write-Host ""
Write-Host "    [1/7] Primary model (Qwen3-8B - this is the largest file)" -ForegroundColor White
Ensure-ModelDir "models\primary" "Qwen3-8B-Q6_K.gguf" "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q6_K.gguf" "Qwen3-8B-Q6_K (primary model)" 6400

# -- Thinking worker: LFM2.5-1.2B-Thinking (~805 MB) --------------------------
Write-Host "    [2/7] Thinking model" -ForegroundColor White
Ensure-ModelDir "models\thinking" "LFM2.5-1.2B-Thinking-Q5_K_M.gguf" "https://huggingface.co/LiquidAI/LFM2.5-1.2B-Thinking-GGUF/resolve/main/LFM2.5-1.2B-Thinking-Q5_K_M.gguf" "LFM2.5-1.2B-Thinking-Q5_K_M (thinking worker)" 820

# -- Tool router: LFM2-1.2B-Tool (~805 MB) ------------------------------------
Write-Host "    [3/6] Tool-calling model" -ForegroundColor White
Ensure-ModelDir "models\tool" "LFM2-1.2B-Tool-Q5_K_M.gguf" "https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF/resolve/main/LFM2-1.2B-Tool-Q5_K_M.gguf" "LFM2-1.2B-Tool-Q5_K_M (tool router)" 820

# -- STT: Whisper small.en (~253 MB) ------------------------------------------
Write-Host "    [4/6] Speech-to-text model (Whisper)" -ForegroundColor White
Download-File "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q8_0.bin" (Join-Path $Root "models\stt\ggml-small.en-q8_0.bin") "ggml-small.en-q8_0 (Whisper STT)" 255

# -- TTS: Piper Amy voice (~61 MB) --------------------------------------------
Write-Host "    [5/6] Text-to-speech voice (Piper Amy)" -ForegroundColor White
Download-File "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx" (Join-Path $Root "models\tts\en_US-amy-medium.onnx") "en_US-amy-medium.onnx (Piper TTS voice)" 63
Download-File "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json" (Join-Path $Root "models\tts\en_US-amy-medium.onnx.json") "en_US-amy-medium.onnx.json (voice config)" 1

# -- Vision: Qwen2.5-VL-3B-Instruct Q4_K_M + f16 mmproj (~3.3 GB) ------------
Write-Host "    [6/6] Vision model (Qwen2.5-VL-3B - for vision mode only)" -ForegroundColor White

$visDir      = Join-Path $Root "models\vision"
$visMain     = Get-ChildItem $visDir -Filter "*.gguf" -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -notmatch "^mmproj" } | Select-Object -First 1
$visMmproj   = Get-ChildItem $visDir -Filter "mmproj*.gguf" -File -ErrorAction SilentlyContinue |
               Select-Object -First 1

if ($visMain -and $visMmproj) {
    Write-Skip "Vision model files already present in models\vision\"
} else {
    if (-not $visMain) {
        # Qwen2.5-VL-3B-Instruct Q4_K_M - 1.93 GB, good quality for 3B
        Download-File "https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf" (Join-Path $Root "models\vision\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf") "Qwen2.5-VL-3B-Instruct-Q4_K_M (vision model)" 1975
    } else {
        Write-Skip "Vision main model already present - $($visMain.Name)"
    }

    if (-not $visMmproj) {
        # f16 mmproj - 1.34 GB, required for vision to work
        Download-File "https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main/mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf" (Join-Path $Root "models\vision\mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf") "mmproj-Qwen2.5-VL-3B-Instruct-f16 (vision projector)" 1375
    } else {
        Write-Skip "Vision mmproj already present - $($visMmproj.Name)"
    }
}


# -- 7. Install Python packages ------------------------------------------------
Write-Step "Installing Python packages"

$reqFile = Join-Path $Root "requirements.txt"
if (Test-Path $reqFile) {
    Write-Host "    Running: pip install -r requirements.txt" -ForegroundColor Gray
    try {
        python -m pip install --upgrade pip --quiet
        python -m pip install -r $reqFile --no-warn-script-location
        Write-OK "Python packages installed"
    } catch {
        Write-Problem "pip install encountered an error: $_"
        Write-Host "    Try running manually: python -m pip install -r requirements.txt" -ForegroundColor Gray
    }
} else {
    Write-Problem "requirements.txt not found at $reqFile"
}


# -- 7b. Verify bundled embedding model ---------------------------------------
Write-Step "Checking bundled embedding model (all-MiniLM-L6-v2)"
$embedPath = Join-Path $Root "models\embedding\all-MiniLM-L6-v2"
if (Test-Path $embedPath) {
    Write-OK "Embedding model found at models\embedding\all-MiniLM-L6-v2"
} else {
    Write-Warn "Embedding model not found at models\embedding\all-MiniLM-L6-v2 — memory retrieval will be disabled until it is placed there."
}


# -- 8. Final verification -----------------------------------------------------
Write-Step "Running file verification"
Write-Host ""

$checkItems = @{
    "Primary model (.gguf in models\primary)"   = @{ type="dir_gguf"; path="models\primary"                    }
    "Thinking model (.gguf in models\thinking)" = @{ type="dir_gguf"; path="models\thinking"                   }
    "Tool model (.gguf in models\tool)"         = @{ type="dir_gguf"; path="models\tool"                       }
    "Whisper STT"                               = @{ type="file";     path="models\stt\ggml-small.en-q8_0.bin" }
    "Piper TTS voice"                           = @{ type="file";     path="models\tts\en_US-amy-medium.onnx"  }
    "Piper binary"                              = @{ type="file";     path="Piper\piper\piper.exe"              }
    "llama-server (CPU)"                        = @{ type="file";     path="llama-CPU\llama-server.exe"         }
}

$gpuBin = Join-Path $Root "llama-b8149-bin-win-cuda-13.1-x64\llama-server.exe"
if ($hasGPU) {
    $checkItems["llama-server (GPU)"] = @{ type="file"; path="llama-b8149-bin-win-cuda-13.1-x64\llama-server.exe" }
}

$allGood = $true
foreach ($name in ($checkItems.Keys | Sort-Object)) {
    $item = $checkItems[$name]
    $full = Join-Path $Root $item.path
    $ok   = if ($item.type -eq "dir_gguf") {
                ($null -ne (Get-ChildItem (Join-Path $Root $item.path) -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1))
            } else {
                Test-Path $full
            }
    if ($ok) { Write-OK $name } else { Write-Problem $name; $allGood = $false }
}

# Vision (optional)
$visOK = (Get-ChildItem (Join-Path $Root "models\vision") -Filter "*.gguf" -File -ErrorAction SilentlyContinue).Count -ge 2
if ($visOK) {
    Write-OK "Vision model (models\vision\) - vision mode available"
} else {
    Write-Warn "Vision model not found - vision mode unavailable (standard mode unaffected)"
}

# Creativity (user-supplied, optional)
$creOK = ($null -ne (Get-ChildItem (Join-Path $Root "models\creativity") -Filter "*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1))
if ($creOK) {
    Write-OK "Creativity model found in models\creativity\ - creativity mode available"
} else {
    Write-Warn "No creativity model found - place any instruct GGUF in models\creativity\ to enable creativity mode"
}


# -- 9. Credential file notes -------------------------------------------------
Write-Host ""
Write-Host $Divider -ForegroundColor DarkGray
Write-Host "  Optional integrations (manual setup required):" -ForegroundColor White
Write-Host ""
Write-Host "    Discord bot:" -ForegroundColor Gray
Write-Host "      Create 'AI personal files\Discord.txt' and paste your bot token" -ForegroundColor DarkGray
Write-Host "      Get a token at: https://discord.com/developers/applications" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    Google Workspace (Calendar, Gmail, Drive):" -ForegroundColor Gray
Write-Host "      Place your OAuth client_secret_*.json in 'AI personal files\'" -ForegroundColor DarkGray
Write-Host "      Create credentials at: https://console.cloud.google.com/" -ForegroundColor DarkGray
Write-Host $Divider -ForegroundColor DarkGray


# -- Done ----------------------------------------------------------------------
Write-Host ""
Write-Host $Divider -ForegroundColor Cyan

if ($allGood) {
    Write-Host "  Setup complete! EOS is ready to launch." -ForegroundColor Green
} else {
    Write-Host "  Setup finished with some missing files (see above)." -ForegroundColor Yellow
    Write-Host "  You can re-run this script to retry failed downloads." -ForegroundColor Gray
}

Write-Host ""
Write-Host "  Launchers:" -ForegroundColor White
Write-Host "    'Start Base Mode.bat'            - Primary + Tool only (lightest)" -ForegroundColor Gray
Write-Host "    'Start Base+Thinking Mode.bat'   - + background thinking worker" -ForegroundColor Gray
Write-Host "    'Start Base+Creativity Mode.bat' - + creativity (place model in models\creativity\)" -ForegroundColor Gray
Write-Host "    'Start Full Mode.bat'            - Thinking + Creativity both active" -ForegroundColor Gray
Write-Host "    'Start Vision Mode.bat'          - Base+Thinking + vision model" -ForegroundColor Gray
Write-Host "    'Start Standard Mode.bat'        - legacy launcher (same as Base+Thinking)" -ForegroundColor Gray
Write-Host "    'Start No-Boot Mode.bat'         - WebUI only (if servers already running)" -ForegroundColor Gray
Write-Host ""
Write-Host "  Web interface opens at:  http://127.0.0.1:7860/" -ForegroundColor Cyan
Write-Host "  Admin panel at:          http://127.0.0.1:7860/admin" -ForegroundColor Cyan
Write-Host $Divider -ForegroundColor Cyan
Write-Host ""

Read-Host "  Press Enter to close"
