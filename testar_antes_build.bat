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
echo [1/5] Verificando Python...
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
echo [2/5] Verificando pip...
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] ERRO: pip nao encontrado!
    goto :erro
) else (
    echo [OK] pip encontrado!
)
echo.

REM Teste 3: main.py
echo [3/5] Verificando main.py...
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
echo [4/5] Verificando dependencias...
python -c "import sys; assert sys.version_info >= (3, 9), 'Python 3.9+ necessario'; import PIL; import imagehash; import numpy; print('[OK] Todas as dependencias instaladas!')" 2>nul
if %errorlevel% neq 0 (
    echo [!] AVISO: Algumas dependencias nao estao instaladas
    echo.
    echo Instalando agora...
    python -m pip install -r requirements.txt
    echo.
    REM Reexecuta a verificacao: instalar nao conserta um Python antigo.
    REM "if errorlevel 1" e avaliado na execucao (ao contrario de %errorlevel%,
    REM que dentro de parenteses e expandido no parse e ficaria desatualizado).
    python -c "import sys; assert sys.version_info >= (3, 9), 'Python 3.9+ necessario'; import PIL; import imagehash; import numpy; print('[OK] Todas as dependencias instaladas!')"
    if errorlevel 1 (
        echo [X] ERRO: Dependencias ou versao do Python ainda com problema!
        echo         E necessario Python 3.9 ou superior.
        goto :erro
    )
) else (
    python -c "import PIL; import imagehash; import numpy; print('[OK] Todas as dependencias instaladas!')"
)
echo.

REM Teste 5 (opcional): suite de testes automatizados, se o pytest existir
echo [5/5] Rodando testes automatizados (opcional)...
python -m pytest --version >nul 2>&1
if errorlevel 1 (
    echo [!] pytest nao instalado: testes ignorados.
    echo     Para rodar: python -m pip install pytest
) else (
    python -m pytest tests -q
    if errorlevel 1 (
        echo [X] ERRO: testes automatizados falharam! Nao gere o .exe.
        goto :erro
    )
    echo [OK] Testes automatizados passaram!
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
