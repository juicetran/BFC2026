@echo off
chcp 65001 >nul
set F1_FANTASY_LEAGUE_ID=C4JXU0PEO03

echo.
echo ============================================
echo  Baby Formula Championship - F1 Fantasy Sync
echo ============================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from python.org
    pause
    exit /b 1
)

python -c "import httpx" >nul 2>&1
if errorlevel 1 (
    echo Installing httpx...
    pip install httpx
)

REM Pass --force flag if called as: run_fantasy_sync.bat --force
set FORCE_FLAG=
if /i "%1"=="--force" set FORCE_FLAG=--force

python scripts/f1_fantasy_sync.py %FORCE_FLAG%
if errorlevel 1 goto :sync_failed

echo.
echo Pushing to GitHub...
git add f1_teams.json history.json
git commit -m "data: sync f1 fantasy [skip ci]"
git push
if errorlevel 1 (
    echo.
    echo ============================================
    echo  WARNING: Git push failed.
    echo  Data files were updated locally but NOT pushed.
    echo  Run manually: git push
    echo ============================================
    echo.
) else (
    echo.
    echo ============================================
    echo  SUCCESS: Synced and pushed to GitHub.
    echo ============================================
    echo.
)
pause
exit /b 0

:sync_failed
echo.
echo ============================================
echo  SYNC FAILED - see error above.
echo ============================================
echo.
echo If you see a 401 error, your cookies have expired.
echo.
echo How to refresh cookies:
echo   1. Open Chrome - fantasy.formula1.com
echo   2. F12 - Network tab - filter: getusergamedaysv1
echo   3. Reload the page
echo   4. Right-click request - Copy - Copy as cURL (bash)
echo   5. Extract the -b '...' value
echo   6. Paste into scripts\f1_session.json as raw_cookies
echo.
pause
exit /b 1
