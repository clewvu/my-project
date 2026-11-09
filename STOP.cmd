@echo off
cd /d "%~dp0"
if not exist state mkdir state
echo stop > state\KILL
echo Kill switch set. No new orders will be placed. Delete state\KILL to resume.
pause
