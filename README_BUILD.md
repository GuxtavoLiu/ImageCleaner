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

### Vídeos e outros arquivos (caixas "Procurar duplicatas em")
Na tela inicial: **Fotos** (ligada por padrão), **Vídeos** e **Outros arquivos**.
- **Fotos** segue como sempre (Idêntica, Mesma foto, Semelhante), com duas diferenças
  deliberadas: as pastas de sistema `$RECYCLE.BIN` e `System Volume Information` deixaram de
  ser percorridas (antes, varrer a raiz de um disco listava a Lixeira como duplicata); e os
  formatos que o Pillow não abre (`PHOTO_BYTES_EXTENSIONS`: HEIC/HEIF do iPhone, RAW de
  câmera) entram com esta caixa, só como cópia exata (para tirá-los, esvazie a lista: eles
  passam a contar como "Outros").
- **Vídeos** (`VIDEO_EXTENSIONS`) e **Outros** (qualquer outra extensão) só têm um status:
  cópia exata, por comparação de **bytes**. O disco é lido o mínimo possível: 1) arquivos de
  tamanho único nem são abertos; 2) os primeiros 64 KB eliminam, com uma leitura só, quase
  todos os que apenas coincidem no tamanho (importante para RAW sem compressão, que tem
  tamanho fixo por câmera); 3) 64 KB do meio e do fim peneiram quem empatou no começo;
  4) o MD5 do arquivo **inteiro** só é calculado para quem ainda empata. A amostragem só elimina, nunca confirma: uma cópia com setores zerados
  no meio (HD recuperado) não vira "idêntica". Arquivo ilegível, em uso ou alterado durante
  a leitura fica **fora** da comparação (um aviso no fim; caminhos no log).
- Nunca entram: arquivos vazios, de sistema, só na nuvem (OneDrive: ler baixaria o acervo),
  atalhos simbólicos, `Thumbs.db`/`desktop.ini`/`.DS_Store`, temporários do Office (`~$`).
  Hardlinks (o mesmo arquivo no disco com dois nomes) não contam como duplicata. As pastas
  `$RECYCLE.BIN` e `System Volume Information` não são percorridas.
- **Cuidado com "Outros"** em pastas de programas ou de projetos: elas têm muitos arquivos
  iguais de propósito.
- Na tela de grupos esses grupos vêm depois dos de fotos, com o título "Grupo N (vídeos)",
  "(fotos)" ou "(arquivos)". A miniatura é a do Explorer (um quadro do vídeo, a 1ª página do
  PDF, ou o ícone do tipo), carregada em segundo plano: uma placa com a extensão aparece na
  hora. A linha mostra o tipo e, para vídeos MP4/MOV, dimensões e duração. A pré-visualização
  tem "Abrir no programa padrão". Um arquivo que **mudou depois da varredura** não é movido
  nem excluído (a prova valia para o conteúdo antigo).
- Cache: tabela `files_v1` no mesmo `hash_cache.sqlite` (chave: caminho + tamanho + data);
  uma segunda varredura não relê nada. Como há programas que alteram o conteúdo preservando
  tamanho e data (contêiner VeraCrypt, editor de tags com "manter a data"), toda prova que
  veio do cache é **relida na hora de mover ou excluir** (janela "Reconferindo Cópias"): o
  arquivo só sai se o MD5 dele ainda bate e se sobra ao menos uma cópia confirmada; o que
  não se confirmar fica onde está e o cache é consertado. Provas lidas na própria varredura
  não são relidas. Progresso em MB, com Cancelar respondendo mesmo no
  meio de um arquivo de vários GB. Ajustes no topo do `main.py`: `BYTE_QUICK_CHUNK`,
  `BYTE_LARGE_FILE`, `BYTE_LARGE_WORKERS`.
- Módulos ao lado do `main.py` (o PyInstaller recolhe sozinho): `shellthumb.py` (miniatura
  do Explorer via ctypes) e `mp4probe.py` (cabeçalho de MP4/MOV em Python puro).

### Tela de grupos: como revisar rápido
- **Fila de revisão**: "Selecionar Idênticas/Semelhantes" ou "Marcar verificado ✓" de
  um grupo tira o grupo dos pendentes; o próximo sobe para o mesmo lugar. Os
  verificados ficam em "Grupos verificados (N)" (com "Voltar para pendentes").
  "Selecionar Todas ..." age só nos pendentes; "Mover/Excluir Todas Selecionadas"
  valem para tudo.
- **Regra de "Selecionar Semelhantes"** (`SIMILAR_KEEP_PRIORITY` no `main.py`): em cada
  grupo, entre as imagens não idênticas, MANTÉM a de melhor qualidade e seleciona as
  outras: 1) maior resolução, 2) maior arquivo, 3) mais antiga, 4) a primeira da lista.
  Se o grupo tem imagem do acervo de referência, ela é a mantida. "Selecionar Idênticas"
  segue mantendo a cópia mais antiga (idênticas têm a mesma qualidade por definição).
- **Status "Mesma foto"** (`SAME_PHOTO_*` no `main.py`; checkbox "Detectar 'Mesma foto'"):
  terceiro nível entre Idêntica (mesmo arquivo) e Semelhante (parecida): a mesma captura
  em outra versão (redimensionada, recomprimida, EXIF alterado, WhatsApp/iCloud). Só entra
  com TODAS as provas: hashes quase iguais (phash <= 2, dhash <= 4), mesma proporção
  bruta, lado mínimo 100 px, correlação de pixels >= 0,98 no todo (miniatura 64x64),
  pior região com erro <= 0,30, correlação de gradientes >= 0,85, e sem indício de rajada
  no EXIF (DateTimeOriginal diferindo em até 1 h, SubSec ou ImageUniqueID diferentes; o
  veto não vale para conteúdo pixel-idêntico, ex.: data corrigida por script). Falta de
  prova = não. Classes por ligação completa (uma cadeia não arrasta fotos diferentes).
  Botões "Selecionar Mesma foto" / "Selecionar Todas Mesma Foto" mantêm a de melhor
  qualidade; classes com cópia "ampliada?" (bytes/pixel muito menor) ficam para você.
  Limitações conhecidas: cena estática de tripé sem EXIF pode entrar como "mesma foto";
  cópia com rotação gravada nos pixels nunca é candidata; edições leves (cor, recorte de
  poucos pixels) contam como a mesma foto. Calibrado no acervo real em 2026-08-24.
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
ImageCleaner.exe --selftest PASTA --videos --others # também compara vídeos e outros por bytes
```
Roda o pipeline inteiro (listar, hash, agrupar, MD5, confirmação) sem alterar
nada nem usar o cache, e imprime um resumo (também gravado no log). Quando há
arquivos comparados por bytes, o resumo ganha o trecho `| BYTES: ...`.

### Testes automatizados
`python -m pytest tests -q` (o `testar_antes_build.bat` roda isso se o pytest
estiver instalado). Inclui snapshots dourados do modo de uma pasta com e sem
a confirmação; regenerá-los só quando uma mudança de comportamento for
intencional (ver docstring de `tests/test_regression_single_mode.py`).
`tests/test_caracterizacao_fluxo.py` fotografa o fluxo completo da interface
(diálogos, janelas de progresso, grupos, barra de status) em 17 cenários, e
exige que vídeos e outros arquivos na pasta não mudem nada com só Fotos marcado.

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
