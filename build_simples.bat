@echo off
echo ============================================
echo   BUILD SIMPLES - ARQUIVO UNICO (.exe)
echo ============================================
echo.
echo Instalando PyInstaller...
python -m pip install pyinstaller pillow imagehash
echo.
echo Compilando... Aguarde 2-5 minutos...
echo.

REM Remove builds anteriores
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

REM Compila com configuração simples e garantida
python -m PyInstaller --onefile --windowed ^
    --name "ImageCleaner" ^
    --hidden-import PIL._tkinter_finder ^
    main.py

echo.
if exist "dist\ImageCleaner.exe" (
    echo ============================================
    echo   SUCESSO! Executavel criado!
    echo ============================================
    echo.
    echo Arquivo: dist\ImageCleaner.exe
    echo.
    echo Pressione qualquer tecla para abrir a pasta...
    pause >nul
    explorer dist
) else (
    echo ============================================
    echo   ERRO! Executavel nao foi criado
    echo ============================================
    echo.
    echo Verifique se:
    echo  1. Python esta instalado
    echo  2. O arquivo main.py existe nesta pasta
    echo  3. Nao ha erros no codigo
    echo.
    pause
)
