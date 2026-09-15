@echo off
rem Python으로 직접 실행할 때 쓰는 파일입니다. 보통은 TrimPDF.exe 를 쓰면 됩니다.
start "" pythonw "%~dp0trimpdf.py" %*
