$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $pythonPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $command = Get-Command python.exe -ErrorAction SilentlyContinue
        $pythonPath = if ($command) { $command.Source } else { $null }
    }
    if (-not $pythonPath) {
        Write-Host 'Python이 설치되어 있지 않습니다.'
        Write-Host 'Python 3.12 설치 후 이 실행 파일을 다시 열어주세요.'
        exit 1
    }
    & $pythonPath -c 'import sys; assert sys.version_info >= (3,10); import tkinter,pywinauto,win32clipboard'
    if ($LASTEXITCODE -ne 0) {
        Write-Host '실행에 필요한 구성요소가 없습니다. PowerShell에서 아래 명령으로 설치하세요.'
        Write-Host 'python -m pip install -r requirements-windows.txt'
        exit 1
    }
    & $pythonPath (Join-Path $PSScriptRoot 'control_panel.py')
    exit $LASTEXITCODE
} catch {
    Write-Host '실행을 중단했습니다:' $_.Exception.GetType().Name
    exit 1
}
