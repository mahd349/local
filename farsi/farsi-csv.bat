@echo off
title Farsi Ingest CSV
set PY=python
cd /d D:\Python
if "%~1"=="" (
    echo Drag a .csv file onto this .bat file.
    pause
    exit /b 1
)
%PY% -m farsi.ingest "%~1" --limit 100000
if errorlevel 1 (pause & exit /b 1)
%PY% -m farsi on
%PY% -m farsi add-file "%~dpn1-fa.txt"
%PY% -m farsi.purge rebuild
%PY% -m farsi.purge size
echo === DONE ===
pause