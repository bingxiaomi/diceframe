@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   DiceFrame WebUI
echo   Open http://localhost:18000 after startup.
echo ========================================
echo.

if exist ".venv314\Scripts\python.exe" goto run_venv314
if exist ".venv\Scripts\python.exe" goto run_venv
rem Also accept a venv one level up: cloning into a subdirectory with the venv
rem sitting beside it is a common layout, and silently falling through to the
rem system python there produces a confusing typing.NotRequired ImportError.
if exist "..\.venv314\Scripts\python.exe" goto run_parent_venv314
if exist "..\.venv\Scripts\python.exe" goto run_parent_venv
goto run_system_python

:run_venv314
".venv314\Scripts\python.exe" scripts\start_webui.py
set "WEBUI_EXIT_CODE=%ERRORLEVEL%"
goto finished

:run_venv
".venv\Scripts\python.exe" scripts\start_webui.py
set "WEBUI_EXIT_CODE=%ERRORLEVEL%"
goto finished

:run_parent_venv314
"..\.venv314\Scripts\python.exe" scripts\start_webui.py
set "WEBUI_EXIT_CODE=%ERRORLEVEL%"
goto finished

:run_parent_venv
"..\.venv\Scripts\python.exe" scripts\start_webui.py
set "WEBUI_EXIT_CODE=%ERRORLEVEL%"
goto finished

:run_system_python
echo.
echo [WARN] No .venv or .venv314 found - falling back to "python" on PATH.
echo        DiceFrame needs Python 3.11 or newer. On 3.10 this fails with:
echo        ImportError: cannot import name 'NotRequired' from 'typing'
echo.
python scripts\start_webui.py
set "WEBUI_EXIT_CODE=%ERRORLEVEL%"

:finished
if not "%WEBUI_EXIT_CODE%"=="0" goto failed
echo.
echo DiceFrame WebUI stopped.
pause
exit /b 0

:failed
echo.
echo DiceFrame WebUI failed to start. Review the error above.
pause
exit /b %WEBUI_EXIT_CODE%
