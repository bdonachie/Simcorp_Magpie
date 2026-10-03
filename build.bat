@echo off
:: Builds the standalone Windows app into dist\SimCorp SF\.
::
:: Run this from a copy of the repo that sits INSIDE the machine's
:: application-whitelisted tree if it has one. An exe built or launched outside
:: it dies in PyInstaller's runtime hooks before a line of the app runs, which
:: looks like a broken build and is not (DEVELOPMENT_NOTES.md, section 14.6).
::
:: Needs:  pip install -r requirements-dev.txt
::
:: Usage:  build.bat [path\to\python.exe]

setlocal
set "PYTHON=%~1"
if "%PYTHON%"=="" set "PYTHON=python"

"%PYTHON%" -m PyInstaller --noconfirm --windowed ^
  --name "SimCorp SF" --icon icon.ico ^
  --collect-all playwright --collect-all tkcalendar --collect-all babel ^
  --collect-all PIL --hidden-import PIL.ImageTk ^
  sc_case_logger.py
if errorlevel 1 exit /b 1

:: Tk sets the window icon from a PNG at runtime; the exe carries the ICO.
:: Both ship beside the exe (section 19.6).
copy /y icon.PNG "dist\SimCorp SF\" >nul
copy /y icon.ico "dist\SimCorp SF\" >nul

echo.
echo Built "dist\SimCorp SF\". Chromium is kept outside the bundle; add it with:
echo.
echo   set PLAYWRIGHT_BROWSERS_PATH=%CD%\dist\SimCorp SF\browsers
echo   "%PYTHON%" -m playwright install chromium
echo.
echo then copy the whole "dist\SimCorp SF\" folder to the target PC.
endlocal
