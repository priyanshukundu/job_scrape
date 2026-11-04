@echo off
cd /d "%~dp0"
git pull --quiet
py -3 job_scout.py --queue --all --html queue.html
start "" queue.html
