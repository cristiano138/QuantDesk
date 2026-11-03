@echo off
REM QuantDesk 启动脚本（D29）：先起后端（uvicorn，127.0.0.1:8787）再开桌面壳
REM 日志重定向到 data\logs\
chcp 65001 >nul
cd /d "%~dp0"

if not exist data\logs mkdir data\logs

echo [QuantDesk] 启动后端（uvicorn 127.0.0.1:8787）...
start "QuantDesk-Backend" /min cmd /c "uv run python -m quantdesk.app >> data\logs\backend_stdout.log 2>&1"

echo [QuantDesk] 等待后端就绪...
powershell -NoProfile -Command "$t0=Get-Date; while($true){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){ break } }catch{}; if(((Get-Date)-$t0).TotalSeconds -gt 25){ exit 1 }; Start-Sleep -Milliseconds 500 }"
if errorlevel 1 (
    echo [QuantDesk] 后端 25 秒内未就绪，请查看 data\logs\backend_stdout.log
    pause
    exit /b 1
)

echo [QuantDesk] 打开桌面窗口...
uv run python desktop\shell.py
echo [QuantDesk] 壳已退出（后端窗口可关闭或继续运行）
