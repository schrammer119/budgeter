@echo off
REM Python Script Scheduler with Hibernation Control
REM Replace the paths below with your actual paths

REM Wait a moment for system to fully wake up
timeout /t 10 /nobreak >nul

REM Set variables
set PYTHON_CMD=py
set PYTHON_TASK="Python Budget Call"
set SCRIPT_PATH="C:\git\budgeter\budgetAnalyzer.py"
set LOG_FILE="C:\git\budgeter\logs\scheduler.log"
set WAKE_REASON_FILE="C:\git\budgeter\logs\wake_reason.txt"

REM Create log directory if it doesn't exist
if not exist "C:\git\budgeter\logs" mkdir "C:\git\budgeter\logs"

REM Log start time
echo %date% %time% - Task started >> %LOG_FILE%

REM Check what woke the system (last wake source)
powershell -Command "powercfg /lastwake" > %WAKE_REASON_FILE% 2>nul

REM Run the Python script
echo %date% %time% - Running Python script with cmd: %PYTHON_CMD% %SCRIPT_PATH% >> %LOG_FILE%
%PYTHON_CMD% %SCRIPT_PATH% >> %LOG_FILE% 2>&1

REM Log completion
echo %date% %time% - Python script completed with exit code %ERRORLEVEL% >> %LOG_FILE%

REM Check if we should hibernate (only if woken by task scheduler)
findstr /i %PYTHON_TASK% %WAKE_REASON_FILE% >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo %date% %time% - System was woken by timer/task scheduler, hibernating in 30 seconds >> %LOG_FILE%
    timeout /t 200 /nobreak >nul
    shutdown /h
) else (
    echo %date% %time% - System was not woken by timer/task scheduler, staying awake >> %LOG_FILE%
)

REM Clean up temp file
if exist %WAKE_REASON_FILE% del %WAKE_REASON_FILE%

echo %date% %time% - Task completed >> %LOG_FILE%