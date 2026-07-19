@echo off
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 main.py >> logs\scheduler.log 2>&1
