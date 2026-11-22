@echo off
rem Makes a virtual environment in .env on the first run, adds the example tests, then starts the bot
if not exist .env\Scripts\activate.bat (
    python -m venv .env
    call .env\Scripts\activate.bat
    pip install -e .
    telexam add examples\general.json examples\python.json
) else (
    call .env\Scripts\activate.bat
)
telexam
pause
