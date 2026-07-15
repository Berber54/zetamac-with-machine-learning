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
echo.

echo === Installing to per-user Programs folder ===
set "INSTALLDIR=%LOCALAPPDATA%\Programs\ZetamacTrainer"
if not exist "%INSTALLDIR%" mkdir "%INSTALLDIR%"
copy /y "%~dp0dist\ZetamacTrainer.exe" "%INSTALLDIR%\ZetamacTrainer.exe" >nul
if errorlevel 1 goto :fail
echo Installed to:  %INSTALLDIR%\ZetamacTrainer.exe
echo (zetamac_stats.json and zetamac_log.csv are created here when you play.)

echo === Creating Start Menu shortcut ===
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t='%INSTALLDIR%\ZetamacTrainer.exe';" ^
  "$icon='%~dp0icon.ico';" ^
  "$lnk=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Zetamac Trainer.lnk';" ^
  "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($lnk);" ^
  "$s.TargetPath=$t; $s.WorkingDirectory=Split-Path $t;" ^
  "if(Test-Path $icon){$s.IconLocation=$icon}else{$s.IconLocation=$t};" ^
  "$s.Description='Zetamac mental math trainer'; $s.Save()"
if errorlevel 1 goto :fail
echo Shortcut created - 'Zetamac Trainer' is now in the Start Menu.
echo.

rem open Explorer with the installed .exe highlighted
explorer /select,"%INSTALLDIR%\ZetamacTrainer.exe"
pause
exit /b 0

:fail
echo.
echo === Build FAILED - see the output above ===
pause
exit /b 1
