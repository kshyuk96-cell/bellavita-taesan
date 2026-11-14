$ErrorActionPreference = 'Stop'
$targetRoom = '기획방'
$reportDirectory = $PSScriptRoot
try {
    $probe = Join-Path $reportDirectory '.diagnostic-write-check.tmp'
    [System.IO.File]::WriteAllText($probe, 'ok')
    Remove-Item -LiteralPath $probe
} catch {
    $reportDirectory = Join-Path $env:TEMP 'TaesanDiagnostics'
    [void][System.IO.Directory]::CreateDirectory($reportDirectory)
}
$sharePath = Join-Path $reportDirectory 'SHARE_THIS_STATUS.txt'
$jsonPath = Join-Path $reportDirectory 'diagnostic-report.json'
$localLog = Join-Path $reportDirectory 'diagnostic-detail-local.txt'
$report = [ordered]@{
    schema = 'taesan-windows-preflight-v2'
    generated_at = [DateTime]::UtcNow.ToString('o')
    target_room = $targetRoom
    status = 'not_checked'
    kakaotalk_running = $false
    matching_room_count = 0
    room_minimized = $false
    input_candidates = @()
    error_code = ''
    python_required = $false
    chat_body_read = $false
    clipboard_read = $false
    chat_sent = $false
    ready_for_send = $false
}
try {
    if ($env:OS -ne 'Windows_NT') { throw 'Windows required' }
    Write-Host '태산이 Windows 진단을 시작합니다. 메시지를 읽거나 보내지 않습니다.'
    $processes = @(Get-Process -Name KakaoTalk -ErrorAction SilentlyContinue)
    $report.kakaotalk_running = ($processes.Count -gt 0)
    if ($processes.Count -eq 0) {
        $report.status = 'kakaotalk_not_running'
    } else {
        if (-not ('TaesanRoomProbe' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class TaesanRoomProbe {
    public delegate bool WindowCallback(IntPtr h, IntPtr arg);
    [DllImport("user32.dll")] public static extern bool EnumWindows(WindowCallback callback, IntPtr arg);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    public static IntPtr[] Find(string room, uint[] pids) {
        var allowed = new HashSet<uint>(pids);
        var result = new List<IntPtr>();
        WindowCallback callback = delegate(IntPtr h, IntPtr arg) {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (!allowed.Contains(pid) || !IsWindowVisible(h)) return true;
            int length = GetWindowTextLength(h);
            if (length < 1 || length > 1024) return true;
            var text = new StringBuilder(length + 1);
            GetWindowText(h, text, text.Capacity);
            if (String.Equals(text.ToString(), room, StringComparison.Ordinal)) result.Add(h);
            return true;
        };
        EnumWindows(callback, IntPtr.Zero);
        return result.ToArray();
    }
}
'@
        }
        [uint32[]]$pids = @($processes | ForEach-Object { [uint32]$_.Id })
        $roomWindows = @([TaesanRoomProbe]::Find($targetRoom, $pids))
        $report.matching_room_count = $roomWindows.Count
        if ($roomWindows.Count -eq 0) {
            $report.status = 'planning_room_not_open'
        } elseif ($roomWindows.Count -gt 1) {
            $report.status = 'planning_room_ambiguous'
        } else {
            $report.room_minimized = [TaesanRoomProbe]::IsIconic($roomWindows[0])
            if ($report.room_minimized) {
                $report.status = 'planning_room_minimized'
            } else {
                Add-Type -AssemblyName UIAutomationClient
                Add-Type -AssemblyName UIAutomationTypes
                $window = [System.Windows.Automation.AutomationElement]::FromHandle($roomWindows[0])
                $condition = New-Object -TypeName System.Windows.Automation.PropertyCondition -ArgumentList @([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
                $controls = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
                foreach ($control in $controls) {
                    $current = $control.Current
                    $report.input_candidates += [ordered]@{
                        automation_id = $current.AutomationId
                        control_type = $current.ControlType.ProgrammaticName
                        class_name = $current.ClassName
                        enabled = $current.IsEnabled
                        offscreen = $current.IsOffscreen
                    }
                }
                $report.status = if ($report.input_candidates.Count -gt 0) { 'input_candidates_need_review' } else { 'input_control_not_exposed' }
            }
        }
    }
} catch {
    $report.status = 'diagnostic_error'
    $report.error_code = $_.Exception.GetType().Name
    try { [System.IO.File]::WriteAllText($localLog, $_.Exception.ToString(), (New-Object System.Text.UTF8Encoding($true))) } catch {}
} finally {
    $lines = @(
        '태산이 PC 진단 결과 / 전송하지 않음',
        ('상태 코드: ' + $report.status),
        ('카카오톡 실행: ' + $report.kakaotalk_running),
        ('기획방 창 수: ' + $report.matching_room_count),
        ('최소화 여부: ' + $report.room_minimized),
        ('입력창 후보 수: ' + $report.input_candidates.Count),
        '대화 본문·입력 초안·클립보드는 읽지 않았습니다.',
        '고객 식별·주문 수집·카톡 전송·원격 접속 성공을 뜻하지 않습니다.',
        '다음: README_KO.txt의 상태 코드 안내를 확인하세요.'
    )
    try {
        [System.IO.File]::WriteAllLines($sharePath, $lines, (New-Object System.Text.UTF8Encoding($true)))
        [System.IO.File]::WriteAllText($jsonPath, ($report | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($true)))
        $lines | ForEach-Object { Write-Host $_ }
        Write-Host ('결과 파일: ' + $sharePath)
        try { Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $sharePath + '"') } catch {}
    } catch {
        Write-Host '결과 파일을 저장하지 못했습니다. 이 콘솔의 상태 코드를 확인하세요.'
    }
}
