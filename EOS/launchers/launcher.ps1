Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$ConfigPath = Join-Path $Root "config.json"

# -- Colours ------------------------------------------------------------------
$cBg        = [System.Drawing.Color]::FromArgb(24,  24,  24 )
$cPanel     = [System.Drawing.Color]::FromArgb(36,  36,  36 )
$cBorder    = [System.Drawing.Color]::FromArgb(60,  60,  60 )
$cText      = [System.Drawing.Color]::FromArgb(220, 220, 220)
$cDim       = [System.Drawing.Color]::FromArgb(130, 130, 130)
$cBlue      = [System.Drawing.Color]::FromArgb(0,   120, 212)
$cGreen     = [System.Drawing.Color]::FromArgb(78,  201, 120)
$cYellow    = [System.Drawing.Color]::FromArgb(220, 180, 60 )
$cRed       = [System.Drawing.Color]::FromArgb(255, 110, 110)
$cConsoleBg = [System.Drawing.Color]::FromArgb(16,  16,  16 )

function Get-RoleCatalog {
    param($Assessment)
    if ($Assessment -and $Assessment.role_catalog) {
        return @($Assessment.role_catalog)
    }

    return @(
        @{ key="primary";    label="Main";       script_base="main";       port=8080; optional=$false }
        @{ key="tool";       label="Tools";      script_base="tools";      port=8082; optional=$true }
        @{ key="thinking";   label="Thinking";   script_base="thinking";   port=8083; optional=$true }
        @{ key="creativity"; label="Creativity"; script_base="creativity"; port=8084; optional=$true }
        @{ key="vision";     label="Vision";     script_base="vision";     port=8081; optional=$true }
    )
}

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

function Add-Rule($y) {
    $p           = New-Object System.Windows.Forms.Panel
    $p.BackColor = $cBorder
    $p.Location  = New-Object System.Drawing.Point(20, $y)
    $p.Size      = New-Object System.Drawing.Size(520, 1)
    $form.Controls.Add($p)
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

function Get-Assessment {
    try {
        $json = & python -m runtime.windows_deployment --json --root $Root --config $ConfigPath 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw ($json -join "`n")
        }
        return ($json -join "`n" | ConvertFrom-Json)
    } catch {
        return [pscustomobject]@{
            config_path = $ConfigPath
            hardware = [pscustomobject]@{ has_nvidia_gpu = $false; gpu_name = $null; total_memory_gb = $null; cpu_only_reason = "assessment failed" }
            roles = @{}
            profiles = @()
            recommended_profile = "compatibility"
            setup_complete = $false
            summary = @("Automatic launcher assessment failed.")
            blocking_issues = @("Could not inspect config.json, models, and runtimes. Run 'python verify.py' in the EOS folder.")
            warnings = @($_.Exception.Message)
        }
    }
}

$assessment = Get-Assessment
$roleMeta = Get-RoleCatalog -Assessment $assessment
$profilesByKey = @{}
foreach ($profile in $assessment.profiles) { $profilesByKey[$profile.key] = $profile }
$rolesByKey = @{}
foreach ($prop in $assessment.roles.PSObject.Properties) { $rolesByKey[$prop.Name] = $prop.Value }

# -- Form ---------------------------------------------------------------------
$form                 = New-Object System.Windows.Forms.Form
$form.Text            = "EOS Launcher"
$form.BackColor       = $cBg
$form.ForeColor       = $cText
$form.Font            = New-Object System.Drawing.Font("Segoe UI", 10)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox     = $false
$form.StartPosition   = "CenterScreen"

$lblTitle           = New-Object System.Windows.Forms.Label
$lblTitle.Text      = "EOS  |  Launcher"
$lblTitle.Font      = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = $cText
$lblTitle.Location  = New-Object System.Drawing.Point(20, 18)
$lblTitle.AutoSize  = $true
$form.Controls.Add($lblTitle)

$lblHint           = New-Object System.Windows.Forms.Label
$lblHint.Text      = "Launcher auto-detects the machine tier, then offers only sane launch profiles. Degraded modes are supported modes."
$lblHint.Font      = New-Object System.Drawing.Font("Segoe UI", 8.5)
$lblHint.ForeColor = $cDim
$lblHint.Location  = New-Object System.Drawing.Point(20, 42)
$lblHint.Size      = New-Object System.Drawing.Size(520, 34)
$form.Controls.Add($lblHint)

$panelDetect           = New-Object System.Windows.Forms.Panel
$panelDetect.BackColor = $cPanel
$panelDetect.Location  = New-Object System.Drawing.Point(20, 82)
$panelDetect.Size      = New-Object System.Drawing.Size(520, 78)
$form.Controls.Add($panelDetect)

$lblDetectTitle           = New-Object System.Windows.Forms.Label
$lblDetectTitle.Text      = "Detected machine tier"
$lblDetectTitle.Font      = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Bold)
$lblDetectTitle.ForeColor = $cText
$lblDetectTitle.Location  = New-Object System.Drawing.Point(12, 10)
$lblDetectTitle.AutoSize  = $true
$panelDetect.Controls.Add($lblDetectTitle)

$lblDetectSummary           = New-Object System.Windows.Forms.Label
$lblDetectSummary.Text      = ""
$lblDetectSummary.ForeColor = $cText
$lblDetectSummary.Location  = New-Object System.Drawing.Point(12, 30)
$lblDetectSummary.Size      = New-Object System.Drawing.Size(490, 20)
$panelDetect.Controls.Add($lblDetectSummary)

$lblDetectNotes           = New-Object System.Windows.Forms.Label
$lblDetectNotes.Text      = ""
$lblDetectNotes.ForeColor = $cDim
$lblDetectNotes.Location  = New-Object System.Drawing.Point(12, 50)
$lblDetectNotes.Size      = New-Object System.Drawing.Size(490, 20)
$panelDetect.Controls.Add($lblDetectNotes)

Add-Rule 172

$lblProfile           = New-Object System.Windows.Forms.Label
$lblProfile.Text      = "Launch profile"
$lblProfile.ForeColor = $cText
$lblProfile.Location  = New-Object System.Drawing.Point(20, 184)
$lblProfile.AutoSize  = $true
$form.Controls.Add($lblProfile)

$cmbProfiles                  = New-Object System.Windows.Forms.ComboBox
$cmbProfiles.DropDownStyle    = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$cmbProfiles.Location         = New-Object System.Drawing.Point(120, 180)
$cmbProfiles.Size             = New-Object System.Drawing.Size(220, 28)
$form.Controls.Add($cmbProfiles)

$chkManual                 = New-Object System.Windows.Forms.CheckBox
$chkManual.Text            = "Allow manual override"
$chkManual.ForeColor       = $cDim
$chkManual.Location        = New-Object System.Drawing.Point(360, 182)
$chkManual.Size            = New-Object System.Drawing.Size(180, 24)
$form.Controls.Add($chkManual)

$lblProfileInfo           = New-Object System.Windows.Forms.Label
$lblProfileInfo.ForeColor = $cDim
$lblProfileInfo.Location  = New-Object System.Drawing.Point(20, 212)
$lblProfileInfo.Size      = New-Object System.Drawing.Size(520, 32)
$form.Controls.Add($lblProfileInfo)

foreach ($pair in @(@(232, "CPU"), @(302, "GPU"), @(372, "Off"), @(434, "Model"))) {
    $h           = New-Object System.Windows.Forms.Label
    $h.Text      = $pair[1]
    $h.ForeColor = $cDim
    $h.Font      = New-Object System.Drawing.Font("Segoe UI", 9)
    $h.Location  = New-Object System.Drawing.Point($pair[0], 248)
    $h.AutoSize  = $true
    $form.Controls.Add($h)
}
Add-Rule 266

$radioMap = @{}
$modelLabels = @{}
$rowY = 276

foreach ($meta in $roleMeta) {
    $row           = New-Object System.Windows.Forms.Panel
    $row.BackColor = $cPanel
    $row.Location  = New-Object System.Drawing.Point(20, $rowY)
    $row.Size      = New-Object System.Drawing.Size(520, 40)
    $form.Controls.Add($row)

    $lbl           = New-Object System.Windows.Forms.Label
    $lbl.Text      = $meta.label
    $lbl.ForeColor = $cText
    $lbl.Location  = New-Object System.Drawing.Point(12, 10)
    $lbl.Size      = New-Object System.Drawing.Size(120, 20)
    $row.Controls.Add($lbl)

    $radioMap[$meta.key] = @{}
    foreach ($opt in @("cpu", "gpu", "off")) {
        $rb           = New-Object System.Windows.Forms.RadioButton
        $rb.Text      = ""
        $rb.BackColor = $cPanel
        $rb.ForeColor = $cText
        $rb.Location  = New-Object System.Drawing.Point(@{cpu=228;gpu=298;off=368}[$opt], 10)
        $rb.Size      = New-Object System.Drawing.Size(20, 20)
        $rb.Enabled   = $true
        $row.Controls.Add($rb)
        $radioMap[$meta.key][$opt] = $rb
        $rb.Add_CheckedChanged({
            if ($chkManual.Checked) {
                Update-SelectionSummary
            }
        })
    }

    $modelLabel           = New-Object System.Windows.Forms.Label
    $modelLabel.ForeColor = $cDim
    $modelLabel.Location  = New-Object System.Drawing.Point(430, 10)
    $modelLabel.Size      = New-Object System.Drawing.Size(80, 20)
    $row.Controls.Add($modelLabel)
    $modelLabels[$meta.key] = $modelLabel

    $rowY += 42
}

Add-Rule $rowY
$rowY += 10

$lblIssues           = New-Object System.Windows.Forms.Label
$lblIssues.ForeColor = $cYellow
$lblIssues.Location  = New-Object System.Drawing.Point(20, $rowY)
$lblIssues.Size      = New-Object System.Drawing.Size(520, 36)
$form.Controls.Add($lblIssues)
$rowY += 44

$console             = New-Object System.Windows.Forms.RichTextBox
$console.BackColor   = $cConsoleBg
$console.ForeColor   = $cGreen
$console.ReadOnly    = $true
$console.Font        = New-Object System.Drawing.Font("Consolas", 9)
$console.Location    = New-Object System.Drawing.Point(20, $rowY)
$console.Size        = New-Object System.Drawing.Size(520, 150)
$console.BorderStyle = "None"
$form.Controls.Add($console)
$rowY += 160

$btn                               = New-Object System.Windows.Forms.Button
$btn.Text                          = "Launch EOS"
$btn.BackColor                     = $cBlue
$btn.ForeColor                     = [System.Drawing.Color]::White
$btn.FlatStyle                     = "Flat"
$btn.FlatAppearance.BorderSize     = 0
$btn.Font                          = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$btn.Location                      = New-Object System.Drawing.Point(20, $rowY)
$btn.Size                          = New-Object System.Drawing.Size(520, 42)
$btn.Cursor                        = [System.Windows.Forms.Cursors]::Hand
$form.Controls.Add($btn)
$rowY += 52
$form.ClientSize = New-Object System.Drawing.Size(560, $rowY)

function Set-RoleAvailability {
    param($RoleKey)
    $role = $rolesByKey[$RoleKey]
    if (-not $role) {
        foreach ($opt in @("cpu", "gpu")) { $radioMap[$RoleKey][$opt].Enabled = $false }
        $radioMap[$RoleKey]["off"].Checked = $true
        $modelLabels[$RoleKey].Text = "missing"
        return
    }

    foreach ($opt in @("cpu", "gpu")) {
        $radioMap[$RoleKey][$opt].Enabled = ($role.available_accels -contains $opt)
    }
    $radioMap[$RoleKey]["off"].Enabled = $true

    if ($role.selected_model) {
        $name = Split-Path $role.selected_model -Leaf
        if ($name.Length -gt 22) { $name = $name.Substring(0, 19) + "..." }
        $modelLabels[$RoleKey].Text = $name
        $modelLabels[$RoleKey].ForeColor = $cDim
    } else {
        $modelLabels[$RoleKey].Text = "missing"
        $modelLabels[$RoleKey].ForeColor = $cRed
    }
}

function Apply-SelectionMap {
    param($Selections)
    foreach ($meta in $roleMeta) {
        $target = if ($Selections.PSObject.Properties.Name -contains $meta.key) { $Selections.$($meta.key) } else { "off" }
        if (-not $radioMap[$meta.key][$target].Enabled) { $target = "off" }
        $radioMap[$meta.key][$target].Checked = $true
    }
    Update-SelectionSummary
}

function Update-SelectionSummary {
    $issues = New-Object System.Collections.Generic.List[string]
    $selected = New-Object System.Collections.Generic.List[string]
    foreach ($meta in $roleMeta) {
        $role = $rolesByKey[$meta.key]
        $choice = "off"
        foreach ($opt in @("cpu", "gpu", "off")) {
            if ($radioMap[$meta.key][$opt].Checked) { $choice = $opt; break }
        }
        if ($choice -ne "off") {
            $selected.Add(("{0} [{1}]" -f $meta.label, $choice.ToUpper()))
            if (-not $radioMap[$meta.key][$choice].Enabled) {
                $issues.Add(("{0} cannot run on {1}." -f $meta.label, $choice.ToUpper()))
            }
            if ($role -and -not $role.selected_model) {
                $issues.Add(("{0} model is missing." -f $meta.label))
            }
        }
    }
    if (-not $radioMap["primary"]["cpu"].Checked -and -not $radioMap["primary"]["gpu"].Checked) {
        $issues.Add("Main must be on for EOS to chat.")
    }

    if ($selected.Count -eq 0) {
        $lblIssues.ForeColor = $cYellow
        $lblIssues.Text = "Nothing selected. Pick a supported profile or enable Main manually."
    } elseif ($issues.Count -gt 0) {
        $lblIssues.ForeColor = $cRed
        $lblIssues.Text = ($issues -join " ")
    } else {
        $lblIssues.ForeColor = $cGreen
        $lblIssues.Text = "Ready to launch: " + ($selected -join ", ")
    }

    $btn.Enabled = ($issues.Count -eq 0 -and $selected.Count -gt 0 -and $assessment.blocking_issues.Count -eq 0)
}

function Show-AssessmentInConsole {
    $console.Clear()
    if ($assessment.hardware.has_nvidia_gpu) {
        Write-Con ("Detected NVIDIA GPU: " + $assessment.hardware.gpu_name) $cGreen
    } else {
        Write-Con "No NVIDIA GPU detected. Compatibility / CPU-first mode is supported on this machine." $cYellow
    }
    if ($assessment.hardware.total_memory_gb) {
        Write-Con (("Detected system RAM: ~{0} GB" -f $assessment.hardware.total_memory_gb)) $cText
    }
    Write-Con ("Recommended profile: " + $assessment.recommended_profile) $cText
    foreach ($line in $assessment.summary) { Write-Con ("- " + $line) $cDim }
    foreach ($issue in $assessment.blocking_issues) { Write-Con ("BLOCKING: " + $issue) $cRed }
    foreach ($warning in $assessment.warnings) { Write-Con ("NOTE: " + $warning) $cYellow }
}

foreach ($meta in $roleMeta) { Set-RoleAvailability -RoleKey $meta.key }

$hardwareSummary = if ($assessment.hardware.has_nvidia_gpu) {
    "NVIDIA GPU detected: " + $assessment.hardware.gpu_name
} else {
    "No NVIDIA GPU detected — supported CPU/degraded tier"
}
$memorySummary = if ($assessment.hardware.total_memory_gb) {
    "Approx. " + $assessment.hardware.total_memory_gb + " GB RAM"
} else {
    "System memory not detected"
}
$lblDetectSummary.Text = $hardwareSummary
$lblDetectNotes.Text = $memorySummary

foreach ($profile in $assessment.profiles) {
    $label = if ($profile.supported) { $profile.label } else { $profile.label + " (unavailable)" }
    [void]$cmbProfiles.Items.Add([pscustomobject]@{ Key = $profile.key; Label = $label; Profile = $profile })
}
$cmbProfiles.DisplayMember = "Label"

function Select-RecommendedProfile {
    $targetKey = $assessment.recommended_profile
    $idx = 0
    foreach ($item in $cmbProfiles.Items) {
        if ($item.Key -eq $targetKey) {
            $cmbProfiles.SelectedIndex = $idx
            return
        }
        $idx += 1
    }
    if ($cmbProfiles.Items.Count -gt 0) { $cmbProfiles.SelectedIndex = 0 }
}

$cmbProfiles.Add_SelectedIndexChanged({
    if ($cmbProfiles.SelectedItem -eq $null) { return }
    $profile = $cmbProfiles.SelectedItem.Profile
    $lblProfileInfo.Text = $profile.description
    if (-not $chkManual.Checked) {
        Apply-SelectionMap -Selections $profile.selections
    }
    if (-not $profile.supported) {
        $lblIssues.ForeColor = $cYellow
        $lblIssues.Text = "Profile is listed for visibility, but this machine is missing something it needs: " + ($profile.issues -join "; ")
    }
})

$chkManual.Add_CheckedChanged({
    if (-not $chkManual.Checked -and $cmbProfiles.SelectedItem -ne $null) {
        Apply-SelectionMap -Selections $cmbProfiles.SelectedItem.Profile.selections
    }
})

Select-RecommendedProfile
Show-AssessmentInConsole
Update-SelectionSummary
if ($assessment.blocking_issues.Count -gt 0) {
    $btn.Enabled = $false
}

$btn.Add_Click({
    $btn.Enabled = $false
    $btn.Text    = "Starting..."
    Show-AssessmentInConsole

    if ($assessment.blocking_issues.Count -gt 0) {
        Write-Con "Launch blocked until setup issues are fixed. Run 'python verify.py' in the EOS folder." $cRed
        $btn.Text = "Launch EOS"
        return
    }

    $toStart = @()
    foreach ($meta in $roleMeta) {
        $choice = "off"
        foreach ($opt in @("cpu", "gpu", "off")) {
            if ($radioMap[$meta.key][$opt].Checked) { $choice = $opt; break }
        }
        if ($choice -ne "off") {
            $bat = Join-Path $Root ("launchers\start-" + $meta.script_base + "-" + $choice + ".bat")
            if (-not (Test-Path $bat)) {
                Write-Con ("Missing launcher: " + $bat) $cRed
                $btn.Text = "Launch EOS"
                $btn.Enabled = $true
                return
            }
            $toStart += [pscustomobject]@{ Name=$meta.label; Accel=$choice; Port=$meta.port; Bat=$bat }
        }
    }

    if ($toStart.Count -eq 0) {
        Write-Con "Nothing selected. Pick a supported profile or enable Main." $cYellow
        $btn.Text = "Launch EOS"
        $btn.Enabled = $true
        return
    }

    Write-Con "Starting selected backends..." $cText
    foreach ($t in $toStart) {
        Write-Con (("  {0} [{1}]" -f $t.Name, $t.Accel.ToUpper())) $cText
        Start-Process "cmd.exe" -ArgumentList "/k `"$($t.Bat)`"" -WorkingDirectory $Root
        Start-Sleep -Milliseconds 400
    }

    Write-Con "" $cText
    Write-Con "Waiting for selected ports to come online..." $cDim
    foreach ($t in $toStart) {
        $up = Wait-Port -Port $t.Port -TimeoutSec 120
        if ($up) {
            Write-Con (("  {0} ready on port {1}." -f $t.Name, $t.Port)) $cGreen
        } else {
            Write-Con (("  {0} did not report healthy on port {1} in time." -f $t.Name, $t.Port)) $cYellow
        }
    }

    Write-Con "" $cText
    Write-Con "Bootstrapping EOS WebUI..." $cGreen
    Start-Sleep -Milliseconds 600
    Start-Process "cmd.exe" -ArgumentList "/k `"$(Join-Path $Root 'start-eos.bat')`"" -WorkingDirectory $Root
    $form.Close()
})

[void]$form.ShowDialog()
