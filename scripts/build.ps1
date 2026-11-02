param(
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$CreateDesktopShortcut
)

# 任一步失败立即停止，不把后续成功输出冒充整个构建通过。
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path # 用户可审查的源码根目录。
$Workspace = $Root # 独立源码仓库，不依赖作者电脑的 outputs/work 布局。
$Scratch = Join-Path $Workspace '.build' # 中间产物仅写入仓库内 .build。
$Venv = Join-Path $Workspace '.venv' # 构建专用虚拟环境，不修改系统 Python。
$Python = Join-Path $Venv 'Scripts\python.exe' # 本次构建使用的解释器。
$Verification = Join-Path $Root 'verification' # 用户可查看的真实验收记录。
$Native = Join-Path $Root 'bin\XiaoguaiCollab.exe' # 最终原生窗口程序。

# Program 为可执行文件，Arguments 为独立参数；不拼接到命令解释器执行。
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "构建步骤失败，退出码 $LASTEXITCODE" }
}

New-Item -ItemType Directory -Path $Scratch,$Verification -Force | Out-Null
if (Test-Path -LiteralPath $Native) {
    # 不自动结束用户正在使用的窗口，也不覆盖运行中的 EXE。
    $Running = Get-Process -Name XiaoguaiCollab -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $Native } # 当前目录已运行的应用。
    if ($Running) { throw '请先关闭小怪协作窗口再构建；不会自动终止正在进行的协作。' }
}
if (-not (Test-Path -LiteralPath $Python)) {
    $SystemPython = (Get-Command python.exe -ErrorAction Stop).Source # 用户已安装的 Python。
    Invoke-Checked $SystemPython @('-m','venv',$Venv)
}
if (-not $SkipInstall) { Invoke-Checked $Python @('-m','pip','install','--requirement',(Join-Path $Root 'requirements.lock.txt')) }

Push-Location -LiteralPath $Root
try {
    if (-not $SkipTests) {
        Invoke-Checked $Python @('-m','ruff','check','backend','main.py','tests','scripts')
        Invoke-Checked $Python @('-m','ruff','format','--check','backend','main.py','tests','scripts')
        Invoke-Checked $Python @('-m','pytest','tests','-q','--tb=short',('--junitxml=' + (Join-Path $Verification 'python-tests.xml')))
        Push-Location -LiteralPath (Join-Path $Root 'sample-project')
        try { Invoke-Checked $Python @('-m','unittest','discover','-s','tests','-v') }
        finally { Pop-Location }
    }
    $Node = Get-Command node.exe -ErrorAction SilentlyContinue # 有 Node 的开发机额外校验网页脚本语法；运行程序本身不需要 Node。
    if ($Node) { Invoke-Checked $Node.Source @('--check','web\app.js') }
    # 不采集 mcp.cli，避免为运行时不需要的开发 CLI 加入 typer 等额外依赖。
    Invoke-Checked $Python @('scripts\restore_webview2.py')
    Invoke-Checked $Python @('-m','PyInstaller','--noconfirm','--onedir','--console','--log-level','WARN','--name','collab-service','--distpath',(Join-Path $Root 'bin'),'--workpath',(Join-Path $Scratch 'pyinstaller'),'--specpath',$Scratch,'--collect-submodules','mcp.server','--collect-submodules','mcp.client','--collect-submodules','uvicorn','--copy-metadata','mcp','--copy-metadata','mcp-types','main.py')
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe' # VS 官方安装定位器。
    $MSBuild = (& $VsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1) # 现有 C++ 构建工具。
    if (-not $MSBuild) { throw '未找到 VS2022 C++ 工具链；需安装桌面 C++ 与 MFC 组件。' }
    Invoke-Checked $MSBuild @('native\XiaoguaiCollab.vcxproj','/p:Configuration=Release','/p:Platform=x64','/m','/nologo','/v:minimal')
    Invoke-Checked $Python @('scripts\collect_licenses.py')
    # 原生自检经过实际打包 EXE、Job Object、DPAPI、WinHTTP 和两个真实 MCP 身份。
    $Check = Join-Path $Workspace ('.build\checks\' + [Guid]::NewGuid().ToString('N')) # 每次验收使用新目录，不能命中旧成功报告。
    $CheckData = Join-Path $Check '数据目录 with spaces' # 验证中文与空格路径。
    $CheckProject = Join-Path $Check '测试项目 with spaces' # 独立临时项目，不触碰用户代码。
    $Report = Join-Path $Verification 'native-selftest.json' # 本次原生验收输出。
    New-Item -ItemType Directory -Path $CheckProject -Force | Out-Null
    $SelfTest = Start-Process -FilePath $Native -ArgumentList @('--self-test',('"' + $CheckData + '"'),('"' + $CheckProject + '"'),('"' + $Report + '"')) -WindowStyle Hidden -Wait -PassThru # 仅测试模式隐藏，无交互窗口。
    if ($SelfTest.ExitCode -ne 0 -or -not (Get-Content -LiteralPath $Report -Raw -Encoding UTF8 | ConvertFrom-Json).passed) { throw '原生打包验收未通过，请查看 verification/native-selftest.json' }
    # 布局夹具只显示本程序自己的测试网页，不连接后端或模型，验证三个真实浏览器视口和四种页面。
    $LayoutReport = Join-Path $Verification 'web-ui-layout.json' # WebView2 实际 DOM 和响应式布局检查。
    $LayoutTest = Start-Process -FilePath $Native -ArgumentList @('--ui-layout-test',('"' + $LayoutReport + '"')) -WindowStyle Hidden -Wait -PassThru # 仅本程序内部测试。
    # Windows PowerShell 5.1 默认使用本地编码，必须明确按 UTF-8 读取中文 JSON。
    if ($LayoutTest.ExitCode -ne 0 -or -not (Get-Content -LiteralPath $LayoutReport -Raw -Encoding UTF8 | ConvertFrom-Json).passed) { throw '网页界面布局检查未通过，请查看 verification/web-ui-layout.json' }
    Invoke-Checked $Python @('scripts\test_ui_mcp_native.py')
    Get-FileHash -Algorithm SHA256 -LiteralPath $Native,(Join-Path $Root 'bin\collab-service\collab-service.exe') | Select-Object Path,Algorithm,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Verification 'binary-sha256.json') -Encoding utf8
    if ($CreateDesktopShortcut) {
        $ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) '小怪 AI 协作中心.lnk' # 用户明确要求的桌面入口。
        $Shell = New-Object -ComObject WScript.Shell # 仅用于创建快捷方式，不执行终端命令。
        $Shortcut = $Shell.CreateShortcut($ShortcutPath) # 同名时先验证目标，避免覆盖用户其他程序。
        if ((Test-Path -LiteralPath $ShortcutPath) -and $Shortcut.TargetPath -ne $Native) { throw '桌面已有同名但不同目标的快捷方式，未覆盖。' }
        $Shortcut.TargetPath = $Native
        $Shortcut.WorkingDirectory = $Root
        $Shortcut.IconLocation = $Native + ',0'
        $Shortcut.Description = '本机多 AI MCP 通信、协作与对话查看'
        $Shortcut.Save()
    }
    Write-Output '构建与原生验收完成。打开桌面“小怪 AI 协作中心”即可使用；不会自动调用模型。'
}
finally { Pop-Location }
