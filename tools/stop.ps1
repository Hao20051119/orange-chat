# 停止本项目相关的后台进程（wx_service / reply_worker / 控制台）
$root = Split-Path -Parent $PSScriptRoot
$entryPoints = @(
    'wxbot\wx_service.py'
    'agent\reply_worker.py'
    'GUI\app.py'
    'GUI\launch.py'
) | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $root $_)) }

$targets = Get-CimInstance Win32_Process | Where-Object {
    if ($_.Name -notmatch '^pythonw?(?:\d+(?:\.\d+)*)?\.exe$' -or -not $_.CommandLine) {
        return $false
    }
    # 启动脚本和 GUI 都把脚本的绝对路径直接传给 Python。
    if ($_.CommandLine -notmatch '^\s*(?:"[^"]+"|\S+)\s+(?:"(?<entry>[^"]+)"|(?<entry>[^"\s]+))(?=\s|$)') {
        return $false
    }
    $entry = $Matches['entry']
    if ($entry -notmatch '^(?:[a-z]:[\\/]|\\\\)') { return $false }
    try {
        $entryPoints -contains [IO.Path]::GetFullPath($entry)
    } catch {
        $false
    }
}

if (-not $targets) {
    Write-Host "没有发现正在运行的本项目进程。"
    exit 0
}

foreach ($p in $targets) {
    try {
        Write-Host ("  停止 PID {0}" -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    } catch {
        Write-Host ("  停止 PID {0} 失败：{1}" -f $p.ProcessId, $_.Exception.Message)
    }
}
Write-Host "完成。"
