@echo off
rem Starts the bonus path explorer and opens it in the default browser.
rem Needs uv (https://docs.astral.sh/uv/) and the processed graph in data\processed\graph.
title Wikipedia path explorer
cd /d "%~dp0"
set PYTHONUTF8=1
echo Starting the path explorer. The browser opens when it is ready.
echo The first start builds a search index and takes about 40 seconds.
echo Close this window to stop.
echo.
uv run python bonus\server.py --open
if errorlevel 1 (
  echo.
  echo The explorer stopped with an error. See the message above.
  pause
)
