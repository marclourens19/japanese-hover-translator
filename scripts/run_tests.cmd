@echo off
rem This script lives in scripts/, one level below the project root -- go up
rem one level so -s tests/-t . below resolve against the real tests/ folder.
cd /d "%~dp0.."
rem -t . matters: without it, unittest treats tests/ itself as the top-level
rem directory and never runs tests/__init__.py (which puts src/ on sys.path)
rem before importing test modules -- every `import hover_translate` would fail.
python -m unittest discover -s tests -t . -v
if errorlevel 1 exit /b %errorlevel%
