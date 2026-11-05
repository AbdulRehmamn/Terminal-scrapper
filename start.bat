@echo off
setlocal EnableExtensions
title LeadScraper
cd /d "%~dp0"

echo ============================================
echo    LeadScraper - setup and start
echo ============================================
echo.

rem ---- 1. Find Python -------------------------------------------------------
set "PY="
call :findpython
if defined PY goto havepython

echo Python was not found. Trying to install it automatically...
where winget >nul 2>&1
if errorlevel 1 goto nowinget
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :findpython
if defined PY goto havepython

:nowinget
echo.
echo Could not install Python automatically.
echo Please install it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add Python to PATH" on the first screen of the installer.
echo Then double-click start.bat again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:havepython
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo Using %%v
echo.

rem ---- 2. Install packages into a private folder (venv) -----------------------
if exist ".venv\Scripts\python.exe" goto venvready
echo Creating a private Python environment (first run only)...
%PY% -m venv .venv
if errorlevel 1 goto venvfail
:venvready
set "VPY=.venv\Scripts\python.exe"

if exist ".venv\installed.ok" goto ready
echo Installing required packages (first run only, about a minute)...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto pipfail
echo ok> ".venv\installed.ok"
echo.
echo Setup complete.

:ready
rem ---- 3. Menu ----------------------------------------------------------------
:menu
echo.
echo ============================================
echo  What do you want to do?
echo ============================================
echo   1. Get Procore leads for California  (you choose how many; resumes if stopped)
echo   2. Procore leads with filters (state, trade, city, how many)
echo   3. Find emails for a leads file (enrich)
echo   4. Scrape company websites from domains.txt
echo   5. Extract leads from PDF files
echo   6. Scrape another directory website
echo   7. Exit
echo.
set "CH="
set /p CH="Type a number and press Enter: "

if "%CH%"=="1" goto opt1
if "%CH%"=="2" goto opt2
if "%CH%"=="3" goto opt3
if "%CH%"=="4" goto opt4
if "%CH%"=="5" goto opt5
if "%CH%"=="6" goto opt6
if "%CH%"=="7" exit /b 0
echo Please type a number from 1 to 7.
goto menu

:opt1
echo.
set "MX="
set /p MX="How many leads? (leave blank for ALL of them): "
if "%MX%"=="" set "MX=0"
echo.
echo Starting. About 3 seconds per lead. Press Ctrl+C to stop; choose 1 again later to continue.
"%VPY%" leadscraper.py procore --state ca --max %MX% --out procore_leads.csv
goto done

:opt2
echo.
set "ST=ca"
set /p ST="State, two letters [ca]: "
set "TR="
set /p TR="Trades to keep, comma-separated (blank = all), e.g. concrete,electrical: "
set "CT="
set /p CT="Cities to keep, comma-separated (blank = all): "
set "MX="
set /p MX="How many leads? (blank = all): "
if "%MX%"=="" set "MX=0"
set "OUT=procore_%ST%_leads.csv"
set "ARGS=procore --state %ST% --max %MX% --out "%OUT%""
if not "%TR%"=="" set ARGS=%ARGS% --trade "%TR%"
if not "%CT%"=="" set ARGS=%ARGS% --city "%CT%"
"%VPY%" leadscraper.py %ARGS%
goto done

:opt3
echo.
set "IN=procore_leads.csv"
set /p IN="Leads file to enrich [procore_leads.csv]: "
if not exist "%IN%" echo File "%IN%" not found in this folder.& goto menu
"%VPY%" leadscraper.py enrich "%IN%"
goto done

:opt4
echo.
if not exist domains.txt (
  echo # Put one website per line, e.g. acmeconcrete.com> domains.txt
  echo Created domains.txt. Add websites to it, save, then choose 4 again.
  start "" notepad domains.txt
  goto menu
)
"%VPY%" leadscraper.py site --file domains.txt --out site_leads.csv
goto done

:opt5
echo.
echo Drag PDF files into this window (or type paths/links), separated by spaces, then press Enter:
set "PDFS="
set /p PDFS="> "
if "%PDFS%"=="" goto menu
"%VPY%" leadscraper.py pdf %PDFS% --out pdf_leads.csv
goto done

:opt6
echo.
echo Example listing URL: https://example.com/contractors?page={page}
set "LU="
set /p LU="Listing URL (use {page} for the page number): "
echo Example: if company pages look like https://example.com/company/acme-123 type /company/
set "FW="
set /p FW="Pattern in company page links: "
set "EP=20"
set /p EP="Last page number [20]: "
if "%LU%"=="" goto menu
if "%FW%"=="" goto menu
set "MX="
set /p MX="How many leads? (blank = all): "
if "%MX%"=="" set "MX=0"
"%VPY%" leadscraper.py directory --list-url "%LU%" --follow "%FW%" --end-page %EP% --max %MX% --out directory_leads.csv
goto done

:done
echo.
echo Finished. Your CSV file is in this folder:
echo   %CD%
goto menu

:venvfail
echo Could not create the Python environment. Try reinstalling Python from python.org.
pause
exit /b 1

:pipfail
echo.
echo Package installation failed. Check your internet connection and run start.bat again.
pause
exit /b 1

rem ---- helper: locate a working Python ---------------------------------------
:findpython
py -3 --version >nul 2>&1 && (set "PY=py -3" & exit /b 0)
python --version >nul 2>&1 && (python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && (set "PY=python" & exit /b 0))
for %%d in (Python313 Python312 Python311 Python310) do (
  if exist "%LocalAppData%\Programs\Python\%%d\python.exe" (
    set "PY="%LocalAppData%\Programs\Python\%%d\python.exe""
    exit /b 0
  )
)
exit /b 0
