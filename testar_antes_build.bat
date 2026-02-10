@echo off
echo ============================================
echo   TESTE DE AMBIENTE - VERIFICACAO
echo ============================================
echo.
echo Este script verifica se tudo esta OK
echo antes de gerar o executavel.
echo.
echo ============================================
echo.

REM Teste 1: Python
echo [1/4] Verificando Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] ERRO: Python nao encontrado!
    echo.
    echo Solucao:
    echo 1. Instale Python de https://www.python.org/downloads/
    echo 2. Marque "Add Python to PATH" durante instalacao
    echo.
    goto :erro
) else (
    python --version
    echo [OK] Python encontrado!
)
echo.

REM Teste 2: pip
echo [2/4] Verificando pip...
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] ERRO: pip nao encontrado!
    goto :erro
) else (
    echo [OK] pip encontrado!
)
echo.

REM Teste 3: main.py
echo [3/4] Verificando main.py...
if exist "main.py" (
    echo [OK] main.py encontrado!
) else (
    echo [X] ERRO: main.py nao encontrado nesta pasta!
    echo.
    echo Solucao: Execute este script na mesma pasta do main.py
    goto :erro
)
echo.

REM Teste 4: Dependências
echo [4/4] Verificando dependencias...
python -c "import PIL; import imagehash; print('[OK] Todas as dependencias instaladas!')" 2>nul
if %errorlevel% neq 0 (
    echo [!] AVISO: Algumas dependencias nao estao instaladas
    echo.
    echo Instalando agora...
    python -m pip install Pillow imagehash
    echo.
    echo [OK] Dependencias instaladas!
) else (
    python -c "import PIL; import imagehash; print('[OK] Todas as dependencias instaladas!')"
)
echo.

REM Sucesso
echo ============================================
echo   TUDO OK! PRONTO PARA GERAR O .EXE
echo ============================================
echo.
echo Recomendacao:
echo 1. Use: build_simples.bat (arquivo unico)
echo 2. Ou:  build_pasta.bat (pasta portátil)
echo.
echo Pressione qualquer tecla para sair...
pause >nul
exit /b 0

:erro
echo ============================================
echo   ERRO ENCONTRADO
echo ============================================
echo.
echo Corrija o erro acima antes de continuar.
echo.
pause
exit /b 1
