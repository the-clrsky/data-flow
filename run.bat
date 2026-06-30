@echo off
cd /d "%~dp0"
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate.bat
pip install -q -e .
data-flow
