@echo off
REM Build GUI executable using PyInstaller
REM This script creates a standalone .exe file that can run without Python installed

echo ========================================
echo Building GUI.exe with PyInstaller
echo ========================================
echo.

REM Check if PyInstaller is installed
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: Failed to install PyInstaller
        pause
        exit /b 1
    )
)

REM Clean previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist gui.spec del gui.spec

echo.
echo Building executable...
echo.

REM Build the executable
REM --onedir: Create a directory with all dependencies (instead of single file)
REM --windowed: No console window (GUI only)
REM --name: Output filename
REM --icon: Application icon (optional, remove if no icon)
REM --add-data: Include additional files (if needed)

pyinstaller --onedir ^
    --windowed ^
    --name "Account_Registration_GUI" ^
    --clean ^
    gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo Build completed successfully!
echo ========================================
echo.
echo Executable location: dist\Account_Registration_GUI\Account_Registration_GUI.exe
echo.
echo The application is built as a directory with all dependencies.
echo To distribute, copy the entire dist\Account_Registration_GUI\ folder.
echo Make sure the following files are in the same directory as the .exe:
echo   - All Python scripts (*.py)
echo   - storage.py
echo   - create_emails.py
echo   - register_devin.py
echo   - add_identities.py
echo   - check_cards.py
echo   - activate_trials.py
echo   - accounts.db (will be created on first run)
echo   - .venv folder (for running scripts)
echo.

pause
