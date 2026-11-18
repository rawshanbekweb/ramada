@echo off
chcp 65001 >nul
REM Windows: ikki marta bosing. Python 3.11+ o'rnatilgan bo'lishi kerak (python.org, "Add to PATH" belgisi bilan).
cd /d "%~dp0"
if not exist .venv (
  echo Birinchi ishga tushirish: kutubxonalar o'rnatilmoqda, bir necha daqiqa...
  python -m venv .venv || (echo Python topilmadi. python.org dan Python 3.12 ni o'rnating. & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -e ".[edge]" || (echo O'rnatishda xato. & pause & exit /b 1)
)
.venv\Scripts\python -m tools.run_local %*
pause
