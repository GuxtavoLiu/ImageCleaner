@echo off
echo ============================================
echo   BUILD PASTA - MAIS CONFIAVEL
echo ============================================
echo.
echo Esta opcao gera uma PASTA com o executavel
echo e suas dependencias. E mais confiavel!
echo.
echo Voce podera copiar a pasta inteira para
echo um pendrive e usar em qualquer PC.
echo.
pause
echo.
echo Instalando PyInstaller...
python -m pip install pyinstaller pillow imagehash
echo.
echo Compilando... Aguarde 1-3 minutos...
echo.

REM Remove builds anteriores
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

REM Compila como pasta (--onedir é o padrão)
python -m PyInstaller --windowed ^
    --name "ImageCleaner" ^
    --hidden-import PIL._tkinter_finder ^
    main.py

echo.
if exist "dist\ImageCleaner\ImageCleaner.exe" (
    echo ============================================
    echo   SUCESSO! Aplicacao criada!
    echo ============================================
    echo.
    echo Pasta: dist\ImageCleaner\
    echo Executavel: dist\ImageCleaner\ImageCleaner.exe
    echo.
    echo IMPORTANTE:
    echo - Copie a PASTA INTEIRA "ImageCleaner" para o pendrive
    echo - Execute o arquivo ImageCleaner.exe dentro da pasta
    echo.
    echo Pressione qualquer tecla para abrir a pasta...
    pause >nul
    explorer "dist\ImageCleaner"
) else (
    echo ============================================
    echo   ERRO! Aplicacao nao foi criada
    echo ============================================
    echo.
    echo Verifique se:
    echo  1. Python esta instalado
    echo  2. O arquivo main.py existe nesta pasta
    echo  3. Nao ha erros no codigo
    echo.
    pause
)
