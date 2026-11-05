# 盘中盯盘 · 一键启动
# 用法：.\start.ps1            # 常驻盯盘并推送
#       .\start.ps1 -DryRun    # 常驻但不推送，只打印卡片

param(
    [switch]$DryRun,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# 从 .env 读配置（存在才读）
if (Test-Path ".env") {
    Get-Content ".env" -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"').Trim("'"))
        }
    }
}

if (-not $DryRun) {
    $configured = @(
        $env:FEISHU_WEBHOOK, $env:DINGTALK_WEBHOOK, $env:WECOM_WEBHOOK,
        $env:BARK_URL, $env:CUSTOM_WEBHOOK
    ) | Where-Object { $_ }
    if (-not $configured -and -not ($env:TG_TOKEN -and $env:TG_CHAT_ID)) {
        Write-Host "未配置任何推送渠道，自动切换到 dry-run 模式。" -ForegroundColor Yellow
        Write-Host "在 .env 里填 FEISHU_WEBHOOK / DINGTALK_WEBHOOK / WECOM_WEBHOOK 等。" -ForegroundColor Yellow
        $DryRun = $true
    }
}

if ($Once) {
    $args = @("run.py", "--once")
} else {
    $args = @("run.py", "--loop")
}
if (-not $DryRun) { $args += "--push" } else { $args += "--dry-run" }

Write-Host "启动：python $($args -join ' ')" -ForegroundColor Cyan
python @args
