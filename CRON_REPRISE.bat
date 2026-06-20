@echo off
REM Cron de reprise : finit toutes les 15 min les taches restees en plan apres une panne d'outil.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python cron_resume.py --loop 900) else (py cron_resume.py --loop 900)
pause
