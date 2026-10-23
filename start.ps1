param([switch]$Build)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if ($Build) {
    & mvn -B -f backend/pom.xml package
    if ($LASTEXITCODE -ne 0) { throw 'Java build failed' }
}
$jar = Join-Path $projectRoot 'backend/target/config-doctor-backend-0.1.0-SNAPSHOT.jar'
if (!(Test-Path -LiteralPath $jar)) { throw 'Run ./start.ps1 -Build with JDK21 selected first' }
if (!$env:DEEPSEEK_API_KEY) { throw 'Set DEEPSEEK_API_KEY in this terminal before starting' }
New-Item -ItemType Directory -Path (Join-Path $projectRoot 'data') -Force | Out-Null
# 两个进程都只监听本机，密钥通过继承的进程环境传递，不写入脚本。
$javaProcess = Start-Process -FilePath 'java' -ArgumentList @('-jar',"`"$jar`"") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data/java.out.log') -RedirectStandardError (Join-Path $projectRoot 'data/java.err.log')
try {
    Write-Host 'Java starts on 127.0.0.1:18080; UI starts on 127.0.0.1:18081'
    & python -m agent.server
} finally {
    if (!$javaProcess.HasExited) { Stop-Process -Id $javaProcess.Id }
}
