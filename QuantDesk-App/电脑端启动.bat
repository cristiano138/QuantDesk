@echo off
REM QuantDesk local launcher (desktop app). Rules for this file: ASCII only + CRLF endings.
REM cmd.exe reads a .bat by byte offset, so bare LF or multi-byte characters desync its parser.
REM This launcher never opens a network port beyond 127.0.0.1 (loopback, local machine only).
chcp 65001 >nul
cd /d "%~dp0.."

if not exist data\logs mkdir data\logs

REM If a healthy local backend is already running, do not start a second one.
powershell -NoProfile -Command "try{ (Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 2).StatusCode }catch{ exit 7 }"
if not errorlevel 1 (
    echo [QuantDesk] Local backend already running on 127.0.0.1:8787, opening window only...
    goto open_shell
)

echo [QuantDesk] Starting local backend on 127.0.0.1:8787 ...
start "QuantDesk-Backend" /min cmd /c "uv run python -m quantdesk.app >> data/logs/backend_out.log 2>&1"

echo [QuantDesk] Waiting for the local backend to become ready...
powershell -NoProfile -Command "$t0=Get-Date; while($true){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){ break } }catch{}; if(((Get-Date)-$t0).TotalSeconds -gt 30){ exit 1 }; Start-Sleep -Milliseconds 500 }"
if errorlevel 1 (
    echo [QuantDesk] Backend not ready within 30s. Check data\logs\backend_out.log
    pause
    exit /b 1
)

:open_shell
echo [QuantDesk] Opening desktop window ...
uv run python desktop\shell.py
echo [QuantDesk] Window closed. The backend can stay running or be closed.
pause
