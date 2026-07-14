@echo off
setlocal
cd /d "%~dp0"

echo === Building Zetamac Trainer standalone .exe ===
echo.

python -m pip install --upgrade --quiet customtkinter pyinstaller
if errorlevel 1 goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name ZetamacTrainer ^
    --icon icon.ico ^
    --add-data "icon.ico;." ^
    --collect-all customtkinter ^
    zetamac.py
if errorlevel 1 goto :fail

echo.
echo === Build succeeded ===
echo Standalone app:  %~dp0dist\ZetamacTrainer.exe
echo It can be copied anywhere; zetamac_stats.json and zetamac_log.csv are
echo created next to the .exe when you play.
echo.
rem open Explorer with the freshly built .exe highlighted
explorer /select,"%~dp0dist\ZetamacTrainer.exe"
pause
exit /b 0

:fail
echo.
echo === Build FAILED - see the output above ===
pause
exit /b 1
