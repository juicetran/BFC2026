@echo off
set F1_FANTASY_LEAGUE_ID=C4JXU0PEO03

echo Running F1 Fantasy Sync from local machine...
cd /d "%~dp0"
python scripts/f1_fantasy_sync.py

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Pushing to GitHub...
    git add f1_fantasy.json f1_teams.json
    git commit -m "chore: update f1 fantasy data [skip ci]"
    git push
    echo Done!
) else (
    echo Sync failed - check error above
)
pause
