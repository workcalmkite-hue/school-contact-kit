$mutex = New-Object Threading.Mutex($false, 'Local\ClaudeUsageWidgetLite')
if (-not $mutex.WaitOne(0)) { exit }

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace Win -Name Api -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ReleaseCapture();
[DllImport("user32.dll")] public static extern int SendMessage(System.IntPtr h, int msg, int wp, int lp);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int cmd);
'@
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$KST = [TimeSpan]::FromHours(9)
$PosFile = Join-Path $env:APPDATA 'claude-usage-widget.json'
$CredFile = Join-Path $env:USERPROFILE '.claude\.credentials.json'
$HistFile = Join-Path $env:APPDATA 'Claude\plan-usage-history.json'
$HistFile = @($HistFile) + @(Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages\Claude_*\LocalCache\Roaming\Claude\plan-usage-history.json') -ErrorAction SilentlyContinue | ForEach-Object FullName) |
    Where-Object { Test-Path $_ } | Sort-Object { (Get-Item $_).LastWriteTime } -Descending | Select-Object -First 1
$Data = @{ Rows = @(); Note = ''; At = '' }

function Get-Usage {
    try {
        $token = (Get-Content $CredFile -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json).claudeAiOauth.accessToken
        $r = Invoke-RestMethod -Uri 'https://api.anthropic.com/api/oauth/usage' -TimeoutSec 10 -UserAgent 'claude-code' `
            -Headers @{ Authorization = "Bearer $token"; 'anthropic-beta' = 'oauth-2025-04-20' }
        $rows = foreach ($k in @(@('five_hour', '5시간'), @('seven_day', '7일'))) {
            $o = $r.($k[0]); $reset = $null
            if ($o.resets_at) { $reset = [DateTimeOffset]::Parse([string]$o.resets_at) }
            [pscustomobject]@{ Name = $k[1]; Pct = [double]$o.utilization; Reset = $reset }
        }
        return @{ Rows = @($rows); Note = '' }
    } catch { }
    try {
        $s = (Get-Content $HistFile -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json).samples | Select-Object -Last 1
        $rows = @(
            [pscustomobject]@{ Name = '5시간'; Pct = [double]$s.u.fh; Reset = $null },
            [pscustomobject]@{ Name = '7일'; Pct = [double]$s.u.sd; Reset = $null })
        $at = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$s.t).ToOffset($KST).ToString('HH:mm')
        return @{ Rows = $rows; Note = "앱 기록 $at" }
    } catch { }
    return $null
}

function Get-ResetText($reset) {
    if (-not $reset) { return '' }
    if (($reset - [DateTimeOffset]::UtcNow).TotalHours -lt 24) { return $reset.ToOffset($KST).ToString('HH:mm') }
    return $reset.ToOffset($KST).ToString('MM\/dd')
}

function Get-BarColor($pct) {
    if ($pct -ge 90) { return [Drawing.Color]::FromArgb(239, 68, 68) }
    if ($pct -ge 70) { return [Drawing.Color]::FromArgb(250, 204, 21) }
    return [Drawing.Color]::FromArgb(74, 222, 128)
}

$form = New-Object Windows.Forms.Form
$form.FormBorderStyle = 'None'
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'
$form.BackColor = [Drawing.Color]::FromArgb(24, 24, 27)
$form.Opacity = 0.92
$form.Size = New-Object Drawing.Size 270, 78

$path = New-Object Drawing.Drawing2D.GraphicsPath
$path.AddArc(0, 0, 14, 14, 180, 90); $path.AddArc(256, 0, 14, 14, 270, 90)
$path.AddArc(256, 64, 14, 14, 0, 90); $path.AddArc(0, 64, 14, 14, 90, 90); $path.CloseFigure()
$form.Region = New-Object Drawing.Region $path

$area = [Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Location = New-Object Drawing.Point ($area.Right - 286), ($area.Bottom - 94)
try {
    $p = Get-Content $PosFile -Raw -ErrorAction Stop | ConvertFrom-Json
    $pt = New-Object Drawing.Point ([int]$p.X), ([int]$p.Y)
    if ([Windows.Forms.Screen]::AllScreens | Where-Object { $_.WorkingArea.Contains($pt) }) { $form.Location = $pt }
} catch { }

$fontBold = New-Object Drawing.Font('맑은 고딕', 9, [Drawing.FontStyle]::Bold)
$font = New-Object Drawing.Font('맑은 고딕', 9)
$gray = New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(161, 161, 170))
$white = New-Object Drawing.SolidBrush ([Drawing.Color]::White)
$track = New-Object Drawing.SolidBrush ([Drawing.Color]::FromArgb(63, 63, 70))

$form.Add_Paint({ param($sender, $ev)
    $g = $ev.Graphics
    $g.SmoothingMode = 'AntiAlias'
    $title = "클로드 사용량  $($Data.At)"
    if ($Data.Note) { $title += "  · $($Data.Note)" }
    $g.DrawString($title, $fontBold, $gray, 12, 6)
    if (-not $Data.Rows.Count) { $g.DrawString('사용량을 못 읽었어요', $font, $white, 12, 30); return }
    $y = 28
    foreach ($item in $Data.Rows) {
        $g.DrawString($item.Name, $font, $white, 12, $y)
        $g.FillRectangle($track, 62, $y + 5, 110, 9)
        $w = [int](110 * [Math]::Min(100, [Math]::Max(0, $item.Pct)) / 100)
        if ($w -gt 0) { $g.FillRectangle((New-Object Drawing.SolidBrush (Get-BarColor $item.Pct)), 62, $y + 5, $w, 9) }
        $g.DrawString(('{0:N0}%' -f $item.Pct), $font, $white, 178, $y)
        $g.DrawString((Get-ResetText $item.Reset), $font, $gray, 216, $y)
        $y += 22
    }
})

$form.Add_MouseDown({ param($sender, $ev)
    if ($ev.Button -ne 'Left') { return }
    [void][Win.Api]::ReleaseCapture()
    [void][Win.Api]::SendMessage($form.Handle, 0xA1, 2, 0)
    try { @{ X = $form.Left; Y = $form.Top } | ConvertTo-Json | Set-Content $PosFile -Encoding UTF8 } catch { }
})

function Update-Widget {
    $u = Get-Usage
    if ($u) { $Data.Rows = $u.Rows; $Data.Note = $u.Note } else { $Data.Note = '갱신 실패' }
    $Data.At = [DateTimeOffset]::UtcNow.ToOffset($KST).ToString('HH:mm')
    $form.Invalidate()
}

$menu = New-Object Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('지금 새로고침', $null, { Update-Widget })
[void]$menu.Items.Add('끄기', $null, { $form.Close() })
$form.ContextMenuStrip = $menu

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 120000
$timer.Add_Tick({ Update-Widget })
$form.Add_Shown({ Update-Widget; $timer.Start() })

[void][Win.Api]::ShowWindow($form.Handle, 0)
[Windows.Forms.Application]::Run($form)
$mutex.ReleaseMutex()
