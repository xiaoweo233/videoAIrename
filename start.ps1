# VAIR · 视频 AI 重命名助手 —— 一键启动（真正的逻辑都在这里）
#
# 为什么逻辑放在 .ps1 而不是全塞进 .bat：
#   cmd.exe 是按「当前控制台代码页」逐行解码批处理文件的，而 goto / call :label
#   还要按字节偏移回头找标签。中文 + chcp 65001 + 标签三者混在一个 .bat 里，
#   实测会让 cmd 在错误的字节位置断行（报一堆「不是内部或外部命令」）。
#   所以 start.bat 只留纯 ASCII 的两行壳，中文和判断都交给 PowerShell。
#
# Python 用**便携版**，解压到 项目\python\：
#   · 不写注册表、不装进 C 盘、不需要管理员权限
#   · 整个项目目录拷到别的机器照样能跑（除了 AI 模型那种大东西）
#   官方安装包做不到这点 —— 它只会装进 %LOCALAPPDATA%\Programs\Python 或 Program Files。
#
# 参数原样转给 后端\run.py：
#   start.bat --lan            局域网可访问（手机连同一 Wi-Fi）
#   start.bat --port 8080      换端口
#   start.bat --no-autostart   不自动拉起 AI 服务（调试前端时省显存）
#   start.bat --selfcheck      只打印环境自检，不启动
#   start.bat --setup-python   只准备便携版 Python，不启动

[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Passthru)

$ErrorActionPreference = 'Continue'
# 故意不用 'Stop'：这里大半是原生命令（python.exe / curl.exe），PowerShell 5.1 在
# 重定向 stderr（2>&1、2>$null）时会把原生命令的 stderr 当成错误抛出来，
# 而后端 uvicorn 的日志恰好都写 stderr —— 用 Stop 会出现「后端刚起来就把启动器打挂」。
# 该管的错误都在下面显式判断了。
$ProgressPreference = 'SilentlyContinue'        # Invoke-WebRequest 的进度条会拖慢几十倍
try {
  [Console]::OutputEncoding = [Text.Encoding]::UTF8   # 中文正常显示
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch { }

$Root      = $PSScriptRoot
$Backend   = Join-Path $Root '后端'
$PyVersion = '3.12.10'
$ZipName   = "python-$PyVersion-embed-amd64.zip"
$PyMirrors = @(
  "https://mirrors.huaweicloud.com/python/$PyVersion/$ZipName",
  "https://mirrors.aliyun.com/python-release/windows/$ZipName"
)
# 官方 get-pip.py 优先；若它的下载不可用，就直接从国内镜像下载 pip wheel。
# 不能退回阿里云的 get-pip.py：它捆绑的旧 pip 依赖 Python 3.12 已移除的 distutils。
$PipMirrors = @('https://bootstrap.pypa.io/get-pip.py')
$PipWheelIndexes = @(
  'https://pypi.tuna.tsinghua.edu.cn/simple/pip/',
  'https://mirrors.aliyun.com/pypi/simple/pip/'
)
$PipIndex = 'https://pypi.tuna.tsinghua.edu.cn/simple'

# 便携版 Python 的位置。VAIR_PYTHON_DIR 是给「想把它放到别的盘」的人留的口子。
$PyDir = if ($env:VAIR_PYTHON_DIR) { $env:VAIR_PYTHON_DIR } else { Join-Path $Root 'python' }
$PyExe = Join-Path $PyDir 'python.exe'

function Info { param([string] $Text) Write-Host "  $Text" }
function Ok   { param([string] $Text) Write-Host "  [OK] $Text" -ForegroundColor Green }
function Warn { param([string] $Text) Write-Host "  [!] $Text" -ForegroundColor Yellow }
function Bad  { param([string] $Text) Write-Host "  [X] $Text" -ForegroundColor Red }

# 微软商店的 python.exe 占位别名「存在」但只会打印一句提示，所以必须真跑一次
function Test-PythonExe {
  param([string] $Path)
  if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
  & $Path -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Find-Python {
  $found = @($PyExe, (Join-Path $Root '后端\python\python.exe'), (Join-Path $Root 'AI\python\python.exe'))

  # py 启动器 / PATH 里的 python：都要问出真实路径（后面统一当文件调用）
  foreach ($name in @('py', 'python')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $real = $null
    try { $real = & $cmd.Source -c "import sys;print(sys.executable)" 2>$null } catch { $real = $null }
    if ($real) { $found += ([string]$real).Trim() }
  }

  # 常见安装位置：用户级优先，再系统级
  foreach ($pattern in @(
      (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
      (Join-Path $env:ProgramFiles 'Python3*\python.exe'),
      'C:\Python3*\python.exe')) {
    $found += (Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
               ForEach-Object { $_.FullName })
  }

  foreach ($candidate in $found) {
    if (Test-PythonExe $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
  }
  return $null
}

function Get-File {
  param([string] $Url, [string] $Out)
  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl) {
    # Windows 10 1803 起自带 curl.exe，比 Invoke-WebRequest 快不少
    & $curl.Source -L --fail --silent --show-error --output $Out $Url
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $Out)) { return $true }
  }
  try {
    Invoke-WebRequest -Uri $Url -OutFile $Out -UseBasicParsing -TimeoutSec 900
    return $true
  } catch {
    Warn "下载没成功：$($_.Exception.Message)"
    return $false
  }
}

function Confirm-PortablePython {
  Write-Host ''
  Info '项目里还没有 Python，可以现在装一个便携版（约 11 MB）：'
  Info "  $PyDir"
  Info '便携版全在项目目录里 —— 不写注册表、不装进 C 盘、不需要管理员权限，'
  Info '整个文件夹拷到别的电脑也照样能用。'
  Write-Host ''
  try { $answer = Read-Host '  现在下载便携版 Python 吗？[Y/n]' } catch { $answer = 'n' }
  return (-not $answer) -or ($answer -match '^[Yy]')
}

function Install-PortablePython {
  New-Item -ItemType Directory -Force -Path $PyDir | Out-Null
  $zip = Join-Path $env:TEMP $ZipName
  Remove-Item $zip -Force -ErrorAction SilentlyContinue

  Write-Host ''
  Info '正在下载便携版 Python…'
  $got = $false
  foreach ($url in $PyMirrors) {
    Info "  $url"
    if (Get-File $url $zip) { $got = $true; break }
  }
  if (-not $got) {
    Bad '镜像都没下动，请检查网络后重试。'
    return $false
  }
  if ((Get-Item -LiteralPath $zip).Length -lt 8MB) {
    Bad '下到的文件不完整（像是个错误页），已放弃。'
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    return $false
  }

  Info "解压到 $PyDir"
  Expand-Archive -LiteralPath $zip -DestinationPath $PyDir -Force
  Remove-Item $zip -Force -ErrorAction SilentlyContinue

  # 便携版默认不带 pip，也不把 Lib\site-packages 放进 sys.path（靠 python3xx._pth 控制）。
  # 这两件事都得自己做，否则 run.py 装依赖时 `python -m pip` 会直接报「找不到模块」。
  $pth = Get-ChildItem -LiteralPath $PyDir -Filter 'python3*._pth' -ErrorAction SilentlyContinue |
         Select-Object -First 1
  if ($pth) {
    $lines = @(Get-Content -LiteralPath $pth.FullName)
    $lines = $lines -replace '^#\s*import site', 'import site'
    if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
    Set-Content -LiteralPath $pth.FullName -Value $lines -Encoding ASCII
  } else {
    Warn "没找到 $PyDir 里的 python3xx._pth，pip 可能装不上。"
  }

  if (-not (Install-Pip)) {
    Warn 'pip 暂时没装上。仍会启动本机诊断页，并在页面说明如何修复。'
    return $false
  }
  return $true
}

# 先验 pip 是否已经能用（解压到一半 / 上次下载中断时可能缺 pip）。
function Test-Pip {
  if (-not (Test-Path -LiteralPath $PyExe -PathType Leaf)) { return $false }
  # 先把退出码置为失败：命令根本不存在时 PowerShell 不会更新 $LASTEXITCODE，
  # 留着上一次的值会把「没跑起来」误判成「pip 可用」。
  $global:LASTEXITCODE = 1
  & $PyExe -m pip --version 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

# 下载 pip wheel（标准 zip 格式），从国内 simple 索引筛出 >=24 版本，
# 按索引提供的 SHA-256 校验后才解压到项目内，不执行下载的代码。
function Install-PipWheel {
  foreach ($index in $PipWheelIndexes) {
    try {
      $page = (Invoke-WebRequest -Uri $index -UseBasicParsing -TimeoutSec 30).Content
      $matches_ = [regex]::Matches($page, 'href=["''][^"'']*pip-(\d+)\.(\d+)(?:\.(\d+))?-py3-none-any\.whl#sha256=[0-9a-f]{64}["'']', 'IgnoreCase')
      $best = $null
      foreach ($match in $matches_) {
        $version = [version]::new([int]$match.Groups[1].Value, [int]$match.Groups[2].Value,
                                  $(if ($match.Groups[3].Success) { [int]$match.Groups[3].Value } else { 0 }))
        if ($version.Major -ge 24 -and (!$best -or $version -gt $best.Version)) {
          $href = $match.Value.Substring(6, $match.Value.Length - 7)
          $best = @{ Version = $version; Href = $href }
        }
      }
      if (-not $best) { throw '镜像页里找不到兼容 Python 3.12 的 pip wheel' }
      $parts = $best.Href -split '#sha256='
      $wheelUrl = ([Uri]::new([Uri]$index, $parts[0])).AbsoluteUri
      $wheel = Join-Path $env:TEMP ("vair-pip-$($best.Version).whl")
      Info "  国内镜像：$index（pip $($best.Version)）"
      if (-not (Get-File $wheelUrl $wheel)) { continue }
      $hash = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
      if ($hash -ne $parts[1].ToLowerInvariant()) {
        Remove-Item -LiteralPath $wheel -Force -ErrorAction SilentlyContinue
        throw 'pip wheel 校验失败'
      }
      $site = Join-Path $PyDir 'Lib\site-packages'
      New-Item -ItemType Directory -Force -Path $site | Out-Null
      # Expand-Archive 只接受 .zip 后缀；先复制到临时 zip 再展开。
      $zipWheel = "$wheel.zip"
      Copy-Item -LiteralPath $wheel -Destination $zipWheel -Force
      try { Expand-Archive -LiteralPath $zipWheel -DestinationPath $site -Force }
      finally {
        Remove-Item -LiteralPath $zipWheel, $wheel -Force -ErrorAction SilentlyContinue
      }
      if (Test-Pip) { return $true }
    } catch {
      Warn "pip wheel 备援失败：$($_.Exception.Message)"
    }
  }
  return $false
}

function Install-Pip {
  if (Test-Pip) { return $true }
  Write-Host ''
  Info '正在补 pip（只装进项目里的便携版 Python）…'
  $env:PIP_INDEX_URL = $PipIndex
  $env:PIP_TRUSTED_HOST = ([Uri] $PipIndex).Host
  try {
    foreach ($url in $PipMirrors) {
      $script = Join-Path $env:TEMP 'vair-get-pip.py'
      Remove-Item $script -Force -ErrorAction SilentlyContinue
      if (-not (Get-File $url $script)) { continue }
      Info "  引导脚本：$(([Uri] $url).Host)"
      try { & $PyExe $script --no-warn-script-location --disable-pip-version-check }
      finally { Remove-Item $script -Force -ErrorAction SilentlyContinue }
      if (Test-Pip) { return $true }
      Warn 'get-pip.py 没装成功，改用国内镜像的 pip wheel。'
    }
    return (Install-PipWheel)
  } finally {
    Remove-Item Env:PIP_INDEX_URL, Env:PIP_TRUSTED_HOST -ErrorAction SilentlyContinue
  }
}

# ---------------------------------------------------------------- 开跑

Write-Host ''
Write-Host '  ============================================================'
Write-Host '   VAIR · 视频 AI 重命名助手'
Write-Host '  ============================================================'
Info "项目目录：$Root"

if (-not (Test-Path -LiteralPath (Join-Path $Backend 'run.py'))) {
  Write-Host ''
  Bad '这里不是项目根目录：找不到 后端\run.py'
  Info '请把 start.bat 放在项目根目录（和 前端\ 后端\ 同级）再运行。'
  exit 1
}

$setupOnly = $Passthru -contains '--setup-python'
$runArgs = @($Passthru | Where-Object { $_ -ne '--setup-python' })

$python = $null
if (Test-PythonExe $PyExe) {
  $python = (Resolve-Path -LiteralPath $PyExe).Path
  if (-not (Test-Pip)) {
    Warn '便携版 Python 已存在，但 pip 不可用；先修复 pip。'
    if (-not (Install-Pip)) { Warn 'pip 尚未就绪；继续启动，后端会显示诊断页。' }
  }
} elseif ($setupOnly -or (Confirm-PortablePython)) {
  # 即使 pip 没装上，Python 本身还能运行；让后端进入诊断模式而非假装成功。
  $null = Install-PortablePython
  if (Test-PythonExe $PyExe) { $python = (Resolve-Path -LiteralPath $PyExe).Path }
  if ($setupOnly -and -not $python) {
    Bad '便携版 Python 没装成。'
    exit 1
  }
}

if (-not $python) { $python = Find-Python }
if (-not $python) {
  Write-Host ''
  Bad '没有可用的 Python（需要 3.10 或更高版本）。'
  Info '手动准备也行，二选一：'
  Info "  · 把 $ZipName 解压到 $PyDir"
  Info '  · 自己装官方版 Python 3.10+，安装时勾上「Add python.exe to PATH」'
  Info "    官方版下载：https://mirrors.huaweicloud.com/python/$PyVersion/python-$PyVersion-amd64.exe"
  exit 1
}

Write-Host ''
Ok "Python：$python"
& $python -V

if ($setupOnly) { Info '（--setup-python 到此为止，没有启动后端）'; exit 0 }

# AI 资产（约 10 GB）缺了不挡启动，但处理视频时会失败 —— 早点说一声
$llama = Get-ChildItem -Path (Join-Path $Root 'AI\llama-*\llama-server.exe') -ErrorAction SilentlyContinue
if (-not $llama) {
  Write-Host ''
  Warn '还没放 AI 资产（llama.cpp 运行时 + 视觉/转写模型，约 10 GB）。'
  Info '  启动后打开页面 → 设置 → 系统 → 点「补齐 AI 依赖与模型」（走 HF-Mirror，支持断点续传）；'
  Info '  要连别的机器上现成的推理服务，就在 设置 → AI 与提示词 里改「接口地址」，不用下这些。'
  Info '  ffmpeg / exiftool 缺了会在依赖装好后自动补；网络不通仍可先打开页面。'
}

Write-Host ''
Info '------------------------------------------------------------'
Info '缺的 Python 依赖会自动装进 后端\libs\（首次启动慢一点，正常）'
Info '启动后浏览器访问 http://127.0.0.1:8000/ ，按 Ctrl+C 停止'
Info '------------------------------------------------------------'
Write-Host ''

Push-Location $Backend
try {
  & $python run.py @runArgs
  $code = $LASTEXITCODE
} finally {
  Pop-Location
}

Write-Host ''
Info "后端已退出（退出码 $code）。"
if ($code -ne 0) { Warn '上面应该写了原因；看不懂就把这段输出发我。' }
exit $code
