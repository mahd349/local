@echo off
title Farsi Exam
set PY=python
cd /d D:\Python
%PY% -m farsi.cloze exam --limit 200
echo.
echo === DONE === copy these numbers.
pause