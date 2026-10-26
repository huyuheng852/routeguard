# RouteGuard 一键启动：自动定位 Python 并设置 PYTHONPATH
# 用法：.\start.ps1                      启动网页版
#       .\start.ps1 plan --text "..."    走命令行

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $here "src"
$env:PYTHONIOENCODING = "utf-8"

function Test-Python($exe) {
    try {
        $v = & $exe -c "import sys;print(sys.version_info[0],sys.version_info[1])" 2>$null
        if ($v) {
            $parts = $v -split ' '
            return ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10)
        }
    } catch { }
    return $false
}

function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($c -and (Test-Python $c.Source)) { return $c.Source }
    }
    $candidates = @(
        "$env:CONDA_PREFIX\python.exe",
        "C:\ProgramData\miniconda3\envs\course5\python.exe",
        "C:\ProgramData\miniconda3\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p) -and (Test-Python $p)) { return $p }
    }
    foreach ($root in @("C:\ProgramData\miniconda3\envs", "$env:USERPROFILE\miniconda3\envs", "$env:USERPROFILE\anaconda3\envs")) {
        if (Test-Path $root) {
            $hit = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName "python.exe" } |
                Where-Object { Test-Path $_ } | Select-Object -First 1
            if ($hit -and (Test-Python $hit)) { return $hit }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host ""
    Write-Host "找不到 Python 3.10 或更高版本。" -ForegroundColor Red
    Write-Host "请任选一种："
    Write-Host "  1) 安装 Python：https://www.python.org/downloads/  （安装时勾选 Add to PATH）"
    Write-Host "  2) 如果装了 conda，先运行：conda activate course5"
    Write-Host "  3) 或者把 Python 的完整路径发给我，我帮你写进这个脚本"
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

Write-Host "使用 Python: $python" -ForegroundColor DarkGray
$ver = & $python -c "import routeguard;print(routeguard.__version__)" 2>$null
if (-not $ver) {
    Write-Host "无法导入 routeguard，请确认本脚本和 src 目录在同一个文件夹。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "RouteGuard v$ver" -ForegroundColor Green

if ($args.Count -gt 0) {
    & $python -m routeguard @args
} else {
    Write-Host "正在启动网页版，浏览器会自动打开 http://127.0.0.1:8765" -ForegroundColor Cyan
    & $python -m routeguard web
}
