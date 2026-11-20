# 在「桌面」和「开始菜单」各创建一个 Orange 快捷方式。
#
# 为什么用脚本生成而不是直接放一个 .lnk：
# .lnk 里存的是绝对路径，别人把项目下载到 D:\、E:\、C:\Users\xxx\ 都可能，
# 仓库里塞一个写死路径的快捷方式过去等于坏的。这里的目标路径按本脚本所在位置现算，
# 放到哪个盘、哪一层目录都能正确生成。
#
# 生成之后再移动文件夹会失效，重新运行「创建快捷方式.bat」即可。

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'GUI\启动 Orange.bat'
$icon = Join-Path $root 'GUI\orange.ico'

if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Host "[失败] 没找到启动脚本：$launcher" -ForegroundColor Red
    Write-Host "请确认项目文件夹是完整复制的，再重新运行。"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$places = @(
    @{ Title = '桌面'; Folder = [Environment]::GetFolderPath('Desktop') },
    @{ Title = '开始菜单'; Folder = [Environment]::GetFolderPath('Programs') }
)

foreach ($place in $places) {
    if (-not $place.Folder) { continue }
    $path = Join-Path $place.Folder 'Orange.lnk'
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = (Join-Path $root 'GUI')
    if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = "$icon,0" }
    $shortcut.WindowStyle = 7
    $shortcut.Description = '启动 Orange'
    $shortcut.Save()
    Write-Host ("[{0}] {1}" -f $place.Title, $path)
}

Write-Host ''
Write-Host '想固定到任务栏：开始菜单搜索「Orange」→ 右键 → 固定到任务栏'
Write-Host '（Windows 不允许程序自动往任务栏钉图标，这一步只能手动点一次）'
Write-Host '以后挪动了项目文件夹，重新运行本脚本就能更新快捷方式。'
