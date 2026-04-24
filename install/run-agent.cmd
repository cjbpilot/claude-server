@echo off
REM Launcher for the Claude Agent scheduled task.
REM The install script rewrites the paths below at install time if they
REM differ from the defaults.
set "AGENT_PY=C:\ClaudeAgent\venv\Scripts\python.exe"
set "AGENT_DIR=C:\ClaudeAgent\app"
set "LOG_DIR=C:\ProgramData\ClaudeAgent\logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

cd /d "%AGENT_DIR%"
"%AGENT_PY%" -m agent 1>>"%LOG_DIR%\stdout.log" 2>>"%LOG_DIR%\stderr.log"
exit /b %ERRORLEVEL%
