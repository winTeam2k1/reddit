@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Khong tim thay .venv\Scripts\python.exe
    echo Hay tao venv tren Windows hoac kiem tra lai duong dan.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "post.py"
set EXIT_CODE=%ERRORLEVEL%

echo.
pause
exit /b %EXIT_CODE%
