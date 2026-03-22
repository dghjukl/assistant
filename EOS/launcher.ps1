Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition

# -- Colours ------------------------------------------------------------------
$cBg        = [System.Drawing.Color]::FromArgb(24,  24,  24 )
$cPanel     = [System.Drawing.Color]::FromArgb(36,  36,  36 )
$cBorder    = [System.Drawing.Color]::FromArgb(60,  60,  60 )
$cText      = [System.Drawing.Color]::FromArgb(220, 220, 220)
$cDim       = [System.Drawing.Color]::FromArgb(130, 130, 130)
$cBlue      = [System.Drawing.Color]::FromArgb(0,   120, 212)
$cGreen     = [System.Drawing.Color]::FromArgb(78,  201, 120)
$cYellow    = [System.Drawing.Color]::FromArgb(220, 180, 60 )
$cConsoleBg = [System.Drawing.Color]::FromArgb(16,  16,  16 )

# -- Server definitions -------------------------------------------------------
$servers = @(
    @{ Name="Main";       Port=8080; CPU=$true;  GPU=$true;  Default="GPU" },
    @{ Name="Tools";      Port=8082; CPU=$true;  GPU=$true;  Default="CPU" },
    @{ Name="Thinking";   Port=8083; CPU=$true;  GPU=$true;  Default="Off" },
    @{ Name="Creativity"; Port=8084; CPU=$true;  GPU=$true;  Default="Off" },
    @{ Name="Vision";     Port=8081; CPU=$false; GPU=$true;  Default="Off" }
)

# -- Form ---------------------------------------------------------------------
$form                 = New-Object System.Windows.Forms.Form
$form.Text            = "EOS Launcher"
$form.BackColor       = $cBg
$form.ForeColor       = $cText
$form.Font            = New-Object System.Drawing.Font("Segoe UI", 10)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox     = $false
$form.StartPosition   = "CenterScreen"

# Title
$lblTitle           = New-Object System.Windows.Forms.Label
$lblTitle.Text      = "EOS  |  Server Launcher"
$lblTitle.Font      = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = $cText
$lblTitle.Location  = New-Object System.Drawing.Point(20, 18)
$lblTitle.AutoSize  = $true
$form.Controls.Add($lblTitle)

# Column headers
$headerY = 52
foreach ($pair in @( @(162,"CPU"), @(232,"GPU"), @(302,"Off") )) {
    $h           = New-Object System.Windows.Forms.Label
    $h.Text      = $pair[1]
    $h.ForeColor = $cDim
    $h.Font      = New-Object System.Drawing.Font("Segoe UI", 9)
    $h.Location  = New-Object System.Drawing.Point($pair[0], $headerY)
    $h.AutoSize  = $true
    $form.Controls.Add($h)
}

# Separator helper
function Add-Rule($y) {
    $p           = New-Object System.Windows.Forms.Panel
    $p.BackColor = $cBorder
    $p.Location  = New-Object System.Drawing.Point(20, $y)
    $p.Size      = New-Object System.Drawing.Size(360, 1)
    $form.Controls.Add($p)
}
Add-Rule 70

# -- Server rows --------------------------------------------------------------
$radioMap = @{}
$rowY     = 80

foreach ($s in $servers) {
    $row           = New-Object System.Windows.Forms.Panel
    $row.BackColor = $cPanel
    $row.Location  = New-Object System.Drawing.Point(20, $rowY)
    $row.Size      = New-Object System.Drawing.Size(360, 36)
    $form.Controls.Add($row)

    $lbl           = New-Object System.Windows.Forms.Label
    $lbl.Text      = $s.Name
    $lbl.ForeColor = $cText
    $lbl.Font      = New-Object System.Drawing.Font("Segoe UI", 10)
    $lbl.Location  = New-Object System.Drawing.Point(12, 8)
    $lbl.AutoSize  = $true
    $row.Controls.Add($lbl)

    $radioMap[$s.Name] = @{}

    $opts = @()
    if ($s.CPU) { $opts += "CPU" }
    if ($s.GPU) { $opts += "GPU" }
    $opts += "Off"

    $xPos = @{ "CPU" = 150; "GPU" = 220; "Off" = 290 }

    foreach ($opt in $opts) {
        $rb           = New-Object System.Windows.Forms.RadioButton
        $rb.Text      = ""
        $rb.BackColor = $cPanel
        $rb.ForeColor = $cText
        $rb.Location  = New-Object System.Drawing.Point($xPos[$opt], 8)
        $rb.Size      = New-Object System.Drawing.Size(20, 20)
        $rb.Checked   = ($opt -eq $s.Default)
        $row.Controls.Add($rb)
        $radioMap[$s.Name][$opt] = $rb
    }

    $rowY += 38
}

Add-Rule $rowY
$rowY += 12

# -- Status console -----------------------------------------------------------
$console             = New-Object System.Windows.Forms.RichTextBox
$console.BackColor   = $cConsoleBg
$console.ForeColor   = $cGreen
$console.ReadOnly    = $true
$console.Font        = New-Object System.Drawing.Font("Consolas", 9)
$console.Location    = New-Object System.Drawing.Point(20, $rowY)
$console.Size        = New-Object System.Drawing.Size(360, 110)
$console.BorderStyle = "None"
$form.Controls.Add($console)

$rowY += 118

# -- Launch button ------------------------------------------------------------
$btn                               = New-Object System.Windows.Forms.Button
$btn.Text                          = "Launch EOS"
$btn.BackColor                     = $cBlue
$btn.ForeColor                     = [System.Drawing.Color]::White
$btn.FlatStyle                     = "Flat"
$btn.FlatAppearance.BorderSize     = 0
$btn.Font                          = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$btn.Location                      = New-Object System.Drawing.Point(20, $rowY)
$btn.Size                          = New-Object System.Drawing.Size(360, 42)
$btn.Cursor                        = [System.Windows.Forms.Cursors]::Hand
$form.Controls.Add($btn)

$rowY += 52
$form.ClientSize = New-Object System.Drawing.Size(400, $rowY)

# -- Helpers ------------------------------------------------------------------
function Write-Con {
    param([string]$msg, [System.Drawing.Color]$col)
    if (-not $col) { $col = $cGreen }
    $console.SelectionStart  = $console.TextLength
    $console.SelectionLength = 0
    $console.SelectionColor  = $col
    $console.AppendText("$msg`n")
    $console.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}

function Wait-Port {
    param([int]$Port, [int]$TimeoutSec = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $r   = $tcp.BeginConnect("127.0.0.1", $Port, $null, $null)
            $ok  = $r.AsyncWaitHandle.WaitOne(400)
            if ($ok -and $tcp.Connected) { $tcp.Close(); return $true }
            $tcp.Close()
        } catch {}
        Start-Sleep -Milliseconds 600
    }
    return $false
}

# -- Launch handler -----------------------------------------------------------
$btn.Add_Click({
    $btn.Enabled = $false
    $btn.Text    = "Starting..."
    $console.Clear()

    # Build work list
    $toStart = @()
    foreach ($s in $servers) {
        $sel = "Off"
        foreach ($opt in $radioMap[$s.Name].Keys) {
            if ($radioMap[$s.Name][$opt].Checked) { $sel = $opt; break }
        }
        if ($sel -ne "Off") {
            $bat = Join-Path $Root ("start-" + $s.Name.ToLower() + "-" + $sel.ToLower() + ".bat")
            $toStart += [pscustomobject]@{ Name=$s.Name; Accel=$sel; Port=$s.Port; Bat=$bat }
        }
    }

    if ($toStart.Count -eq 0) {
        Write-Con "Nothing selected - pick at least one server." $cYellow
        $btn.Text    = "Launch EOS"
        $btn.Enabled = $true
        return
    }

    # Start each server in its own window
    foreach ($t in $toStart) {
        Write-Con ("  Starting " + $t.Name + " [" + $t.Accel + "]...") $cText
        Start-Process "cmd.exe" -ArgumentList "/k `"$($t.Bat)`"" -WorkingDirectory $Root
        Start-Sleep -Milliseconds 400
    }

    Write-Con "" $cText
    Write-Con "  Waiting for servers to come online..." $cDim

    # Verify ports
    foreach ($t in $toStart) {
        Write-Con ("    " + $t.Name + " (port " + $t.Port + ")...") $cDim
        $up = Wait-Port -Port $t.Port -TimeoutSec 120
        if ($up) {
            Write-Con ("    " + $t.Name + " ready.") $cGreen
        } else {
            Write-Con ("    " + $t.Name + " timed out - continuing anyway.") $cYellow
        }
    }

    Write-Con "" $cText
    Write-Con "  All checks done. Launching EOS..." $cGreen
    Start-Sleep -Milliseconds 600

    Start-Process "cmd.exe" -ArgumentList "/k `"$(Join-Path $Root 'start-eos.bat')`"" -WorkingDirectory $Root
    $form.Close()
})

$form.ShowDialog() | Out-Null
