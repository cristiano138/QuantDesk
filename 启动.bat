@echo off
REM QuantDesk launcher: start backend (uvicorn on 127.0.0.1:8787), then open the desktop shell.
REM Keep this file ASCII-only with CRLF endings. cmd.exe reads a .bat by byte offset, so
REM multi-byte UTF-8 comments and bare LF line endings can desync its parser and make it
REM execute a fragment of a comment (seen as "'esk.app' is not recognized").
chcp 65001 >/dev/null
cd /d "%~dp0"

if not exist data\logs mkdir data\logs

REM A second backend cannot bind the port; if one is already healthy, just open the window.
powershell -NoProfile -Command "try{ (Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 2).StatusCode }catch{ exit 7 }"
if not errorlevel 1 (
    echo [QuantDesk] Backend already running on 127.0.0.1:8787, opening window only...
    goto open_shell
)

echo [QuantDesk] Starting backend on 127.0.0.1:8787 ...
start "QuantDesk-Backend" /min cmd /c "uv run python -m quantdesk.app >> data\logsackend_stdout.log 2>&1"

echo [QuantDesk] Waiting for backend to become ready...
powershell -NoProfile -Command "$t0=Get-Date; while($true){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){ break } }catch{}; if(((Get-Date)-$t0).TotalSeconds -gt 25){ exit 1 }; Start-Sleep -Milliseconds 500 }"
if errorlevel 1 (
    echo [QuantDesk] Backend not ready within 25s. Check data\logsackend_stdout.log
    pause
    exit /b 1
)

:open_shell
echo [QuantDesk] Opening desktop window ...
uv run python desktop\shell.py
echo [QuantDesk] Shell exited. The backend window can be closed or left running.
