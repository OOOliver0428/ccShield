@echo off
setlocal
cd /d "%~dp0"

if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%CD%\.uv-cache"

set "UV_CMD="
where uv >nul 2>nul && set "UV_CMD=uv"
if not defined UV_CMD if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_CMD=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_CMD if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_CMD=%USERPROFILE%\.cargo\bin\uv.exe"

if not defined UV_CMD (
  echo [start] uv was not found. Install uv and try again.
  pause
  exit /b 2
)

set "UV_PROJECT_ENVIRONMENT=%CD%\backend\.venv"
"%UV_CMD%" sync --project backend --extra dev
if errorlevel 1 (
  echo.
  echo [start] Backend dependency installation failed.
  pause
  exit /b 2
)

"%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" scripts\start.py %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo [start] Startup failed with code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
