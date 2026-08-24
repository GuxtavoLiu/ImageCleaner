# 🛠️ Como Gerar o Executável (.exe) do ImageCleaner

Este guia explica o passo a passo para transformar o código Python em um executável do Windows (.exe).

---

## 📋 Pré-requisitos

Antes de começar, certifique-se de ter:

1. **Python 3.9 ou superior** instalado no Windows
   - Verifique com: `python --version`
   - Download: https://www.python.org/downloads/

2. **pip** atualizado
   ```bash
   python -m pip install --upgrade pip
   ```

3. **Dependências do projeto** instaladas
   ```bash
   pip install -r requirements.txt
   ```

---

## 🚀 Método 1: Automático (RECOMENDADO)

Este é o jeito mais fácil e rápido!

### Passo a Passo:

1. **Abra o Explorador de Arquivos** e navegue até a pasta do projeto
   ```
   D:\Projetos\LIU\ImageCleaner
   ```

2. **Dê um duplo clique** no arquivo `build_exe.bat`
   - O script irá automaticamente:
     - Verificar dependências
     - Limpar builds anteriores
     - Compilar o executável
     - Abrir a pasta com o resultado

3. **Aguarde o processo** (pode levar 2-5 minutos)

4. **Pronto!** O executável estará em: `dist/ImageCleaner.exe`

### ✅ Vantagens:
- Processo totalmente automatizado
- Verifica e instala dependências
- Limpa builds anteriores automaticamente

---

## 🔧 Método 2: Manual (Avançado)

Se preferir ter mais controle sobre o processo:

### Passo 1: Instalar PyInstaller

```bash
pip install pyinstaller
```

### Passo 2: Limpar builds anteriores (opcional)

```bash
rmdir /s /q build
rmdir /s /q dist
del ImageCleaner.spec
```

### Passo 3: Gerar o executável

Execute um dos comandos abaixo:

#### Opção A: Executável único (RECOMENDADO)
```bash
pyinstaller --onefile --windowed --name "ImageCleaner" main.py
```

#### Opção B: Com ícone personalizado
```bash
pyinstaller --onefile --windowed --name "ImageCleaner" --icon="icon.ico" main.py
```

#### Opção C: Com arquivo de configuração .spec
⚠️ Atenção: rodar o PyInstaller pela linha de comando (ou pelos .bat) REGENERA
o `ImageCleaner.spec`, descartando os ajustes feitos à mão (hiddenimports,
excludes, datas). Se usar o spec, builde sempre com `pyinstaller ImageCleaner.spec`.

```bash
pyinstaller ImageCleaner.spec
```

### Passo 4: Localizar o executável

O arquivo `ImageCleaner.exe` estará na pasta `dist/`

---

## 📦 O que fazem os parâmetros do PyInstaller?

| Parâmetro | Descrição |
|-----------|-----------|
| `--onefile` | Gera um único arquivo .exe (em vez de pasta com DLLs) |
| `--windowed` | Remove a janela do console (apenas GUI) |
| `--name` | Define o nome do executável |
| `--icon` | Define o ícone do executável |
| `--add-data` | Inclui arquivos extras no executável |
| `--hidden-import` | Força importação de módulos não detectados |

---

## 🔄 Quando o Código For Atualizado

**Sempre que você modificar o código**, siga um destes passos:

### Opção 1: Usar o script automático (RECOMENDADO)
```bash
build_exe.bat
```

### Opção 2: Recompilar manualmente
```bash
# 1. Limpar build anterior
rmdir /s /q build dist

# 2. Recompilar
pyinstaller --onefile --windowed --name "ImageCleaner" main.py

# 3. Testar o novo executável
dist\ImageCleaner.exe
```

---

## ⚠️ Problemas Comuns e Soluções

### 1. "PyInstaller não é reconhecido como comando"
**Solução:**
```bash
python -m PyInstaller --onefile --windowed --name "ImageCleaner" main.py
```

### 2. Executável não abre ou fecha imediatamente
**Causas possíveis:**
- Erro no código Python
- Dependências faltando
- Módulos não detectados pelo PyInstaller

**Solução:**
```bash
# Teste sem --windowed para ver erros
pyinstaller --onefile --name "ImageCleaner" main.py
```

### 3. "Módulo não encontrado" ao executar o .exe
**Solução:**
```bash
# Adicione importações ocultas
pyinstaller --onefile --windowed --name "ImageCleaner" ^
    --hidden-import PIL._tkinter_finder ^
    --hidden-import imagehash ^
    main.py
```

### 4. Antivírus bloqueia o executável
**Solução:**
- Adicione exceção no antivírus
- Use código assinado digitalmente (avançado)

### 5. Executável muito grande
**Soluções:**
- Use `--exclude-module` para remover módulos não usados
- Use UPX para comprimir: `--upx-dir caminho/para/upx`

---

## 📝 Estrutura de Arquivos Após Build

```
ImageCleaner/
│
├── main.py                 # Código fonte principal
├── requirements.txt        # Dependências Python
├── build_exe.bat          # Script de build automatizado
├── README_BUILD.md        # Este arquivo
│
├── build/                 # Arquivos temporários (pode deletar)
│   └── ...
│
├── dist/                  # PASTA COM O EXECUTÁVEL FINAL
│   └── ImageCleaner.exe   # ⭐ EXECUTÁVEL FINAL
│
└── ImageCleaner.spec      # Configuração PyInstaller (opcional)
```

---

## 🎯 Checklist Rápido

Antes de distribuir o executável, verifique:

- [ ] O executável abre sem erros
- [ ] Todas as funcionalidades funcionam
- [ ] Não há janela de console aparecendo
- [ ] O tamanho do arquivo é razoável (< 50MB)
- [ ] Testado em outro computador Windows
- [ ] Antivírus não bloqueia (falso positivo comum)

---

## 🌟 Dicas Extras

### Para reduzir o tamanho do executável:
```bash
pyinstaller --onefile --windowed --name "ImageCleaner" ^
    --exclude-module matplotlib ^
    --exclude-module pandas ^
    main.py
```
⚠️ NÃO exclua `numpy`, `scipy` ou `pywt`: o `imagehash` depende deles e o
executável não abre sem eles.

### Para bases muito grandes (dezenas/centenas de GB):
- Prefira o build em pasta (`build_pasta.bat`): o `.exe` único precisa
  descompactar ~150 MB em `%TEMP%` a cada abertura.
- O programa guarda um **cache de hashes** e um **log** em
  `%LOCALAPPDATA%\ImageCleaner\` (`hash_cache.sqlite` e `imagecleaner.log`).
  - Cache: re-escanear a mesma pasta fica quase instantâneo e um escaneamento
    cancelado é retomado de onde parou. Pode ser desligado pela checkbox
    "Usar cache de hashes" na tela inicial ou apagando o arquivo.
  - Log: se algo der errado no `.exe` (que não tem console), os detalhes
    estão nesse arquivo.
- Ajustes disponíveis no topo do `main.py`:
  - `HASH_WORKERS` (padrão 8): threads de leitura/hash. Em HDD externo não
    aumente; em NVMe pode subir para 16. Memória: com a decodificação completa
    (padrão), cada thread usa ~120 MB em fotos de 24 MP (8 threads = ~1 GB de
    pico durante o hash); com `USE_FAST_JPEG_DECODE = True` cai para poucos MB.
  - `USE_FAST_JPEG_DECODE` (padrão `False`): decodifica JPEG em escala
    reduzida (2-3x mais rápido). Altera minimamente o hash, por isso vem
    desligado; hashes com e sem essa opção usam caches separados.
  - `MAX_IMAGES_PER_GROUP_DISPLAY` (padrão 200): miniaturas exibidas por
    grupo (as ações continuam valendo para o grupo inteiro).
  - `CONFIRM_SIMILAR` (padrão `True`) e `DHASH_THRESHOLD` (padrão 14):
    confirmação de "Semelhante" por um segundo hash (dhash). O agrupamento
    por phash (threshold 10) não muda; dentro de cada grupo, um par com MD5
    diferente só continua junto se o dhash também estiver próximo. Elimina
    fotos diferentes agrupadas por coincidência de luz/sombra grossa.
    Calibrado no acervo real: quase-duplicatas reais ficam a dhash 0 a 6 e
    os falsos positivos a 23 a 36; com 14, 99,6% das quase-duplicatas
    continuam agrupadas. Pode ser desligado pela checkbox "Confirmar
    semelhantes com segundo hash" (o resultado volta a ser o antigo).
    Imagens com hash degenerado (toda preta/branca, PNG transparente) só
    ficam em grupo por MD5 igual; PNGs com transparência passam a ser
    compostos sobre branco antes do hash.

### Modo de comparação de duas pastas (referência protegida)
Na tela inicial, além da pasta alvo, é possível escolher uma **pasta de
referência** (um acervo já organizado). Nada da referência é selecionado,
movido ou excluído: as duplicatas saem só da pasta alvo, e a seleção
automática mantém a cópia do acervo em vez da mais antiga. Grupos só com
imagens da referência não são exibidos; a checkbox "Mostrar duplicatas
internas da pasta alvo" controla os grupos sem par no acervo.

### Tela de grupos: como revisar rápido
- **Fila de revisão**: "Selecionar Idênticas/Semelhantes" ou "Marcar verificado ✓" de
  um grupo tira o grupo dos pendentes; o próximo sobe para o mesmo lugar. Os
  verificados ficam em "Grupos verificados (N)" (com "Voltar para pendentes").
  "Selecionar Todas ..." age só nos pendentes; "Mover/Excluir Todas Selecionadas"
  valem para tudo.
- **Linha da imagem**: nome em destaque, pasta curta `[ALVO]/[REF]` (caminho completo
  no tooltip), resolução, tamanho, datas e rótulos "maior resolução", "mais antiga",
  "maior arquivo" (só quando há diferença). Clique na linha alterna a seleção
  (linha fica verde-clara); clique na miniatura abre a pré-visualização; botão direito
  abre/copia caminho ou abre a pasta no Explorer.
- **Pré-visualização lado a lado**: até 3 imagens do grupo com metadados;
  ← → mudam a coluna atual, Espaço seleciona/desmarca, Enter = "Manter esta
  (selecionar as outras)", Esc fecha. Nunca seleciona a referência.
- **Barra de status**: progresso "Verificados x / N" e "Selecionadas: n (GB)".
- **Teclado**: F1/F2 páginas, PageUp/PageDown/Home/End/↑/↓ rolagem.
- **Grupos grandes** (mais de 8 imagens) aparecem recolhidos: "Expandir" mostra tudo;
  as ações valem sempre para o grupo inteiro.
- **Segurança**: "Excluir" envia para a **Lixeira do Windows** (`send2trash`; sem ela o
  app recusa excluir). Cada sessão com ações gera um CSV em
  `%LOCALAPPDATA%\ImageCleaner\relatorios\`. Menu "Mais ▾": "Desfazer último lote
  (mover)" devolve os arquivos movidos e os re-seleciona; exclusões se restauram pela
  Lixeira.
- **Lembrança**: últimas pastas (menus "Recentes ▾") e opções ficam em
  `%LOCALAPPDATA%\ImageCleaner\settings.json`.
- Ajustes no topo do `main.py`: `THUMB_SIZE` (miniaturas, padrão 200 px),
  `COLLAPSE_THRESHOLD`, `PREVIEW_COLUMNS`, paleta `PALETTE`/`BUTTON_KINDS`.

### Diagnóstico sem interface (útil para validar o `.exe`)
```bash
ImageCleaner.exe --selftest PASTA
ImageCleaner.exe --selftest PASTA --ref PASTA_DE_REFERENCIA
ImageCleaner.exe --selftest PASTA --no-confirm      # sem a confirmação por dhash
```
Roda o pipeline inteiro (listar, hash, agrupar, MD5, confirmação) sem alterar
nada nem usar o cache, e imprime um resumo (também gravado no log).

### Testes automatizados
`python -m pytest tests -q` (o `testar_antes_build.bat` roda isso se o pytest
estiver instalado). Inclui snapshots dourados do modo de uma pasta com e sem
a confirmação; regenerá-los só quando uma mudança de comportamento for
intencional (ver docstring de `tests/test_regression_single_mode.py`).

### Para incluir um README junto:
```bash
pyinstaller --onefile --windowed --name "ImageCleaner" ^
    --add-data "README.md;." ^
    main.py
```

### Para criar um instalador:
Use ferramentas como:
- **Inno Setup** (gratuito): https://jrsoftware.org/isinfo.php
- **NSIS** (gratuito): https://nsis.sourceforge.io/
- **PyInstaller + Inno Setup** (combo recomendado)

---

## 📞 Suporte

Se encontrar problemas:

1. Verifique se todas as dependências estão instaladas
2. Execute o Python diretamente para testar: `python main.py`
3. Verifique os logs em `build/ImageCleaner/warn-ImageCleaner.txt`
4. Pesquise o erro específico no GitHub do PyInstaller

---

## 📚 Referências

- [Documentação PyInstaller](https://pyinstaller.readthedocs.io/)
- [PyInstaller GitHub](https://github.com/pyinstaller/pyinstaller)
- [Troubleshooting Guide](https://pyinstaller.readthedocs.io/en/stable/when-things-go-wrong.html)

---

**Última atualização:** 2026-01-13
**Versão:** 1.0
