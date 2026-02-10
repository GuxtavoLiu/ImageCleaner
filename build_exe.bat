@echo off
echo =========================================
echo   IMAGE CLEANER - BUILD EXECUTAVEL
echo =========================================
echo.

REM Verifica se PyInstaller está instalado
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] PyInstaller nao esta instalado!
    echo Instalando PyInstaller...
    python -m pip install pyinstaller
    if %errorlevel% neq 0 (
        echo [ERRO] Falha ao instalar PyInstaller
        pause
        exit /b 1
    )
)

echo [1/4] Limpando builds anteriores...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "ImageCleaner.spec" del "ImageCleaner.spec"
echo      Limpeza concluida!
echo.

echo [2/4] Verificando dependencias...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [AVISO] Algumas dependencias podem nao ter sido instaladas
)
echo      Dependencias verificadas!
echo.

echo [3/4] Compilando executavel...
echo      Isso pode levar alguns minutos...
pyinstaller --onefile --windowed --name "ImageCleaner" --icon=NONE ^
    --add-data "README.md;." ^
    --hidden-import PIL._tkinter_finder ^
    main.py

if %errorlevel% neq 0 (
    echo [ERRO] Falha ao compilar o executavel
    pause
    exit /b 1
)
echo      Compilacao concluida!
echo.

echo [4/4] Finalizando...
echo.
echo =========================================
echo   BUILD CONCLUIDO COM SUCESSO!
echo =========================================
echo.
echo O executavel foi gerado em: dist\ImageCleaner.exe
echo.
echo Pressione qualquer tecla para abrir a pasta...
pause >nul
explorer dist
