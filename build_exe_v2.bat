@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo =========================================
echo   IMAGE CLEANER - BUILD EXECUTAVEL V2
echo =========================================
echo.

REM Define cor para mensagens
set "LOG_FILE=build_log.txt"
echo Build iniciado em %date% %time% > "%LOG_FILE%"

REM Verifica se Python está instalado
echo [0/5] Verificando Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERRO] Python nao encontrado no PATH!
    echo Instale o Python 3.7 ou superior de https://www.python.org/downloads/
    echo.
    echo Verifique a opcao "Add Python to PATH" durante a instalacao
    pause
    exit /b 1
)

python --version
echo      Python encontrado!
echo.

REM Verifica se PyInstaller está instalado
echo [1/5] Verificando PyInstaller...
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [AVISO] PyInstaller nao esta instalado!
    echo Instalando PyInstaller...
    python -m pip install pyinstaller >> "%LOG_FILE%" 2>&1
    if !errorlevel! neq 0 (
        echo [ERRO] Falha ao instalar PyInstaller
        echo Verifique o arquivo build_log.txt para mais detalhes
        pause
        exit /b 1
    )
    echo      PyInstaller instalado com sucesso!
) else (
    echo      PyInstaller ja esta instalado!
)
echo.

REM Verifica dependências
echo [2/5] Instalando dependencias...
if exist "requirements.txt" (
    python -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
    echo      Dependencias instaladas!
) else (
    echo [AVISO] requirements.txt nao encontrado
    echo Instalando dependencias basicas...
    python -m pip install Pillow imagehash >> "%LOG_FILE%" 2>&1
    echo      Dependencias basicas instaladas!
)
echo.

REM Limpa builds anteriores
echo [3/5] Limpando builds anteriores...
if exist "build" (
    rmdir /s /q "build"
    echo      Pasta build removida
)
if exist "dist" (
    rmdir /s /q "dist"
    echo      Pasta dist removida
)
if exist "ImageCleaner.spec" (
    del "ImageCleaner.spec"
    echo      Arquivo .spec removido
)
echo      Limpeza concluida!
echo.

REM Compila o executável
echo [4/5] Compilando executavel...
echo      Isso pode levar 2-5 minutos. Aguarde...
echo.

REM Usa python -m PyInstaller para garantir que funcione
python -m PyInstaller --onefile --windowed --name "ImageCleaner" ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import PIL.Image ^
    --hidden-import imagehash ^
    --collect-all PIL ^
    --noconfirm ^
    main.py >> "%LOG_FILE%" 2>&1

if %errorlevel% neq 0 (
    echo [ERRO] Falha ao compilar o executavel!
    echo.
    echo Verifique o arquivo build_log.txt para detalhes do erro
    echo.
    echo Problemas comuns:
    echo   - main.py nao encontrado nesta pasta
    echo   - Erro no codigo Python
    echo   - Dependencias faltando
    echo.
    pause
    exit /b 1
)

echo      Compilacao concluida com sucesso!
echo.

REM Verifica se o executável foi criado
echo [5/5] Verificando executavel...
if exist "dist\ImageCleaner.exe" (
    echo      [OK] Executavel gerado com sucesso!
    echo.

    REM Mostra informações do arquivo
    for %%A in ("dist\ImageCleaner.exe") do (
        set size=%%~zA
        set /a sizeMB=!size! / 1048576
        echo      Arquivo: dist\ImageCleaner.exe
        echo      Tamanho: !sizeMB! MB
    )
    echo.
    echo =========================================
    echo   BUILD CONCLUIDO COM SUCESSO!
    echo =========================================
    echo.
    echo O executavel esta pronto em:
    echo ^> dist\ImageCleaner.exe
    echo.
    echo Voce pode copiar este arquivo para um pendrive
    echo e executar em qualquer computador Windows!
    echo.
    echo Pressione qualquer tecla para abrir a pasta...
    pause >nul
    explorer dist
) else (
    echo [ERRO] O executavel nao foi encontrado!
    echo.
    echo Possivel problema durante a compilacao.
    echo Verifique o arquivo build_log.txt para mais detalhes.
    echo.
    pause
    exit /b 1
)
