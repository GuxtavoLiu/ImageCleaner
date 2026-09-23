import bisect
import csv
import json
import os
import subprocess
import hashlib
import logging
import logging.handlers
import shutil
import sqlite3
import stat
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from PIL import Image, ImageDraw, ImageFont, ImageTk, ImageFile
import imagehash
import numpy as np
from datetime import datetime

# Módulos-folha do próprio projeto (não importam nada daqui)
import ffmpegtools
import mp4probe
import shellthumb

# Exclusão só pela Lixeira do Windows. Sem o send2trash o app RECUSA excluir
# (nunca cai para exclusão definitiva).
try:
    from send2trash import send2trash as _send2trash
except Exception:  # pragma: no cover - depende do ambiente
    _send2trash = None

# Permite carregar imagens truncadas/corrompidas parcialmente
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ---------------------------------------------------------------------------
# Configurações de desempenho (pensadas para volumes grandes, ex: 100 GB)
# ---------------------------------------------------------------------------

# Número de threads usadas para calcular os hashes das imagens.
# O Pillow libera o GIL durante a decodificação, então threads escalam bem.
# 8 é um bom equilíbrio: em SSD já é quase o máximo; em HDD externo mais do
# que isso vira "tempestade de seeks" e piora a leitura sequencial.
HASH_WORKERS = max(2, min(8, os.cpu_count() or 4))

# JPEGs podem ser decodificados diretamente em escala reduzida (1/2, 1/4, 1/8),
# o que deixa o cálculo do phash de 2 a 4x mais rápido. O phash já reduz a
# imagem para 32x32, então o resultado é praticamente o mesmo (em testes com
# fotos reais, 98% dos hashes ficaram idênticos e o restante variou 2 bits).
# DESLIGADO por padrão para manter os hashes bit a bit iguais aos da versão
# anterior. Hashes calculados com e sem esta opção ficam em tabelas de cache
# separadas, então nunca se misturam.
USE_FAST_JPEG_DECODE = False

# Tamanho de bloco para leitura de arquivos no cálculo do MD5.
MD5_CHUNK_SIZE = 1024 * 1024

# Versão do cache de hashes. Se o algoritmo de hash mudar, incremente este
# número para invalidar caches antigos.
HASH_CACHE_VERSION = 1

# Extensões de imagem consideradas no escaneamento (comparação case-insensitive).
VALID_EXTENSIONS = [".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"]

# ---------------------------------------------------------------------------
# Comparação por BYTES (vídeos, outros arquivos e fotos que o Pillow não abre).
# Só existe um status aqui: "Idêntica", e só com MD5 do arquivo INTEIRO igual.
# A amostragem (chave rápida) serve para eliminar, nunca para confirmar: uma
# cópia com setores zerados no meio (HD recuperado) passaria numa amostragem.
# ---------------------------------------------------------------------------

# Chave rápida: BYTE_QUICK_CHUNK bytes do início, do meio e do fim. Arquivos de
# até 3 blocos são lidos inteiros de uma vez (e isso já é o MD5 de verdade).
BYTE_QUICK_CHUNK = 64 * 1024

# Arquivos a partir deste tamanho são lidos com poucos workers: várias leituras
# sequenciais longas ao mesmo tempo viram "tempestade de seeks" num HD.
BYTE_LARGE_FILE = 32 * 1024 * 1024
BYTE_LARGE_WORKERS = 2

# Fotos que o Pillow não abre (HEIC do iPhone, RAW de câmera): entram com a
# caixa "Fotos", mas só por bytes (nunca "Semelhante" nem "Mesma foto").
PHOTO_BYTES_EXTENSIONS = [".heic", ".heif", ".cr2", ".cr3", ".nef", ".arw", ".dng",
                          ".orf", ".rw2", ".raf"]
VIDEO_EXTENSIONS = [".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".avi", ".mkv", ".wmv", ".mpg",
                    ".mpeg", ".mts", ".m2ts", ".webm", ".flv", ".vob", ".asf", ".divx", ".ogv"]
# Qualquer outra extensão é "outro". Nunca entram: arquivos de sistema/miniatura
# e temporários do Office; pastas de sistema do Windows não são percorridas.
IGNORED_FILE_NAMES = {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"}
IGNORED_DIR_NAMES = {"$recycle.bin", "system volume information"}

KIND_PHOTO = "photo"              # pipeline de aparência (phash), como sempre
KIND_PHOTO_BYTES = "photo_bytes"  # foto comparada só por bytes
KIND_VIDEO = "video"
KIND_OTHER = "other"

# Atributos do Windows que tiram um arquivo da comparação por bytes: de sistema,
# ou só na nuvem (OneDrive): ler dispararia o download do acervo inteiro.
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_BYTE_SKIP_ATTRIBUTES = (_FILE_ATTRIBUTE_SYSTEM | _FILE_ATTRIBUTE_OFFLINE
                         | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def classify_file(name):
    """Tipo de um arquivo pela extensão: KIND_PHOTO, KIND_PHOTO_BYTES,
       KIND_VIDEO ou KIND_OTHER."""
    ext = os.path.splitext(name)[1].lower()
    if ext in _PHOTO_EXT_SET:
        return KIND_PHOTO
    if ext in _PHOTO_BYTES_EXT_SET:
        return KIND_PHOTO_BYTES
    if ext in _VIDEO_EXT_SET:
        return KIND_VIDEO
    return KIND_OTHER


_PHOTO_EXT_SET = frozenset(VALID_EXTENSIONS)
_PHOTO_BYTES_EXT_SET = frozenset(PHOTO_BYTES_EXTENSIONS)
_VIDEO_EXT_SET = frozenset(VIDEO_EXTENSIONS)


def byte_file_ok(entry, st):
    """Um arquivo só entra na comparação por bytes se dá para lê-lo com
       segurança: tem stat, não é vazio, não é atalho simbólico (apagar o alvo
       e manter o link seria perda de dados), não é de sistema nem está só na
       nuvem, e não é miniatura/temporário conhecido."""
    name = entry.name.lower()
    if name in IGNORED_FILE_NAMES or name.startswith("~$"):
        return False
    if st is None or not st.st_size:
        return False
    try:
        if entry.is_symlink():
            return False
    except OSError:
        return False
    return not (getattr(st, "st_file_attributes", 0) & _BYTE_SKIP_ATTRIBUTES)


def make_scan_accept(photos=True, videos=False, others=False):
    """
    Predicado accept(entry, st) para list_image_files listar, numa única
    caminhada pelo disco, tudo o que as caixas da tela inicial pedem. Para as
    fotos do pipeline de aparência a regra é a de sempre (só a extensão; stat
    com erro ainda lista). Para o resto vale byte_file_ok.
    """
    def accept(entry, st):
        kind = classify_file(entry.name)
        if kind == KIND_PHOTO:
            return photos
        wanted = photos if kind == KIND_PHOTO_BYTES else videos if kind == KIND_VIDEO else others
        return wanted and byte_file_ok(entry, st)
    return accept

# Quantidade de miniaturas exibidas por vez dentro de um grupo. Grupos maiores
# ganham botões "anteriores/seguintes" dentro do próprio grupo (para grupos com
# milhares de imagens não travarem a interface). As ações (selecionar, mover,
# excluir) continuam valendo para TODAS as imagens do grupo.
MAX_IMAGES_PER_GROUP_DISPLAY = 200

# Tamanho (px) das miniaturas na tela de grupos. Não afeta a velocidade de
# leitura (o draft do JPEG cai na mesma escala 1/8 para fotos grandes); afeta a
# altura das linhas e a memória do cache.
THUMB_SIZE = 200

# Miniaturas já geradas ficam em cache na tela de grupos: trocar de página ou
# verificar um grupo não decodifica de novo os JPEGs originais. O limite é
# calculado para ~160 MB independentemente do THUMB_SIZE (4000 x 100x100).
THUMB_CACHE_SIZE = max(500, int(4000 * (100 / THUMB_SIZE) ** 2))

# Renderização da página de grupos em lotes (ver render_page): quantos grupos
# aparecem de imediato e quantos entram por lote nos ciclos seguintes.
RENDER_FIRST_CHUNK = 4
RENDER_CHUNK = 3

# Grupos com mais de COLLAPSE_THRESHOLD imagens (rajadas, dezenas de cópias)
# aparecem recolhidos: só as COLLAPSE_SHOW primeiras + "e mais N [Expandir]".
COLLAPSE_THRESHOLD = 8
COLLAPSE_SHOW = 4

# Pré-visualização lado a lado: colunas visíveis por vez
PREVIEW_COLUMNS = 3

# ---------------------------------------------------------------------------
# Aparência: uma paleta e um único helper de botões para toda a interface.
# Tema ttk "vista" (nativo do Windows) para caixas, barras e scrollbars.
# ---------------------------------------------------------------------------
PALETTE = {
    "primary": "#2E7D32", "bg": "#FAFAFA", "panel": "#FFFFFF", "text": "#333333",
    "muted": "#777777", "selected_row": "#E8F5E9", "reference_row": "#F1F8E9",
}
FONT_UI = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 18, "bold")
# kind -> (fundo, fundo ao clicar, texto)
BUTTON_KINDS = {
    "primary": ("#2E7D32", "#1B5E20", "white"),
    "same": ("#00897B", "#00695C", "white"),
    "select": ("#4CAF50", "#388E3C", "white"),
    "similar": ("#FF9800", "#EF6C00", "white"),
    "move": ("#1565C0", "#0D47A1", "white"),
    "danger": ("#D32F2F", "#B71C1C", "white"),
    "neutral": ("#607D8B", "#455A64", "white"),
    "light": ("#E0E0E0", "#BDBDBD", "#333333"),
}


def make_button(parent, text, kind="light", **kw):
    """Botão plano com a paleta do app. kind: chave de BUTTON_KINDS.
       Continua sendo um tk.Button (os testes localizam por classe)."""
    bg, active, fg = BUTTON_KINDS[kind]
    opts = dict(text=text, bg=bg, fg=fg, activebackground=active, activeforeground=fg,
                relief="flat", bd=0, padx=10, pady=4, cursor="hand2", font=FONT_UI,
                disabledforeground="#9E9E9E")
    opts.update(kw)
    return tk.Button(parent, **opts)


def format_bytes(n):
    """Tamanho legível em pt-BR: 1.245 bytes -> '1,2 KB'; 3.4e9 -> '3,2 GB'."""
    if n is None:
        return "?"
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f}".replace(".", ",") + f" {unit}"
        value /= 1024
    return f"{n} bytes"


def format_resolution(dims):
    """(largura, altura) -> '4608 x 3456 (15,9 MP)'; None -> 'desconhecida'."""
    if not dims:
        return "desconhecida"
    w, h = dims
    mp = w * h / 1e6
    mp_str = (f"{mp:.1f}" if mp < 10 else f"{mp:.0f}").replace(".", ",")
    return f"{w} x {h} ({mp_str} MP)"


# Quando o resultado tem vídeos ou outros arquivos, os textos da tela falam em
# "arquivos" (com a concordância certa). Só com fotos, NADA é trocado: os
# textos de sempre ficam byte a byte iguais. Ordem importa: frases primeiro.
FILE_WORDING = [
    ("imagens idênticas foram selecionadas (mantendo a mais antiga",
     "arquivos idênticos foram selecionados (mantendo o mais antigo"),
    ("imagens idênticas foram selecionadas", "arquivos idênticos foram selecionados"),
    ("Imagens da referência nunca são selecionadas", "Arquivos da referência nunca são selecionados"),
    ("imagem(ns) da referência foram ignoradas (protegidas)",
     "arquivo(s) da referência foram ignorados (protegidos)"),
    ("Nenhuma imagem selecionada", "Nenhum arquivo selecionado"),
    ("imagens selecionadas", "arquivos selecionados"),
    ("imagem(ns) selecionada(s)", "arquivo(s) selecionado(s)"),
    ("imagens enviadas", "arquivos enviados"),
    ("imagem(ns) enviada(s)", "arquivo(s) enviado(s)"),
    ("imagens movidas", "arquivos movidos"),
    ("imagem(ns) movida(s)", "arquivo(s) movido(s)"),
    ("imagem(ns) devolvida(s)", "arquivo(s) devolvido(s)"),
    ("(e selecionada(s) de novo)", "(e selecionado(s) de novo)"),
    ("imagem(ns) ignorada(s)", "arquivo(s) ignorado(s)"),
    ("ignorada(s):", "ignorado(s):"),
    ("restaurá-las", "restaurá-los"),
    ("não puderam ser devolvidas", "não puderam ser devolvidos"),
    ("não selecionada", "não selecionado"),
    ("SELECIONADA", "SELECIONADO"),
    ("(continuam selecionadas)", "(continuam selecionados)"),
    ("selecionada(s) no total", "selecionado(s) no total"),
    ("Selecionadas neste grupo", "Selecionados neste grupo"),
    ("Selecionadas:", "Selecionados:"),
    ("Grupos de Imagens Similares", "Grupos de Arquivos Duplicados"),
    ("Abrir imagem", "Abrir arquivo"),
    ("imagem(ns)", "arquivo(s)"),
    ("imagens", "arquivos"),
    ("imagem", "arquivo"),
]


def is_file_kind(kind):
    """True para o que a tela chama de "arquivo" (vídeo, outro). Foto HEIC/RAW
       é comparada por bytes, mas continua sendo foto: o vocabulário não muda."""
    return kind in (KIND_VIDEO, KIND_OTHER)


def file_wording(text):
    """Troca o vocabulário de 'imagens' pelo de 'arquivos' (ver FILE_WORDING)."""
    for old, new in FILE_WORDING:
        text = text.replace(old, new)
    return text


def media_summary(kind, filepath, info):
    """Texto curto que ocupa o lugar de 'Resolução' nas linhas que não são
       foto: 'Vídeo 1280 x 720, 0:33' ou 'Arquivo MP4'."""
    ext = os.path.splitext(filepath)[1].lstrip(".").upper() or "sem extensão"
    if kind == KIND_VIDEO:
        parts = []
        if info and info.get("width"):
            parts.append(f"{info['width']} x {info['height']}")
        if info and info.get("duration"):
            parts.append(mp4probe.format_duration(info["duration"]))
        return f"Vídeo {ext}" + (": " + ", ".join(parts) if parts else "")
    if kind == KIND_PHOTO_BYTES:
        return f"Foto {ext} (comparada só por conteúdo)"
    return f"Arquivo {ext}"


_PLATE_COLORS = {KIND_VIDEO: "#37474F", KIND_PHOTO_BYTES: "#4E342E", KIND_OTHER: "#607D8B"}


def extension_plate(filepath, kind, width, height):
    """Placa com a extensão em destaque: o que aparece na hora para um arquivo
       que não é foto, até (e se) a miniatura do Explorer chegar."""
    img = Image.new("RGB", (max(16, width), max(16, height)), _PLATE_COLORS.get(kind, "#607D8B"))
    text = (os.path.splitext(filepath)[1].lstrip(".").upper() or "?")[:6]
    draw = ImageDraw.Draw(img)
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            font = ImageFont.truetype(name, max(12, min(width, height) // 4))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((img.width - (box[2] - box[0])) // 2 - box[0],
               (img.height - (box[3] - box[1])) // 2 - box[1]), text, fill="white", font=font)
    return img


BADGE_STYLES = {
    "mesma foto": ("#004D40", "#B2DFDB"),
    "live photo": ("#4A148C", "#E1BEE7"),
    "mesmo vídeo": ("#01579B", "#B3E5FC"),
    "maior resolução": ("#1B5E20", "#C8E6C9"),
    "mais antiga": ("#0D47A1", "#BBDEFB"),
    "maior arquivo": ("#424242", "#EEEEEE"),
}


def plan_badges(metas):
    """
    Rótulos que destacam o que difere entre as imagens de um grupo.
    metas: lista de dicts com 'pixels' (int ou None), 'size' (int ou None) e
    'mtime' (float ou None). Retorna {posição: [rótulos]}. Um rótulo só
    aparece quando há diferença real no grupo (cópias idênticas não ganham
    "maior arquivo"); empates dão o rótulo a todas as empatadas.
    """
    out = {}

    def mark(key, label, best):
        values = [m.get(key) for m in metas]
        if any(v is None for v in values) or len(set(values)) < 2:
            return
        target = best(values)
        for pos, v in enumerate(values):
            if v == target:
                out.setdefault(pos, []).append(label)

    mark("pixels", "maior resolução", max)
    mark("mtime", "mais antiga", min)
    mark("size", "maior arquivo", max)
    return out


def shorten_path(filepath, roots):
    """
    Caminho curto para a tela: (tag, pasta_relativa, nome_do_arquivo).
    roots: lista de (tag, pasta_raiz); a primeira raiz que contém o arquivo
    vence (ex.: [("ALVO", alvo), ("REF", referência)]). Fora de qualquer raiz:
    tag vazia e a pasta completa.
    """
    name = os.path.basename(filepath)
    key = cache_key(filepath)
    for tag, root in roots:
        if root and key.startswith(folder_prefix(root)):
            rel = os.path.dirname(os.path.relpath(filepath, root))
            return tag, ("" if rel == "." else rel), name
    return "", os.path.dirname(filepath), name


def get_app_icon_path():
    """assets/icon.ico ao lado do main.py ou dentro do pacote PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "icon.ico")


def set_windows_app_identity():
    """No Windows, dá ao processo uma identidade própria (AppUserModelID).
       Sem isso a barra de tarefas agrupa a janela com o pythonw.exe e mostra
       o ícone do Python em vez do ícone do app."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GustavoLiu.ImageCleaner")
    except Exception as e:
        log.warning("AppUserModelID não definido: %s", e)


def apply_app_icon(root):
    """Ícone do app na janela principal e, no Windows, em todas as janelas
       filhas (default=). Sem o arquivo, fica o ícone padrão do Tk."""
    path = get_app_icon_path()
    if not os.path.exists(path):
        return
    try:
        root.iconbitmap(default=path)
    except tk.TclError as e:
        log.warning("Ícone não aplicado (%s): %s", path, e)


def apply_theme(root):
    """Tema visual global: ttk 'vista' (fallback 'clam'), fonte e fundo
       padrão para os widgets criados a partir daqui. Só aparência."""
    try:
        style = ttk.Style(root)
        names = style.theme_names()
        style.theme_use("vista" if "vista" in names else "clam")
    except tk.TclError:
        pass
    try:
        root.option_add("*Font", FONT_UI)
        root.option_add("*Background", PALETTE["bg"])
        root.option_add("*Text.background", "white")
        root.option_add("*Entry.background", "white")
        root.configure(bg=PALETTE["bg"])
    except tk.TclError:
        pass

# Threshold de similaridade (distância de Hamming máxima entre phashes).
SIMILARITY_THRESHOLD = 10

# "Selecionar Semelhantes": qual imagem do grupo é MANTIDA (as outras são
# selecionadas). Critérios em ordem; o seguinte só decide em caso de empate.
#   "resolution": maior largura x altura (desconhecida perde para conhecida)
#   "size":       maior arquivo em bytes
#   "mtime":      data de modificação mais antiga
# Empate total: a primeira na ordem do grupo. Com ("mtime",) volta a regra
# original "mantém a mais antiga". Não afeta "Selecionar Idênticas".
SIMILAR_KEEP_PRIORITY = ("resolution", "size", "mtime")
SIMILAR_RULE_TOOLTIP = ("Semelhantes: fotos parecidas entre si, mas que não são a mesma imagem\n"
                        "(ou o programa não teve certeza de que são).\n"
                        "Este botão mantém a melhor versão de cada grupo (maior resolução, depois\n"
                        "arquivo maior, depois a mais antiga) e seleciona as outras.\n"
                        "Se houver foto do acervo de referência no grupo, ela é a mantida.\n"
                        "Cópias idênticas não entram aqui: use 'Selecionar Idênticas'.")

# ---------------------------------------------------------------------------
# "Mesma foto": mesma captura em outra versão (redimensionada, recomprimida,
# EXIF alterado). Terceiro status entre "Idêntica" e "Semelhante". Pós-filtro
# dentro dos grupos: nada do agrupamento muda. Desligado = resultado atual.
# Toda prova é obrigatória e falta de prova é "não" (ver same_photo_pair_ok).
# ---------------------------------------------------------------------------
# Calibração em 2026-08-24 no acervo real (667 grupos, 1.324 pares com MD5
# diferente): 605 pares confirmados em 506 classes; nenhuma foto apenas
# parecida (phash 8 a 10) passa; rajadas de câmera ficam a NCC 0,973 a 0,979
# ou são vetadas pelo EXIF (12); uma rajada sem EXIF (NCC 0,981) cai só no
# gradiente (0,81 contra p1 = 0,93 dos confirmados). Porta do dhash em 4
# acrescenta 6 confirmações sem nenhum nome de rajada.
SAME_PHOTO_ENABLED = True
SAME_PHOTO_PHASH_MAX = 2            # distâncias de phash são sempre pares: 0 ou 2
SAME_PHOTO_DHASH_MAX = 4            # porta de candidatura (2 = 632 candidatos; 4 = 662; 6 = 677)
SAME_PHOTO_MIN_SIDE = 100           # lado menor mínimo (px): ícones não entram
SAME_PHOTO_ASPECT_TOL = 0.01        # tolerância relativa da proporção (dimensões brutas)
SAME_PHOTO_GRAY_SIZE = 64           # miniatura em cinza (32x32 deixa passar 14% das quase-rajadas)
SAME_PHOTO_MIN_STD = 6.0            # desvio mínimo da miniatura (níveis de cinza)
SAME_PHOTO_NCC_MIN = 0.98           # correlação global mínima (candidatos p5 = 0,998; rajadas < 0,98)
SAME_PHOTO_BLOCKS = 4               # grade 4x4 = 16 blocos para a pior região
SAME_PHOTO_BLOCK_ERR_MAX = 0.30     # erro quadrático médio máximo num bloco (candidatos p99 = 0,26)
SAME_PHOTO_GRAD_NCC_MIN = 0.85      # correlação de gradientes (confirmados p1 = 0,93; parecidas p95 = 0,66)
SAME_PHOTO_EXIF_VETO = True
SAME_PHOTO_EXIF_WINDOW = 3600       # DateTimeOriginal diferindo até isto (s) = rajada/sequência: veta
SAME_PHOTO_PIXEL_IDENTICAL_NCC = 0.999   # conteúdo pixel-idêntico: o veto EXIF não se aplica
SAME_PHOTO_PIXEL_IDENTICAL_ERR = 0.01
SAME_PHOTO_UPSCALE_BPP_RATIO = 0.5  # maior em pixels com bytes/pixel < 50% da menor = "ampliada?"
SAME_PHOTO_RULE_TOOLTIP = ("Mesma foto: a mesma imagem guardada mais de uma vez em versões diferentes\n"
                           "(menor, mais comprimida, reenviada pelo WhatsApp, com data alterada...).\n"
                           "O programa só usa esse rótulo quando tem certeza, depois de várias\n"
                           "verificações independentes; em dúvida, deixa como 'Semelhante'.\n"
                           "Este botão mantém a melhor versão (maior resolução, depois arquivo maior,\n"
                           "depois a mais antiga) e seleciona as outras para você mover ou excluir.\n"
                           "Se uma cópia parecer 'ampliada' artificialmente, o programa não decide por você.")


# Confirmação de "Semelhante" por um segundo hash (dhash). É um pós-filtro:
# não altera o agrupamento por phash (find_similar_groups); dentro de cada
# grupo, um par com MD5 diferente só continua junto se o dhash também estiver
# próximo. Desligado, o resultado é exatamente o de sempre. Motivo: fotos
# totalmente diferentes podem cair a distância 10 de phash por coincidência de
# luz/sombra grossa; o dhash (gradientes) nessas mesmas fotos fica em 23 a 36,
# enquanto quase-duplicatas reais ficam em 0 a 6.
CONFIRM_SIMILAR = True

# Distância de Hamming máxima entre dhashes (64 bits) para confirmar um par.
# Calibrado no acervo real (ver calibração de 2026-08-24).
DHASH_THRESHOLD = 14

# phash "degenerado": quase nenhum ou quase todos os bits ligados. Acontece com
# imagens lisas (toda preta/branca) e com PNGs transparentes convertidos para
# preto. Todas caem no mesmo hash, então imagens degeneradas só ficam em grupo
# por MD5 igual (nunca como "Semelhante"). Um phash normal tem 32 bits ligados.
DEGENERATE_MIN_BITS = 4
DEGENERATE_MAX_BITS = 60

# Extensões que podem carregar transparência (para recalcular hashes antigos
# degenerados que vieram do cache: ver hash_files).
ALPHA_CAPABLE_EXTENSIONS = {".png", ".gif", ".webp", ".tif", ".tiff"}


def get_app_data_dir():
    """Pasta de dados do app (cache e log) em %LOCALAPPDATA%/ImageCleaner."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    app_dir = os.path.join(base, "ImageCleaner")
    os.makedirs(app_dir, exist_ok=True)
    return app_dir


def get_hash_cache_path():
    """Retorna o caminho do arquivo de cache de hashes."""
    return os.path.join(get_app_data_dir(), "hash_cache.sqlite")


def get_log_path():
    return os.path.join(get_app_data_dir(), "imagecleaner.log")


def get_settings_path():
    return os.path.join(get_app_data_dir(), "settings.json")


DEFAULT_SETTINGS = {
    "recent_targets": [],        # últimas pastas alvo (mais recente primeiro)
    "recent_references": [],     # últimas pastas de referência
    "scan_subfolders": 1,
    "use_cache": 1,
    "confirm_similar": 1,
    "same_photo": 1,
    "show_target_only": 1,
    "scan_photos": 1,            # tipos procurados (caixas da tela inicial)
    "scan_videos": 0,
    "scan_others": 0,
    "same_video": 1,             # "Mesmo vídeo" (só age com Vídeos marcado e ffmpeg encontrado)
    "ffmpeg_path": "",           # escolhido à mão em "Localizar..."; vazio = busca automática
}
RECENT_LIMIT = 5


def load_settings(path=None):
    """Lê o settings.json; arquivo ausente/corrompido => padrões (com log)."""
    settings = dict(DEFAULT_SETTINGS)
    path = path or get_settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in DEFAULT_SETTINGS:
                if key in data and type(data[key]) is type(DEFAULT_SETTINGS[key]):
                    settings[key] = data[key]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Configurações: %s ilegível (%s); usando padrões", path, e)
    return settings


def save_settings(settings, path=None):
    """Grava o settings.json (erro só vai para o log)."""
    path = path or get_settings_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Configurações: falha ao gravar %s: %s", path, e)


def push_recent(items, folder, limit=RECENT_LIMIT):
    """Nova lista de recentes com `folder` na frente (sem duplicar, limitada)."""
    key = cache_key(folder)
    rest = [f for f in items if cache_key(f) != key]
    return [folder] + rest[:limit - 1]


def get_reports_dir():
    """Pasta dos relatórios CSV de sessão."""
    path = os.path.join(get_app_data_dir(), "relatorios")
    os.makedirs(path, exist_ok=True)
    return path


class SessionReport:
    """
    Relatório CSV da sessão: uma linha por arquivo movido, enviado à Lixeira
    ou devolvido pelo "Desfazer". O arquivo só é criado no primeiro registro
    (sessões sem ação não deixam relatório). Erros são logados, nunca
    interrompem a ação que estava sendo registrada.
    """
    COLUMNS = ["data_hora", "acao", "grupo", "status", "origem", "caminho", "destino", "tamanho"]

    def __init__(self, directory=None):
        self.directory = directory
        self.path = None

    def record(self, rows):
        if not rows:
            return
        try:
            if self.path is None:
                directory = self.directory or get_reports_dir()
                os.makedirs(directory, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.path = os.path.join(directory, f"sessao_{stamp}.csv")
                with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
                    csv.DictWriter(f, fieldnames=self.COLUMNS, delimiter=";").writeheader()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.path, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=self.COLUMNS, delimiter=";", extrasaction="ignore")
                for row in rows:
                    writer.writerow({"data_hora": now, **row})
        except Exception as e:
            log.warning("Relatório: falha ao gravar %s: %s", self.path, e)


def trash_available():
    return _send2trash is not None


def trash_file(filepath):
    """Envia o arquivo para a Lixeira do Windows. Levanta exceção se não der."""
    if _send2trash is None:
        raise RuntimeError("send2trash indisponível: exclusão recusada")
    _send2trash(os.path.normpath(filepath))


log = logging.getLogger("imagecleaner")


def setup_logging():
    """
    Log rotativo em %LOCALAPPDATA%/ImageCleaner/imagecleaner.log (2 MB x 3).
    Indispensável no .exe (--windowed não tem console): qualquer erro
    inesperado fica registrado ali. Se não der para criar o arquivo, o
    programa segue sem log.
    """
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        handler = logging.handlers.RotatingFileHandler(
            get_log_path(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except Exception:
        pass
    if sys.stderr is not None:
        try:
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            log.addHandler(console)
        except Exception:
            pass


def cache_key(filepath):
    """Chave normalizada do cache (não altera o caminho usado na interface)."""
    return os.path.normcase(os.path.abspath(filepath))


class HashCache:
    """
    Cache persistente (SQLite) de hashes já calculados.

    A chave é (caminho normalizado, tamanho, mtime). Se qualquer um mudar, o
    cache é ignorado e o hash é recalculado. Isso permite re-escanear pastas
    enormes quase instantaneamente e retomar escaneamentos cancelados.

    Qualquer erro no cache é tratado silenciosamente: o programa continua
    funcionando normalmente, apenas sem cache. Só a thread principal usa o cache.
    """
    def __init__(self, path=None):
        self.conn = None
        self.pending = []
        self.loaded = {}
        self.loaded_prefixes = []
        self.path = path or get_hash_cache_path()
        mode = "draft" if USE_FAST_JPEG_DECODE else "full"
        self.table = f"hashes_v{HASH_CACHE_VERSION}_{mode}"
        try:
            self.conn = sqlite3.connect(self.path, timeout=5)
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
            except Exception as e:
                log.warning("Cache: PRAGMA falhou (%s): %s", self.path, e)
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} ("
                "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
                "phash TEXT, md5 TEXT, dhash TEXT)"
            )
            self.conn.commit()
        except Exception as e:
            log.warning("Cache desativado: não foi possível abrir %s: %s", self.path, e)
            self.conn = None
        # Migração: bancos criados antes da coluna dhash ganham a coluna sem
        # perder nada (ALTER TABLE ADD COLUMN não reescreve as linhas; o
        # phash/MD5 já calculados continuam válidos).
        self.has_dhash = False
        if self.conn is not None:
            try:
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({self.table})")}
                if "dhash" not in cols:
                    self.conn.execute(f"ALTER TABLE {self.table} ADD COLUMN dhash TEXT")
                    self.conn.commit()
                    log.info("Cache: coluna dhash adicionada a %s", self.table)
                self.has_dhash = True
            except Exception as e:
                log.warning("Cache: sem coluna dhash (%s); segundo hash não será cacheado", e)
        self._cols = "size, mtime_ns, phash, md5" + (", dhash" if self.has_dhash else "")

    @staticmethod
    def _row5(row):
        """Normaliza um registro para (size, mtime_ns, phash, md5, dhash)."""
        return tuple(row) + (None,) * (5 - len(row))

    @property
    def active(self):
        """True se o cache está realmente funcionando (arquivo aberto)."""
        return self.conn is not None

    def load_prefix(self, root, merge=False):
        """
        Carrega em memória, de uma vez, todos os registros sob a pasta `root`
        (uma consulta por faixa na chave primária, muito mais rápido do que
        consultar arquivo por arquivo).

        Com merge=True, soma os registros aos já carregados em vez de
        substituí-los (modo de comparação de duas pastas: uma chamada por raiz).
        """
        if not merge:
            self.loaded = {}
            self.loaded_prefixes = []
        if self.conn is None:
            return
        try:
            prefix = cache_key(root).rstrip("\\/") + os.sep
            # Limite superior da faixa: prefixo com o último caractere incrementado.
            # (prefixo + "\uffff" perderia caminhos com caracteres fora do BMP,
            # como emojis, na ordenação binária UTF-8 do SQLite.)
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            rows = self.conn.execute(
                f"SELECT path, {self._cols} FROM {self.table} "
                "WHERE path >= ? AND path < ?",
                (prefix, upper)
            ).fetchall()
            self.loaded.update({r[0]: self._row5(r[1:]) for r in rows})
            self.loaded_prefixes.append(prefix)
            log.info("Cache: %d registros carregados para %s", len(rows), root)
        except Exception as e:
            # Falha em UMA raiz não invalida as já carregadas: apenas não
            # registra o prefixo desta (seus arquivos caem na consulta
            # individual do lookup, que continua funcionando).
            log.warning("Cache: falha ao carregar registros de %s: %s", root, e)

    def _find_row(self, filepath):
        """Registro (size, mtime_ns, phash, md5, dhash) do arquivo, ou None."""
        key = cache_key(filepath)
        row = self.loaded.get(key)
        if row is None:
            # Se a pasta já foi carregada em memória, ausência no dict é ausência
            # no banco: não vale a pena consultar o SQLite arquivo por arquivo.
            if any(key.startswith(p) for p in self.loaded_prefixes):
                return None
            try:
                row = self.conn.execute(
                    f"SELECT {self._cols} FROM {self.table} WHERE path = ?",
                    (key,)
                ).fetchone()
            except Exception as e:
                log.warning("Cache: falha na consulta de %s: %s", filepath, e)
                return None
            if row is not None:
                row = self._row5(row)
        return row

    def lookup(self, filepath, size, mtime_ns):
        """Retorna (phash_str, md5_ou_None) se houver cache válido, senão None."""
        if self.conn is None:
            return None
        row = self._find_row(filepath)
        if row and row[0] == size and row[1] == mtime_ns:
            return row[2], row[3]
        return None

    def lookup_dhash(self, filepath, size, mtime_ns):
        """Retorna o dhash (hex) se houver cache válido com dhash, senão None."""
        if self.conn is None:
            return None
        row = self._find_row(filepath)
        if row and row[0] == size and row[1] == mtime_ns:
            return row[4]
        return None

    def store(self, filepath, size, mtime_ns, phash_str, md5=None, dhash=None):
        """Agenda a gravação de um registro (gravado em lote no flush).
           Substitui a linha inteira: md5/dhash não repassados ficam nulos."""
        if self.conn is None:
            return
        self.pending.append(("insert", (cache_key(filepath), size, mtime_ns, phash_str, md5, dhash)))
        if len(self.pending) >= 500:
            self.flush()

    def update_md5(self, filepath, md5):
        """Agenda a gravação do MD5 de um arquivo já cacheado."""
        if self.conn is None:
            return
        self.pending.append(("md5", (md5, cache_key(filepath))))
        if len(self.pending) >= 500:
            self.flush()

    def update_dhash(self, filepath, dhash_str):
        """Agenda a gravação do dhash de um arquivo já cacheado."""
        if self.conn is None or not self.has_dhash:
            return
        self.pending.append(("dhash", (dhash_str, cache_key(filepath))))
        if len(self.pending) >= 500:
            self.flush()

    def flush(self):
        if self.conn is None or not self.pending:
            return
        inserts = [args for kind, args in self.pending if kind == "insert"]
        md5s = [args for kind, args in self.pending if kind == "md5"]
        dhashes = [args for kind, args in self.pending if kind == "dhash"]
        self.pending = []
        if self.has_dhash:
            insert_sql = (f"INSERT OR REPLACE INTO {self.table} "
                          "(path, size, mtime_ns, phash, md5, dhash) VALUES (?, ?, ?, ?, ?, ?)")
        else:
            insert_sql = (f"INSERT OR REPLACE INTO {self.table} "
                          "(path, size, mtime_ns, phash, md5) VALUES (?, ?, ?, ?, ?)")
            inserts = [row[:5] for row in inserts]
        update_sql = f"UPDATE {self.table} SET md5 = ? WHERE path = ?"
        dhash_sql = f"UPDATE {self.table} SET dhash = ? WHERE path = ?"
        try:
            if inserts:
                self.conn.executemany(insert_sql, inserts)
            if md5s:
                self.conn.executemany(update_sql, md5s)
            if dhashes:
                self.conn.executemany(dhash_sql, dhashes)
            self.conn.commit()
        except Exception as e:
            # Um único registro inválido (ex.: nome de arquivo com surrogate
            # UTF-16) não pode derrubar o lote inteiro: regrava um a um e
            # descarta apenas os problemáticos.
            log.warning("Cache: falha no lote de %d registros (%s); regravando um a um",
                        len(inserts) + len(md5s) + len(dhashes), e)
            dropped = 0
            for sql, rows in ((insert_sql, inserts), (update_sql, md5s), (dhash_sql, dhashes)):
                for row in rows:
                    try:
                        self.conn.execute(sql, row)
                    except Exception:
                        dropped += 1
            try:
                self.conn.commit()
            except Exception as e2:
                log.warning("Cache: commit do lote falhou: %s", e2)
                return
            if dropped:
                log.warning("Cache: %d registro(s) descartado(s) no lote", dropped)

    def close(self):
        self.flush()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception as e:
                log.warning("Cache: falha ao fechar: %s", e)
            self.conn = None
        self.loaded = {}
        self.loaded_prefixes = []


class SamePhotoCache:
    """
    Cache das provas de "Mesma foto" em tabela própria (mesmo arquivo SQLite
    do HashCache, tabela same_photo_v1): dimensões brutas, campos EXIF e a
    miniatura em cinza (BLOB de gray_size**2 bytes). Fica fora da tabela
    quente de hashes de propósito: load_prefix não a carrega e store() de
    hashes não a toca. Consultas só por lote de caminhos envolvidos.
    """
    TABLE = "same_photo_v1"

    def __init__(self, path=None):
        self.conn = None
        self.pending = []
        self.path = path or get_hash_cache_path()
        try:
            self.conn = sqlite3.connect(self.path, timeout=5)
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
            except Exception as e:
                log.warning("Cache mesma foto: PRAGMA falhou: %s", e)
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
                "width INTEGER, height INTEGER, gray_size INTEGER, gray BLOB, exif TEXT)"
            )
            self.conn.commit()
        except Exception as e:
            log.warning("Cache mesma foto desativado: %s", e)
            self.conn = None

    @property
    def active(self):
        return self.conn is not None

    def lookup_many(self, items, gray_size=None):
        """items: lista de (filepath, size, mtime_ns). Retorna {filepath: features}
           só para registros válidos (size/mtime iguais, blob do tamanho certo)."""
        out = {}
        if self.conn is None or not items:
            return out
        gray_size = gray_size or SAME_PHOTO_GRAY_SIZE
        by_key = {cache_key(fp): (fp, size, mt) for fp, size, mt in items}
        keys = list(by_key)
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self.conn.execute(
                    f"SELECT path, size, mtime_ns, width, height, gray_size, gray, exif "
                    f"FROM {self.TABLE} WHERE path IN ({marks})", chunk).fetchall()
            except Exception as e:
                log.warning("Cache mesma foto: consulta falhou: %s", e)
                return out
            for path, size, mt, w, h, gs, gray, exif in rows:
                fp, want_size, want_mt = by_key[path]
                if size != want_size or mt != want_mt:
                    continue
                if gs != gray_size or gray is None or len(gray) != gray_size * gray_size:
                    continue
                try:
                    exif_d = json.loads(exif) if exif else {}
                except Exception:
                    exif_d = {}
                out[fp] = {"dims": (w, h), "exif": exif_d, "gray": bytes(gray)}
        return out

    def store(self, filepath, size, mtime_ns, features, gray_size=None):
        if self.conn is None:
            return
        gray_size = gray_size or SAME_PHOTO_GRAY_SIZE
        w, h = features["dims"]
        self.pending.append((cache_key(filepath), size, mtime_ns, w, h, gray_size,
                             sqlite3.Binary(features["gray"]),
                             json.dumps(features.get("exif") or {}, ensure_ascii=False)))
        if len(self.pending) >= 500:
            self.flush()

    def flush(self):
        if self.conn is None or not self.pending:
            return
        rows = self.pending
        self.pending = []
        sql = (f"INSERT OR REPLACE INTO {self.TABLE} "
               "(path, size, mtime_ns, width, height, gray_size, gray, exif) VALUES (?,?,?,?,?,?,?,?)")
        try:
            self.conn.executemany(sql, rows)
            self.conn.commit()
        except Exception as e:
            log.warning("Cache mesma foto: lote de %d falhou (%s); um a um", len(rows), e)
            dropped = 0
            for row in rows:
                try:
                    self.conn.execute(sql, row)
                except Exception:
                    dropped += 1
            try:
                self.conn.commit()
            except Exception as e2:
                log.warning("Cache mesma foto: commit falhou: %s", e2)
            if dropped:
                log.warning("Cache mesma foto: %d registro(s) descartado(s)", dropped)

    def close(self):
        self.flush()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


class FileHashCache:
    """
    Cache da comparação por bytes, em tabela própria (mesmo arquivo SQLite do
    HashCache, tabela files_v1): chave rápida, MD5 do arquivo inteiro e, para
    vídeos, hash dos fluxos e dados do cabeçalho. Mesmo desenho do
    SamePhotoCache: consulta só por lote de caminhos candidatos (a grande
    maioria dos arquivos tem tamanho único e nunca chega aqui) e gravação em
    lote, sempre na thread principal. Registro com tamanho ou data diferentes
    do arquivo atual é ignorado e, na gravação seguinte, zerado por inteiro.
    """
    TABLE = "files_v1"
    FIELDS = ("head", "quick", "md5", "stream", "probe")

    def __init__(self, path=None):
        self.conn = None
        self.pending = []
        self.path = path or get_hash_cache_path()
        try:
            self.conn = sqlite3.connect(self.path, timeout=5)
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=5000")
            except Exception as e:
                log.warning("Cache de arquivos: PRAGMA falhou: %s", e)
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self.TABLE} ("
                "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
                + ", ".join(f"{name} TEXT" for name in self.FIELDS) + ")"
            )
            # Banco criado por uma versão com menos campos: acrescenta os que
            # faltam (ADD COLUMN não reescreve as linhas já gravadas)
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({self.TABLE})")}
            for name in self.FIELDS:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {self.TABLE} ADD COLUMN {name} TEXT")
            self.conn.commit()
        except Exception as e:
            log.warning("Cache de arquivos desativado: %s", e)
            self.conn = None

    @property
    def active(self):
        return self.conn is not None

    def lookup_many(self, items):
        """items: lista de (filepath, size, mtime_ns). Retorna {filepath: dict
           com as chaves de FIELDS} só para registros válidos (size/mtime iguais)."""
        out = {}
        if self.conn is None or not items:
            return out
        by_key = {cache_key(fp): (fp, size, mt) for fp, size, mt in items}
        keys = list(by_key)
        cols = ", ".join(self.FIELDS)
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self.conn.execute(
                    f"SELECT path, size, mtime_ns, {cols} FROM {self.TABLE} "
                    f"WHERE path IN ({marks})", chunk).fetchall()
            except Exception as e:
                log.warning("Cache de arquivos: consulta falhou: %s", e)
                return out
            for row in rows:
                fp, want_size, want_mt = by_key[row[0]]
                if row[1] == want_size and row[2] == want_mt:
                    out[fp] = dict(zip(self.FIELDS, row[3:]))
        return out

    def store(self, filepath, size, mtime_ns, **fields):
        """Agenda a gravação dos campos informados (os não informados são
           preservados, desde que o registro seja do mesmo tamanho e data)."""
        if self.conn is None or size is None or mtime_ns is None:
            return   # sem tamanho E data não há como saber depois se o registro ainda vale
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise ValueError(f"campos desconhecidos: {sorted(unknown)}")
        self.pending.append((cache_key(filepath), size, mtime_ns)
                            + tuple(fields.get(name) for name in self.FIELDS))
        if len(self.pending) >= 500:
            self.flush()

    def flush(self):
        if self.conn is None or not self.pending:
            return
        rows = self.pending
        self.pending = []
        t = self.TABLE
        same = f"{t}.size = excluded.size AND {t}.mtime_ns = excluded.mtime_ns"
        keep = ", ".join(
            f"{name} = CASE WHEN {same} THEN COALESCE(excluded.{name}, {t}.{name}) "
            f"ELSE excluded.{name} END" for name in self.FIELDS)
        sql = (f"INSERT INTO {t} (path, size, mtime_ns, {', '.join(self.FIELDS)}) "
               f"VALUES (?,?,?,{','.join('?' * len(self.FIELDS))}) "
               f"ON CONFLICT(path) DO UPDATE SET {keep}, "
               "size = excluded.size, mtime_ns = excluded.mtime_ns")
        try:
            self.conn.executemany(sql, rows)
            self.conn.commit()
        except Exception as e:
            log.warning("Cache de arquivos: lote de %d falhou (%s); um a um", len(rows), e)
            dropped = 0
            for row in rows:
                try:
                    self.conn.execute(sql, row)
                except Exception:
                    dropped += 1
            try:
                self.conn.commit()
            except Exception as e2:
                log.warning("Cache de arquivos: commit falhou: %s", e2)
            if dropped:
                log.warning("Cache de arquivos: %d registro(s) descartado(s)", dropped)

    def close(self):
        self.flush()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


class ScanCancelled(Exception):
    """Levantada quando o usuário cancela durante o agrupamento."""


class UnionFind:
    """Classe simples de Union-Find (Disjoint Set)."""
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0]*n

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        rootX = self.find(x)
        rootY = self.find(y)
        if rootX != rootY:
            if self.rank[rootX] < self.rank[rootY]:
                self.parent[rootX] = rootY
            elif self.rank[rootX] > self.rank[rootY]:
                self.parent[rootY] = rootX
            else:
                self.parent[rootY] = rootX
                self.rank[rootX] += 1

def get_file_md5(filepath):
    """
    Retorna o hash MD5 de um arquivo.
    Se dois arquivos tiverem o mesmo MD5, são bit-a-bit idênticos.
    """
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(MD5_CHUNK_SIZE), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def _flatten_alpha(img):
    """
    Compõe a transparência sobre fundo branco. Sem isso, o Pillow descarta o
    alfa ao converter para cinza e um PNG com texto preto sobre fundo
    transparente vira uma imagem toda preta (phash degenerado, igual ao de
    qualquer outra imagem preta). Imagens sem transparência voltam intocadas:
    nenhum bit muda para o resto do acervo.
    Detecta: canal "A" (RGBA, LA, PA...) ou chave "transparency" (P, L, RGB
    com tRNS; GIF fica no primeiro quadro).
    """
    if "A" not in img.getbands() and "transparency" not in img.info:
        return img
    rgba = img.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def _prepare_for_hash(img):
    """Preparação comum a phash e dhash: draft JPEG (se ligado) e alfa."""
    if USE_FAST_JPEG_DECODE and img.format == "JPEG":
        # O phash trabalha com a imagem em tons de cinza reduzida a 32x32.
        # Pedimos ao decoder JPEG algo >= 64x64 em modo "L": ele escolhe a
        # maior redução DCT possível (1/2, 1/4 ou 1/8) sem ficar abaixo disso.
        # (JPEG nunca tem alfa, então draft e _flatten_alpha não colidem.)
        img.draft("L", (64, 64))
    return _flatten_alpha(img)


def compute_phash(filepath):
    """
    Calcula o perceptual hash (phash) de uma imagem.
    Para JPEGs, usa decodificação em escala reduzida (muito mais rápida).
    """
    with Image.open(filepath) as img:
        return imagehash.phash(_prepare_for_hash(img))


def compute_dhash(filepath):
    """
    Calcula o difference hash (dhash, 64 bits) de uma imagem: sinal do
    gradiente horizontal numa grade 9x8. Independente do phash (DCT), por
    isso serve de segunda opinião para confirmar "Semelhante".
    """
    with Image.open(filepath) as img:
        return imagehash.dhash(_prepare_for_hash(img))


def is_degenerate_hash(h):
    """
    True se o hash (ImageHash ou int de 64 bits) tem quase nenhum ou quase
    todos os bits ligados (imagem lisa ou transparente convertida para preto).
    """
    value = h if isinstance(h, int) else hash_to_int(h)
    bits = bin(value).count("1")
    return bits <= DEGENERATE_MIN_BITS or bits >= DEGENERATE_MAX_BITS


def hash_to_int(h):
    """Converte um ImageHash (matriz de bits) em inteiro Python."""
    bits = np.asarray(h.hash, dtype=bool).flatten()
    return int.from_bytes(np.packbits(bits).tobytes(), "big")


_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount_u64(arr):
    """Conta bits 1 em cada elemento de um array uint64 (vetorizado)."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(arr)
    # Fallback para numpy antigo: tabela de 8 bits
    arr = np.ascontiguousarray(arr)
    return _POPCOUNT_TABLE[arr.view(np.uint8)].reshape(arr.shape + (8,)).sum(axis=-1)


def find_similar_groups(hashes, threshold, progress_cb=None, cancel_check=None):
    """
    Agrupa índices de imagens cujos hashes têm distância de Hamming <= threshold
    (fechamento transitivo, via Union-Find), exatamente como a comparação par a
    par original, porém vetorizada com numpy.

    Retorna a lista de grupos (listas de índices) com mais de 1 elemento,
    na ordem de aparição do primeiro índice de cada grupo.

    Complexidade: ainda compara todos os pares, mas em blocos numpy
    (~100k imagens em segundos, contra horas na versão em Python puro).

    progress_cb(feitos, total) é chamado a cada bloco; se cancel_check()
    retornar True, a função levanta ScanCancelled.
    """
    n = len(hashes)
    uf = UnionFind(n)
    if n == 0:
        return []

    # 1) Imagens com hash exatamente igual são unidas diretamente (O(n)).
    #    Só um representante de cada hash distinto vai para a comparação vetorizada.
    hash_ints = [hash_to_int(h) for h in hashes]
    first_index_of_hash = {}
    unique_indices = []
    for i, key in enumerate(hash_ints):
        if key in first_index_of_hash:
            uf.union(first_index_of_hash[key], i)
        else:
            first_index_of_hash[key] = i
            unique_indices.append(i)

    m = len(unique_indices)
    if m > 1:
        bit_length = np.asarray(hashes[0].hash).size
        if bit_length <= 64:
            H = np.array([hash_ints[i] for i in unique_indices], dtype=np.uint64)
            # Tamanho de bloco limitado para manter o uso de memória previsível
            # (cada bloco compara `block` linhas contra todas as colunas seguintes).
            block = int(max(16, min(512, 8_000_000 // m)))
            total_pairs = m * (m - 1) / 2
            for start in range(0, m, block):
                if cancel_check and cancel_check():
                    raise ScanCancelled()
                if progress_cb:
                    # progresso em % de pares comparados (o custo por bloco diminui
                    # ao longo do laço, então contar linhas superestimaria o restante)
                    done_pairs = start * m - start * (start + 1) / 2
                    progress_cb(int(100 * done_pairs / total_pairs), 100)
                end = min(start + block, m)
                cols = H[start + 1:]
                if cols.size == 0:
                    break
                rows = H[start:end]
                dist = _popcount_u64(rows[:, None] ^ cols[None, :])
                rr, cc = np.nonzero(dist <= threshold)
                gi_all = rr + start
                gj_all = cc + (start + 1)
                keep = gj_all > gi_all  # só pares (i < j), evita comparar consigo mesmo
                gi_all = gi_all[keep]
                gj_all = gj_all[keep]
                # Converte em sub-lotes: um bloco pode gerar milhões de pares
                # (muitas quase-duplicatas); materializar tudo de uma vez
                # estouraria a memória e deixaria o Cancelar morto.
                CHUNK = 200_000
                for k in range(0, gi_all.size, CHUNK):
                    if k and cancel_check and cancel_check():
                        raise ScanCancelled()
                    for gi, gj in zip(gi_all[k:k + CHUNK].tolist(),
                                      gj_all[k:k + CHUNK].tolist()):
                        uf.union(unique_indices[gi], unique_indices[gj])
        else:
            # Hashes maiores que 64 bits: comparação par a par tradicional
            for a in range(m):
                for b in range(a + 1, m):
                    ia, ib = unique_indices[a], unique_indices[b]
                    if abs(hashes[ia] - hashes[ib]) <= threshold:
                        uf.union(ia, ib)

    if progress_cb:
        progress_cb(100, 100)
    root_to_group = {}
    for i in range(n):
        root_to_group.setdefault(uf.find(i), []).append(i)
    return [g for g in root_to_group.values() if len(g) > 1]


# ---------------------------------------------------------------------------
# Pipeline de escaneamento (funções puras, sem interface): usadas pela classe
# ImageCleaner e também pelos testes de regressão.
# ---------------------------------------------------------------------------

def categorize_scan_error(filepath, e):
    """Categoriza um erro de processamento de imagem (mesma lógica da versão original)."""
    error_type = "Desconhecido"
    error_msg = str(e)

    if "truncated" in error_msg.lower():
        error_type = "Arquivo Truncado"
        error_msg = "Imagem incompleta ou corrompida (dados faltando)"
    elif "broken data stream" in error_msg.lower():
        error_type = "Dados Corrompidos"
        error_msg = "Fluxo de dados da imagem está quebrado"
    elif "cannot identify image file" in error_msg.lower():
        error_type = "Formato Inválido"
        error_msg = "Arquivo não é uma imagem válida ou formato não suportado"
    elif "permission" in error_msg.lower():
        error_type = "Sem Permissão"
        error_msg = "Sem permissão para ler o arquivo"
    else:
        error_msg = str(e)

    return {
        'filepath': filepath,
        'type': error_type,
        'message': error_msg
    }


def list_image_files(root, recursive, extensions=None, progress_cb=None, cancel_check=None,
                     accept=None):
    """
    Lista as imagens de `root` na MESMA ordem que os.walk (top-down: arquivos da
    pasta, depois cada subpasta na ordem do sistema), já capturando tamanho e
    mtime pelo os.scandir (no Windows isso não custa uma chamada extra ao disco).

    Retorna lista de tuplas (filepath, size, mtime_ns). Se o stat falhar,
    size/mtime ficam None (o arquivo ainda será processado normalmente).
    Pastas sem permissão são ignoradas, como no os.walk.

    accept(entry, st_ou_None), se informado, substitui o teste de extensão
    (ver make_scan_accept): fotos, vídeos e outros saem da MESMA caminhada,
    na mesma ordem relativa de sempre.
    """
    exts = set(e.lower() for e in (extensions or VALID_EXTENSIONS))
    entries = []

    def wanted(entry):
        """(entra?, stat já lido ou None)"""
        if accept is None:
            return os.path.splitext(entry.name)[1].lower() in exts, None
        try:
            st = entry.stat()
        except OSError:
            st = None
        return accept(entry, st), st

    def add_entry(entry, st=None):
        try:
            if st is None:
                st = entry.stat()
            entries.append((entry.path, st.st_size, st.st_mtime_ns))
        except OSError:
            entries.append((entry.path, None, None))
        if len(entries) % 1000 == 0:
            if progress_cb:
                progress_cb(len(entries))
            if cancel_check and cancel_check():
                raise ScanCancelled()

    def is_reparse_point(entry):
        """Junctions/pontos de reparse do Windows: não descer (evita ciclos).
           No Python 3.12+ is_symlink() não cobre junctions."""
        try:
            st = entry.stat(follow_symlinks=False)
            return bool(getattr(st, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        except OSError:
            return True

    def walk(top):
        # Iterativo (pilha explícita) para não estourar o limite de recursão em
        # árvores muito profundas; mesma ordem do os.walk top-down.
        pending = [top]
        while pending:
            if cancel_check and cancel_check():
                return
            directory = pending.pop()
            try:
                with os.scandir(directory) as it:
                    children = list(it)
            except OSError:
                continue
            subdirs = []
            for entry in children:
                try:
                    if entry.is_dir():
                        if (not entry.is_symlink() and not is_reparse_point(entry)
                                and entry.name.lower() not in IGNORED_DIR_NAMES):
                            subdirs.append(entry.path)
                        continue
                    if entry.is_file():
                        ok, st = wanted(entry)
                        if ok:
                            add_entry(entry, st)
                except OSError:
                    continue
            # Empilha em ordem inversa: o primeiro subdiretório é processado primeiro
            pending.extend(reversed(subdirs))

    try:
        if recursive:
            walk(root)
        else:
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_file():
                            ok, st = wanted(entry)
                            if ok:
                                add_entry(entry, st)
                    except OSError:
                        continue
    except ScanCancelled:
        pass  # cancelado: retorna o que já foi listado (o chamador checa a flag)
    return entries


def _run_parallel_bounded(items, worker, on_result, workers, cancel_check=None):
    """
    Executa worker(item) em um ThreadPoolExecutor mantendo no máximo
    `workers * 4` tarefas em voo (evita criar centenas de milhares de futures
    de uma vez e permite cancelar quase imediatamente). on_result(retorno) é
    chamado na thread principal a cada conclusão. Retorna True se cancelado.
    """
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=workers)
    in_flight = set()
    next_pos = 0
    max_in_flight = workers * 4
    try:
        while next_pos < len(items) or in_flight:
            while next_pos < len(items) and len(in_flight) < max_in_flight:
                in_flight.add(executor.submit(worker, items[next_pos]))
                next_pos += 1
            for future in as_completed(list(in_flight)):
                in_flight.discard(future)
                on_result(future.result())
                if cancel_check and cancel_check():
                    cancelled = True
                    break
                # volta a encher a fila assim que houver espaço
                if len(in_flight) < workers * 2 and next_pos < len(items):
                    break
            if cancelled:
                break
    except BaseException:
        cancelled = True
        raise
    finally:
        # Cancelado (ou erro): descarta o que não começou e não espera o resto
        executor.shutdown(wait=not cancelled, cancel_futures=True)
    return cancelled


def _run_parallel_ticking(items, worker, on_result, workers, tick=None, cancel_event=None,
                          interval=0.1):
    """
    Variante do _run_parallel_bounded para tarefas LONGAS (ler arquivos de GB,
    esperar um ffmpeg). O executor original só devolve o controle à thread
    principal quando uma tarefa termina; com tarefas de minutos a janela
    congelaria e o botão Cancelar não responderia. Aqui a thread principal
    acorda a cada `interval` segundos e chama tick() (atualizar o progresso,
    processar cliques, pedir cancelamento) mesmo sem nenhuma conclusão.

    cancel_event (threading.Event) é a via de cancelamento: quem quiser
    cancelar liga o evento (normalmente o próprio tick); os workers devem
    consultá-lo entre blocos para parar logo. on_result(retorno) roda na
    thread principal. Retorna True se cancelado.
    """
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=workers)
    in_flight = set()
    next_pos = 0
    max_in_flight = workers * 2
    try:
        while next_pos < len(items) or in_flight:
            while next_pos < len(items) and len(in_flight) < max_in_flight:
                in_flight.add(executor.submit(worker, items[next_pos]))
                next_pos += 1
            done, _ = wait(in_flight, timeout=interval, return_when=FIRST_COMPLETED)
            for future in done:
                in_flight.discard(future)
                on_result(future.result())
            if tick:
                tick()
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
    except BaseException:
        cancelled = True
        if cancel_event is not None:
            cancel_event.set()   # solta os workers que estão no meio de um arquivo
        raise
    finally:
        executor.shutdown(wait=not cancelled, cancel_futures=True)
    return cancelled


def hash_files(entries, cache, workers, progress_cb=None, cancel_check=None):
    """
    Calcula o phash de cada arquivo (em paralelo), consultando o cache antes.

    entries: lista de (filepath, size, mtime_ns).
    Retorna (results, errors_by_idx, cancelled):
      results[idx]     = (filepath, ImageHash, md5_ou_None) ou None se deu erro
      errors_by_idx    = {idx: dict de erro}
      cancelled        = True se o cancelamento foi acionado
    A ordem dos resultados é a de `entries`, independentemente das threads.
    """
    total = len(entries)
    results = [None] * total
    errors_by_idx = {}
    processed = 0
    cancelled = False

    def report(filepath):
        if progress_cb:
            progress_cb(processed, total, filepath)

    # 1) Cache: separa o que ainda precisa ser calculado
    to_compute = []
    cached_md5 = {}   # idx -> md5 do cache preservado num recálculo de phash
    for idx, (filepath, size, mtime_ns) in enumerate(entries):
        if idx % 500 == 0:
            if cancel_check and cancel_check():
                return results, errors_by_idx, True
            if progress_cb:
                progress_cb(processed, total, "(verificando cache)")
        cached = None
        if cache is not None and size is not None:
            cached = cache.lookup(filepath, size, mtime_ns)
        if cached is not None:
            try:
                hash_val = imagehash.hex_to_hash(cached[0])
                # phash degenerado de um formato com transparência: foi
                # calculado antes da composição sobre branco (imagem lida
                # como toda preta). Recalcula só esses, preservando o MD5.
                if (is_degenerate_hash(hash_val)
                        and os.path.splitext(filepath)[1].lower() in ALPHA_CAPABLE_EXTENSIONS):
                    cached_md5[idx] = cached[1]
                else:
                    results[idx] = (filepath, hash_val, cached[1])
                    processed += 1
                    continue
            except Exception:
                pass  # cache inválido: recalcula
        to_compute.append(idx)
    report("(cache)")

    if not to_compute:
        return results, errors_by_idx, False

    # 2) Hash em paralelo (pool limitado; ver _run_parallel_bounded)
    def worker(idx):
        filepath = entries[idx][0]
        try:
            return idx, compute_phash(filepath), None
        except Exception as e:
            return idx, None, e

    def on_result(result):
        nonlocal processed
        idx, hash_val, err = result
        filepath, size, mtime_ns = entries[idx]
        if err is None:
            md5_val = cached_md5.get(idx)
            results[idx] = (filepath, hash_val, md5_val)
            if cache is not None and size is not None:
                cache.store(filepath, size, mtime_ns, str(hash_val), md5_val)
        else:
            errors_by_idx[idx] = categorize_scan_error(filepath, err)
        processed += 1
        report(filepath)

    try:
        cancelled = _run_parallel_bounded(to_compute, worker, on_result, workers, cancel_check)
    finally:
        if cache is not None:
            cache.flush()

    return results, errors_by_idx, cancelled


def md5_for_groups(images_data, stats, groups_idx, cache, workers, progress_cb=None, cancel_check=None):
    """
    Calcula o MD5 apenas das imagens que caíram em grupos E que têm pelo menos
    outra imagem do MESMO tamanho no grupo (MD5 igual implica tamanho igual;
    tamanho único já garante "Semelhante", sem precisar ler o arquivo).

    images_data: lista de (filepath, ImageHash, md5_ou_None)
    stats:       lista alinhada de (size, mtime_ns) (ou (None, None))
    groups_idx:  lista de grupos (listas de índices em images_data)
    Retorna (md5_by_idx, cancelled). md5_by_idx tem uma string para TODO índice
    que aparece em groups_idx: MD5 real, ou sentinela única quando não é preciso
    (tamanho único) ou não foi possível (erro) calcular.
    """
    md5_by_idx = {}
    to_compute = []

    for group in groups_idx:
        size_count = {}
        for i in group:
            size = stats[i][0] if stats[i] else None
            size_count[size] = size_count.get(size, 0) + 1
        # Se algum membro está sem tamanho conhecido (stat falhou na listagem),
        # não dá para usar o pré-filtro nesse grupo: calcula MD5 de todos.
        unknown_size = None in size_count
        for i in group:
            filepath, _, md5_val = images_data[i]
            size = stats[i][0] if stats[i] else None
            if md5_val:
                md5_by_idx[i] = md5_val            # já veio do cache
            elif not unknown_size and size_count[size] == 1:
                md5_by_idx[i] = f"SIZE:{size}:{i}"  # único no tamanho => não pode ser idêntica
            else:
                to_compute.append(i)

    total = len(to_compute)
    processed = 0
    cancelled = False
    if total == 0:
        return md5_by_idx, False

    def worker(idx):
        filepath = images_data[idx][0]
        try:
            return idx, get_file_md5(filepath), None
        except Exception as e:
            return idx, None, e

    def on_result(result):
        nonlocal processed
        idx, md5_val, err = result
        filepath = images_data[idx][0]
        if err is None:
            md5_by_idx[idx] = md5_val
            if cache is not None:
                cache.update_md5(filepath, md5_val)
        else:
            # Sem MD5 não dá para afirmar que é idêntica: fica "Semelhante"
            log.warning("MD5 falhou para %s (tratada como Semelhante): %s", filepath, err)
            md5_by_idx[idx] = f"ERR:{filepath}"
        processed += 1
        if progress_cb:
            progress_cb(processed, total, filepath)

    try:
        cancelled = _run_parallel_bounded(to_compute, worker, on_result, workers, cancel_check)
    finally:
        if cache is not None:
            cache.flush()

    return md5_by_idx, cancelled


def build_groups(images_data, groups_idx, md5_by_idx):
    """
    Monta os grupos no formato usado pela interface: lista de listas de tuplas
    (filepath, ImageHash, md5), na ordem dos índices (mesma ordem de sempre).
    """
    groups = []
    for group in groups_idx:
        items = []
        for i in group:
            filepath, hash_val, md5_val = images_data[i]
            items.append((filepath, hash_val, md5_by_idx.get(i, md5_val)))
        groups.append(items)
    return groups


def plan_identical_selection(images):
    """
    Decide quais imagens de UM grupo selecionar no botão "Selecionar Idênticas".
    images: lista de dicts com 'md5', 'mtime' e 'is_reference' (bool).
    Retorna a lista de índices (posições em `images`) a selecionar.

    Regra por subgrupo de MD5 com 2+ imagens (subgrupos percorridos na ordem
    de primeira aparição, como sempre foi):
      - se alguma cópia é da pasta de referência: seleciona TODAS as do alvo
        (a cópia preservada é a do acervo, independentemente de data);
      - senão: ordena as do alvo por mtime (sort estável: empate mantém a
        ordem original) e seleciona todas exceto a primeira (a mais antiga).
    Índices de imagens da referência nunca aparecem no retorno.
    """
    selected = []
    md5_groups = {}
    for i, img in enumerate(images):
        md5_groups.setdefault(img['md5'], []).append(i)
    for idxs in md5_groups.values():
        if len(idxs) <= 1:
            continue
        targets = [i for i in idxs if not images[i].get('is_reference')]
        if len(targets) < len(idxs):
            selected.extend(targets)
        else:
            targets.sort(key=lambda i: images[i]['mtime'])
            selected.extend(targets[1:])
    return selected


def keep_sort_key(img, index, priority=SIMILAR_KEEP_PRIORITY):
    """
    Chave de ordenação para escolher a imagem MANTIDA num grupo: quanto menor,
    melhor. Cada critério vira uma tupla (desconhecido?, valor) para que valor
    desconhecido sempre perca para conhecido; o índice na lista é o último
    desempate (primeira da ordem do grupo).
    img: dict com 'pixels' (int ou None), 'size' (int ou None), 'mtime'.
    """
    key = []
    for crit in priority:
        if crit == "resolution":
            v = img.get('pixels')
            key.append((0, -v) if v else (1, 0))
        elif crit == "size":
            v = img.get('size')
            key.append((0, -v) if v is not None else (1, 0))
        elif crit == "mtime":
            v = img.get('mtime')
            key.append((0, v) if v is not None and v != float("inf") else (1, 0))
    key.append(index)
    return tuple(key)


def plan_similar_selection(images, md5_count, priority=SIMILAR_KEEP_PRIORITY):
    """
    Decide a seleção do botão "Selecionar Semelhantes" para UM grupo.
    images: dicts com 'md5', 'mtime', 'is_reference' e, para a qualidade,
    'pixels' e 'size' (ausentes = desconhecidos); md5_count: contagem de cada
    MD5 no grupo inteiro (inclui as imagens da referência).
    Candidatas: MD5 único no grupo (não é "Idêntica") e não é da referência.
      - se o grupo contém ALGUMA imagem da referência: seleciona todas as
        candidatas (a versão do acervo é a preservada);
      - senão: com 2+ candidatas, MANTÉM a melhor segundo `priority`
        (SIMILAR_KEEP_PRIORITY: maior resolução, maior arquivo, mais antiga;
        empate total: a primeira do grupo) e seleciona as outras; com 0 ou 1
        candidata, não seleciona nada.
    Retorna lista de índices em `images`; nunca inclui referência.
    """
    candidates = [i for i, img in enumerate(images)
                  if md5_count[img['md5']] == 1 and not img.get('is_reference')]
    if any(img.get('is_reference') for img in images):
        return candidates
    if len(candidates) > 1:
        keep = min(candidates, key=lambda i: keep_sort_key(images[i], i, priority))
        return [i for i in candidates if i != keep]
    return []


# ---------------------------------------------------------------------------
# Modo de comparação de duas pastas (pasta alvo x pasta de referência protegida)
# ---------------------------------------------------------------------------

def folder_prefix(folder):
    """Chave normalizada da pasta com separador final (para teste de prefixo
       sem confundir 'E:\\Fotos' com 'E:\\Fotos2')."""
    return cache_key(folder).rstrip("\\/") + os.sep


def folder_conflict(target, reference):
    """
    Verifica se a pasta alvo e a pasta de referência podem ser comparadas.
    Retorna uma string com o motivo do conflito, ou None se são disjuntas.
    """
    kt = folder_prefix(target)
    kr = folder_prefix(reference)
    if kt == kr:
        return "as duas pastas são a mesma"
    if kt.startswith(kr):
        return "a pasta alvo está dentro da pasta de referência"
    if kr.startswith(kt):
        return "a pasta de referência está dentro da pasta alvo"
    return None


def merge_scan_entries(target_entries, reference_entries):
    """
    Concatena as listagens (alvo primeiro, para os grupos saírem na ordem de
    caminhada da pasta alvo) e devolve (entries, reference_keys), onde
    reference_keys é o set de cache_key de TODOS os arquivos listados na
    referência: a origem é definida por construção (veio da listagem da
    referência), não por teste de string no caminho.
    """
    reference_keys = {cache_key(fp) for (fp, _, _) in reference_entries}
    overlap = sum(1 for (fp, _, _) in target_entries if cache_key(fp) in reference_keys)
    if overlap:
        # Só acontece com junction/atalho escondido: não deduplica (mudaria
        # índices e ordem), apenas avisa.
        log.warning("Modo referência: %d arquivo(s) aparecem nas DUAS listagens", overlap)
    return list(target_entries) + list(reference_entries), reference_keys


def filter_groups_for_reference(groups_idx, images_data, reference_keys, hide_target_only=False):
    """
    Remove grupos compostos SÓ por imagens da referência (nada a limpar ali).
    Com hide_target_only=True remove também grupos sem nenhuma imagem da
    referência (duplicatas internas da pasta alvo). Preserva a ordem dos
    grupos e a ordem interna dos índices. Deve ser chamada ANTES do MD5,
    para não ler do disco arquivos de grupos descartados.
    """
    out = []
    for group in groups_idx:
        in_ref = [cache_key(images_data[i][0]) in reference_keys for i in group]
        if all(in_ref):
            continue
        if hide_target_only and not any(in_ref):
            continue
        out.append(group)
    return out


def is_protected_path(filepath, reference_keys, reference_prefix):
    """
    Defesa em profundidade para mover/excluir: True se o arquivo veio da
    listagem da referência (set) OU está sob a raiz da referência (prefixo),
    mesmo que por algum motivo não esteja no set.
    """
    key = cache_key(filepath)
    if reference_keys and key in reference_keys:
        return True
    return bool(reference_prefix) and key.startswith(reference_prefix)


# ---------------------------------------------------------------------------
# Comparação por bytes: arquivos idênticos (vídeos, outros, fotos que o Pillow
# não abre). Funções puras, sem interface. Regra de ouro: um grupo só nasce de
# MD5 do arquivo INTEIRO igual; qualquer dúvida ou erro degrada para "não
# agrupa" (o arquivo simplesmente não aparece), nunca para "Idêntica".
# ---------------------------------------------------------------------------

class FileChangedError(OSError):
    """O arquivo mudou entre a listagem e a leitura, ou durante a leitura."""


class _ByteProgress:
    """Bytes lidos (somados pelos workers, lidos pelo tick da thread principal)."""
    def __init__(self):
        self._lock = threading.Lock()
        self.done = 0
        self.current = ""

    def add(self, n):
        with self._lock:
            self.done += n


def _stat_before_read(filepath, size, mtime_ns=None):
    """stat logo antes de ler: o arquivo tem de ser o MESMO que foi listado
       (tamanho igual e, quando informada, data igual com a folga de 2 s do
       FAT). Sem isso, um arquivo regravado entre a listagem e a leitura iria
       para o cache com o conteúdo novo sob a data velha."""
    st = os.stat(filepath)
    if st.st_size != size:
        raise FileChangedError(f"tamanho mudou desde a listagem ({size} -> {st.st_size} bytes)")
    if mtime_ns is not None and abs(st.st_mtime_ns - mtime_ns) > 2_000_000_000:
        raise FileChangedError("data de modificação mudou desde a listagem")
    return st


def _check_unchanged(filepath, before):
    after = os.stat(filepath)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise FileChangedError("arquivo alterado durante a leitura")


def file_quick_key(filepath, size, on_bytes=None, mtime_ns=None, part="head"):
    """
    Chave barata para ELIMINAR arquivos de mesmo tamanho que diferem.
    Retorna (tipo, valor):
      ("md5", hex)     arquivo de até 3 blocos: lido inteiro, é o MD5 de verdade;
      ("head", valor)  part="head": só o 1º bloco. É a primeira peneira: o
                       começo (cabeçalho, EXIF, data de gravação) quase sempre
                       já separa arquivos diferentes com UMA leitura;
      ("quick", valor) part="rest": blocos do meio e do fim, para quem empatou
                       no começo.
    O valor traz o tamanho do bloco na frente ("65536:<hex>"): chaves gravadas
    no cache com outro BYTE_QUICK_CHUNK nunca são comparadas com as novas.
    Levanta FileChangedError se o arquivo não é mais o que foi listado.
    """
    before = _stat_before_read(filepath, size, mtime_ns)
    chunk = BYTE_QUICK_CHUNK
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        if size <= 3 * chunk:
            data = f.read(size + 1)
            if len(data) != size:
                raise FileChangedError("tamanho lido diferente do listado")
            h.update(data)
            kind, read = "md5", size
        else:
            offsets = (0,) if part == "head" else ((size - chunk) // 2, size - chunk)
            for offset in offsets:
                f.seek(offset)
                data = f.read(chunk)
                if len(data) != chunk:
                    raise FileChangedError("arquivo encolheu durante a leitura")
                h.update(data)
            kind, read = ("head" if part == "head" else "quick"), chunk * len(offsets)
    if on_bytes:
        on_bytes(read)
    _check_unchanged(filepath, before)
    return kind, (h.hexdigest() if kind == "md5" else f"{chunk}:{h.hexdigest()}")


def file_full_md5(filepath, size, cancel_event=None, on_bytes=None, mtime_ns=None):
    """
    MD5 do arquivo inteiro, em blocos, consultando cancel_event entre blocos
    (levanta ScanCancelled) e informando os bytes lidos a on_bytes. Só devolve
    o hash se leu exatamente `size` bytes e o arquivo não mudou no caminho.
    """
    before = _stat_before_read(filepath, size, mtime_ns)
    h = hashlib.md5()
    total = 0
    with open(filepath, "rb") as f:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ScanCancelled()
            data = f.read(MD5_CHUNK_SIZE)
            if not data:
                break
            h.update(data)
            total += len(data)
            if on_bytes:
                on_bytes(len(data))
    if total != size:
        raise FileChangedError(f"lidos {total} bytes de {size}")
    _check_unchanged(filepath, before)
    return h.hexdigest()


def _split_groups(groups, key_by_idx):
    """Divide cada grupo pela chave de cada índice. Índice sem chave (erro)
       sai; subgrupo com um só sai. Ordem de primeira aparição preservada."""
    out = []
    for group in groups:
        buckets = {}
        for i in group:
            key = key_by_idx.get(i)
            if key is not None:
                buckets.setdefault(key, []).append(i)
        out.extend(b for b in buckets.values() if len(b) > 1)
    return out


def _collapse_hardlinks(groups, entries, stats):
    """Dois caminhos para o MESMO arquivo no disco (hardlink) não são
       duplicata: excluir um não libera espaço. Fica só o primeiro."""
    out = []
    for group in groups:
        seen = set()
        kept = []
        for i in group:
            ident = None
            try:
                st = os.stat(entries[i][0])
                if st.st_ino:
                    ident = (st.st_dev, st.st_ino)
            except OSError:
                pass
            if ident is not None:
                if ident in seen:
                    stats["hardlinks"] += 1
                    log.info("Hardlink ignorado (mesmo arquivo no disco): %s", entries[i][0])
                    continue
                seen.add(ident)
            kept.append(i)
        if len(kept) > 1:
            out.append(kept)
    return out


def _new_byte_stats():
    return {"files": 0, "no_size": 0, "size_candidates": 0, "head_computed": 0,
            "quick_computed": 0, "full_computed": 0, "bytes_read": 0, "hardlinks": 0,
            "errors": [], "groups": 0, "identical_files": 0, "md5_from_cache": []}


def find_identical_files(entries, cache=None, workers=HASH_WORKERS, reference_keys=None,
                         hide_target_only=False, progress_cb=None, phase_cb=None,
                         cancel_check=None):
    """
    Acha arquivos bit a bit idênticos lendo o mínimo possível do disco:
      1. grupos por TAMANHO (tamanho único = impossível ser idêntico: nem abre);
      2. o 1º bloco de 64 KB ("head") elimina quase todos os que só coincidem
         no tamanho, com UMA leitura por arquivo;
      3. os blocos do meio e do fim ("quick") peneiram quem empatou no começo;
      4. MD5 do arquivo inteiro só de quem ainda empata.
    No modo de comparação (reference_keys não None), grupos só da referência
    (e, com hide_target_only, os só do alvo) caem ANTES de qualquer leitura e
    de novo depois de cada refinamento (um balde dividido pode virar só-alvo).

    entries: lista de (filepath, size, mtime_ns), como a de list_image_files.
    cache: FileHashCache ou None. progress_cb(bytes_feitos, bytes_total, caminho);
    phase_cb(fase, n_arquivos, bytes_total) com fase "head", "quick" ou "full".
    Retorna (groups_idx, md5_by_idx, stats, cancelled): grupos como listas de
    índices em `entries`, na ordem de primeira aparição; md5_by_idx tem o MD5
    real de todo índice agrupado. stats["md5_from_cache"] lista os caminhos
    agrupados cujo MD5 NÃO foi lido nesta rodada (veio do cache): quem for
    apagar com base neles deve reconferir antes (ver verify_cached_proof).
    """
    stats = _new_byte_stats()
    stats["files"] = len(entries)
    cancel_event = threading.Event()
    chunk_tag = f"{BYTE_QUICK_CHUNK}:"

    def ref_filter(groups):
        if reference_keys is None:
            return groups
        return filter_groups_for_reference(groups, entries, reference_keys,
                                           hide_target_only=hide_target_only)

    def run_stage(phase, todo, job, cost_of, store, split_large):
        """Roda job(idx, on_bytes) para cada idx de `todo`; devolve {idx: valor}."""
        results = {}
        if not todo:
            return results
        total = sum(cost_of(i) for i in todo)
        progress = _ByteProgress()
        if phase_cb:
            phase_cb(phase, len(todo), total)

        def worker(i):
            progress.current = entries[i][0]
            try:
                return i, job(i, progress.add), None
            except ScanCancelled:
                return i, None, None
            except Exception as e:
                return i, None, e

        def on_result(result):
            i, value, err = result
            if err is not None:
                # Sem leitura confiável não há prova: o arquivo fica fora
                log.warning("Comparação por bytes: %s ignorado: %s", entries[i][0], err)
                stats["errors"].append({'filepath': entries[i][0], 'message': str(err)})
            elif value is not None:
                results[i] = value
                store(i, value)

        def tick():
            if progress_cb:
                progress_cb(min(progress.done, total), total, progress.current)
            if cancel_check and cancel_check():
                cancel_event.set()

        if split_large:
            batches = (([i for i in todo if entries[i][1] < BYTE_LARGE_FILE], workers),
                       ([i for i in todo if entries[i][1] >= BYTE_LARGE_FILE],
                        min(workers, BYTE_LARGE_WORKERS)))
        else:
            batches = ((todo, workers),)
        for batch, n_workers in batches:
            if batch and not cancel_event.is_set():
                _run_parallel_ticking(batch, worker, on_result, n_workers, tick, cancel_event)
        tick()
        stats["bytes_read"] += progress.done
        return results

    try:
        # 1) Tamanho
        by_size = {}
        for i, (_, size, _) in enumerate(entries):
            if not size:
                stats["no_size"] += 1   # desconhecido (stat falhou) ou vazio: sem prova possível
                continue
            by_size.setdefault(size, []).append(i)
        groups = ref_filter([g for g in by_size.values() if len(g) > 1])
        candidates = [i for g in groups for i in g]
        stats["size_candidates"] = len(candidates)
        if not candidates:
            return [], {}, stats, False
        cached = cache.lookup_many([entries[i] for i in candidates]) if cache is not None else {}

        def cached_field(i, name):
            value = (cached.get(entries[i][0]) or {}).get(name)
            if value and name in ("head", "quick") and not value.startswith(chunk_tag):
                return None   # gravada com outro tamanho de bloco: não é comparável
            return value

        md5_by_idx = {}
        cached_md5 = set()
        for i in candidates:
            if cached_field(i, "md5"):
                md5_by_idx[i] = cached_field(i, "md5")
                cached_md5.add(i)

        def small(i):
            return entries[i][1] <= 3 * BYTE_QUICK_CHUNK

        def sample_stage(phase, part, members, cost):
            """Chave de amostra (cache ou leitura) para cada índice de `members`."""
            keys, todo = {}, []
            for i in members:
                if small(i) and i in md5_by_idx:
                    keys[i] = ("md5", md5_by_idx[i])
                elif not small(i) and cached_field(i, phase):
                    keys[i] = (phase, cached_field(i, phase))
                else:
                    todo.append(i)

            def job(i, on_bytes):
                return file_quick_key(entries[i][0], entries[i][1], on_bytes,
                                      mtime_ns=entries[i][2], part=part)

            def store(i, value):
                kind, digest = value
                if kind == "md5":
                    md5_by_idx[i] = digest
                if cache is not None:
                    cache.store(*entries[i], **{kind: digest})

            computed = run_stage(phase, todo, job,
                                 lambda i: min(entries[i][1], cost * BYTE_QUICK_CHUNK) if not small(i)
                                 else entries[i][1], store, split_large=False)
            stats[phase + "_computed"] = len(computed)
            keys.update(computed)
            return keys

        # 2) Começo do arquivo (para os pequenos, já é o MD5 do arquivo inteiro)
        keys = sample_stage("head", "head", candidates, 1)
        if cancel_event.is_set():
            return [], {}, stats, True
        groups = ref_filter(_split_groups(groups, keys))

        # 3) Meio e fim, só para os grandes que empataram no começo
        large = [i for g in groups for i in g if not small(i)]
        keys.update(sample_stage("quick", "rest", large, 2))
        if cancel_event.is_set():
            return [], {}, stats, True
        groups = ref_filter(_split_groups(groups, keys))

        # 4) MD5 do arquivo inteiro para quem ainda empata
        todo = [i for g in groups for i in g if i not in md5_by_idx]

        def full_job(i, on_bytes):
            return file_full_md5(entries[i][0], entries[i][1], cancel_event, on_bytes,
                                 mtime_ns=entries[i][2])

        def full_store(i, digest):
            md5_by_idx[i] = digest
            if cache is not None:
                cache.store(*entries[i], md5=digest)

        computed = run_stage("full", todo, full_job, lambda i: entries[i][1],
                             full_store, split_large=True)
        stats["full_computed"] = len(computed)
        if cancel_event.is_set():
            return [], {}, stats, True
        groups = ref_filter(_split_groups(groups, md5_by_idx))
        groups = ref_filter(_collapse_hardlinks(groups, entries, stats))
        groups.sort(key=lambda g: g[0])
        stats["groups"] = len(groups)
        stats["identical_files"] = sum(len(g) for g in groups)
        stats["md5_from_cache"] = [entries[i][0] for g in groups for i in g if i in cached_md5]
        return groups, {i: md5_by_idx[i] for g in groups for i in g}, stats, False
    finally:
        if cache is not None:
            cache.flush()


def verify_cached_proof(filepath, size, expected_md5, cancel_event=None, on_bytes=None):
    """
    Reconfere, lendo o arquivo inteiro AGORA, uma prova de "cópia exata" que
    veio do cache. O cache vale por caminho + tamanho + data, e há programas
    que alteram o conteúdo preservando os três (contêiner VeraCrypt, editor de
    tags com "manter a data"). Antes de apagar com base numa prova antiga, o
    MD5 tem de bater de novo. Retorna (ok, md5_atual_ou_None); erro de leitura
    ou arquivo alterado = (False, None). ScanCancelled propaga.
    """
    try:
        current = file_full_md5(filepath, size, cancel_event, on_bytes)
    except ScanCancelled:
        raise
    except Exception as e:
        log.warning("Reconferência falhou para %s: %s", filepath, e)
        return False, None
    return current == expected_md5, current


def build_byte_groups(entries, groups_idx, md5_by_idx):
    """Grupos de bytes no formato da interface: (filepath, None, md5). O lugar
       do ImageHash fica vazio: nada depois de build_groups lê esse campo."""
    return [[(entries[i][0], None, md5_by_idx[i]) for i in group] for group in groups_idx]


# ---------------------------------------------------------------------------
# Live Photos do iPhone: a foto (JPG/HEIC) tem ao lado um vídeo curto .MOV com
# o mesmo nome. Ao tirar a foto duplicada de uma pasta, o .MOV dela ficaria
# órfão. Funções puras; a regra de segurança é a mesma do resto: o vídeo só
# acompanha a foto se sobrar uma cópia BYTE A BYTE idêntica dele.
# ---------------------------------------------------------------------------
LIVE_PHOTO_STILL_EXTENSIONS = (".jpg", ".jpeg", ".heic", ".heif")


def live_photo_companion(photo_path):
    """
    Caminho do .MOV par de uma Live Photo, ou None. É par quando: mesma pasta,
    mesmo nome-base, e o .MOV traz a marca de Live Photo da Apple (um vídeo
    comum que por acaso tenha o mesmo número da foto NÃO é par). Se a foto e o
    vídeo trazem identificadores e eles diferem, também não é par.
    """
    stem, ext = os.path.splitext(photo_path)
    if ext.lower() not in LIVE_PHOTO_STILL_EXTENSIONS:
        return None
    mov = next((c for c in (stem + ".MOV", stem + ".mov") if os.path.isfile(c)), None)
    if mov is None:
        return None
    info = mp4probe.probe(mov)
    if not info["live_photo"]:
        return None
    photo_id = mp4probe.photo_content_id(photo_path)
    if photo_id and info["content_id"] and photo_id != info["content_id"]:
        return None
    return mov


def is_live_photo_video(path):
    """True se `path` é o .MOV de uma Live Photo (pela marca da Apple)."""
    return os.path.splitext(path)[1].lower() == ".mov" and mp4probe.probe(path)["live_photo"]


def stills_with_same_stem(path, folder_index=None):
    """Fotos (JPG/HEIC) na MESMA pasta com o mesmo nome-base de `path`, lidas
       do diretório de verdade (ex.: IMG_1.JPG e IMG_1.HEIC dividem um .MOV).
       folder_index: dict reaproveitado entre chamadas, para cada pasta ser
       listada UMA vez por ação (milhares de selecionados na mesma pasta)."""
    folder = os.path.dirname(path)
    if folder_index is None:
        folder_index = {}
    by_stem = folder_index.get(folder)
    if by_stem is None:
        by_stem = {}
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        for name in names:
            base, ext = os.path.splitext(name)
            if ext.lower() in LIVE_PHOTO_STILL_EXTENSIONS:
                by_stem.setdefault(base.lower(), []).append(os.path.join(folder, name))
        folder_index[folder] = by_stem
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    own = cache_key(path)
    return [full for full in by_stem.get(stem, []) if cache_key(full) != own]


def same_content(path_a, path_b):
    """True só se os dois arquivos têm exatamente os mesmos bytes (lidos agora).
       Qualquer erro de leitura = False (sem prova, não é cópia)."""
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            while True:
                a = fa.read(MD5_CHUNK_SIZE)
                b = fb.read(MD5_CHUNK_SIZE)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def plan_live_companions(groups, selected_keys, is_protected=None, acting_keys=None, tick=None):
    """
    Decide quais vídeos de Live Photo acompanham as fotos selecionadas.
    groups: lista de listas de caminhos (os grupos da tela); selected_keys:
    set de cache_key dos caminhos selecionados; is_protected(caminho) -> bool.

    Um .MOV só entra no plano se TODAS valem:
      - o par é de verdade (live_photo_companion) e a foto dele está selecionada;
      - ele mesmo não está selecionado nem é protegido (referência);
      - nenhuma OUTRA foto de mesmo nome-base fica na pasta (ela ainda usa o vídeo);
      - sobra uma cópia byte a byte idêntica dele: o .MOV par de uma foto NÃO
        selecionada do mesmo grupo, conferido agora (same_content), ou um
        arquivo idêntico a ele, não selecionado, num grupo de vídeos.
    acting_keys (padrão: selected_keys): as fotos que saem AGORA. Numa ação de
    um grupo só, selected_keys continua sendo TUDO o que está marcado na tela:
    um vídeo marcado em outro grupo não serve de cópia que fica (ele pode sair
    logo em seguida).
    tick(n), se informado, é chamado a cada par conferido (a conferência lê os
    dois vídeos inteiros: com centenas de pares a interface precisa respirar;
    tick pode levantar ScanCancelled para abortar).
    Retorna (plano, sem_copia): plano = [(foto, mov)], sem_copia = quantos
    pares de verdade ficaram de fora por não haver cópia provada do vídeo.
    """
    is_protected = is_protected or (lambda p: False)
    acting_keys = selected_keys if acting_keys is None else acting_keys
    companion_cache = {}
    folder_index = {}

    def companion(photo):
        key = cache_key(photo)
        if key not in companion_cache:
            companion_cache[key] = live_photo_companion(photo)
        return companion_cache[key]

    # vídeos que têm um idêntico NÃO selecionado em algum grupo (prova = MD5 da varredura)
    group_of = {}
    for members in groups:
        for fp in members:
            group_of[cache_key(fp)] = members
    plan, without_copy, taken = [], 0, set()
    for members in groups:
        for photo in members:
            if cache_key(photo) not in acting_keys or is_protected(photo):
                continue
            mov = companion(photo)
            if mov is None:
                continue
            mov_key = cache_key(mov)
            if mov_key in selected_keys or mov_key in taken or is_protected(mov):
                continue
            if any(cache_key(s) not in selected_keys for s in stills_with_same_stem(photo, folder_index)):
                continue   # outra foto que fica ainda usa este vídeo
            if tick:
                tick(len(plan) + without_copy + 1)
            proof = False
            for other in members:                       # par idêntico ao lado de uma foto mantida
                if cache_key(other) in selected_keys or cache_key(other) == cache_key(photo):
                    continue
                other_mov = companion(other)
                if (other_mov and cache_key(other_mov) != mov_key
                        and cache_key(other_mov) not in selected_keys and same_content(mov, other_mov)):
                    proof = True
                    break
            if not proof:                               # ou um idêntico que fica, num grupo de vídeos
                proof = any(cache_key(fp) != mov_key and cache_key(fp) not in selected_keys
                            and same_content(mov, fp) for fp in group_of.get(mov_key, []))
            if proof:
                plan.append((photo, mov))
                taken.add(mov_key)
            else:
                without_copy += 1
    return plan, without_copy


def orphaned_live_photos(selected_paths, selected_keys):
    """Quantos vídeos SELECIONADOS são a parte em vídeo de uma Live Photo cuja
       foto (mesma pasta, mesmo nome) NÃO está selecionada: a foto ficaria sem
       o vídeo dela."""
    count = 0
    folder_index = {}
    for path in selected_paths:
        if os.path.splitext(path)[1].lower() != ".mov":
            continue
        stills = [s for s in stills_with_same_stem(path, folder_index) if cache_key(s) not in selected_keys]
        if stills and is_live_photo_video(path):
            count += 1
    return count


# ---------------------------------------------------------------------------
# "Mesmo vídeo": o mesmo vídeo e o mesmo áudio em arquivos DIFERENTES (remux
# MP4 -> MOV/MKV, metadados ou datas regravados). Precisa do ffmpeg (opcional).
# A prova é o hash dos pacotes de vídeo e de áudio (ffmpegtools.stream_hashes),
# que lê o arquivo inteiro: por isso só é calculado para candidatos de mesmas
# dimensões, mesmo codec e duração quase igual, achados sem ffmpeg (mp4probe).
# ---------------------------------------------------------------------------
# Folga da duração entre candidatos, em segundos. Medido em vídeos reais de iPhone:
# um remux (MP4, MOV, MKV, faststart) muda a duração do cabeçalho em 0 a 0,034 s.
# 0,1 dá o triplo de margem; cada décimo a mais quase dobra os candidatos (e o
# custo: 32 candidatos com 0,1 contra 90 com 0,5 num acervo de 179 vídeos).
SAME_VIDEO_DURATION_TOL = 0.1
SAME_VIDEO_TOOLTIP = ("Mesmo vídeo: exatamente o mesmo vídeo e o mesmo áudio guardados em arquivos\n"
                      "diferentes (convertido de MP4 para MOV ou MKV sem recomprimir, ou com datas e\n"
                      "metadados regravados). A comparação por bytes não pega esses casos.\n"
                      "Precisa do ffmpeg, que é opcional: o programa procura sozinho (pasta do\n"
                      "programa, PATH, C:\\ffmpeg, Arquivos de Programas, WinGet, Chocolatey, Scoop).\n"
                      "Se não achar, use 'Localizar...' e aponte o ffmpeg.exe. Sem ffmpeg, só as\n"
                      "cópias exatas de vídeo são detectadas.\n"
                      "Vídeo recomprimido (ex.: reenviado pelo WhatsApp) NÃO entra: os dados mudam.\n"
                      "O botão mantém o arquivo mais antigo de cada grupo e seleciona os outros.")
_CODEC_ALIASES = {"avc1": "h264", "avc3": "h264", "hvc1": "hevc", "hev1": "hevc", "h265": "hevc",
                  "mp4v": "mpeg4", "jpeg": "mjpeg", "mjpa": "mjpeg", "vp09": "vp9", "av01": "av1",
                  "s263": "h263", "dvh1": "hevc", "dvhe": "hevc"}


def _new_same_video_stats():
    return {"videos": 0, "probed": 0, "candidates": 0, "hashed": 0, "merged_groups": 0,
            "files_in_merged": 0, "errors": [], "stream_from_cache": []}


def same_video_candidates(infos):
    """
    infos: {rep: dict com width, height, codec, duration (ou None)}. Devolve o
    set de reps que valem o custo do hash de fluxos: no mesmo balde (dimensões
    + codec) há outro de duração a menos de SAME_VIDEO_DURATION_TOL. Duração
    desconhecida (MP4 fragmentado tem 0 no cabeçalho) é coringa: compara com
    o balde inteiro.
    """
    buckets = {}
    for rep, info in infos.items():
        if info and info.get("width") and info.get("height"):
            codec = _CODEC_ALIASES.get((info.get("codec") or "").lower(), (info.get("codec") or "").lower())
            buckets.setdefault((info["width"], info["height"], codec), []).append(rep)
    out = set()
    for reps in buckets.values():
        if len(reps) < 2:
            continue
        if any(not infos[r].get("duration") for r in reps):
            out.update(reps)
            continue
        ordered = sorted(reps, key=lambda r: infos[r]["duration"])
        for a, b in zip(ordered, ordered[1:]):
            if infos[b]["duration"] - infos[a]["duration"] <= SAME_VIDEO_DURATION_TOL:
                out.update((a, b))
    return out


def find_same_videos(entries, groups_idx, md5_by_idx, ffmpeg_path, ffprobe_path=None, cache=None,
                     workers=HASH_WORKERS, reference_keys=None, hide_target_only=False,
                     progress_cb=None, phase_cb=None, cancel_check=None):
    """
    Junta aos grupos de bytes os vídeos com os MESMOS fluxos em arquivos
    diferentes. entries/groups_idx/md5_by_idx: os de find_identical_files.
    Arquivos de bytes idênticos dividem um hash de fluxos (um representante
    por classe de MD5); vídeos sem nenhuma cópia exata também entram.

    Retorna (groups_idx, md5_by_idx, same_video_idx, stream_by_idx, stats,
    cancelled): os grupos novos substituem os antigos (um grupo unido absorve
    os grupos de bytes das suas classes); md5_by_idx ganha uma sentinela única
    ("SIZE:...") para quem entrou sem cópia exata (nunca vira "Idêntica");
    same_video_idx = índices dos membros de grupos unidos; stream_by_idx = o
    hash de fluxos de cada um deles.
    """
    stats = _new_same_video_stats()
    cancel_event = threading.Event()
    videos = [i for i, (fp, size, _) in enumerate(entries) if size and classify_file(fp) == KIND_VIDEO]
    stats["videos"] = len(videos)
    unchanged = (groups_idx, md5_by_idx, set(), {}, stats, False)
    if len(videos) < 2 or not ffmpeg_path:
        return unchanged

    # Um representante por classe de bytes (os idênticos têm os mesmos fluxos)
    class_of = {}
    for i in videos:
        class_of[i] = md5_by_idx.get(i) or ("solo", i)
    members = {}
    for i in videos:
        members.setdefault(class_of[i], []).append(i)
    reps = [m[0] for m in members.values()]
    if len(reps) < 2:
        return unchanged
    cached = cache.lookup_many([entries[i] for i in reps]) if cache is not None else {}

    def run_stage(phase, todo, job, store):
        results = {}
        if not todo:
            return results
        total = sum(entries[i][1] for i in todo)
        state = {"done": 0, "current": ""}
        if phase_cb:
            phase_cb(phase, len(todo), total)

        def worker(i):
            state["current"] = entries[i][0]
            try:
                return i, job(i), None
            except Exception as e:
                return i, None, e

        def on_result(result):
            i, value, err = result
            state["done"] += entries[i][1]
            if err is not None:
                stats["errors"].append({'filepath': entries[i][0], 'message': str(err)})
            elif value is not None:
                results[i] = value
                store(i, value)

        def tick():
            if progress_cb:
                progress_cb(min(state["done"], total), total, state["current"])
            if cancel_check and cancel_check():
                cancel_event.set()

        # O custo de um vídeo pequeno é quase só iniciar o ffmpeg (~0,9 s): esses
        # rodam com todos os workers; os grandes, com poucos (leitura longa em HD)
        small = [i for i in todo if phase == "probe" or entries[i][1] < BYTE_LARGE_FILE]
        small_set = set(small)
        large = [i for i in todo if i not in small_set]
        for batch, n_workers in ((small, workers), (large, min(workers, BYTE_LARGE_WORKERS))):
            if batch and not cancel_event.is_set():
                _run_parallel_ticking(batch, worker, on_result, n_workers, tick, cancel_event)
        tick()
        return results

    try:
        # 1) Cabeçalho: dimensões, codec e duração (mp4probe; ffprobe para o que ele não lê)
        infos, todo = {}, []
        for i in reps:
            raw = (cached.get(entries[i][0]) or {}).get("probe")
            try:
                infos[i] = json.loads(raw) if raw else None
            except ValueError:
                infos[i] = None
            if infos[i] is None:
                todo.append(i)

        def probe_job(i):
            fp = entries[i][0]
            if os.path.splitext(fp)[1].lower() in mp4probe.MP4_EXTENSIONS:
                info = mp4probe.probe(fp)
            elif ffprobe_path:
                info = ffmpegtools.probe_video(ffprobe_path, fp, cancel_event) or {}
            else:
                info = {}
            return {k: info.get(k) for k in ("duration", "width", "height", "codec")}

        def probe_store(i, info):
            if cache is not None:
                cache.store(*entries[i], probe=json.dumps(info))

        infos.update(run_stage("probe", todo, probe_job, probe_store))
        stats["probed"] = len(todo)
        if cancel_event.is_set():
            return groups_idx, md5_by_idx, set(), {}, stats, True

        # 2) Hash dos fluxos só dos candidatos
        candidates = sorted(same_video_candidates(infos))
        stats["candidates"] = len(candidates)
        stream_by_rep, todo, from_cache = {}, [], set()
        for i in candidates:
            value = (cached.get(entries[i][0]) or {}).get("stream")
            if value:
                stream_by_rep[i] = value
                from_cache.add(i)
            else:
                todo.append(i)

        def stream_job(i):
            before = _stat_before_read(entries[i][0], entries[i][1], entries[i][2])
            value = ffmpegtools.stream_hashes(ffmpeg_path, entries[i][0], cancel_event)
            if value is not None:
                _check_unchanged(entries[i][0], before)
            return value

        def stream_store(i, value):
            if cache is not None:
                cache.store(*entries[i], stream=value)

        stream_by_rep.update(run_stage("stream", todo, stream_job, stream_store))
        stats["hashed"] = len(todo)
        if cancel_event.is_set():
            return groups_idx, md5_by_idx, set(), {}, stats, True

        # 3) Classes de bytes com o mesmo hash de fluxos viram um grupo só
        by_stream = {}
        for rep, value in stream_by_rep.items():
            by_stream.setdefault(value, []).append(rep)
        merged, absorbed = [], set()
        for value, same in by_stream.items():
            if len(same) < 2:
                continue
            group = sorted(i for rep in same for i in members[class_of[rep]])
            merged.append((group, value))
            absorbed.update(group)
        if not merged:
            return unchanged
        new_md5 = dict(md5_by_idx)
        same_video_idx, stream_by_idx, out_groups = set(), {}, []
        for group in groups_idx:
            if not any(i in absorbed for i in group):
                out_groups.append(group)
        for group, value in merged:
            out_groups.append(group)
        if reference_keys is not None:
            out_groups = filter_groups_for_reference(out_groups, entries, reference_keys,
                                                     hide_target_only=hide_target_only)
        kept = {id(g) for g in out_groups}
        for group, value in merged:
            if id(group) not in kept:
                continue
            stats["merged_groups"] += 1
            stats["files_in_merged"] += len(group)
            for i in group:
                same_video_idx.add(i)
                stream_by_idx[i] = value
                new_md5.setdefault(i, f"SIZE:{entries[i][1]}:{i}")   # sem cópia exata: nunca "Idêntica"
                if any(rep in from_cache for rep in members[class_of[i]]):
                    stats["stream_from_cache"].append(entries[i][0])
        out_groups.sort(key=lambda g: g[0])
        return (out_groups, {i: new_md5[i] for g in out_groups for i in g}, same_video_idx,
                stream_by_idx, stats, False)
    finally:
        if cache is not None:
            cache.flush()


def plan_same_video_selection(images):
    """
    Botão "Selecionar Mesmo vídeo": num grupo unido todos os arquivos têm o
    mesmo vídeo e o mesmo áudio. Mantém UM (o mais antigo; no empate, o maior)
    e seleciona os outros. Com arquivo da referência no grupo, seleciona todos
    os do alvo. Nunca devolve referência.
    """
    idxs = [i for i, img in enumerate(images) if img.get('same_video')]
    if len(idxs) < 2:
        return []
    targets = [i for i in idxs if not images[i].get('is_reference')]
    if len(targets) < len(idxs):
        return targets
    keep = min(targets, key=lambda i: keep_sort_key(images[i], i, ("mtime", "size")))
    return [i for i in targets if i != keep]


# ---------------------------------------------------------------------------
# Confirmação de "Semelhante" por segundo hash (dhash): pós-filtro dos grupos
# ---------------------------------------------------------------------------

def dhash_for_groups(images_data, stats, groups_idx, md5_by_idx, cache, workers,
                     progress_cb=None, cancel_check=None):
    """
    Calcula (ou lê do cache) o dhash apenas das imagens que a confirmação vai
    consultar: membros de grupos com pelo menos dois MD5 distintos (um grupo
    em que todas são idênticas não precisa de segunda opinião), exceto as de
    phash degenerado (que só podem ficar em grupo por MD5 igual).

    Mesmo padrão de md5_for_groups. Retorna (dhash_by_idx, cancelled), com
    dhash_by_idx: idx -> int de 64 bits. Índice ausente = não calculado (erro
    de leitura, registrado no log): o par cai no critério antigo (só phash).
    """
    dhash_by_idx = {}
    to_compute = []
    for group in groups_idx:
        if len({md5_by_idx.get(i, images_data[i][2]) for i in group}) < 2:
            continue
        for i in group:
            filepath, hash_val, _ = images_data[i]
            if is_degenerate_hash(hash_val):
                continue
            size, mtime_ns = stats[i] if stats[i] else (None, None)
            cached = None
            if cache is not None and size is not None:
                cached = cache.lookup_dhash(filepath, size, mtime_ns)
            if cached:
                try:
                    dhash_by_idx[i] = hash_to_int(imagehash.hex_to_hash(cached))
                    continue
                except Exception:
                    pass  # cache inválido: recalcula
            to_compute.append(i)

    total = len(to_compute)
    processed = 0
    cancelled = False
    if total == 0:
        return dhash_by_idx, False

    def worker(idx):
        filepath = images_data[idx][0]
        try:
            return idx, compute_dhash(filepath), None
        except Exception as e:
            return idx, None, e

    def on_result(result):
        nonlocal processed
        idx, hash_val, err = result
        filepath = images_data[idx][0]
        if err is None:
            dhash_by_idx[idx] = hash_to_int(hash_val)
            if cache is not None:
                cache.update_dhash(filepath, str(hash_val))
        else:
            log.warning("dhash falhou para %s (pares dela confirmados só pelo phash): %s",
                        filepath, err)
        processed += 1
        if progress_cb:
            progress_cb(processed, total, filepath)

    try:
        cancelled = _run_parallel_bounded(to_compute, worker, on_result, workers, cancel_check)
    finally:
        if cache is not None:
            cache.flush()

    return dhash_by_idx, cancelled


def _new_confirm_stats():
    return {
        "groups_in": 0, "groups_out": 0, "groups_unchanged": 0, "groups_split": 0,
        "groups_dropped": 0, "images_in": 0, "images_out": 0, "images_dropped": 0,
        "images_degenerate": 0, "pairs_checked": 0, "pairs_rejected_dhash": 0,
        "pairs_rejected_degenerate": 0, "pairs_without_dhash": 0,
    }


def confirm_similar_groups(images_data, groups_idx, md5_by_idx, dhash_by_idx,
                           phash_threshold, dhash_threshold,
                           progress_cb=None, cancel_check=None):
    """
    Pós-filtro dos grupos: dentro de cada grupo, um par (i, j) é confirmado se
        MD5 igual
        OU ( nenhum dos dois com phash degenerado
             E distância de phash <= phash_threshold
             E ( distância de dhash <= dhash_threshold, se ambos têm dhash;
                 senão confirmado só pelo phash, como antes ) ).
    O grupo é refeito por Union-Find sobre as arestas confirmadas (isso também
    impede que uma cadeia de coincidências arraste imagens não relacionadas);
    componentes de uma única imagem desaparecem.

    Garantias: cada subgrupo está contido num grupo de entrada; imagens com
    MD5 igual nunca se separam; a ordem dos grupos e dos índices é preservada
    (subgrupos de um grupo dividido saem na posição dele, pelo menor índice).
    Retorna (groups_out, stats). Levanta ScanCancelled se cancel_check() der True.
    """
    stats = _new_confirm_stats()
    out = []
    total = len(groups_idx)
    for gi, group in enumerate(groups_idx):
        if cancel_check and cancel_check():
            raise ScanCancelled()
        k = len(group)
        stats["groups_in"] += 1
        stats["images_in"] += k
        md5s = [md5_by_idx.get(i, images_data[i][2]) for i in group]
        if len(set(md5s)) < 2:
            # Todas idênticas entre si: nada a confirmar
            out.append(group)
            stats["groups_out"] += 1
            stats["groups_unchanged"] += 1
            stats["images_out"] += k
            if progress_cb:
                progress_cb(gi + 1, total)
            continue

        P = np.array([hash_to_int(images_data[i][1]) for i in group], dtype=np.uint64)
        deg = np.array([is_degenerate_hash(p) for p in P.tolist()], dtype=bool)
        class_of = {}
        cls = np.array([class_of.setdefault(m, len(class_of)) for m in md5s], dtype=np.int64)
        has = np.array([i in dhash_by_idx for i in group], dtype=bool)
        D = np.array([dhash_by_idx.get(i, 0) for i in group], dtype=np.uint64)
        stats["images_degenerate"] += int(deg.sum())

        uf = UnionFind(k)
        first_of_class = {}
        for pos, c in enumerate(cls.tolist()):
            if c in first_of_class:
                uf.union(first_of_class[c], pos)
            else:
                first_of_class[c] = pos

        # Pares (r < c) em blocos, como em find_similar_groups, para grupos
        # gigantes não estourarem a memória.
        block = int(max(16, min(512, 4_000_000 // k)))
        for start in range(0, k, block):
            if cancel_check and cancel_check():
                raise ScanCancelled()
            if start + 1 >= k:
                break
            end = min(start + block, k)
            rows = slice(start, end)
            cols = slice(start + 1, k)
            pd = _popcount_u64(P[rows][:, None] ^ P[cols][None, :])
            dd = _popcount_u64(D[rows][:, None] ^ D[cols][None, :])
            both = has[rows][:, None] & has[cols][None, :]
            nondeg = (~deg[rows])[:, None] & (~deg[cols])[None, :]
            same = cls[rows][:, None] == cls[cols][None, :]
            close_p = pd <= phash_threshold
            close_d = dd <= dhash_threshold
            # linha global start+r, coluna global start+1+c: par válido se r <= c
            tri = np.arange(end - start)[:, None] <= np.arange(k - start - 1)[None, :]
            cand = close_p & ~same & tri            # pares que dependem da confirmação
            stats["pairs_checked"] += int(cand.sum())
            stats["pairs_rejected_degenerate"] += int((cand & ~nondeg).sum())
            stats["pairs_rejected_dhash"] += int((cand & nondeg & both & ~close_d).sum())
            stats["pairs_without_dhash"] += int((cand & nondeg & ~both).sum())
            ok = cand & nondeg & (close_d | ~both)
            rr, cc = np.nonzero(ok)
            gr = (rr + start).tolist()
            gc = (cc + start + 1).tolist()
            CHUNK = 200_000
            for s in range(0, len(gr), CHUNK):
                if s and cancel_check and cancel_check():
                    raise ScanCancelled()
                for a, b in zip(gr[s:s + CHUNK], gc[s:s + CHUNK]):
                    uf.union(a, b)

        components = {}
        for pos in range(k):
            components.setdefault(uf.find(pos), []).append(pos)
        subgroups = [[group[p] for p in comp] for comp in components.values() if len(comp) > 1]
        kept = sum(len(s) for s in subgroups)
        out.extend(subgroups)
        stats["groups_out"] += len(subgroups)
        stats["images_out"] += kept
        stats["images_dropped"] += k - kept
        if not subgroups:
            stats["groups_dropped"] += 1
        elif len(subgroups) == 1 and kept == k:
            stats["groups_unchanged"] += 1
        else:
            stats["groups_split"] += 1
        if progress_cb:
            progress_cb(gi + 1, total)
    return out, stats


# ---------------------------------------------------------------------------
# "Mesma foto": provas por par e classes dentro dos grupos (funções puras)
# ---------------------------------------------------------------------------

def _exif_str(v):
    if v is None:
        return None
    try:
        s = str(v).strip().strip("\x00")
    except Exception:
        return None
    return s or None


def read_exif_fields(img):
    """Campos EXIF usados pelo veto, lidos do cabeçalho (sem decodificar):
       DateTimeOriginal e SubSecTimeOriginal ficam no sub-IFD Exif (0x8769)."""
    out = {"dto": None, "subsec": None, "uid": None, "make": None, "model": None}
    try:
        ex = img.getexif()
        if not ex:
            return out
        out["make"] = _exif_str(ex.get(271))
        out["model"] = _exif_str(ex.get(272))
        sub = ex.get_ifd(0x8769)
        out["dto"] = _exif_str(sub.get(0x9003))
        out["subsec"] = _exif_str(sub.get(0x9291))
        out["uid"] = _exif_str(sub.get(0xA420))
    except Exception:
        pass
    return out


def parse_exif_datetime(text):
    """'AAAA:MM:DD HH:MM:SS' -> timestamp (s) ou None."""
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        return None


def same_photo_features(filepath, size=None):
    """
    Provas de UMA imagem numa única abertura: dimensões brutas, campos EXIF e
    miniatura size x size em cinza dos pixels BRUTOS (sem exif_transpose, como
    o phash/dhash), via _flatten_alpha + draft JPEG + LANCZOS.
    """
    size = size or SAME_PHOTO_GRAY_SIZE
    with Image.open(filepath) as img:
        dims = img.size
        exif = read_exif_fields(img)
        if img.format == "JPEG":
            img.draft("L", (size * 2, size * 2))
        gray = _flatten_alpha(img).convert("L").resize((size, size), Image.LANCZOS)
        return {"dims": dims, "exif": exif, "gray": gray.tobytes()}


def gray_array(blob, size=None):
    """BLOB -> array float32 (size, size), ou None se o tamanho não bate."""
    size = size or SAME_PHOTO_GRAY_SIZE
    if blob is None or len(blob) != size * size:
        return None
    return np.frombuffer(blob, dtype=np.uint8).astype(np.float32).reshape(size, size)


def normalize_gray(a, min_std=None):
    """Média 0 e desvio 1; None se a imagem é quase lisa (sem prova)."""
    min_std = SAME_PHOTO_MIN_STD if min_std is None else min_std
    if a is None:
        return None
    std = float(a.std())
    if std < min_std:
        return None
    return (a - a.mean()) / std


def ncc_global(na, nb):
    """Correlação de Pearson entre duas miniaturas já normalizadas."""
    return float((na * nb).mean())


def block_max_error(na, nb, blocks=None):
    """
    Pior região: maior erro quadrático médio entre blocos correspondentes
    (grade blocks x blocks) das miniaturas normalizadas. Mede diferença
    LOCAL na escala global (uma pessoa que se mexeu, um adesivo), sem a
    instabilidade da correlação por bloco em regiões lisas (céu).
    """
    blocks = blocks or SAME_PHOTO_BLOCKS
    n = na.shape[0]
    h = n // blocks
    d = (na - nb)[:h * blocks, :h * blocks]
    d = d * d
    per_block = d.reshape(blocks, h, blocks, h).mean(axis=(1, 3))
    return float(per_block.max())


def gradient_vector(na):
    """Magnitude do gradiente da miniatura normalizada, centrada e com norma 1
       (None se não há estrutura). Pré-calculada uma vez por imagem."""
    gx = np.diff(na, axis=1)[:-1, :]
    gy = np.diff(na, axis=0)[:, :-1]
    g = np.sqrt(gx * gx + gy * gy)
    g = g - g.mean()
    norm = float(np.sqrt((g * g).sum()))
    if norm < 1e-6:
        return None
    return g / norm


def ncc_gradient(na, nb):
    """Correlação das magnitudes de gradiente (estrutura, não iluminação)."""
    ga, gb = gradient_vector(na), gradient_vector(nb)
    if ga is None or gb is None:
        return 0.0
    return float((ga * gb).sum())


def prepare_proof(feat, params=None):
    """
    Pré-processa as provas de UMA imagem para comparar muitos pares sem
    repetir trabalho: miniatura normalizada, vetor de gradiente e motivo de
    rejeição individual ("sem_prova", "dimensao", "lisa") ou None se está apta.
    """
    p = params or {}
    if not feat:
        return {"reason": "sem_prova"}
    dims = feat.get("dims")
    if not dims or min(dims) < p.get("min_side", SAME_PHOTO_MIN_SIDE):
        return {"reason": "dimensao", "dims": dims}
    na = normalize_gray(gray_array(feat.get("gray"), p.get("gray_size")), p.get("min_std"))
    if na is None:
        return {"reason": "lisa", "dims": dims}
    grad_min = p.get("grad_ncc_min", SAME_PHOTO_GRAD_NCC_MIN)
    return {"reason": None, "dims": dims, "exif": feat.get("exif") or {}, "na": na,
            "grad": gradient_vector(na) if grad_min is not None else None}


def aspect_close(d1, d2, tol=None):
    tol = SAME_PHOTO_ASPECT_TOL if tol is None else tol
    if not d1 or not d2 or 0 in d1 or 0 in d2:
        return False
    r1, r2 = d1[0] / d1[1], d2[0] / d2[1]
    return abs(r1 - r2) <= tol * max(r1, r2)


def exif_veto(e1, e2, pixel_identical, window=None):
    """
    EXIF nunca confirma; só derruba. Veta quando:
      - DateTimeOriginal existe nas duas, difere e a diferença é de até
        `window` segundos (rajada/sequência; diferença de dias é edição de
        data, ex.: scripts que corrigem a data), ou
      - DTO igual e SubSecTimeOriginal existe nas duas e difere, ou
      - ImageUniqueID existe nas duas e difere.
    Conteúdo pixel-idêntico nunca é vetado (não pode ser outra captura).
    """
    if pixel_identical or not e1 or not e2:
        return False
    window = SAME_PHOTO_EXIF_WINDOW if window is None else window
    d1, d2 = e1.get("dto"), e2.get("dto")
    if d1 and d2:
        if d1 != d2:
            t1, t2 = parse_exif_datetime(d1), parse_exif_datetime(d2)
            if t1 is None or t2 is None:
                return True
            if abs(t1 - t2) <= window:
                return True
        else:
            s1, s2 = e1.get("subsec"), e2.get("subsec")
            if s1 and s2 and s1 != s2:
                return True
    u1, u2 = e1.get("uid"), e2.get("uid")
    if u1 and u2 and u1 != u2:
        return True
    return False


def same_photo_pair_ok(fa, fb, params=None, pa=None, pb=None):
    """
    Provas de pixels/EXIF para um par já candidato pelos hashes.
    fa, fb: features (dims, exif, gray) OU None (sem prova => False);
    pa, pb: provas pré-processadas por prepare_proof (opcional, para não
    repetir trabalho em grupos grandes).
    Retorna (ok, motivo) com motivo em: "ok", "sem_prova", "dimensao",
    "proporcao", "lisa", "ncc", "bloco", "gradiente", "exif".
    """
    p = params or {}
    pa = pa if pa is not None else prepare_proof(fa, p)
    pb = pb if pb is not None else prepare_proof(fb, p)
    for reason in ("sem_prova", "dimensao", "lisa"):
        if pa["reason"] == reason or pb["reason"] == reason:
            return False, reason
    if not aspect_close(pa["dims"], pb["dims"], p.get("aspect_tol")):
        return False, "proporcao"
    na, nb = pa["na"], pb["na"]
    ncc = ncc_global(na, nb)
    if ncc < p.get("ncc_min", SAME_PHOTO_NCC_MIN):
        return False, "ncc"
    err = block_max_error(na, nb, p.get("blocks"))
    if err > p.get("block_err_max", SAME_PHOTO_BLOCK_ERR_MAX):
        return False, "bloco"
    grad_min = p.get("grad_ncc_min", SAME_PHOTO_GRAD_NCC_MIN)
    if grad_min is not None:
        ga, gb = pa.get("grad"), pb.get("grad")
        g = float((ga * gb).sum()) if ga is not None and gb is not None else 0.0
        if g < grad_min:
            return False, "gradiente"
    if p.get("exif_veto", SAME_PHOTO_EXIF_VETO):
        identical = (ncc >= p.get("pixel_identical_ncc", SAME_PHOTO_PIXEL_IDENTICAL_NCC)
                     and err <= p.get("pixel_identical_err", SAME_PHOTO_PIXEL_IDENTICAL_ERR))
        if exif_veto(pa["exif"], pb["exif"], identical, p.get("exif_window")):
            return False, "exif"
    return True, "ok"


def same_photo_candidate_pairs(images_data, groups_idx, md5_by_idx, dhash_by_idx,
                               phash_max=None, dhash_max=None, cancel_check=None):
    """
    Pares (gi, i, j) com i < j, dentro de cada grupo, que passam na porta dos
    hashes: MD5 diferente (e sem sentinela ERR:), nenhum degenerado, ambos com
    dhash, distância de phash <= phash_max e de dhash <= dhash_max.
    Grupos com um único MD5 são pulados. Retorna (pairs, stats).
    """
    phash_max = SAME_PHOTO_PHASH_MAX if phash_max is None else phash_max
    dhash_max = SAME_PHOTO_DHASH_MAX if dhash_max is None else dhash_max
    pairs = []
    stats = {"groups_checked": 0, "pairs_hash_rule": 0, "pairs_without_dhash": 0}
    for gi, group in enumerate(groups_idx):
        if cancel_check and cancel_check():
            raise ScanCancelled()
        md5s = [md5_by_idx.get(i, images_data[i][2]) for i in group]
        if len(set(md5s)) < 2:
            continue
        stats["groups_checked"] += 1
        k = len(group)
        P = np.array([hash_to_int(images_data[i][1]) for i in group], dtype=np.uint64)
        deg = np.array([is_degenerate_hash(p) for p in P.tolist()], dtype=bool)
        bad = np.array([(m is None) or str(m).startswith("ERR:") for m in md5s], dtype=bool)
        class_of = {}
        cls = np.array([class_of.setdefault(m, len(class_of)) for m in md5s], dtype=np.int64)
        has = np.array([i in dhash_by_idx for i in group], dtype=bool)
        D = np.array([dhash_by_idx.get(i, 0) for i in group], dtype=np.uint64)
        block = int(max(16, min(512, 4_000_000 // k)))
        for start in range(0, k, block):
            if start + 1 >= k:
                break
            end = min(start + block, k)
            rows, cols = slice(start, end), slice(start + 1, k)
            pd = _popcount_u64(P[rows][:, None] ^ P[cols][None, :])
            dd = _popcount_u64(D[rows][:, None] ^ D[cols][None, :])
            tri = np.arange(end - start)[:, None] <= np.arange(k - start - 1)[None, :]
            diff_md5 = cls[rows][:, None] != cls[cols][None, :]
            usable = (~deg[rows])[:, None] & (~deg[cols])[None, :] & (~bad[rows])[:, None] & (~bad[cols])[None, :]
            close = (pd <= phash_max) & tri & diff_md5 & usable
            both = has[rows][:, None] & has[cols][None, :]
            stats["pairs_without_dhash"] += int((close & ~both).sum())
            ok = close & both & (dd <= dhash_max)
            rr, cc = np.nonzero(ok)
            for r, c in zip((rr + start).tolist(), (cc + start + 1).tolist()):
                pairs.append((gi, group[r], group[c]))
    stats["pairs_hash_rule"] = len(pairs)
    return pairs, stats


def same_photo_features_for_indices(images_data, stats, indices, cache, workers,
                                    progress_cb=None, cancel_check=None):
    """Features (dims, exif, gray) dos índices pedidos: cache em lote, depois
       cálculo em paralelo (padrão de dhash_for_groups). -> (feat_by_idx, cancelled)."""
    feat_by_idx = {}
    indices = sorted(set(indices))
    items = []
    for i in indices:
        size, mt = stats[i] if stats[i] else (None, None)
        items.append((images_data[i][0], size, mt))
    cached = cache.lookup_many([it for it in items if it[1] is not None]) if cache is not None else {}
    to_compute = []
    for i, (fp, size, mt) in zip(indices, items):
        if fp in cached:
            feat_by_idx[i] = cached[fp]
        else:
            to_compute.append(i)
    total = len(to_compute)
    processed = 0
    cancelled = False
    if total == 0:
        return feat_by_idx, False

    def worker(idx):
        fp = images_data[idx][0]
        try:
            return idx, same_photo_features(fp), None
        except Exception as e:
            return idx, None, e

    def on_result(result):
        nonlocal processed
        idx, feat, err = result
        fp = images_data[idx][0]
        if err is None:
            feat_by_idx[idx] = feat
            size, mt = stats[idx] if stats[idx] else (None, None)
            if cache is not None and size is not None:
                cache.store(fp, size, mt, feat)
        else:
            log.warning("Mesma foto: sem provas para %s (%s)", fp, err)
        processed += 1
        if progress_cb:
            progress_cb(processed, total, fp)

    try:
        cancelled = _run_parallel_bounded(to_compute, worker, on_result, workers, cancel_check)
    finally:
        if cache is not None:
            cache.flush()
    return feat_by_idx, cancelled


def _new_same_photo_stats():
    return {"groups_checked": 0, "pairs_hash_rule": 0, "pairs_without_dhash": 0,
            "pairs_confirmed": 0, "rejected": {}, "unions_refused_clique": 0,
            "classes": 0, "images_in_classes": 0, "classes_suspect": 0}


def same_photo_classes(groups_idx, pairs, images_data, stats, md5_by_idx, feat_by_idx,
                       params=None, cancel_check=None):
    """
    Classes de "mesma foto" por grupo: Union-Find sobre pares confirmados com
    LIGAÇÃO COMPLETA (duas classes só se unem se todo par cruzado é aresta),
    memorizando recusas por par de raízes. Cópias com MD5 igual entram na
    classe da sua cópia. Só classes com 2+ MD5 distintos são devolvidas.
    Retorna (class_by_idx, suspect_class_ids, sp_stats). Id da classe = menor
    índice; classe "suspeita" = a de mais pixels tem bytes/pixel muito menor
    que outra (cópia ampliada?).
    """
    p = params or {}
    sp = _new_same_photo_stats()
    pairs_by_group = {}
    for gi, i, j in pairs:
        pairs_by_group.setdefault(gi, []).append((i, j))
    class_by_idx = {}
    suspect = set()
    bpp_ratio = p.get("upscale_bpp_ratio", SAME_PHOTO_UPSCALE_BPP_RATIO)
    for gi, group in enumerate(groups_idx):
        gpairs = pairs_by_group.get(gi)
        if not gpairs:
            continue
        if cancel_check and cancel_check():
            raise ScanCancelled()
        pos = {idx: k for k, idx in enumerate(group)}
        k = len(group)
        uf = UnionFind(k)
        edges = set()
        md5s = [md5_by_idx.get(i, images_data[i][2]) for i in group]
        first = {}
        for a, m in enumerate(md5s):
            if m is None or str(m).startswith("ERR:"):
                continue
            if m in first:
                uf.union(first[m], a)
                edges.add((min(first[m], a), max(first[m], a)))
                # cópias bit a bit também são arestas entre si e com quem já liga a uma delas
            else:
                first[m] = a
        # arestas por cópia idêntica: propaga para todas as duplas de mesmo MD5
        same_md5_members = {}
        for a, m in enumerate(md5s):
            same_md5_members.setdefault(m, []).append(a)
        for members in same_md5_members.values():
            for x in members:
                for y in members:
                    if x < y:
                        edges.add((x, y))
        confirmed = []
        proofs = {}
        for i, j in gpairs:
            for x in (i, j):
                if x not in proofs:
                    proofs[x] = prepare_proof(feat_by_idx.get(x), p)
            ok, reason = same_photo_pair_ok(None, None, p, proofs[i], proofs[j])
            if ok:
                a, b = pos[i], pos[j]
                confirmed.append((min(a, b), max(a, b)))
            else:
                sp["rejected"][reason] = sp["rejected"].get(reason, 0) + 1
        sp["pairs_confirmed"] += len(confirmed)
        for (a, b) in confirmed:
            edges.add((a, b))
        # aresta confirmada entre a e b vale para todas as cópias idênticas de a e de b
        for (a, b) in list(edges):
            for x in same_md5_members.get(md5s[a], [a]):
                for y in same_md5_members.get(md5s[b], [b]):
                    if x != y:
                        edges.add((min(x, y), max(x, y)))
        refused = set()
        members_of = {}
        for a in range(k):
            members_of.setdefault(uf.find(a), []).append(a)
        for (a, b) in sorted(confirmed):
            ra, rb = uf.find(a), uf.find(b)
            if ra == rb:
                continue
            key = (min(ra, rb), max(ra, rb))
            if key in refused:
                continue
            ma, mb = members_of[ra], members_of[rb]
            if all((min(x, y), max(x, y)) in edges for x in ma for y in mb):
                uf.union(a, b)
                root = uf.find(a)
                merged = ma + mb
                for r in (ra, rb):
                    members_of.pop(r, None)
                members_of[root] = merged
            else:
                refused.add(key)
                sp["unions_refused_clique"] += 1
        for root, members in members_of.items():
            if len(members) < 2:
                continue
            if len({md5s[a] for a in members}) < 2:
                continue
            idxs = [group[a] for a in members]
            cid = min(idxs)
            for idx in idxs:
                class_by_idx[idx] = cid
            sp["classes"] += 1
            sp["images_in_classes"] += len(idxs)
            # trava de cópia ampliada: bytes por pixel
            bpp = {}
            for idx in idxs:
                f = feat_by_idx.get(idx)
                size = stats[idx][0] if stats[idx] else None
                if f and f.get("dims") and size:
                    px = f["dims"][0] * f["dims"][1]
                    if px:
                        bpp[idx] = (px, size / px)
            if len(bpp) >= 2:
                best = max(bpp, key=lambda i: bpp[i][0])
                best_px, best_bpp = bpp[best]
                for idx, (px, val) in bpp.items():
                    if px < best_px and best_bpp < bpp_ratio * val:
                        suspect.add(cid)
                        sp["classes_suspect"] += 1
                        break
    return class_by_idx, suspect, sp


def same_photo_stage(images_data, stats, groups_idx, md5_by_idx, dhash_by_idx,
                     hash_cache, sp_cache, workers, progress_cb=None, cancel_check=None,
                     params=None):
    """
    Orquestra "mesma foto": porta dos hashes -> provas (cache/cálculo) ->
    classes. dhash_by_idx=None significa "não calculado": calcula só o
    necessário via dhash_for_groups; {} significa "nada a confirmar".
    Retorna (class_by_idx, suspect_ids, stats, cancelled).
    """
    cancelled = False
    if dhash_by_idx is None:
        dhash_by_idx, cancelled = dhash_for_groups(images_data, stats, groups_idx, md5_by_idx,
                                                   hash_cache, workers, progress_cb, cancel_check)
        if cancelled:
            return {}, set(), _new_same_photo_stats(), True
    pairs, pstats = same_photo_candidate_pairs(
        images_data, groups_idx, md5_by_idx, dhash_by_idx,
        (params or {}).get("phash_max"), (params or {}).get("dhash_max"), cancel_check)
    indices = sorted({i for _, i, j in pairs} | {j for _, i, j in pairs})
    feat_by_idx, cancelled = same_photo_features_for_indices(
        images_data, stats, indices, sp_cache, workers, progress_cb, cancel_check)
    if cancelled:
        return {}, set(), _new_same_photo_stats(), True
    class_by_idx, suspect, sp = same_photo_classes(
        groups_idx, pairs, images_data, stats, md5_by_idx, feat_by_idx, params, cancel_check)
    sp.update({k: pstats[k] for k in ("groups_checked", "pairs_hash_rule", "pairs_without_dhash")})
    return class_by_idx, suspect, sp, False


def image_status(info, md5_count):
    """'Idêntica' (MD5 repetido no grupo) > 'Mesma foto' (classe) > 'Mesmo vídeo'
       (mesmos fluxos, arquivo diferente) > 'Semelhante'."""
    if md5_count.get(info['md5'], 0) > 1:
        return "Idêntica"
    if info.get('same_photo') is not None:
        return "Mesma foto"
    if info.get('same_video'):
        return "Mesmo vídeo"
    return "Semelhante"


def plan_same_photo_selection(images, priority=SIMILAR_KEEP_PRIORITY):
    """
    Botão "Selecionar Mesma foto": por classe (info['same_photo']), entre
    TODAS as imagens do alvo da classe (cópias idênticas incluídas), mantém a
    melhor por keep_sort_key e seleciona as outras. Referência na classe =>
    seleciona todas as do alvo. Classe suspeita (cópia ampliada?) => nada.
    Nunca devolve referência.
    """
    classes = {}
    for i, img in enumerate(images):
        c = img.get('same_photo')
        if c is not None:
            classes.setdefault(c, []).append(i)
    selected = []
    for idxs in classes.values():
        if any(images[i].get('same_photo_suspect') for i in idxs):
            continue
        targets = [i for i in idxs if not images[i].get('is_reference')]
        if len(targets) < len(idxs):
            selected.extend(targets)
        elif len(targets) > 1:
            keep = min(targets, key=lambda i: keep_sort_key(images[i], i, priority))
            selected.extend(i for i in targets if i != keep)
    return selected


class ImageCleaner:
    def __init__(self, master):
        self.master = master
        self.master.title("Image Cleaner")
        apply_theme(self.master)
        apply_app_icon(self.master)
        self.groups = []
        self.images_data = []
        self.selected_folder = ""
        self.current_page = 0
        self.groups_per_page = 20
        self.group_check_vars = {}  # Armazena check_vars por grupo
        self.scan_errors = []  # Armazena erros de escaneamento
        # Modo de comparação de duas pastas (vazio = modo normal de uma pasta)
        self.reference_folder = ""
        self.reference_keys = set()     # cache_key dos arquivos listados na referência
        self.reference_prefix = None    # folder_prefix(reference_folder) durante o scan
        self.confirm_stats = None       # estatísticas da confirmação por dhash (se ligada)
        self.groups_same_photo = None   # por grupo, classe "mesma foto" de cada imagem (ou None)
        self.same_photo_suspect = set() # classes com cópia "ampliada?"
        self.same_photo_stats = None
        self.byte_groups = []           # grupos de arquivos idênticos por bytes (vídeos, outros)
        self.byte_stats = None
        self.byte_entry_count = 0
        self.byte_cached_proof = set()
        self.byte_same_video = set()    # cache_key dos membros de grupos "Mesmo vídeo"
        self.byte_stream = {}           # cache_key -> hash de fluxos (para reconferir)
        self.byte_cached_stream = set() # cache_key cuja prova de fluxos veio do cache
        self.same_video_stats = None
        self.ffmpeg_path = None         # achado na máquina (ou escolhido à mão)
        self.ffmpeg_version = None      # None = ainda não validado; "" = não funciona
        self.session_report = SessionReport()   # CSV criado no primeiro registro
        self.action_log = []            # lotes de ações desta sessão (para "Desfazer")
        self.settings = load_settings()
        self.create_widgets()
        self._apply_settings()

    def _apply_settings(self):
        """Opções e últimas pastas da sessão anterior (se ainda existirem)."""
        st = self.settings
        self.scan_subfolders_var.set(st["scan_subfolders"])
        self.use_cache_var.set(st["use_cache"])
        self.confirm_similar_var.set(st["confirm_similar"] if CONFIRM_SIMILAR else 0)
        self.same_photo_var.set(st["same_photo"] if SAME_PHOTO_ENABLED else 0)
        self.show_target_only_var.set(st["show_target_only"])
        self.scan_photos_var.set(st["scan_photos"])
        self.scan_videos_var.set(st["scan_videos"])
        self.scan_others_var.set(st["scan_others"])
        self.same_video_var.set(st["same_video"])
        self.ffmpeg_path = ffmpegtools.find_ffmpeg(st["ffmpeg_path"] or None, self._app_dir())
        self._on_kinds_changed()
        targets = [f for f in st["recent_targets"] if os.path.isdir(f)]
        refs = [f for f in st["recent_references"] if os.path.isdir(f)]
        if targets:
            self._apply_target_folder(targets[0])
            if refs and not folder_conflict(targets[0], refs[0]):
                self._apply_reference_folder(refs[0])

    def _save_settings(self):
        """Grava opções e pastas atuais (chamado ao iniciar o escaneamento)."""
        st = self.settings
        st["scan_subfolders"] = self.scan_subfolders_var.get()
        st["use_cache"] = self.use_cache_var.get()
        st["confirm_similar"] = self.confirm_similar_var.get()
        st["same_photo"] = self.same_photo_var.get()
        st["show_target_only"] = self.show_target_only_var.get()
        st["scan_photos"] = self.scan_photos_var.get()
        st["scan_videos"] = self.scan_videos_var.get()
        st["scan_others"] = self.scan_others_var.get()
        st["same_video"] = self.same_video_var.get()
        if self.selected_folder:
            st["recent_targets"] = push_recent(st["recent_targets"], self.selected_folder)
        if self.reference_folder:
            st["recent_references"] = push_recent(st["recent_references"], self.reference_folder)
        save_settings(st)

    def _fill_recent_menu(self, menu, key, apply):
        """Preenche um menu 'Recentes' com as pastas que ainda existem."""
        menu.delete(0, "end")
        folders = [f for f in self.settings.get(key, []) if os.path.isdir(f)]
        if not folders:
            menu.add_command(label="(nenhuma pasta recente)", state="disabled")
        for f in folders:
            menu.add_command(label=f, command=lambda p=f: apply(p))

    def create_widgets(self):
        # Janela inicial: tamanho decente e centralizada (só aparência; o
        # fluxo de botões/opções abaixo é o mesmo de sempre).
        width, height = 700, 650
        self.master.minsize(660, 610)
        try:
            sw = self.master.winfo_screenwidth()
            sh = self.master.winfo_screenheight()
            self.master.geometry(f"{width}x{height}+{(sw - width) // 2}+{(sh - height) // 3}")
        except tk.TclError:
            self.master.geometry(f"{width}x{height}")

        header = tk.Frame(self.master, bg="#2E7D32", padx=20, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="Image Cleaner", font=FONT_TITLE,
                 fg="white", bg="#2E7D32").pack(anchor="w")
        tk.Label(header, text="Encontre fotos duplicadas ou semelhantes e limpe seu acervo com segurança",
                 font=("Segoe UI", 10), fg="#E8F5E9", bg="#2E7D32").pack(anchor="w")

        steps = tk.Label(
            self.master, justify="left", fg="#444444", font=("Segoe UI", 9),
            text=("1. Selecione a pasta a limpar e marque o que procurar (fotos, vídeos, outros arquivos).\n"
                  "2. (Opcional) Selecione uma pasta de referência já organizada: nada dela será alterado.\n"
                  "3. Clique em Iniciar e revise os grupos encontrados antes de mover ou excluir.")
        )
        steps.pack(anchor="w", padx=20, pady=(14, 6))

        folder_row = tk.Frame(self.master)
        folder_row.pack(pady=10)
        self.select_btn = make_button(folder_row, "Selecionar Pasta", "primary",
                                      command=self.select_folder,
                                      font=("Segoe UI", 10, "bold"), padx=18, pady=6)
        self.select_btn.pack(side="left")
        self.recent_targets_mb = self._make_recent_menubutton(
            folder_row, "recent_targets", self._apply_target_folder)
        self.recent_targets_mb.pack(side="left", padx=(6, 0))

        # Label para exibir o caminho selecionado
        self.path_label = tk.Label(self.master, text="", fg="blue", wraplength=600)
        self.path_label.pack(pady=5)

        # Frame das opções (inicialmente oculto): duas linhas, para caber na
        # largura padrão da janela
        self.subfolder_frame = tk.Frame(self.master)
        kinds_row = tk.Frame(self.subfolder_frame)
        kinds_row.pack(anchor="w", pady=(0, 4))
        video_row = tk.Frame(self.subfolder_frame)
        video_row.pack(anchor="w", pady=(0, 4))
        options_row1 = tk.Frame(self.subfolder_frame)
        options_row1.pack(anchor="w")
        options_row2 = tk.Frame(self.subfolder_frame)
        options_row2.pack(anchor="w", pady=(4, 0))

        # O que procurar. Fotos: por aparência (como sempre). Vídeos e outros:
        # só cópias exatas, comparando o conteúdo byte a byte.
        tk.Label(kinds_row, text="Procurar duplicatas em:", font=FONT_BOLD).pack(side="left")
        self.scan_photos_var = tk.IntVar(value=1)
        self.scan_videos_var = tk.IntVar(value=0)
        self.scan_others_var = tk.IntVar(value=0)
        self.kind_checks = []
        for text, var in (("Fotos", self.scan_photos_var), ("Vídeos", self.scan_videos_var),
                          ("Outros arquivos", self.scan_others_var)):
            chk = tk.Checkbutton(kinds_row, text=text, variable=var, command=self._on_kinds_changed)
            chk.pack(side="left", padx=(10, 0))
            self.kind_checks.append(chk)
        self.kinds_info_label = tk.Label(kinds_row, text="ℹ️", fg="blue", cursor="hand2")
        self.kinds_info_label.pack(side="left", padx=5)
        self.create_tooltip(self.kinds_info_label,
                            "Fotos: acha cópias idênticas, a mesma foto em outra versão e fotos\n"
                            "semelhantes. Formatos que o programa não abre (HEIC, RAW) entram\n"
                            "só como cópia exata.\n"
                            "Vídeos e Outros arquivos: só cópias EXATAS (mesmo conteúdo, byte a\n"
                            "byte). O programa lê o mínimo possível: arquivos de tamanho único\n"
                            "nem são abertos.\n"
                            "Cuidado com 'Outros' em pastas de programas ou de projetos: eles\n"
                            "têm muitos arquivos iguais de propósito, e apagar um deles pode\n"
                            "quebrar o programa. Use em pastas de documentos e acervos.")

        # "Mesmo vídeo": precisa do ffmpeg (opcional). O programa procura sozinho;
        # "Localizar..." aponta o ffmpeg.exe à mão quando a busca não acha.
        self.same_video_var = tk.IntVar(value=1)
        self.same_video_check = tk.Checkbutton(video_row, text="Detectar 'Mesmo vídeo'",
                                               variable=self.same_video_var, state="disabled")
        self.same_video_check.pack(side="left", padx=(20, 0))
        self.ffmpeg_label = tk.Label(video_row, text="", fg=PALETTE["muted"])
        self.ffmpeg_label.pack(side="left", padx=(6, 0))
        self.ffmpeg_btn = make_button(video_row, "Localizar...", "light", command=self.locate_ffmpeg,
                                      pady=1, state="disabled")
        self.ffmpeg_btn.pack(side="left", padx=(6, 0))
        self.ffmpeg_info_label = tk.Label(video_row, text="ℹ️", fg="blue", cursor="hand2")
        self.ffmpeg_info_label.pack(side="left", padx=5)
        self.create_tooltip(self.ffmpeg_info_label, SAME_VIDEO_TOOLTIP)

        # Checkbox para escanear subpastas (marcada por padrão)
        self.scan_subfolders_var = tk.IntVar(value=1)
        self.subfolder_check = tk.Checkbutton(
            options_row1,
            text="Escanear subpastas",
            variable=self.scan_subfolders_var
        )
        self.subfolder_check.pack(side="left")

        # Ícone de informação (tooltip)
        self.info_label = tk.Label(options_row1, text="ℹ️", fg="blue", cursor="hand2")
        self.info_label.pack(side="left", padx=5)

        # Binds para o tooltip
        self.create_tooltip(self.info_label,
                           "Se marcado, o programa irá escanear a pasta selecionada\n"
                           "e todas as suas subpastas recursivamente.\n"
                           "Se desmarcado, apenas a pasta raiz será escaneada.")

        # Checkbox para usar cache de hashes (marcada por padrão)
        self.use_cache_var = tk.IntVar(value=1)
        self.cache_check = tk.Checkbutton(
            options_row1,
            text="Usar cache de hashes",
            variable=self.use_cache_var
        )
        self.cache_check.pack(side="left", padx=(15, 0))

        self.cache_info_label = tk.Label(options_row1, text="ℹ️", fg="blue", cursor="hand2")
        self.cache_info_label.pack(side="left", padx=5)
        self.create_tooltip(self.cache_info_label,
                            "Guarda os hashes já calculados em um cache local\n"
                            "(chave: caminho + tamanho + data de modificação).\n"
                            "Re-escanear a mesma pasta fica quase instantâneo e\n"
                            "um escaneamento cancelado pode ser retomado depois.\n"
                            "Arquivos alterados são sempre recalculados.")

        # Confirmação de semelhantes por segundo hash (2ª linha das opções)
        self.confirm_similar_var = tk.IntVar(value=1 if CONFIRM_SIMILAR else 0)
        self.confirm_check = tk.Checkbutton(
            options_row2,
            text="Confirmar semelhantes com segundo hash",
            variable=self.confirm_similar_var
        )
        self.confirm_check.pack(side="left")
        self.confirm_info_label = tk.Label(options_row2, text="ℹ️", fg="blue", cursor="hand2")
        self.confirm_info_label.pack(side="left", padx=5)
        self.create_tooltip(self.confirm_info_label,
                            "Antes de chamar duas fotos de 'Semelhantes', faz uma segunda\n"
                            "verificação independente. Evita juntar fotos diferentes que só\n"
                            "coincidem na distribuição de luz e sombra.\n"
                            "Cópias idênticas nunca são afetadas.\n"
                            "Desmarque para ver o agrupamento amplo de antes.")

        # Detecção de "Mesma foto" (mesma captura em outra versão)
        self.same_photo_var = tk.IntVar(value=1 if SAME_PHOTO_ENABLED else 0)
        self.same_photo_check = tk.Checkbutton(
            options_row2,
            text="Detectar 'Mesma foto'",
            variable=self.same_photo_var
        )
        if SAME_PHOTO_ENABLED:
            self.same_photo_check.pack(side="left", padx=(15, 0))
            self.same_photo_info_label = tk.Label(options_row2, text="ℹ️", fg="blue", cursor="hand2")
            self.same_photo_info_label.pack(side="left", padx=5)
            self.create_tooltip(self.same_photo_info_label, SAME_PHOTO_RULE_TOOLTIP)

        # Área da pasta de referência (modo comparação; inicialmente oculta)
        self.reference_container = tk.Frame(self.master)
        ref_row = tk.Frame(self.reference_container)
        ref_row.pack(fill="x")
        self.reference_btn = make_button(
            ref_row, "Selecionar Pasta de Referência (protegida)...", "light",
            command=self.select_reference_folder
        )
        self.reference_btn.pack(side="left")
        self.recent_refs_mb = self._make_recent_menubutton(
            ref_row, "recent_references", self._apply_reference_folder)
        self.recent_refs_mb.pack(side="left", padx=(4, 0))
        self.reference_clear_btn = make_button(
            ref_row, "Remover referência", "light", command=self.clear_reference_folder,
            state="disabled"
        )
        self.reference_clear_btn.pack(side="left", padx=(5, 0))
        self.reference_info_label = tk.Label(ref_row, text="ℹ️", fg="blue", cursor="hand2")
        self.reference_info_label.pack(side="left", padx=5)
        self.create_tooltip(self.reference_info_label,
                            "Opcional. Compara a pasta selecionada acima (ALVO) com um\n"
                            "acervo de REFERÊNCIA já organizado.\n"
                            "Imagens da referência NUNCA são selecionadas, movidas ou\n"
                            "excluídas: as duplicatas são removidas somente da pasta alvo.\n"
                            "A referência é sempre escaneada com subpastas.\n"
                            "Sem referência, o programa funciona no modo normal.")
        self.reference_path_label = tk.Label(
            self.reference_container, text="Nenhuma pasta de referência (modo normal)",
            fg="#2E7D32", wraplength=600
        )
        self.reference_path_label.pack(pady=(3, 0))
        self.show_target_only_var = tk.IntVar(value=1)
        self.show_target_only_check = tk.Checkbutton(
            self.reference_container,
            text="Mostrar duplicatas internas da pasta alvo (sem par na referência)",
            variable=self.show_target_only_var
        )
        # (exibido só quando há referência selecionada; ver select_reference_folder)

        # Botão Iniciar (inicialmente oculto)
        self.start_btn = make_button(self.master, "Iniciar escaneamento", "move",
                                     command=self.start_scan,
                                     font=("Segoe UI", 10, "bold"), padx=18, pady=6)
        # Não exibe o botão nem o frame de subpastas inicialmente

    def _scan_kinds(self):
        """(fotos, vídeos, outros) marcados na tela inicial."""
        return (self.scan_photos_var.get() == 1, self.scan_videos_var.get() == 1,
                self.scan_others_var.get() == 1)

    def _on_kinds_changed(self):
        """As opções de 2º hash e 'Mesma foto' só fazem sentido com Fotos; a de
           'Mesmo vídeo', com Vídeos (e é aí que o ffmpeg passa a interessar)."""
        state = "normal" if self.scan_photos_var.get() == 1 else "disabled"
        for chk in (self.confirm_check, self.same_photo_check):
            try:
                chk.config(state=state)
            except tk.TclError:
                pass
        self._refresh_ffmpeg_status()

    @staticmethod
    def _app_dir():
        """Pasta do programa (do .exe empacotado ou do main.py)."""
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _refresh_ffmpeg_status(self):
        """Linha do ffmpeg na tela inicial. Só com Vídeos marcado o ffmpeg é
           validado (roda `-version` numa thread: leva ~1 s); quem só usa Fotos
           nunca dispara processo nenhum."""
        videos = self.scan_videos_var.get() == 1
        try:
            self.ffmpeg_btn.config(state="normal" if videos else "disabled")
            if not videos:
                self.same_video_check.config(state="disabled")
                self.ffmpeg_label.config(text="(marque Vídeos)", fg=PALETTE["muted"])
                return
            if not self.ffmpeg_path:
                self.same_video_check.config(state="disabled")
                self.ffmpeg_label.config(text="ffmpeg não encontrado: só cópias exatas", fg="#B26A00")
                return
            if self.ffmpeg_version is None:
                self.same_video_check.config(state="disabled")
                self.ffmpeg_label.config(text="conferindo o ffmpeg...", fg=PALETTE["muted"])
                self._validate_ffmpeg_async()
                return
            if not self.ffmpeg_version:
                self.same_video_check.config(state="disabled")
                self.ffmpeg_label.config(text="o ffmpeg encontrado não funciona", fg="#B26A00")
                return
            self.same_video_check.config(state="normal")
            short = self.ffmpeg_version.split("-")[0][:18]      # "8.1.1-full_build-www..." -> "8.1.1"
            self.ffmpeg_label.config(text=f"ffmpeg {short} encontrado", fg=PALETTE["primary"])
        except tk.TclError:
            pass

    def _validate_ffmpeg_async(self):
        if getattr(self, '_ffmpeg_check_running', False):
            return
        self._ffmpeg_check_running = True
        path = self.ffmpeg_path
        box = {}

        def work():
            box["version"] = ffmpegtools.check_version(path) or ""

        threading.Thread(target=work, name="ffmpeg-version", daemon=True).start()

        def poll():
            if "version" not in box:
                try:
                    self.master.after(150, poll)
                except tk.TclError:
                    self._ffmpeg_check_running = False
                return
            self._ffmpeg_check_running = False
            if path == self.ffmpeg_path:
                self.ffmpeg_version = box["version"]
                log.info("ffmpeg: %s -> %s", path, self.ffmpeg_version or "não funciona")
            self._refresh_ffmpeg_status()

        poll()

    def locate_ffmpeg(self):
        """Aponta o ffmpeg.exe à mão; só é aceito (e salvo) se funcionar."""
        chosen = filedialog.askopenfilename(
            title="Onde está o ffmpeg.exe?",
            filetypes=[("ffmpeg", "ffmpeg.exe ffmpeg"), ("Todos os arquivos", "*.*")])
        if not chosen:
            return
        version = ffmpegtools.check_version(chosen)
        if not version:
            messagebox.showerror("ffmpeg", f"Este arquivo não é um ffmpeg que funciona:\n{chosen}")
            return
        self.ffmpeg_path = os.path.normpath(chosen)
        self.ffmpeg_version = version
        self.settings["ffmpeg_path"] = self.ffmpeg_path
        save_settings(self.settings)
        self._refresh_ffmpeg_status()

    def _same_video_ready(self):
        """ffmpeg pronto para o estágio 'Mesmo vídeo' desta varredura? (valida na
           hora se a checagem em segundo plano ainda não terminou)"""
        if self.scan_videos_var.get() != 1 or self.same_video_var.get() != 1 or not self.ffmpeg_path:
            return False
        if self.ffmpeg_version is None:
            self.ffmpeg_version = ffmpegtools.check_version(self.ffmpeg_path) or ""
        return bool(self.ffmpeg_version)

    def _make_recent_menubutton(self, parent, key, apply):
        mb = tk.Menubutton(parent, text="Recentes ▾", relief="flat", bg="#E0E0E0",
                           activebackground="#BDBDBD", padx=8, pady=4, cursor="hand2")
        menu = tk.Menu(mb, tearoff=0)
        menu.configure(postcommand=lambda m=menu: self._fill_recent_menu(m, key, apply))
        mb["menu"] = menu
        return mb

    def _hide_tooltip(self, event=None):
        """Fecha o tooltip ativo (há no máximo um por vez)."""
        tip = getattr(self, '_active_tooltip', None)
        self._active_tooltip = None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass

    def create_tooltip(self, widget, text):
        """Tooltip de um widget. Só existe um tooltip ativo por vez e ele fecha
           ao sair do widget, ao clicar nele e quando o widget é destruído
           (ex.: o grupo some da fila com o mouse ainda sobre o botão)."""
        def on_enter(event):
            self._hide_tooltip()
            tooltip = tk.Toplevel(self.master)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
            tk.Label(tooltip, text=text, justify='left', background="#ffffe0",
                     relief='solid', borderwidth=1, font=("Segoe UI", 9), padx=6, pady=4).pack()
            self._active_tooltip = tooltip

        widget.bind('<Enter>', on_enter)
        widget.bind('<Leave>', self._hide_tooltip)
        widget.bind('<Button-1>', self._hide_tooltip, add='+')
        widget.bind('<Destroy>', self._hide_tooltip, add='+')

    def select_folder(self):
        folder = filedialog.askdirectory(title="Selecione a pasta com imagens")
        if folder:
            self._apply_target_folder(folder)

    def _apply_target_folder(self, folder):
        """Define a pasta alvo (pelo diálogo, pelos recentes ou pelas configurações)."""
        self.selected_folder = folder
        self.path_label.config(text=f"Pasta selecionada: {folder}")
        self.subfolder_frame.pack(pady=5)
        self.reference_container.pack(pady=5)
        self.start_btn.pack(pady=10)
        # O alvo pode ter sido trocado por uma pasta que engloba (ou está
        # dentro) da referência já escolhida: nesse caso a referência cai.
        if self.reference_folder:
            reason = folder_conflict(folder, self.reference_folder)
            if reason:
                messagebox.showerror(
                    "Pastas em conflito",
                    f"A pasta de referência foi removida: {reason}.\n"
                    "Escolha pastas separadas (uma não pode conter a outra)."
                )
                self.clear_reference_folder()

    def select_reference_folder(self):
        """Escolhe a pasta de referência (protegida) do modo de comparação."""
        folder = filedialog.askdirectory(title="Selecione a pasta de referência (protegida)")
        if folder:
            self._apply_reference_folder(folder)

    def _apply_reference_folder(self, folder):
        reason = folder_conflict(self.selected_folder, folder) if self.selected_folder else None
        if reason:
            messagebox.showerror(
                "Pastas em conflito",
                f"Não é possível usar esta referência: {reason}.\n"
                "Escolha pastas separadas (uma não pode conter a outra)."
            )
            return
        self.reference_folder = folder
        self.reference_path_label.config(text=f"Referência (protegida): {folder}")
        self.reference_clear_btn.config(state="normal")
        self.show_target_only_check.pack(pady=(3, 0))

    def clear_reference_folder(self):
        """Volta ao modo normal de uma pasta."""
        self.reference_folder = ""
        self.reference_path_label.config(text="Nenhuma pasta de referência (modo normal)")
        self.reference_clear_btn.config(state="disabled")
        self.show_target_only_check.pack_forget()

    def start_scan(self):
        """Inicia o escaneamento quando o usuário clicar no botão Iniciar"""
        if self.selected_folder:
            if self.reference_folder:
                # Cinto de segurança: as pastas podem ter sido escolhidas em
                # qualquer ordem; revalida antes de tocar em qualquer arquivo.
                reason = folder_conflict(self.selected_folder, self.reference_folder)
                if reason:
                    messagebox.showerror("Pastas em conflito",
                                         f"Não é possível iniciar: {reason}.")
                    return
            if not any(self._scan_kinds()):
                messagebox.showerror("Nada para procurar",
                                     "Marque pelo menos um tipo: Fotos, Vídeos ou Outros arquivos.")
                return
            self._save_settings()
            self.scan_cancelled = False
            self.close_requested = False
            self.create_progress_window()
            # Agenda o scan para depois que a janela for criada
            self.master.after(100, self.scan_folder)

    def _on_main_window_close(self):
        """Fechar a janela principal durante um escaneamento: cancela o scan
           primeiro (para não deixar threads/janelas órfãs) e fecha em seguida."""
        if getattr(self, 'scan_in_progress', False):
            self.close_requested = True
            self.cancel_scan()
        else:
            self.master.destroy()

    def create_progress_window(self):
        """Cria janela de progresso"""
        self.progress_window = tk.Toplevel(self.master)
        self.progress_window.title("Escaneando Imagens")
        self.progress_window.geometry("500x200")
        self.progress_window.resizable(False, False)

        # Centraliza a janela
        self._make_modal(self.progress_window)

        # Fechar a janela pelo "X" equivale a cancelar
        # (a flag scan_cancelled é zerada em start_scan, não aqui: um pedido de
        # cancelamento feito entre duas fases não pode ser perdido)
        self.progress_window.protocol("WM_DELETE_WINDOW", self.cancel_scan)

        # Frame principal
        main_frame = tk.Frame(self.progress_window, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)

        # Label de status
        self.progress_label = tk.Label(main_frame, text="Inicializando...", font=("Arial", 10))
        self.progress_label.pack(pady=(0, 10))

        # Barra de progresso
        self.progress_bar = ttk.Progressbar(main_frame, length=450, mode='determinate')
        self.progress_bar.pack(pady=10)

        # Label de contagem
        self.progress_count_label = tk.Label(main_frame, text="0 / 0 imagens", font=("Arial", 9))
        self.progress_count_label.pack(pady=(5, 0))

        # Label de tempo (decorrido / estimado)
        self.progress_time_label = tk.Label(main_frame, text="", font=("Arial", 9), fg="#555555")
        self.progress_time_label.pack(pady=(2, 0))

        # Botão cancelar
        self.progress_cancel_btn = make_button(main_frame, "Cancelar", "light", command=self.cancel_scan)
        self.progress_cancel_btn.pack(pady=(8, 0))

        self.progress_started_at = time.time()
        self._last_progress_update = 0.0

    def cancel_scan(self):
        """Marca o escaneamento atual para ser cancelado"""
        self.scan_cancelled = True
        try:
            if hasattr(self, 'progress_label') and self.progress_window.winfo_exists():
                self.progress_label.config(text="Cancelando...")
                self.progress_cancel_btn.config(state="disabled")
        except tk.TclError:
            pass

    @staticmethod
    def _format_seconds(seconds):
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m:02d}m {s:02d}s"

    def update_progress(self, current, total, filename, force=False, unit="imagens"):
        """Atualiza a barra de progresso (no máximo ~10x por segundo, para não
           deixar a interface lenta com centenas de milhares de arquivos).
           Se a janela tiver sido destruída (ex.: app fechado), pede cancelamento."""
        try:
            self._update_progress_widgets(current, total, filename, force, unit)
        except tk.TclError:
            self.scan_cancelled = True

    def _update_progress_widgets(self, current, total, filename, force, unit):
        if hasattr(self, 'progress_window') and self.progress_window.winfo_exists():
            now = time.time()
            if not force and (now - self._last_progress_update) < 0.1 and current != total:
                return
            self._last_progress_update = now

            # Atualiza o progresso
            progress_percent = (current / total * 100) if total > 0 else 0
            self.progress_bar['value'] = progress_percent

            # Atualiza labels
            if not self.scan_cancelled:
                self.progress_label.config(text=f"Processando: {os.path.basename(filename)}")
            self.progress_count_label.config(text=f"{current} / {total} {unit}")

            elapsed = now - self.progress_started_at
            if current > 0 and elapsed > 1:
                remaining = elapsed * (total - current) / current
                self.progress_time_label.config(
                    text=f"Decorrido: {self._format_seconds(elapsed)}   |   "
                         f"Restante (estimado): {self._format_seconds(remaining)}"
                )
            else:
                self.progress_time_label.config(text=f"Decorrido: {self._format_seconds(elapsed)}")

            # Força atualização da interface (também processa o clique em Cancelar)
            self.progress_window.update()

    def _main_window_alive(self):
        try:
            return bool(self.master.winfo_exists())
        except tk.TclError:
            return False

    def _make_modal(self, win, master=None):
        """Torna `win` modal sobre `master` (transient + grab) sem deixar o
           programa preso quando a dona está, ou fica, minimizada.

           No Windows, o Tk esconde a transient ("withdrawn") enquanto a dona
           está minimizada. Se o grab estiver nessa janela invisível, a dona
           recusa ser restaurada pela barra de tarefas e não fecha: o programa
           parece travado, com wait_window esperando um clique que ninguém
           consegue dar. Aconteceu com o relatório de erros ao fim de uma
           varredura de 20 minutos, com a principal minimizada. Por isso, a
           dona é restaurada antes do grab, e o grab é solto enquanto ela
           estiver minimizada e refeito quando ela reaparece."""
        master = master if master is not None else self.master
        try:
            if master.state() == "iconic":
                master.deiconify()
        except tk.TclError:
            pass
        win.transient(master)
        win.grab_set()
        if not hasattr(self, "_modal_windows"):
            self._modal_windows = []            # [(modal, dona)]; mortas são varridas
            self._modal_bound = []              # donas que já têm o par de binds
        self._modal_windows.append((win, master))
        if not any(m is master for m in self._modal_bound):
            # A marca fica na instância, não no widget: nos testes a mesma raiz
            # Tk serve a vários ImageCleaner, cada um com sua lista de modais.
            self._modal_bound.append(master)
            master.bind("<Unmap>", lambda e, m=master: self._on_master_state(e, m, "unmap"), add="+")
            master.bind("<Map>", lambda e, m=master: self._on_master_state(e, m, "map"), add="+")

    def _on_master_state(self, event, master, what):
        """Solta o grab dos modais de `master` quando ela é minimizada e o
           devolve ao modal mais recente quando ela volta."""
        if event.widget is not master:
            return                              # filhos também geram <Map>/<Unmap>
        self._modal_windows = [(w, m) for (w, m) in self._modal_windows if self._widget_alive(w)]
        mine = [w for (w, m) in self._modal_windows if m is master]
        if not mine:
            return
        try:
            if what == "unmap":
                for w in mine:
                    w.grab_release()
            elif master.state() != "iconic":
                mine[-1].grab_set()
        except tk.TclError:
            pass

    def scan_folder(self):
        """Lista os arquivos e calcula os hashes (em paralelo, com cache e cancelamento)."""
        self.scan_in_progress = True
        try:
            self._scan_folder_impl()
        except (tk.TclError, RuntimeError) as e:
            # RuntimeError: tkinter levanta "no default root" (não TclError)
            # quando a janela principal é destruída no meio da preparação
            if self.close_requested or not self._main_window_alive():
                # Janela fechada no meio do processo: encerra em silêncio
                log.warning("Escaneamento interrompido: janela fechada")
            else:
                log.exception("Erro de interface (Tk) durante o escaneamento")
                if isinstance(e, RuntimeError):
                    raise
                self._close_progress_window()
                try:
                    messagebox.showerror(
                        "Erro inesperado",
                        f"Erro de interface durante o escaneamento: {e}\n\n"
                        f"Detalhes no log: {get_log_path()}"
                    )
                except tk.TclError:
                    pass
        except Exception:
            log.exception("Erro inesperado durante o escaneamento")
            self._close_progress_window()
            try:
                messagebox.showerror(
                    "Erro inesperado",
                    "Ocorreu um erro inesperado durante o escaneamento.\n\n"
                    f"Detalhes no log: {get_log_path()}"
                )
            except tk.TclError:
                pass
        finally:
            self.scan_in_progress = False
            if getattr(self, 'close_requested', False):
                try:
                    self.master.destroy()
                except tk.TclError:
                    pass

    def _scan_folder_impl(self):
        self.images_data = []
        self.scan_errors = []  # Reseta lista de erros
        self.file_stats = {}   # filepath -> (size, mtime_ns), reaproveitado depois
        self.reference_keys = set()
        self.reference_prefix = None
        self.scan_origin_counts = None  # (n_alvo, n_referência) no modo comparação
        self.byte_groups = []
        self.byte_stats = None
        self.byte_entry_count = 0       # quantos arquivos foram para a comparação por bytes
        self.byte_cached_proof = set()  # cache_key dos agrupados cuja prova veio do cache
        self.byte_same_video = set()
        self.byte_stream = {}
        self.byte_cached_stream = set()
        self.same_video_stats = None
        ref = self.reference_folder
        scan_started = time.time()
        log.info("Iniciando escaneamento de: %s (subpastas=%s, cache=%s, threads=%d, "
                 "confirmar semelhantes=%s)",
                 self.selected_folder, self.scan_subfolders_var.get() == 1,
                 self.use_cache_var.get() == 1, HASH_WORKERS,
                 CONFIRM_SIMILAR and self.confirm_similar_var.get() == 1)
        if ref:
            log.info("Modo referência: pasta protegida = %s (duplicatas internas do alvo: %s)",
                     ref, "exibidas" if self.show_target_only_var.get() == 1 else "ocultas")

        # Verifica se deve escanear subpastas
        scan_subfolders = self.scan_subfolders_var.get() == 1

        # Cache de hashes (opcional)
        use_cache = self.use_cache_var.get() == 1
        self.hash_cache = None
        if use_cache and self._scan_kinds()[0]:   # sem Fotos, o cache de fotos nem é carregado
            self.hash_cache = HashCache()
            self.hash_cache.load_prefix(self.selected_folder)
            if ref:
                self.hash_cache.load_prefix(ref, merge=True)

        cancel_check = lambda: self.scan_cancelled

        try:
            # Primeira passagem: listar arquivos (com feedback, pode demorar em rede/HDD)
            self.progress_label.config(text="Contando arquivos...")
            self.progress_window.update()

            def on_listing_progress(count):
                if self.progress_window.winfo_exists():
                    self.progress_label.config(text=f"Contando arquivos... {count}")
                    self.progress_window.update()

            # Uma única caminhada pelo disco lista tudo o que as caixas pedem;
            # as fotos saem na mesma ordem relativa de sempre.
            want_photos, want_videos, want_others = self._scan_kinds()
            byte_kinds = want_videos or want_others
            accept = make_scan_accept(want_photos, want_videos, want_others)
            try:
                entries = list_image_files(self.selected_folder, scan_subfolders,
                                           VALID_EXTENSIONS, on_listing_progress, cancel_check,
                                           accept=accept)
                if ref and not self.scan_cancelled:
                    # A referência é sempre listada com subpastas (é um acervo).
                    def on_ref_listing_progress(count):
                        if self.progress_window.winfo_exists():
                            self.progress_label.config(
                                text=f"Contando arquivos da referência... {count}")
                            self.progress_window.update()

                    self.progress_label.config(text="Contando arquivos da referência...")
                    self.progress_window.update()
                    ref_entries = list_image_files(ref, True, VALID_EXTENSIONS,
                                                   on_ref_listing_progress, cancel_check,
                                                   accept=accept)
                    n_target = len(entries)
                    # O resumo fala de IMAGENS: conta só as do pipeline de fotos
                    self.scan_origin_counts = tuple(
                        sum(1 for (fp, _, _) in lst if classify_file(fp) == KIND_PHOTO)
                        for lst in (entries, ref_entries))
                    entries, self.reference_keys = merge_scan_entries(entries, ref_entries)
                    self.reference_prefix = folder_prefix(ref)
                    log.info("Modo referência: %d arquivos no alvo, %d na referência",
                             n_target, len(ref_entries))
            except Exception as e:
                self._close_progress_window()
                messagebox.showerror("Erro", f"Erro ao listar arquivos: {e}")
                return

            if self.scan_cancelled:
                self._close_progress_window()
                log.info("Escaneamento cancelado durante a listagem")
                if not self.close_requested:
                    messagebox.showinfo("Escaneamento Cancelado", "Escaneamento cancelado durante a listagem de arquivos.")
                return

            # Fotos seguem para o pipeline de aparência; o resto (vídeos, outros
            # e fotos que o Pillow não abre) só é comparado por bytes. Uma foto
            # NUNCA entra nos dois: um caminho aparece em no máximo um grupo.
            photo_entries = [e for e in entries if classify_file(e[0]) == KIND_PHOTO]
            byte_entries = [e for e in entries if classify_file(e[0]) != KIND_PHOTO]
            self.byte_entry_count = len(byte_entries)
            total_files = len(photo_entries)
            log.info("%d arquivos de imagem listados em %.1fs (mais %d para comparar por bytes)",
                     total_files, time.time() - scan_started, len(byte_entries))
            if not entries:
                self._close_progress_window()
                messagebox.showinfo("Resultado", "Nenhum arquivo encontrado." if byte_kinds
                                    else "Nenhuma imagem encontrada.")
                return

            # Todas as entradas: a tela de grupos usa tamanho e data daqui
            self.file_stats = {fp: (size, mtime_ns) for (fp, size, mtime_ns) in entries}

            # Segunda passagem: hashes (threads), consultando o cache antes.
            # O MD5 NÃO é calculado aqui: só é necessário para as imagens que
            # caírem em algum grupo (ver group_images), evitando ler o acervo 2x.
            self.progress_started_at = time.time()
            results, errors_by_idx, cancelled = [], {}, False
            if photo_entries:
                results, errors_by_idx, cancelled = hash_files(
                    photo_entries, self.hash_cache, HASH_WORKERS,
                    progress_cb=self.update_progress, cancel_check=cancel_check
                )

            # Comparação por bytes, ainda ANTES do resumo: toda a leitura pesada
            # acontece de uma vez, sem pedir um clique no meio do caminho.
            byte_cancelled = False
            if byte_entries and not (cancelled or self.scan_cancelled):
                byte_cancelled = self._run_byte_stage(byte_entries, use_cache, cancel_check)

            self._close_progress_window()

            if byte_cancelled:
                log.info("Comparação por bytes cancelada")
                if not self.close_requested:
                    messagebox.showinfo(
                        "Escaneamento Cancelado",
                        "Comparação de arquivos cancelada."
                        + ("\n\nO que já foi calculado ficou no cache: ao escanear novamente,\n"
                           "o programa continua de onde parou." if use_cache else ""))
                return

            if cancelled or self.scan_cancelled:
                processed = sum(1 for r in results if r is not None) + len(errors_by_idx)
                self.scan_errors = [errors_by_idx[i] for i in sorted(errors_by_idx)]
                log.info("Escaneamento cancelado: %d de %d processados, %d com erro",
                         processed, total_files, len(self.scan_errors))
                for err in self.scan_errors[:200]:
                    log.info("  erro [%s] %s: %s", err['type'], err['filepath'], err['message'])
                if self.close_requested:
                    return
                cache_ok = self.hash_cache is not None and self.hash_cache.active
                messagebox.showinfo(
                    "Escaneamento Cancelado",
                    f"Escaneamento cancelado. {processed} de {total_files} arquivos foram processados"
                    + (f" ({len(self.scan_errors)} com erro, listados no log)." if self.scan_errors else ".")
                    + ("\n\nOs hashes já calculados ficaram no cache: ao escanear novamente,\n"
                       "o programa continua de onde parou." if cache_ok else "")
                )
                return

            # Monta as listas finais na ordem original dos arquivos
            self.images_data = [r for r in results if r is not None]
            self.scan_errors = [errors_by_idx[i] for i in sorted(errors_by_idx)]
            log.info("Hashes concluídos em %.1fs: %d ok, %d com erro",
                     time.time() - scan_started, len(self.images_data), len(self.scan_errors))
            for err in self.scan_errors[:200]:
                log.info("  erro [%s] %s: %s", err['type'], err['filepath'], err['message'])

            byte_errors = self.byte_stats["errors"] if self.byte_stats else []
            if byte_errors:
                for err in byte_errors[:200]:
                    log.info("  fora da comparação por bytes: %s: %s", err['filepath'], err['message'])
                messagebox.showwarning(
                    "Comparação incompleta",
                    f"{len(byte_errors)} arquivo(s) não puderam ser lidos (sem permissão, em uso ou "
                    "alterados durante a leitura) e ficaram FORA da comparação por conteúdo: o "
                    "programa nunca chama de 'Idêntica' o que não conseguiu ler inteiro.\n\n"
                    f"Os caminhos estão no log: {get_log_path()}"
                )

            # Exibe resumo do escaneamento (processadas = sem erro)
            self.show_scan_summary(total_files, len(self.images_data), byte_count=len(byte_entries))

            # Agrupa (o cache é fechado dentro de group_images)
            self.group_images(threshold=SIMILARITY_THRESHOLD)
        finally:
            self._close_progress_window()
            if self.hash_cache is not None:
                self.hash_cache.close()
                self.hash_cache = None

    def _run_byte_stage(self, byte_entries, use_cache, cancel_check):
        """Comparação por bytes dentro da janela de progresso já aberta.
           Preenche self.byte_groups/self.byte_stats. Retorna True se cancelada."""
        t0 = time.time()
        self.progress_window.title("Comparando Arquivos")
        phase_text = {
            "probe": "Lendo o cabeçalho dos vídeos (dimensões, duração)...",
            "stream": "Comparando o vídeo e o áudio dos candidatos a 'Mesmo vídeo' (ffmpeg)...",
            "head": "Comparando o começo dos arquivos de mesmo tamanho...",
            "quick": "Comparando o meio e o fim dos que empataram no começo...",
            "full": "Conferindo o conteúdo inteiro dos candidatos a cópia exata...",
        }
        state = {"phase": "head"}

        def on_phase(phase, n_files, total_bytes):
            state["phase"] = phase
            self.progress_started_at = time.time()
            self._last_progress_update = 0.0
            log.info("Comparação por bytes, fase %s: %d arquivos, %s a ler",
                     phase, n_files, format_bytes(total_bytes))

        def on_progress(done, total, filepath):
            # Progresso por BYTES: com arquivos de GB, contar arquivos não diz nada
            div, unit = (1024 * 1024, "MB") if total >= 10 * 1024 * 1024 else (1024, "KB")
            # arredonda para cima: o fim tem de mostrar "N / N", nunca "0 / 1"
            self.update_progress(-(-done // div), max(1, -(-total // div)), filepath or "", unit=unit)
            try:
                if not self.scan_cancelled and self.progress_window.winfo_exists():
                    self.progress_label.config(
                        text=f"{phase_text[state['phase']]}\n{os.path.basename(filepath or '')}")
            except tk.TclError:
                pass

        cache = FileHashCache() if use_cache else None
        ref_keys = self.reference_keys if self.reference_folder else None
        hide_target_only = self.show_target_only_var.get() == 0
        same_idx, stream_by_idx, sv_stats = set(), {}, None
        try:
            groups_idx, md5_by_idx, stats, cancelled = find_identical_files(
                byte_entries, cache, HASH_WORKERS, reference_keys=ref_keys,
                hide_target_only=hide_target_only,
                progress_cb=on_progress, phase_cb=on_phase, cancel_check=cancel_check)
            if not (cancelled or self.scan_cancelled) and self._same_video_ready():
                # "Mesmo vídeo": mesmos fluxos em arquivos diferentes (só com ffmpeg)
                groups_idx, md5_by_idx, same_idx, stream_by_idx, sv_stats, cancelled = find_same_videos(
                    byte_entries, groups_idx, md5_by_idx, self.ffmpeg_path,
                    ffmpegtools.ffprobe_beside(self.ffmpeg_path), cache, HASH_WORKERS,
                    reference_keys=ref_keys, hide_target_only=hide_target_only,
                    progress_cb=on_progress, phase_cb=on_phase, cancel_check=cancel_check)
        finally:
            if cache is not None:
                cache.close()
        self.byte_stats = stats
        if cancelled or self.scan_cancelled:
            return True
        self.byte_groups = build_byte_groups(byte_entries, groups_idx, md5_by_idx)
        self.same_video_stats = sv_stats
        self.byte_same_video = {cache_key(byte_entries[i][0]) for i in same_idx}
        self.byte_stream = {cache_key(byte_entries[i][0]): v for i, v in stream_by_idx.items()}
        self.byte_cached_stream = {cache_key(fp) for fp in (sv_stats or {}).get("stream_from_cache", [])}
        if sv_stats:
            stats["groups"] = len(groups_idx)     # o resumo conta os grupos como ficaram
            log.info("Mesmo vídeo: %s", {k: v for k, v in sv_stats.items() if k != "errors"})
        # Provas que vieram do cache (não foram lidas nesta rodada): são
        # reconferidas na hora de mover/excluir (ver _verify_cached_proofs)
        self.byte_cached_proof = {cache_key(fp) for fp in stats["md5_from_cache"]}
        log.info("Comparação por bytes em %.1fs: %d arquivos, %d candidatos por tamanho, "
                 "%d inícios, %d meio/fim, %d MD5 completos, %s lidos, %d provas do cache, "
                 "%d hardlinks, %d erros, %d grupos com %d arquivos",
                 time.time() - t0, stats["files"], stats["size_candidates"], stats["head_computed"],
                 stats["quick_computed"], stats["full_computed"], format_bytes(stats["bytes_read"]),
                 len(stats["md5_from_cache"]), stats["hardlinks"],
                 len(stats["errors"]), stats["groups"], stats["identical_files"])
        return False

    def _close_progress_window(self):
        """Fecha a janela de progresso, se existir"""
        try:
            if hasattr(self, 'progress_window') and self.progress_window.winfo_exists():
                self.progress_window.destroy()
        except tk.TclError:
            pass

    def show_scan_summary(self, total_files, processed_files, byte_count=0):
        """Exibe resumo do escaneamento com detalhes de erros.
           total_files/processed_files contam só IMAGENS (pipeline de fotos);
           byte_count é quantos outros arquivos foram comparados por conteúdo
           (0 = mensagens exatamente como sempre foram)."""
        # Linha extra só no modo de comparação (modo normal: mensagem intocada)
        origin_line = ""
        if self.reference_folder and self.scan_origin_counts:
            n_target, n_ref = self.scan_origin_counts
            origin_line = (f"Sendo {n_target} da pasta alvo e {n_ref} da referência "
                           f"(protegida).\n")
        byte_line = ""
        if byte_count:
            n_groups = len(self.byte_groups)
            byte_line = (f"✓ {byte_count} arquivo(s) comparados só por conteúdo (vídeos, outros, "
                         f"fotos HEIC/RAW): {n_groups} grupo(s) de cópias exatas.\n")

        if not self.scan_errors:
            # Sem erros
            photo_line = (f"✓ {processed_files} de {total_files} imagens processadas com sucesso!\n"
                          + origin_line) if (total_files or not byte_count) else ""
            message = (
                photo_line + byte_line +
                f"\n⚠️ Ao clicar em OK, o carregamento pode demorar alguns minutos.\n"
                f"Por favor, aguarde."
            )
            messagebox.showinfo("Escaneamento Concluído", message)
            return

        # Há erros - mostra janela detalhada
        error_window = tk.Toplevel(self.master)
        error_window.title("Relatório de Escaneamento")
        error_window.geometry("700x500")

        # Torna a janela modal (e traz a principal de volta se estiver minimizada)
        self._make_modal(error_window)

        # Frame superior com resumo
        summary_frame = tk.Frame(error_window, bg="#fff3cd", padx=10, pady=10)
        summary_frame.pack(fill="x", padx=10, pady=10)

        success_count = processed_files
        error_count = len(self.scan_errors)

        summary_text = (
            f"✓ Imagens processadas: {success_count}\n"
            f"✗ Imagens com erro: {error_count}\n"
            f"📊 Total encontrado: {total_files}"
            + (f"\n{origin_line.rstrip()}" if origin_line else "")
            + (f"\n{byte_line.rstrip()}" if byte_line else "")
        )

        tk.Label(summary_frame, text=summary_text, font=("Arial", 10, "bold"),
                bg="#fff3cd", justify="left").pack(anchor="w")

        # Aviso sobre tempo de carregamento
        warning_frame = tk.Frame(error_window, bg="#fff3cd", padx=10)
        warning_frame.pack(fill="x", padx=10, pady=(0, 10))

        warning_text = "⚠️ Ao fechar esta janela, o carregamento pode demorar alguns minutos. Por favor, aguarde."
        tk.Label(warning_frame, text=warning_text, font=("Arial", 9),
                bg="#fff3cd", fg="#856404", justify="left").pack(anchor="w")

        # Categoriza erros
        error_types = {}
        for error in self.scan_errors:
            error_type = error['type']
            if error_type not in error_types:
                error_types[error_type] = []
            error_types[error_type].append(error)

        # Frame com categorias
        categories_frame = tk.Frame(error_window, padx=10)
        categories_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(categories_frame, text="Tipos de Erro Encontrados:",
                font=("Arial", 9, "bold")).pack(anchor="w")

        for error_type, errors in error_types.items():
            tk.Label(categories_frame, text=f"  • {error_type}: {len(errors)} arquivo(s)",
                    font=("Arial", 9)).pack(anchor="w")

        # Lista de erros em scrolled text
        tk.Label(error_window, text="Detalhes dos Erros:", font=("Arial", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 5))

        text_frame = tk.Frame(error_window)
        text_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        error_text = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, font=("Courier", 8))
        error_text.pack(fill="both", expand=True)

        # Agrupa por tipo
        for error_type, errors in error_types.items():
            error_text.insert(tk.END, f"\n═══ {error_type} ({len(errors)}) ═══\n", "header")
            for error in errors:
                error_text.insert(tk.END, f"\n📁 Arquivo: {error['filepath']}\n")
                error_text.insert(tk.END, f"💬 Detalhes: {error['message']}\n")

        # Configurações de tag
        error_text.tag_config("header", font=("Courier", 9, "bold"), foreground="#d9534f")
        error_text.config(state="disabled")

        # Frame inferior com botões e informações
        bottom_frame = tk.Frame(error_window)
        bottom_frame.pack(fill="x", padx=10, pady=10)

        # Informações úteis
        info_frame = tk.LabelFrame(bottom_frame, text="O que fazer?", padx=10, pady=10)
        info_frame.pack(fill="x", pady=(0, 10))

        info_text = (
            "• Arquivo Truncado: Imagem incompleta, possivelmente download interrompido\n"
            "• Dados Corrompidos: Arquivo danificado, pode estar corrompido no disco\n"
            "• Formato Inválido: Extensão .jpg mas não é uma imagem válida\n\n"
            "💡 Recomendação: Você pode tentar recuperar essas imagens com ferramentas\n"
            "   especializadas ou movê-las para uma pasta separada para análise manual."
        )

        tk.Label(info_frame, text=info_text, justify="left", font=("Arial", 8)).pack(anchor="w")

        # Botão fechar
        make_button(bottom_frame, "Fechar", "select", command=error_window.destroy, padx=20).pack()

        # Aguarda o usuário fechar a janela antes de continuar
        error_window.wait_window()

    def group_images(self, threshold=SIMILARITY_THRESHOLD):
        """
        Orquestra o resultado: grupos de fotos (_photo_groups, o pipeline de
        sempre) seguidos dos grupos de arquivos idênticos por bytes (já
        calculados em _scan_folder_impl), e exibe a tela de grupos.

        _photo_groups termina em um de três estados:
          "ok"     há grupos de fotos;
          "empty"  nada a mostrar, com o (título, texto) que sempre foi
                   exibido nesse caso: só aparece se também não houver
                   grupos de bytes;
          "abort"  cancelado ou janela fechada (a mensagem, se havia, já foi
                   mostrada): nada mais roda.
        Só com Fotos marcado, diálogos e resultado são os de sempre.
        """
        self.confirm_stats = None
        self.groups_same_photo = None
        self.same_photo_suspect = set()
        self.same_photo_stats = None
        byte_groups = list(self.byte_groups or [])
        state, payload = self._photo_groups(threshold)
        if state == "abort":
            return
        photo_groups = payload if state == "ok" else []
        if not photo_groups and not byte_groups:
            # Sem nada comparado por bytes, a mensagem é a de sempre das fotos
            if state == "empty" and not self.byte_entry_count:
                messagebox.showinfo(*payload)
            else:
                messagebox.showinfo("Resultado", "Nenhuma duplicata encontrada.")
            return
        if state == "empty" and self.images_data:
            # Havia fotos e nenhuma ficou em grupo: o motivo (ex.: a confirmação
            # por segundo hash descartou tudo) não pode sumir só porque há
            # grupos de outros arquivos para mostrar
            messagebox.showinfo(payload[0], payload[1] + chr(10) + chr(10)
                                + "Os grupos exibidos a seguir são de arquivos comparados por conteúdo.")
        if self.groups_same_photo is not None:
            # Grupos de bytes não têm classes "Mesma foto": linhas vazias,
            # para a lista continuar alinhada com self.groups
            self.groups_same_photo = self.groups_same_photo + [[None] * len(g) for g in byte_groups]
        self.groups = photo_groups + byte_groups
        assert self.groups_same_photo is None or len(self.groups_same_photo) == len(self.groups)
        # A partir daqui não há mais processamento em lote: o "X" da janela
        # principal volta a fechar o app imediatamente.
        self.scan_in_progress = False
        self.show_groups()

    def _photo_groups(self, threshold):
        """
        Agrupa imagens cuja distância de phash é <= threshold (fechamento
        transitivo via Union-Find, igual à versão original, porém vetorizado),
        depois calcula o MD5 só das imagens agrupadas para rotular
        Idêntica/Semelhante. Retorna (estado, dado): ver group_images.
        """
        n = len(self.images_data)
        if n == 0:
            return "empty", ("Resultado", "Nenhuma imagem encontrada.")

        if self.close_requested:
            return "abort", None

        hashes = [h for (_, h, _) in self.images_data]
        t0 = time.time()
        self.create_progress_window()
        self.progress_window.title("Agrupando Imagens")
        self.progress_label.config(text="Comparando imagens (agrupamento por similaridade)...")
        self.progress_window.update()
        try:
            groups_idx = find_similar_groups(
                hashes, threshold,
                progress_cb=lambda c, t: self.update_progress(c, t, "comparando pares de imagens", unit="% concluído"),
                cancel_check=lambda: self.scan_cancelled
            )
        except ScanCancelled:
            self._close_progress_window()
            log.info("Agrupamento cancelado")
            if not self.close_requested:
                messagebox.showinfo("Cancelado", "Agrupamento cancelado.")
            return "abort", None
        finally:
            self._close_progress_window()
        log.info("Agrupamento de %d imagens em %.1fs: %d grupos", n, time.time() - t0, len(groups_idx))

        if self.close_requested:
            return "abort", None
        hide_target_only = self.show_target_only_var.get() == 0
        if self.reference_folder and groups_idx:
            # Modo comparação: descarta grupos só da referência (nada a limpar)
            # ANTES do MD5, para não ler do disco arquivos que serão descartados.
            before = len(groups_idx)
            groups_idx = filter_groups_for_reference(
                groups_idx, self.images_data, self.reference_keys,
                hide_target_only=hide_target_only)
            log.info("Modo referência: %d grupos descartados (só referência%s), %d restantes",
                     before - len(groups_idx),
                     " ou só alvo" if hide_target_only else "", len(groups_idx))
            if not groups_idx:
                return "empty", (
                    "Resultado",
                    "Nenhuma imagem da pasta alvo é duplicata do acervo de referência"
                    + ("" if hide_target_only else " nem de outra imagem da pasta alvo") + "."
                )
        if not groups_idx:
            return "empty", ("Resultado", "Nenhuma imagem similar encontrada.")

        # MD5 apenas para as imagens agrupadas (com pré-filtro por tamanho)
        stats = [self.file_stats.get(fp, (None, None)) for (fp, _, _) in self.images_data]
        self.create_progress_window()
        self.progress_window.title("Verificando Imagens Idênticas")
        self.progress_label.config(text="Verificando arquivos idênticos (MD5)...")
        self.progress_window.update()
        try:
            md5_by_idx, cancelled = md5_for_groups(
                self.images_data, stats, groups_idx, self.hash_cache, HASH_WORKERS,
                progress_cb=lambda c, t, f: self.update_progress(c, t, f, unit="arquivos"),
                cancel_check=lambda: self.scan_cancelled
            )
        finally:
            self._close_progress_window()

        if cancelled or self.scan_cancelled or self.close_requested:
            log.info("Verificação de MD5 cancelada")
            if not self.close_requested:
                messagebox.showinfo("Cancelado", "Verificação de imagens idênticas cancelada.")
            return "abort", None

        # Confirmação de "Semelhante" por segundo hash (pós-filtro opcional).
        # Desligada, nada daqui executa e o resultado é o de sempre.
        dhash_by_idx = None
        confirm = CONFIRM_SIMILAR and self.confirm_similar_var.get() == 1
        if confirm and groups_idx:
            t1 = time.time()
            self.create_progress_window()
            self.progress_window.title("Confirmando Semelhantes")
            self.progress_label.config(text="Calculando segundo hash (dhash) das imagens agrupadas...")
            self.progress_window.update()
            confirm_stats = None
            try:
                dhash_by_idx, cancelled = dhash_for_groups(
                    self.images_data, stats, groups_idx, md5_by_idx, self.hash_cache, HASH_WORKERS,
                    progress_cb=lambda c, t, f: self.update_progress(c, t, f, unit="imagens"),
                    cancel_check=lambda: self.scan_cancelled
                )
                if not (cancelled or self.scan_cancelled):
                    self.progress_started_at = time.time()
                    groups_idx, confirm_stats = confirm_similar_groups(
                        self.images_data, groups_idx, md5_by_idx, dhash_by_idx,
                        SIMILARITY_THRESHOLD, DHASH_THRESHOLD,
                        progress_cb=lambda c, t: self.update_progress(c, t, "confirmando grupos", unit="grupos"),
                        cancel_check=lambda: self.scan_cancelled
                    )
            except ScanCancelled:
                cancelled = True
            finally:
                self._close_progress_window()
            if cancelled or self.scan_cancelled or self.close_requested:
                log.info("Confirmação de semelhantes cancelada")
                if not self.close_requested:
                    messagebox.showinfo("Cancelado", "Confirmação de imagens semelhantes cancelada.")
                return "abort", None
            self.confirm_stats = confirm_stats
            log.info("Confirmação (dhash<=%d) em %.1fs: %d grupos -> %d (%d divididos, %d descartados), "
                     "%d imagens descartadas, %d degeneradas, %d pares rejeitados, %d pares sem dhash",
                     DHASH_THRESHOLD, time.time() - t1, confirm_stats["groups_in"],
                     confirm_stats["groups_out"], confirm_stats["groups_split"],
                     confirm_stats["groups_dropped"], confirm_stats["images_dropped"],
                     confirm_stats["images_degenerate"], confirm_stats["pairs_rejected_dhash"],
                     confirm_stats["pairs_without_dhash"])
            if self.reference_folder and groups_idx:
                # Subgrupos podem ter virado só-referência (ou só-alvo)
                before = len(groups_idx)
                groups_idx = filter_groups_for_reference(
                    groups_idx, self.images_data, self.reference_keys,
                    hide_target_only=hide_target_only)
                log.info("Modo referência (pós-confirmação): %d grupos descartados, %d restantes",
                         before - len(groups_idx), len(groups_idx))
            if confirm_stats["pairs_without_dhash"]:
                messagebox.showwarning(
                    "Confirmação incompleta",
                    f"{confirm_stats['pairs_without_dhash']} par(es) de imagens não puderam ser "
                    f"confirmados pelo segundo hash (arquivo ilegível) e valeram pelo critério "
                    f"antigo.\n\nOs caminhos estão no log: {get_log_path()}"
                )
            if not groups_idx:
                return "empty", (
                    "Resultado",
                    "Nenhuma imagem similar confirmada.\n\n"
                    f"A confirmação por segundo hash descartou {confirm_stats['groups_in']} grupo(s) "
                    "candidato(s) como coincidência. Desmarque 'Confirmar semelhantes com segundo "
                    "hash' para vê-los."
                )

        # "Mesma foto": classes dentro dos grupos (pós-filtro; nada muda desligado)
        same_photo = SAME_PHOTO_ENABLED and self.same_photo_var.get() == 1
        if same_photo and groups_idx and not self.close_requested:
            t1 = time.time()
            self.create_progress_window()
            self.progress_window.title("Identificando 'Mesma foto'")
            self.progress_label.config(text="Lendo provas (dimensões, EXIF, miniatura) das candidatas...")
            self.progress_window.update()
            sp_cache = SamePhotoCache() if (self.hash_cache is not None) else None
            cancelled = False
            class_by_idx, suspect, sp_stats = {}, set(), None
            try:
                class_by_idx, suspect, sp_stats, cancelled = same_photo_stage(
                    self.images_data, stats, groups_idx, md5_by_idx, dhash_by_idx,
                    self.hash_cache, sp_cache, HASH_WORKERS,
                    progress_cb=lambda c, t, f: self.update_progress(c, t, f, unit="imagens"),
                    cancel_check=lambda: self.scan_cancelled)
            except ScanCancelled:
                cancelled = True
            finally:
                self._close_progress_window()
                if sp_cache is not None:
                    sp_cache.close()
            if cancelled or self.scan_cancelled or self.close_requested:
                log.info("Identificação de 'Mesma foto' cancelada")
                if not self.close_requested:
                    messagebox.showinfo("Cancelado", "Identificação de 'Mesma foto' cancelada.")
                return "abort", None
            self.groups_same_photo = [[class_by_idx.get(i) for i in g] for g in groups_idx]
            self.same_photo_suspect = suspect
            self.same_photo_stats = sp_stats
            log.info("Mesma foto em %.1fs: %s", time.time() - t1, sp_stats)

        photo_groups = build_groups(self.images_data, groups_idx, md5_by_idx)
        n_ident = 0
        for g in photo_groups:
            counts = {}
            for (_, _, m) in g:
                counts[m] = counts.get(m, 0) + 1
            n_ident += sum(1 for (_, _, m) in g if counts[m] > 1)
        log.info("MD5 concluído em %.1fs: %d imagens idênticas em %d grupos",
                 time.time() - t0, n_ident, len(photo_groups))
        md5_failures = sum(1 for m in md5_by_idx.values() if m.startswith("ERR:"))
        if md5_failures:
            messagebox.showwarning(
                "Verificação incompleta",
                f"{md5_failures} arquivo(s) não puderam ser lidos na verificação de "
                f"idênticas (MD5) e serão exibidos como 'Semelhante'.\n\n"
                f"Os caminhos estão no log: {get_log_path()}"
            )
        return "ok", photo_groups

    def create_groups_progress_window(self):
        """Cria janela de progresso para inicialização de grupos"""
        self.groups_progress_window = tk.Toplevel(self.master)
        self.groups_progress_window.title("Preparando Grupos")
        self.groups_progress_window.geometry("500x150")
        self.groups_progress_window.resizable(False, False)

        # Centraliza a janela
        self._make_modal(self.groups_progress_window)

        # Frame principal
        main_frame = tk.Frame(self.groups_progress_window, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)

        # Label de status
        self.groups_progress_label = tk.Label(main_frame, text="Inicializando grupos...", font=("Arial", 10))
        self.groups_progress_label.pack(pady=(0, 10))

        # Barra de progresso
        self.groups_progress_bar = ttk.Progressbar(main_frame, length=450, mode='determinate')
        self.groups_progress_bar.pack(pady=10)

        # Label de contagem
        self.groups_progress_count_label = tk.Label(main_frame, text="0 / 0 grupos", font=("Arial", 9))
        self.groups_progress_count_label.pack(pady=(5, 0))

        # Força atualização
        self.groups_progress_window.update()

    def update_groups_progress(self, current, total):
        """Atualiza a barra de progresso de grupos"""
        if hasattr(self, 'groups_progress_window') and self.groups_progress_window.winfo_exists():
            # Atualiza o progresso
            progress_percent = (current / total * 100) if total > 0 else 0
            self.groups_progress_bar['value'] = progress_percent

            # Atualiza labels
            self.groups_progress_label.config(text=f"Preparando grupo {current} de {total}...")
            self.groups_progress_count_label.config(text=f"{current} / {total} grupos")

            # Força atualização da interface
            self.groups_progress_window.update()

    def _get_mtime(self, filepath):
        """Data de modificação em segundos: usa o valor já lido no escaneamento
           (evita reler dezenas de milhares de arquivos em disco/rede); se não
           houver, consulta o disco; se o arquivo sumiu, retorna 0."""
        stats = getattr(self, 'file_stats', {}).get(filepath)
        if stats and stats[1] is not None:
            return stats[1] / 1e9
        try:
            return os.path.getmtime(filepath)
        except OSError:
            # Data desconhecida (arquivo ilegível/sumido): trata como "mais
            # recente" para nunca virar o exemplar mantido na seleção
            # automática no lugar das cópias legíveis.
            return float("inf")

    def _is_reference(self, filepath):
        """True se o arquivo veio da listagem da pasta de referência."""
        return bool(self.reference_keys) and cache_key(filepath) in self.reference_keys

    def _is_protected(self, filepath):
        """Trava de mover/excluir: nunca tocar em arquivo da referência."""
        if not self.reference_folder:
            return False
        return is_protected_path(filepath, self.reference_keys, self.reference_prefix)

    def _dest_under_reference(self, dest_folder):
        """True se a pasta de destino de um "Mover" é (ou está dentro de) a
           pasta de referência: mover para lá alteraria o acervo protegido."""
        if not self.reference_folder:
            return False
        return folder_prefix(dest_folder).startswith(folder_prefix(self.reference_folder))

    def initialize_all_groups(self):
        """Inicializa estrutura de dados para todos os grupos antes de renderizar"""
        total_groups = len(self.groups)

        last_update = 0.0
        for idx, group in enumerate(self.groups):
            # Atualiza progresso (no máximo ~10x por segundo; com dezenas de
            # milhares de grupos, atualizar a cada grupo deixaria a UI lenta)
            now = time.time()
            if idx == 0 or idx + 1 == total_groups or (now - last_update) >= 0.1:
                self.update_groups_progress(idx + 1, total_groups)
                last_update = now

            check_vars = []
            image_info_list = []

            # Conta MD5 duplicados
            md5_count = {}
            for (_, _, md5_val) in group:
                md5_count[md5_val] = md5_count.get(md5_val, 0) + 1

            # Cria IntVar para cada imagem (com trace: pinta a linha e atualiza
            # os contadores quando a seleção muda, por qualquer caminho)
            same_photo_row = None
            if self.groups_same_photo and len(self.groups_same_photo) == len(self.groups):
                same_photo_row = self.groups_same_photo[idx]
            for pos, (filepath, p_hash, md5_val) in enumerate(group):
                var = tk.IntVar()
                check_vars.append(var)
                var.trace_add("write", lambda *a, g=idx, p=len(check_vars) - 1: self._on_var_changed(g, p))

                image_info_list.append({
                    'filepath': filepath,
                    'md5': md5_val,
                    # KIND_PHOTO = imagem que o Pillow abre; o resto veio da
                    # comparação por bytes (miniatura do Explorer, sem resolução)
                    'kind': classify_file(filepath),
                    # mesmos fluxos de vídeo e áudio que os outros do grupo (ffmpeg)
                    'same_video': cache_key(filepath) in self.byte_same_video,
                    'var': var,
                    'mtime': self._get_mtime(filepath),
                    # True só no modo comparação, para arquivos vindos da
                    # listagem da referência (set vazio no modo normal)
                    'is_reference': self._is_reference(filepath),
                    # classe "mesma foto" (None = fora de classe / feature desligada)
                    'same_photo': same_photo_row[pos] if same_photo_row else None,
                    'same_photo_suspect': bool(same_photo_row and same_photo_row[pos] is not None
                                               and same_photo_row[pos] in self.same_photo_suspect),
                })

            # Armazena dados do grupo
            self.group_check_vars[idx] = {
                'check_vars': check_vars,
                'images': image_info_list,
                'md5_count': md5_count,
                'group': group
            }

    def show_groups(self):
        """Exibe a janela listando os grupos de imagens com opções de mover ou excluir,
           dentro de um canvas com scrollbar e paginação."""
        self.current_page = 0
        self.group_check_vars = {}  # Reseta a estrutura
        self.group_page_offset = {}  # idx do grupo -> primeira imagem exibida (grupos grandes)
        # Fila de revisão: cada grupo está em "pendentes" ou em "verificados".
        # Os índices são sempre os ORIGINAIS (o rótulo "Grupo N" não muda).
        self.pending_idx = []
        self.verified_idx = []
        self.view_mode = "pending"          # vista exibida: "pending" | "verified"
        self.page_by_view = {"pending": 0, "verified": 0}
        self.group_frames = {}              # idx -> LabelFrame na página atual
        self.thumb_cache = {}               # filepath -> PhotoImage (ou None se falhou)
        self.image_dims = {}                # filepath -> (largura, altura), lido com a miniatura
        self.row_widgets = {}               # (idx, pos) -> widgets da linha (para pintar a seleção)
        self.badge_frames = {}              # (idx, pos) -> frame dos rótulos de diferença
        self._counter_pending = False
        self.group_expanded = {}            # idx -> True quando o usuário expandiu um grupo grande
        # Arquivos que não são foto: miniatura do Explorer carregada em segundo
        # plano (a placa com a extensão aparece na hora) e dados do cabeçalho
        self.has_non_photos = any(is_file_kind(classify_file(fp)) for g in self.groups for (fp, _, _) in g)
        self.media_info = {}                # MD5 (ou caminho) -> dict do mp4probe (vídeos)
        self.preview_photos = {}
        self.preview_requested = set()
        self.live_cache = {}                # cache_key(foto) -> .MOV par de Live Photo (ou None)
        self.thumb_labels = {}              # chave da miniatura -> Labels que a exibem agora
        self._cancel_thumb_poll()           # agendamento de uma janela de grupos anterior
        self._thumb_loader = shellthumb.ThumbnailLoader()
        self._thumb_poll_scheduled = False
        self._thumb_after = None

        # Cria janela de progresso
        self.create_groups_progress_window()

        # Inicializa estrutura de dados para TODOS os grupos
        self.initialize_all_groups()
        self.pending_idx = list(range(len(self.groups)))

        # Fecha janela de progresso
        if hasattr(self, 'groups_progress_window') and self.groups_progress_window.winfo_exists():
            self.groups_progress_window.destroy()

        self.groups_window = tk.Toplevel(self.master)
        self.groups_window.title(self._t("Grupos de Imagens Similares"))
        self.groups_window.bind("<Destroy>", self._cancel_thumb_poll, add="+")

        # Frame superior com informações e navegação
        top_frame = tk.Frame(self.groups_window)
        top_frame.pack(fill="x", padx=10, pady=5)

        # A informação de paginação/contagens fica na barra de status (2ª linha),
        # para a 1ª linha caber só com os botões mesmo em telas menores.

        # Botão para selecionar idênticas
        btn_select_identical = make_button(top_frame, "Selecionar Todas Idênticas", "select",
                                           command=self.select_identical_images)
        btn_select_identical.pack(side="left", padx=5)

        # Botão para selecionar semelhantes
        btn_select_similar = make_button(top_frame, "Selecionar Todas Semelhantes", "similar",
                                         command=self.select_similar_images)
        btn_select_similar.pack(side="left", padx=5)
        self.create_tooltip(btn_select_similar, SIMILAR_RULE_TOOLTIP)

        if self.groups_same_photo is not None:
            btn_same = make_button(top_frame, "Selecionar Todas Mesma Foto", "same",
                                   command=self.select_same_photo_images)
            btn_same.pack(side="left", padx=5)
            self.create_tooltip(btn_same, SAME_PHOTO_RULE_TOOLTIP)

        if self.byte_same_video:
            btn_video = make_button(top_frame, "Selecionar Todos Mesmo Vídeo", "same",
                                    command=self.select_same_video_images)
            btn_video.pack(side="left", padx=5)
            self.create_tooltip(btn_video, SAME_VIDEO_TOOLTIP)

        # Botões de ação global
        btn_move_all = make_button(top_frame, "Mover Todas Selecionadas", "move",
                                   command=self.move_all_selected)
        btn_move_all.pack(side="left", padx=5)

        btn_delete_all = make_button(top_frame, "Excluir Todas Selecionadas", "danger",
                                     command=self.delete_all_selected)
        btn_delete_all.pack(side="left", padx=5)

        # Alterna entre a fila de pendentes e a lista de grupos já verificados
        self.view_toggle_btn = make_button(top_frame, "", "neutral", command=self.toggle_view)
        self.view_toggle_btn.pack(side="left", padx=(20, 5))

        # Menu "Mais": desfazer, relatórios, log
        more = tk.Menubutton(top_frame, text="Mais ▾", relief="flat", bg="#E0E0E0",
                             activebackground="#BDBDBD", padx=10, pady=4, cursor="hand2")
        more_menu = tk.Menu(more, tearoff=0)
        more_menu.add_command(label="Desfazer último lote (mover)", command=self.undo_last_action)
        more_menu.add_separator()
        more_menu.add_command(label="Abrir pasta de relatórios (CSV da sessão)",
                              command=lambda: self.open_folder(get_reports_dir()))
        more_menu.add_command(label="Abrir pasta do log e do cache",
                              command=lambda: self.open_folder(get_app_data_dir()))
        more["menu"] = more_menu
        more.pack(side="left", padx=5)
        self.create_tooltip(self.view_toggle_btn,
                            "Ao usar 'Selecionar Idênticas/Semelhantes' ou 'Marcar verificado' de um\n"
                            "grupo, ele sai da fila de pendentes e vai para 'Grupos verificados'.\n"
                            "As seleções feitas continuam valendo para 'Mover/Excluir Todas Selecionadas'.\n"
                            "A lista de verificados vale só nesta sessão.")
        self._update_view_toggle()

        # Botões de navegação
        nav_frame = tk.Frame(top_frame)
        nav_frame.pack(side="right")

        self.prev_btn = make_button(nav_frame, "← Anterior (F1)", "light", command=self.prev_page)
        self.prev_btn.pack(side="left", padx=5)

        self.next_btn = make_button(nav_frame, "Próximo (F2) →", "light", command=self.next_page)
        self.next_btn.pack(side="left", padx=5)

        # Atalhos de teclado da paginação (valem com a janela de grupos em foco)
        self.groups_window.bind("<F1>", lambda e: self.prev_page())
        self.groups_window.bind("<F2>", lambda e: self.next_page())

        # Barra de status: progresso da revisão e o que está selecionado
        status_frame = tk.Frame(self.groups_window)
        status_frame.pack(fill="x", padx=15, pady=(0, 4))
        self.review_progress = ttk.Progressbar(status_frame, length=220, mode="determinate")
        self.review_progress.pack(side="left")
        self.review_label = tk.Label(status_frame, text="", font=FONT_BOLD)
        self.review_label.pack(side="left", padx=(8, 20))
        self.selection_label = tk.Label(status_frame, text="", fg=PALETTE["muted"])
        self.selection_label.pack(side="left")
        self.page_info_label = tk.Label(status_frame, text="", fg=PALETTE["text"])
        self.page_info_label.pack(side="left", padx=(20, 0))
        self._update_counters()

        # --- Cria um Frame para conter o Canvas e a Scrollbar ---
        scroll_container = tk.Frame(self.groups_window)
        scroll_container.pack(fill="both", expand=True)

        # --- Cria o Canvas ---
        self.canvas = tk.Canvas(scroll_container, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        # Rolagem: passo de 40 px (roda do mouse e setas) e teclas de página.
        # Espaço/Enter ficam livres (alternam a caixa de seleção com foco).
        self.canvas.configure(yscrollincrement=40)
        gw = self.groups_window
        gw.bind("<Next>", lambda e: self.canvas.yview_scroll(1, "pages"))
        gw.bind("<Prior>", lambda e: self.canvas.yview_scroll(-1, "pages"))
        gw.bind("<Home>", lambda e: self.canvas.yview_moveto(0))
        gw.bind("<End>", lambda e: self.canvas.yview_moveto(1))
        gw.bind("<Down>", lambda e: self.canvas.yview_scroll(3, "units"))
        gw.bind("<Up>", lambda e: self.canvas.yview_scroll(-3, "units"))

        # --- Cria a Scrollbar e vincula ao Canvas ---
        scrollbar = tk.Scrollbar(scroll_container, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        # --- Cria o Frame que conterá todo o conteúdo (grupos, imagens, etc.) ---
        self.content_frame = tk.Frame(self.canvas)
        # Insere o content_frame dentro do canvas como uma "janela"
        self.content_window = self.canvas.create_window((0, 0), window=self.content_frame, anchor="nw")

        # Função para ajustar a região de rolagem sempre que o content_frame mudar de tamanho
        def on_configure(event):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self._on_content_configure = on_configure
        self.content_frame.bind("<Configure>", on_configure)

        # Adiciona suporte ao scroll do mouse
        def on_mouse_wheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        # Bind para Windows/MacOS
        self.canvas.bind_all("<MouseWheel>", on_mouse_wheel)
        # Bind para Linux
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        # Renderiza a primeira página
        self.render_page()

    def _visible_groups(self):
        """Índices dos grupos da vista atual (pendentes ou verificados)."""
        return self.pending_idx if self.view_mode == "pending" else self.verified_idx

    def _t(self, text, files=None):
        """Texto da tela com o vocabulário certo: 'imagens' só com fotos (sem
           tocar em nada), 'arquivos' quando há outros tipos. files=True/False
           força a escolha (ex.: texto de UM grupo)."""
        if files is None:
            files = getattr(self, 'has_non_photos', False)
        return file_wording(text) if files else text

    def _live_companion(self, filepath):
        """.MOV par de Live Photo desta foto (ou None), conferido uma vez por tela."""
        key = cache_key(filepath)
        cache = self.live_cache
        if key not in cache:
            cache[key] = live_photo_companion(filepath)
        return cache[key]

    def _media_info(self, filepath, kind, share_key=None):
        """Dados do cabeçalho de um vídeo MP4/MOV (mp4probe), lidos uma vez por
           CONTEÚDO: cópias idênticas (mesmo MD5) dividem a leitura. Outros
           contêineres (avi, mkv...) nem são abertos: o leitor não os entende."""
        if kind != KIND_VIDEO or os.path.splitext(filepath)[1].lower() not in mp4probe.MP4_EXTENSIONS:
            return None
        key = share_key or filepath
        info = self.media_info.get(key)
        if info is None:
            info = mp4probe.probe(filepath)
            self.media_info[key] = info
        return info

    def _load_file_thumbnail(self, filepath, kind, share_key, label=None):
        """
        Miniatura de um arquivo que NÃO é foto. Devolve na hora a placa com a
        extensão e pede a miniatura do Explorer à thread de miniaturas; quando
        ela chega, os Labels registrados em thumb_labels são atualizados.
        share_key: arquivos idênticos (mesmo MD5) dividem uma miniatura só.
        """
        key = ("md5", share_key) if share_key else ("path", filepath)
        if label is not None:
            # só Labels vivos: um grupo redesenhado deixa os antigos para trás
            alive = [l for l in self.thumb_labels.get(key, []) if self._widget_alive(l)]
            self.thumb_labels[key] = alive + [label]
        cache = self.thumb_cache
        if key in cache:
            return cache[key]
        plate = ImageTk.PhotoImage(extension_plate(filepath, kind, THUMB_SIZE, int(THUMB_SIZE * 0.62)))
        if len(cache) >= THUMB_CACHE_SIZE:
            cache.pop(next(iter(cache)))
        cache[key] = plate
        if shellthumb.available():
            self._thumb_loader.request(key, filepath, THUMB_SIZE)
            self._schedule_thumb_poll()
        return plate

    @staticmethod
    def _widget_alive(widget):
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _schedule_thumb_poll(self):
        if self._thumb_poll_scheduled:
            return
        win = getattr(self, 'groups_window', None)
        if win is None:
            return   # a janela ainda está sendo montada: render_page agenda depois
        try:
            self._thumb_poll_scheduled = True
            self._thumb_after = (win, win.after(60, self._poll_thumbnails))
        except tk.TclError:
            self._thumb_poll_scheduled = False

    def _cancel_thumb_poll(self, event=None):
        """A janela de grupos está sendo destruída: cancela o agendamento
           pendente (senão o Tcl reclama de um comando que não existe mais)."""
        if event is not None and event.widget is not getattr(self, 'groups_window', None):
            return   # <Destroy> de um filho: ignora
        pending = getattr(self, '_thumb_after', None)
        self._thumb_after = None
        self._thumb_poll_scheduled = False
        if pending:
            try:
                pending[0].after_cancel(pending[1])
            except tk.TclError:
                pass

    def _poll_thumbnails(self):
        """Thread principal: transforma as miniaturas prontas em PhotoImage e
           troca a placa nos Labels que ainda existem."""
        self._thumb_poll_scheduled = False
        self._thumb_after = None
        for key, image, err in self._thumb_loader.drain():
            if image is None:
                log.info("Sem miniatura do Explorer para %s: %s", key[1], err)
                continue
            try:
                photo = ImageTk.PhotoImage(image)
            except Exception as e:
                log.warning("Miniatura inválida para %s: %s", key[1], e)
                continue
            if key[0] == "preview":
                self.preview_photos[key] = photo   # fora do cache de miniaturas (é grande)
            else:
                self.thumb_cache[key] = photo
            for lbl in self.thumb_labels.get(key, []):
                try:
                    if lbl.winfo_exists():
                        lbl.config(image=photo)
                        lbl.image = photo
                except tk.TclError:
                    pass
        if self._thumb_loader.pending:
            self._schedule_thumb_poll()

    def _load_thumbnail(self, filepath):
        """
        Miniatura THUMB_SIZE x THUMB_SIZE como PhotoImage, com cache. JPEGs são decodificados
        já em escala reduzida (draft: 1/2, 1/4 ou 1/8 via DCT), o que é
        várias vezes mais rápido do que decodificar os 20 MP inteiros para
        depois encolher. Retorna None se a imagem não puder ser lida.
        """
        cache = self.thumb_cache
        if filepath in cache:
            return cache[filepath]
        photo = None
        try:
            with Image.open(filepath) as img:
                self.image_dims[filepath] = img.size   # antes do draft: tamanho real
                if img.format == "JPEG":
                    img.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
                img.thumbnail((THUMB_SIZE, THUMB_SIZE))
                photo = ImageTk.PhotoImage(img)
        except Exception as e:
            log.warning("Erro ao carregar miniatura %s: %s", filepath, e)
        if len(cache) >= THUMB_CACHE_SIZE:
            cache.pop(next(iter(cache)))   # descarta a mais antiga
        cache[filepath] = photo
        return photo

    def _update_page_info(self):
        """Atualiza o rótulo de paginação e os botões Anterior/Próximo.
           Retorna (total_de_grupos_visíveis, total_de_páginas)."""
        visible = self._visible_groups()
        total = len(visible)
        total_pages = max(1, (total + self.groups_per_page - 1) // self.groups_per_page)
        if self.current_page > total_pages - 1:
            self.current_page = total_pages - 1
        self.page_by_view[self.view_mode] = self.current_page
        end_idx = min((self.current_page + 1) * self.groups_per_page, total)
        vista = "Verificados" if self.view_mode == "verified" else "Pendentes"
        info = (f"{vista}: página {self.current_page + 1} de {total_pages}   |   "
                f"{len(self.pending_idx)} pendentes, {len(self.verified_idx)} verificados, "
                f"{len(self.groups)} no total")
        cs = self.confirm_stats
        if cs:
            info += (f"   |   2º hash descartou {cs['images_dropped']} img em "
                     f"{cs['groups_dropped']} grupo(s)")
        sp = self.same_photo_stats
        if sp:
            info += (f"   |   Mesma foto: {sp['classes']} classe(s), {sp['images_in_classes']} img"
                     + (f", {sp['classes_suspect']} suspeita(s)" if sp['classes_suspect'] else ""))
        sv = getattr(self, 'same_video_stats', None)
        if sv and sv.get("merged_groups"):
            info += f"   |   Mesmo vídeo: {sv['merged_groups']} grupo(s), {sv['files_in_merged']} arq"
        self.page_info_label.config(text=info)
        self.prev_btn.config(state="normal" if self.current_page > 0 else "disabled")
        self.next_btn.config(state="normal" if end_idx < total else "disabled")
        return total, total_pages

    def render_page(self, keep_scroll_px=None):
        """Renderiza os grupos da página atual da vista atual.
           keep_scroll_px: posição vertical (em pixels do canvas) a manter após
           renderizar; None rola para o topo."""
        # A página nova é montada num frame FORA da tela e trocada de uma vez
        # no canvas; o frame antigo é destruído já desmapeado. Evita o Tk
        # relayoutar a página a cada um dos ~300 widgets criados/destruídos
        # (era o que deixava a troca de página lenta).
        old_frame = self.content_frame
        self.content_frame = tk.Frame(self.canvas)
        self.group_frames = {}
        self.row_widgets = {}
        self.badge_frames = {}
        self.thumb_labels = {}   # os Labels da página antiga vão ser destruídos
        # Pedidos de miniatura que ainda não começaram eram da página antiga:
        # descarta (e tira a placa do cache, senão nunca seriam pedidos de novo)
        for key in self._thumb_loader.discard_pending():
            self.thumb_cache.pop(key, None)

        total, _ = self._update_page_info()
        visible = self._visible_groups()
        start_idx = self.current_page * self.groups_per_page
        end_idx = min(start_idx + self.groups_per_page, total)

        if total == 0:
            msg = ("Todos os grupos foram verificados. Use 'Voltar aos pendentes' para revê-los."
                   if self.view_mode == "pending" else "Nenhum grupo verificado ainda.")
            tk.Label(self.content_frame, text=msg, fg="#555555", font=("Arial", 11),
                     padx=20, pady=30).pack(anchor="w")

        # Renderiza em lotes: no Windows cada widget Tk é uma janela nativa e
        # mapear os ~360 widgets de 20 grupos de uma vez trava a tela por ~1 s.
        # Os primeiros grupos aparecem já; os demais entram abaixo, em lotes,
        # enquanto o usuário revisa o topo. Um token cancela lotes pendentes se
        # a página mudar no meio.
        self._render_token = getattr(self, '_render_token', 0) + 1
        token = self._render_token
        page_groups = list(visible[start_idx:end_idx])
        first = page_groups[:RENDER_FIRST_CHUNK]
        rest = page_groups[RENDER_FIRST_CHUNK:]
        for idx in first:
            self._render_group(idx)

        self.content_frame.bind("<Configure>", self._on_content_configure)
        self.canvas.itemconfigure(self.content_window, window=self.content_frame)
        try:
            old_frame.destroy()
        except tk.TclError:
            pass

        def apply_scroll():
            if keep_scroll_px is None:
                self.canvas.yview_moveto(0)
            else:
                self._scroll_to_px(keep_scroll_px)

        apply_scroll()

        def render_chunk():
            if token != self._render_token or not self.content_frame.winfo_exists():
                return
            for idx in rest[:RENDER_CHUNK]:
                self._render_group(idx)
            del rest[:RENDER_CHUNK]
            if rest:
                self.groups_window.after(1, render_chunk)
            elif keep_scroll_px is not None:
                apply_scroll()   # altura final conhecida: reaplica a posição pedida

        if rest:
            self.groups_window.after(1, render_chunk)

    def _scroll_to_px(self, px):
        """Rola o canvas para a posição vertical px (após recalcular a região)."""
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all")
        self.canvas.configure(scrollregion=bbox)
        height = (bbox[3] - bbox[1]) if bbox else 0
        self.canvas.yview_moveto(px / height if height > 0 else 0)

    def _render_group(self, idx):
        """Cria o LabelFrame de UM grupo no fim do content_frame e o devolve.
           Os botões de ação ficam no TOPO do grupo: assim ficam sempre na mesma
           posição em relação ao início do grupo, não importa quantas imagens
           ele tenha (o cursor não precisa se mover entre um grupo e o próximo)."""
        verified_view = self.view_mode == "verified"
        group_data = self.group_check_vars[idx]
        group = group_data['group']
        md5_count = group_data['md5_count']
        images = group_data['images']

        # Grupo de fotos: título de sempre. Grupo vindo da comparação por
        # bytes: diz o que é (todos os membros têm o mesmo conteúdo).
        kinds = {im.get('kind', KIND_PHOTO) for im in images}
        group_files = kinds != {KIND_PHOTO}
        kind_tag = ""
        if group_files:
            kind_tag = (" (vídeos)" if kinds == {KIND_VIDEO}
                        else " (fotos)" if kinds == {KIND_PHOTO_BYTES} else " (arquivos)")
        title = f"Grupo {idx + 1}" + kind_tag + (" ✓ verificado" if verified_view else "")
        frame = tk.LabelFrame(self.content_frame, text=title, padx=10, pady=10)
        frame.pack(padx=10, pady=10, fill="x", expand=True)
        self.group_frames[idx] = frame

        # Botões do grupo (no topo)
        btn_frame = tk.Frame(frame)
        btn_frame.pack(fill="x", pady=(0, 5))

        # Seleção automática só deste grupo: o botão aparece apenas se a
        # regra encontrar algo para marcar aqui (grupo só de idênticas
        # não ganha "Semelhantes", e vice-versa). Na fila de pendentes,
        # selecionar também marca o grupo como verificado (ele sai da fila
        # e o seguinte sobe para o mesmo lugar).
        select_cmd = self.select_group if verified_view else self.select_and_verify
        if plan_identical_selection(images):
            make_button(btn_frame, "Selecionar Idênticas", "select",
                        command=lambda g=idx, c=select_cmd: c(g, "identical")).pack(side="left", padx=5)
        if plan_similar_selection(images, md5_count):
            b_sim = make_button(btn_frame, "Selecionar Semelhantes", "similar",
                                command=lambda g=idx, c=select_cmd: c(g, "similar"))
            b_sim.pack(side="left", padx=5)
            self.create_tooltip(b_sim, SIMILAR_RULE_TOOLTIP)
        if plan_same_photo_selection(images):
            b_same = make_button(btn_frame, "Selecionar Mesma foto", "same",
                                 command=lambda g=idx, c=select_cmd: c(g, "same_photo"))
            b_same.pack(side="left", padx=5)
            self.create_tooltip(b_same, SAME_PHOTO_RULE_TOOLTIP)
        if plan_same_video_selection(images):
            b_video = make_button(btn_frame, "Selecionar Mesmo vídeo", "same",
                                  command=lambda g=idx, c=select_cmd: c(g, "same_video"))
            b_video.pack(side="left", padx=5)
            self.create_tooltip(b_video, SAME_VIDEO_TOOLTIP)
        if verified_view:
            make_button(btn_frame, "Voltar para pendentes", "light",
                        command=lambda g=idx: self.unverify_group(g)).pack(side="left", padx=5)
        else:
            make_button(btn_frame, "Marcar verificado ✓", "light",
                        command=lambda g=idx: self.verify_group(g)).pack(side="left", padx=5)

        btn_move = make_button(btn_frame, "Mover Selecionadas", "light",
                               command=lambda grp=group, vars=group_data['check_vars']: self.move_images(grp, vars))
        btn_move.pack(side="left", padx=5)

        btn_delete = make_button(btn_frame, "Excluir Selecionadas", "light",
                                 command=lambda grp=group, vars=group_data['check_vars']: self.delete_images(grp, vars))
        btn_delete.pack(side="left", padx=5)

        # Grupos grandes começam recolhidos (só as primeiras imagens)
        collapsible = len(images) > COLLAPSE_THRESHOLD
        collapsed = collapsible and not self.group_expanded.get(idx, False)
        if collapsible and not collapsed:
            make_button(btn_frame, "Recolher", "light",
                        command=lambda g=idx: self.set_group_expanded(g, False)).pack(side="left", padx=5)

        # Grupos gigantes: exibe MAX_IMAGES_PER_GROUP_DISPLAY por vez, com
        # navegação dentro do grupo (toda imagem marcada continua alcançável).
        # As ações continuam valendo para o grupo inteiro.
        offset = 0
        if collapsed:
            pass
        elif len(images) > MAX_IMAGES_PER_GROUP_DISPLAY:
            offset = self.group_page_offset.get(idx, 0)
            offset = max(0, min(offset, len(images) - 1))
            offset -= offset % MAX_IMAGES_PER_GROUP_DISPLAY
            self.group_page_offset[idx] = offset
            self._render_group_nav(frame, idx, images, offset)

        # Exibe cada imagem do grupo usando os IntVar já criados
        rows = images[:COLLAPSE_SHOW] if collapsed else images[offset:offset + MAX_IMAGES_PER_GROUP_DISPLAY]
        # Miniaturas primeiro (preenche image_dims), badges depois: assim as
        # linhas sem rótulo não criam widgets extras.
        for img_info in rows:
            if img_info.get('kind', KIND_PHOTO) == KIND_PHOTO:
                self._load_thumbnail(img_info['filepath'])
        badges = self._badges_for_group(images)
        for pos, img_info in enumerate(rows, start=offset):
            filepath = img_info['filepath']
            md5_val = img_info['md5']
            var = img_info['var']
            is_ref = img_info.get('is_reference', False)
            kind = img_info.get('kind', KIND_PHOTO)

            # Monta um frame interno para cada imagem
            item_frame = tk.Frame(frame)
            item_frame.pack(side="top", fill="x", pady=5)

            # Clicar em qualquer ponto da linha (fora da miniatura) alterna a
            # seleção; imagens da referência continuam bloqueadas.
            def toggle_row(event, v=var, ref=is_ref):
                if not ref:
                    v.set(0 if v.get() else 1)
            row_widgets = [item_frame]

            # Miniatura (clique abre a pré-visualização grande)
            if kind == KIND_PHOTO:
                photo = self._load_thumbnail(filepath)
                if photo is not None:
                    lbl_img = tk.Label(item_frame, image=photo, cursor="hand2")
                    lbl_img.image = photo
                else:
                    lbl_img = tk.Label(item_frame, text="(Erro ao carregar)", cursor="hand2")
            else:
                # Não é foto: placa com a extensão agora; a miniatura do
                # Explorer chega depois, sem travar a tela
                lbl_img = tk.Label(item_frame, cursor="hand2")
                photo = self._load_file_thumbnail(filepath, kind, md5_val, label=lbl_img)
                lbl_img.config(image=photo)
                lbl_img.image = photo
            lbl_img.pack(side="left", padx=5)
            lbl_img.bind("<Button-1>",
                         lambda e, g=idx, p=pos: self.open_preview(g, p))

            # Área de texto e checkbox
            text_frame = tk.Frame(item_frame)
            text_frame.pack(side="left", fill="both", expand=True)
            row_widgets.append(text_frame)

            # Imagem da referência: rótulo destacado e checkbox desabilitado
            # (impossível selecionar, mesmo clicando)
            if is_ref:
                lbl_ref = tk.Label(text_frame, text="REFERÊNCIA (protegida)", fg="white",
                                   bg=PALETTE["primary"], font=FONT_BOLD, padx=4)
                lbl_ref.pack(anchor="w")
                row_widgets.append(lbl_ref)

            # Checkbutton usando o IntVar já existente
            row_badges = badges.get(pos, [])
            # Sem badges: o checkbox vai direto no text_frame (menos widgets;
            # no Windows cada widget é uma janela nativa e custa caro)
            head = tk.Frame(text_frame) if row_badges else text_frame
            if row_badges:
                head.pack(anchor="w", fill="x")
            chk = tk.Checkbutton(head, text="Selecionar", variable=var,
                                 state="disabled" if is_ref else "normal")
            chk.pack(side="left" if row_badges else "top", anchor="w")
            badge_widgets = []
            if row_badges:
                badge_frame = tk.Frame(head)
                badge_frame.pack(side="left", padx=(10, 0))
                self.badge_frames[(idx, pos)] = badge_frame
                badge_widgets = [head, badge_frame]
                for text in row_badges:
                    bfg, bbg = BADGE_STYLES[text]
                    tk.Label(badge_frame, text=text, fg=bfg, bg=bbg, font=("Segoe UI", 8),
                             padx=6, pady=1).pack(side="left", padx=(0, 4))

            # Nome do arquivo em destaque; a pasta curta vai na primeira linha do
            # bloco de texto; caminho completo no tooltip e no menu de contexto
            tag, rel_dir, name = shorten_path(filepath, self._path_roots())
            lbl_name = tk.Label(text_frame, text=name, font=FONT_BOLD, anchor="w",
                                cursor="" if is_ref else "hand2")
            lbl_name.pack(anchor="w")
            where = (f"[{tag}] " if tag else "") + (rel_dir or "(raiz)")
            self.create_tooltip(lbl_name, filepath)

            # Status: Idêntica (MD5 repetido) > Mesma foto (classe) > Semelhante
            status = image_status(img_info, md5_count)
            if status == "Mesma foto":
                tag_text = "MESMA FOTO" + ("  ·  ampliada?" if img_info.get('same_photo_suspect') else "")
                tfg, tbg = BADGE_STYLES["mesma foto"]
                lbl_same = tk.Label(text_frame, text=tag_text, fg=tfg, bg=tbg, font=FONT_BOLD, padx=4)
                lbl_same.pack(anchor="w")
                lbl_same.bind("<Button-1>", toggle_row)
            if status == "Mesmo vídeo":
                vfg, vbg = BADGE_STYLES["mesmo vídeo"]
                lbl_video = tk.Label(text_frame, text="MESMO VÍDEO  ·  mesmo conteúdo, arquivo diferente",
                                     fg=vfg, bg=vbg, font=FONT_BOLD, padx=4)
                lbl_video.pack(anchor="w")
                lbl_video.bind("<Button-1>", toggle_row)
            if kind in (KIND_PHOTO, KIND_PHOTO_BYTES) and self._live_companion(filepath):
                # Live Photo do iPhone: avisa que há um vídeo .MOV par ao lado
                lfg, lbg = BADGE_STYLES["live photo"]
                lbl_live = tk.Label(text_frame, text="LIVE PHOTO  ·  tem o vídeo .MOV ao lado",
                                    fg=lfg, bg=lbg, font=FONT_BOLD, padx=4)
                lbl_live.pack(anchor="w")
                lbl_live.bind("<Button-1>", toggle_row)

            # Metadados do arquivo (protegido: o arquivo pode ter sido
            # movido/excluído por uma ação anterior nesta mesma tela)
            if kind == KIND_PHOTO:
                status_line = (f"Status: {status}   |   "
                               f"Resolução: {format_resolution(self.image_dims.get(filepath))}")
            else:
                # Sem "Resolução": no lugar, o tipo e (vídeo) dimensões e duração
                status_line = (f"Status: {'Cópia exata' if status == 'Idêntica' else status}   |   "
                               f"{media_summary(kind, filepath, self._media_info(filepath, kind, md5_val))}")
            try:
                st = os.stat(filepath)
                ctime_str = datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
                mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                info_text = (
                    f"{where}\n"
                    f"{status_line}\n"
                    f"Tamanho: {format_bytes(st.st_size)} ({st.st_size} bytes)\n"
                    f"Criado em: {ctime_str}   |   Modificado em: {mtime_str}"
                )
            except OSError:
                info_text = (
                    f"{where}\n"
                    f"{status_line}\n"
                    f"Arquivo não encontrado (movido ou excluído)"
                )

            lbl_info = tk.Label(text_frame, text=info_text, justify="left", anchor="w",
                                cursor="" if is_ref else "hand2")
            lbl_info.pack(anchor="w")
            row_widgets.extend([lbl_name, lbl_info])
            for w in row_widgets:
                w.bind("<Button-1>", toggle_row)
            # Menu de contexto (botão direito) na linha inteira e na miniatura
            for w in row_widgets + [lbl_img, chk]:
                w.bind("<Button-3>",
                       lambda e, fp=filepath, g=idx, p=pos: self._show_row_menu(e, fp, g, p))

            # Cor de fundo da linha conforme a seleção (pintada pelo trace do var)
            self.row_widgets[(idx, pos)] = [item_frame, text_frame, chk, lbl_img,
                                            lbl_name, lbl_info] + badge_widgets
            self._paint_row(idx, pos)

        if collapsed:
            hidden = len(images) - COLLAPSE_SHOW
            selected = sum(1 for im in images if im['var'].get() == 1)
            strip = tk.Frame(frame, bg="#FFF8E1", padx=8, pady=6)
            strip.pack(fill="x", pady=(4, 0))
            tk.Label(strip, bg="#FFF8E1", fg="#6D4C00", anchor="w", justify="left",
                     text=self._t(f"… e mais {hidden} imagem(ns) neste grupo ({selected} selecionada(s) no total). "
                                  "As ações de selecionar, mover e excluir valem para o grupo inteiro.",
                                  files=any(is_file_kind(k) for k in kinds))
                     ).pack(side="left", fill="x", expand=True)
            make_button(strip, f"Expandir ({len(images)})", "light",
                        command=lambda g=idx: self.set_group_expanded(g, True)).pack(side="right")
        elif len(images) > MAX_IMAGES_PER_GROUP_DISPLAY:
            # Repete a navegação no fim de grupos grandes (evita rolar até o topo)
            self._render_group_nav(frame, idx, images, offset)
        return frame

    def set_group_expanded(self, idx, expanded):
        """Expande/recolhe um grupo grande, redesenhando só ele (no lugar)."""
        self.group_expanded[idx] = expanded
        self._rerender_group(idx)

    def _forget_group_widgets(self, idx):
        """Descarta as referências de widgets (linhas, badges) de um grupo que
           vai ser destruído ou redesenhado."""
        for d in (self.row_widgets, self.badge_frames):
            for key in [k for k in d if k[0] == idx]:
                del d[key]

    def _rerender_group(self, idx):
        """Redesenha um único grupo na mesma posição da página."""
        old = self.group_frames.get(idx)
        self._forget_group_widgets(idx)
        try:
            top = self.canvas.canvasy(0)
            new = self._render_group(idx)
            if old is not None and old.winfo_exists():
                new.pack_configure(after=old)
                old.destroy()
            self._scroll_to_px(max(0, top))
        except tk.TclError:
            self.render_page()

    def _badges_for_group(self, images):
        """Rótulos de diferença por posição, com os MESMOS metadados que a
           regra de seleção usa (assim rótulo e decisão coincidem, inclusive
           para linhas ocultas de grupos recolhidos)."""
        self._ensure_metrics(images)
        metas = []
        for info in images:
            mtime = info.get('mtime')
            metas.append({
                'pixels': info.get('pixels'),
                'size': info.get('size'),
                'mtime': None if mtime in (None, float("inf")) else mtime,
            })
        return plan_badges(metas)

    def _path_roots(self):
        """Raízes para encurtar caminhos na tela: alvo e (se houver) referência."""
        roots = [("ALVO", self.selected_folder)]
        if self.reference_folder:
            roots.append(("REF", self.reference_folder))
        return roots

    def _paint_row(self, idx, pos):
        """Fundo da linha: verde-claro quando selecionada; referência em tom fixo."""
        widgets = self.row_widgets.get((idx, pos))
        if not widgets:
            return
        info = self.group_check_vars[idx]['images'][pos]
        if info.get('is_reference'):
            color = PALETTE["reference_row"]
        else:
            color = PALETTE["selected_row"] if info['var'].get() == 1 else PALETTE["bg"]
        for w in widgets:
            try:
                if not w.winfo_exists():
                    return
                w.configure(bg=color)
                if isinstance(w, tk.Checkbutton):
                    w.configure(activebackground=color, selectcolor="white")
            except tk.TclError:
                return

    def _on_var_changed(self, idx, pos):
        """Trace dos IntVar: chamado em qualquer mudança de seleção."""
        self._paint_row(idx, pos)
        self._schedule_counter_update()

    def _schedule_counter_update(self):
        """Atualização coalescida dos contadores: muitas mudanças seguidas
           ("Selecionar Todas") viram uma única atualização no próximo idle."""
        if getattr(self, '_counter_pending', False):
            return
        win = getattr(self, 'groups_window', None)
        if win is None:
            return
        try:
            self._counter_pending = True
            win.after_idle(self._update_counters)
        except tk.TclError:
            self._counter_pending = False

    def _update_counters(self):
        """Barra de status: 'Verificados x / N' e 'Selecionadas: n (bytes)'."""
        self._counter_pending = False
        if not hasattr(self, 'review_label'):
            return
        total = len(self.groups)
        verified = len(self.verified_idx)
        n_sel = 0
        bytes_sel = 0
        for group_data in self.group_check_vars.values():
            for info in group_data['images']:
                if info['var'].get() == 1:
                    n_sel += 1
                    size = self.file_stats.get(info['filepath'], (None, None))[0]
                    bytes_sel += size or 0
        try:
            self.review_progress.configure(maximum=max(1, total), value=verified)
            self.review_label.config(text=f"Verificados {verified} / {total}")
            n_txt = f"{n_sel:,}".replace(",", ".")   # separador de milhar pt-BR
            self.selection_label.config(
                text=self._t(f"Selecionadas: {n_txt} imagem(ns), {format_bytes(bytes_sel)}"))
        except tk.TclError:
            pass

    def _show_row_menu(self, event, filepath, group_idx, pos):
        menu = tk.Menu(self.groups_window, tearoff=0)
        menu.add_command(label="Pré-visualizar", command=lambda: self.open_preview(group_idx, pos))
        is_file = is_file_kind(classify_file(filepath))
        menu.add_command(label=self._t("Abrir imagem", files=is_file),
                         command=lambda: self.open_image(filepath))
        menu.add_command(label="Abrir pasta no Explorer", command=lambda: self.open_in_explorer(filepath))
        menu.add_separator()
        menu.add_command(label="Copiar caminho", command=lambda: self.copy_path(filepath))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_image(self, filepath):
        """Abre a imagem no visualizador padrão do Windows."""
        try:
            os.startfile(filepath)
        except OSError as e:
            messagebox.showerror(self._t("Abrir imagem", files=is_file_kind(classify_file(filepath))),
                                 f"Não foi possível abrir:\n{filepath}\n\n{e}")

    def open_in_explorer(self, filepath):
        """Abre o Explorer com o arquivo selecionado."""
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(filepath)])
        except OSError as e:
            messagebox.showerror("Abrir pasta", f"Não foi possível abrir a pasta de:\n{filepath}\n\n{e}")

    def copy_path(self, filepath):
        """Copia o caminho completo para a área de transferência."""
        try:
            self.master.clipboard_clear()
            self.master.clipboard_append(filepath)
        except tk.TclError:
            pass

    def _scroll_anchor_for(self, idx):
        """Posição vertical (px) a manter ao remover o grupo idx da vista:
           o menor entre o topo visível e o topo do grupo."""
        frame = self.group_frames.get(idx)
        try:
            if frame is not None and frame.winfo_exists():
                return max(0, min(self.canvas.canvasy(0), frame.winfo_y()))
        except tk.TclError:
            pass
        return None

    def verify_group(self, idx):
        """Tira o grupo da fila de pendentes e o coloca em 'verificados'."""
        if idx not in self.pending_idx:
            return
        self.pending_idx.remove(idx)
        self.verified_idx.append(idx)
        self._update_view_toggle()
        self._schedule_counter_update()
        if self.view_mode == "pending":
            self._remove_group_from_page(idx)
        else:
            self.render_page()

    def unverify_group(self, idx):
        """Devolve o grupo à fila de pendentes, na posição original."""
        if idx not in self.verified_idx:
            return
        self.verified_idx.remove(idx)
        bisect.insort(self.pending_idx, idx)
        self._update_view_toggle()
        self._schedule_counter_update()
        if self.view_mode == "verified":
            self._remove_group_from_page(idx)
        else:
            self.render_page()

    def _remove_group_from_page(self, idx):
        """
        Atualização incremental: destrói só o frame do grupo que saiu da vista
        e puxa o próximo grupo da fila para o FIM da página. Os demais grupos
        não são redesenhados (rápido) e o grupo seguinte ocupa exatamente o
        lugar do removido (o cursor fica sobre o mesmo botão).
        """
        visible = self._visible_groups()
        start = self.current_page * self.groups_per_page
        end = start + self.groups_per_page
        frame = self.group_frames.pop(idx, None)
        self._forget_group_widgets(idx)
        if start >= len(visible) or frame is None:
            # Página ficou vazia (volta uma página) ou frame desconhecido
            self.render_page()
            return
        try:
            top = self.canvas.canvasy(0)
            frame.destroy()
        except tk.TclError:
            self.render_page()
            return
        if len(visible) >= end:
            self._render_group(visible[end - 1])
        self._update_page_info()
        self._scroll_to_px(max(0, top))

    def select_and_verify(self, idx, kind):
        """Botão de seleção de um grupo pendente: seleciona e verifica."""
        self.select_group(idx, kind)
        self.verify_group(idx)

    def toggle_view(self):
        """Alterna entre a fila de pendentes e a lista de verificados."""
        self.page_by_view[self.view_mode] = self.current_page
        self.view_mode = "verified" if self.view_mode == "pending" else "pending"
        self.current_page = self.page_by_view[self.view_mode]
        self._update_view_toggle()
        self.render_page()

    def _update_view_toggle(self):
        btn = getattr(self, 'view_toggle_btn', None)
        if btn is None:
            return
        try:
            if self.view_mode == "pending":
                btn.config(text=f"Grupos verificados ({len(self.verified_idx)})")
            else:
                btn.config(text=f"← Voltar aos pendentes ({len(self.pending_idx)})")
        except tk.TclError:
            pass

    def open_preview(self, group_idx, pos):
        """
        Pré-visualização lado a lado das imagens de um grupo (até
        PREVIEW_COLUMNS colunas por vez; grupos maiores deslizam com as setas).
        Cada coluna mostra a imagem, nome, resolução, tamanho, data, status,
        origem, rótulos de diferença e o estado da seleção, com os botões
        "Selecionar/Desmarcar" e "Manter esta (selecionar as outras)".
        Teclado: ← → movem a coluna atual, Espaço alterna a atual, Enter =
        "manter esta", Esc ou clique no fundo fecha. Só uma janela por vez.
        """
        images = self.group_check_vars[group_idx]['images']
        md5_count = self.group_check_vars[group_idx]['md5_count']
        n = len(images)
        if n == 0:
            return
        # Só uma pré-visualização por vez: fecha a anterior, se existir
        prev = getattr(self, 'preview_window', None)
        if prev is not None:
            try:
                if prev.winfo_exists():
                    prev.grab_release()
                    prev.destroy()
            except tk.TclError:
                pass
            self.preview_window = None
        win = tk.Toplevel(self.groups_window)
        self.preview_window = win
        self.preview_photos = {}        # miniaturas grandes já recebidas nesta janela
        self.preview_requested = set()
        dark, panel, fg = "#111111", "#1E1E1E", "#EEEEEE"
        win.configure(bg=dark)
        win.transient(self.groups_window)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = int(sw * 0.9), int(sh * 0.88)
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        ncols = min(PREVIEW_COLUMNS, n)
        state = {'cur': pos % n, 'start': 0}
        state['start'] = max(0, min(state['cur'], n - ncols))

        header = tk.Label(win, fg=fg, bg=dark, font=FONT_BOLD)
        header.pack(side="top", fill="x", pady=(8, 2))
        hint = tk.Label(win, fg="#AAAAAA", bg=dark, font=FONT_UI,
                        text="← → coluna atual   |   Espaço selecionar/desmarcar   |   "
                             "Enter manter esta   |   Esc ou clique no fundo fecha")
        hint.pack(side="bottom", fill="x", pady=(0, 6))
        columns_frame = tk.Frame(win, bg=dark)
        columns_frame.pack(fill="both", expand=True, padx=10, pady=6)
        background_widgets = {win, header, hint, columns_frame}

        col_w = (w - 20) // ncols - 12
        img_h = h - 260

        def all_dims_known():
            return all(self.image_dims.get(im['filepath']) for im in images)

        def badges_for():
            metas = []
            for im in images:
                fp = im['filepath']
                d = self.image_dims.get(fp)
                size = self.file_stats.get(fp, (None, None))[0]
                mt = im.get('mtime')
                metas.append({'pixels': d[0] * d[1] if d else None, 'size': size,
                              'mtime': None if mt in (None, float("inf")) else mt})
            return plan_badges(metas)

        def load_photo(fp, kind=KIND_PHOTO, label=None):
            if kind != KIND_PHOTO:
                # Não é foto: a miniatura grande do Explorer (um quadro do vídeo,
                # a 1ª página do PDF...), pedida à thread de miniaturas para não
                # travar a janela (um vídeo 4K a frio pode levar segundos).
                # Enquanto não chega (ou se não existir), a placa com a extensão.
                key = ("preview", fp, max(64, min(col_w, img_h)))
                ready = self.preview_photos.get(key)
                if ready is not None:
                    return ready
                if label is not None:
                    self.thumb_labels.setdefault(key, []).append(label)
                if key not in self.preview_requested and shellthumb.available():
                    self.preview_requested.add(key)
                    self._thumb_loader.request(key, fp, key[2])
                    self._schedule_thumb_poll()
                return ImageTk.PhotoImage(extension_plate(fp, kind, min(col_w, 480), min(img_h, 300)))
            with Image.open(fp) as im:
                self.image_dims[fp] = im.size
                if im.format == "JPEG":
                    im.draft("RGB", (col_w * 2, img_h * 2))
                im.thumbnail((col_w, img_h))
                return ImageTk.PhotoImage(im)

        def toggle(i):
            info = images[i]
            if not info.get('is_reference'):
                info['var'].set(0 if info['var'].get() else 1)
                refresh()

        def keep_this(i):
            """Mantém a imagem i: desmarca ela e seleciona as outras do alvo."""
            for k, info in enumerate(images):
                if info.get('is_reference'):
                    continue
                info['var'].set(0 if k == i else 1)
            refresh()

        def refresh():
            for c in columns_frame.winfo_children():
                c.destroy()
            start = state['start']
            visible = list(range(start, min(start + ncols, n)))
            badges = badges_for()
            for i in visible:
                info = images[i]
                fp = info['filepath']
                is_cur = i == state['cur']
                col = tk.Frame(columns_frame, bg=panel, highlightthickness=3,
                               highlightbackground=("#4CAF50" if is_cur else panel),
                               highlightcolor=("#4CAF50" if is_cur else panel))
                col.pack(side="left", fill="both", expand=True, padx=6)
                background_widgets.add(col)
                col.bind("<Button-1>", lambda e, k=i: set_cur(k))
                lbl_img = tk.Label(col, bg=panel)
                lbl_img.pack(pady=(8, 4))
                kind = info.get('kind', KIND_PHOTO)
                try:
                    photo = load_photo(fp, kind, lbl_img)
                    lbl_img.config(image=photo)
                    lbl_img.image = photo
                except Exception as e:
                    log.warning("Erro ao carregar pré-visualização %s: %s", fp, e)
                    lbl_img.config(text="(Erro ao carregar a imagem)", fg=fg)
                lbl_img.bind("<Button-1>", lambda e, k=i: set_cur(k))

                sel = info['var'].get() == 1
                ref = info.get('is_reference', False)
                status = image_status(info, md5_count)
                dims = self.image_dims.get(fp)
                size = self.file_stats.get(fp, (None, None))[0]
                mt = info.get('mtime')
                mt_str = (datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
                          if mt not in (None, float("inf")) else "?")
                tag, rel_dir, name = shorten_path(fp, self._path_roots())
                tk.Label(col, text=name, fg=fg, bg=panel, font=FONT_BOLD, wraplength=col_w).pack()
                tk.Label(col, text=(f"[{tag}] " if tag else "") + (rel_dir or "(raiz)"),
                         fg="#9E9E9E", bg=panel, wraplength=col_w).pack()
                if kind == KIND_PHOTO:
                    summary = f"{status}   |   {format_resolution(dims)}"
                else:
                    summary = (f"{'Cópia exata' if status == 'Idêntica' else status}   |   "
                               f"{media_summary(kind, fp, self._media_info(fp, kind, info['md5']))}")
                tk.Label(col, text=(f"{summary}   |   "
                                    f"{format_bytes(size)}\nModificado em: {mt_str}"),
                         fg=fg, bg=panel, justify="center").pack(pady=(4, 2))
                badge_row = tk.Frame(col, bg=panel)
                badge_row.pack()
                for text in badges.get(i, []):
                    bfg, bbg = BADGE_STYLES[text]
                    tk.Label(badge_row, text=text, fg=bfg, bg=bbg, font=("Segoe UI", 8),
                             padx=6, pady=1).pack(side="left", padx=3)
                if ref:
                    tk.Label(col, text="REFERÊNCIA (protegida)", fg="white", bg=PALETTE["primary"],
                             font=FONT_BOLD, padx=6).pack(pady=(4, 0))
                state_lbl = tk.Label(col, text=self._t("SELECIONADA" if sel else "não selecionada",
                                                       files=is_file_kind(kind)),
                                     fg=("#A5D6A7" if sel else "#BDBDBD"), bg=panel, font=FONT_BOLD)
                state_lbl.pack(pady=(4, 2))
                btns = tk.Frame(col, bg=panel)
                btns.pack(pady=(2, 8))
                if not ref:
                    make_button(btns, "Desmarcar" if sel else "Selecionar", "select" if not sel else "light",
                                command=lambda k=i: toggle(k)).pack(side="left", padx=4)
                make_button(btns, "Manter esta (selecionar as outras)", "move",
                            command=lambda k=i: keep_this(k)).pack(side="left", padx=4)
                if kind != KIND_PHOTO:
                    # Vídeo/outro: o jeito de conferir é abrir (tocar o vídeo, ler o PDF)
                    make_button(btns, "Abrir no programa padrão", "light",
                                command=lambda p=fp: self.open_image(p)).pack(side="left", padx=4)
                for wdg in (badge_row, btns, state_lbl):
                    background_widgets.add(wdg)
            cur = images[state['cur']]
            header.config(text=self._t(f"Grupo {group_idx + 1}: imagem {state['cur'] + 1} de {n}   |   ",
                                       files=is_file_kind(cur.get('kind', KIND_PHOTO)))
                               + os.path.basename(cur['filepath']))
            win.title(f"Pré-visualização: {os.path.basename(cur['filepath'])}")

        def set_cur(i):
            state['cur'] = i % n
            if state['cur'] < state['start']:
                state['start'] = state['cur']
            elif state['cur'] >= state['start'] + ncols:
                state['start'] = state['cur'] - ncols + 1
            refresh()

        def go(delta):
            set_cur(state['cur'] + delta)

        def close(event=None):
            if self.preview_window is win:
                self.preview_window = None
            try:
                win.grab_release()
                win.destroy()
            except tk.TclError:
                pass

        def click(event):
            # Clique no fundo (fora de imagens/botões) fecha
            if event.widget in background_widgets and event.widget is not columns_frame \
                    and not isinstance(event.widget, tk.Frame):
                close()
            elif event.widget is win:
                close()

        win.bind("<Left>", lambda e: go(-1))
        win.bind("<Right>", lambda e: go(1))
        win.bind("<space>", lambda e: toggle(state['cur']))
        win.bind("<Return>", lambda e: keep_this(state['cur']))
        win.bind("<Escape>", close)
        win.bind("<Button-1>", click)
        win.protocol("WM_DELETE_WINDOW", close)
        refresh()
        win.focus_force()
        # Modal: enquanto aberta, cliques na lista de grupos não passam
        try:
            self._make_modal(win, self.groups_window)
        except tk.TclError:
            pass

    def _render_group_nav(self, frame, idx, images, offset):
        """Barra de navegação interna de um grupo grande: mostra a faixa exibida,
           quantas imagens do grupo estão selecionadas (inclusive as fora da faixa)
           e botões para avançar/voltar dentro do grupo."""
        total = len(images)
        end = min(offset + MAX_IMAGES_PER_GROUP_DISPLAY, total)
        selected = sum(1 for im in images if im['var'].get() == 1)

        nav = tk.Frame(frame, bg="#fff3cd", padx=5, pady=4)
        nav.pack(fill="x", pady=(0, 5))

        info = self._t(f"⚠️ Grupo com {total} imagens: exibindo {offset + 1} a {end}. "
                       f"Selecionadas neste grupo: {selected} (as ações de selecionar, mover e "
                       f"excluir valem para o grupo inteiro; use os botões para ver as demais).",
                       files=any(is_file_kind(im.get('kind', KIND_PHOTO)) for im in images))
        tk.Label(nav, text=info, fg="#856404", bg="#fff3cd", justify="left",
                 anchor="w", wraplength=800).pack(side="left", fill="x", expand=True)

        def go(new_offset, group_idx=idx):
            self.group_page_offset[group_idx] = new_offset
            # Preserva a posição de rolagem: trocar de faixa dentro de um grupo
            # não deve jogar o usuário de volta ao topo da página.
            self.render_page(keep_scroll_px=max(0, self.canvas.canvasy(0)))

        btn_next = make_button(nav, f"{MAX_IMAGES_PER_GROUP_DISPLAY} seguintes ▶", "light",
                               command=lambda: go(offset + MAX_IMAGES_PER_GROUP_DISPLAY),
                               state="normal" if end < total else "disabled")
        btn_next.pack(side="right", padx=3)
        btn_prev = make_button(nav, f"◀ {MAX_IMAGES_PER_GROUP_DISPLAY} anteriores", "light",
                               command=lambda: go(offset - MAX_IMAGES_PER_GROUP_DISPLAY),
                               state="normal" if offset > 0 else "disabled")
        btn_prev.pack(side="right", padx=3)

    def prev_page(self):
        """Navega para a página anterior"""
        if self.current_page > 0:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        """Navega para a próxima página"""
        total_pages = (len(self._visible_groups()) + self.groups_per_page - 1) // self.groups_per_page
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.render_page()

    def _ensure_metrics(self, images):
        """
        Garante 'size' e 'pixels' nos dicts das imagens (usados pela regra de
        qualidade e pelos rótulos). Tamanho vem do escaneamento; dimensões vêm
        das miniaturas já lidas ou de uma leitura só do cabeçalho (sem
        decodificar). Retorna quantos cabeçalhos foram lidos.
        """
        read = 0
        for info in images:
            fp = info['filepath']
            if info.get('size') is None:
                info['size'] = self.file_stats.get(fp, (None, None))[0]
            if info.get('kind', KIND_PHOTO) != KIND_PHOTO:
                continue   # vídeo/outro: sem resolução (e nunca passa pelo Pillow)
            if info.get('pixels') is None:
                dims = self.image_dims.get(fp)
                if dims is None:
                    read += 1
                    try:
                        with Image.open(fp) as im:
                            dims = im.size
                        self.image_dims[fp] = dims
                    except Exception:
                        dims = None
                info['pixels'] = dims[0] * dims[1] if dims else None
        return read

    def _plan_for_group(self, group_data, kind):
        """Índices a selecionar num grupo: kind = "identical" | "similar"."""
        images = group_data['images']
        if kind == "identical":
            return plan_identical_selection(images)
        self._ensure_metrics(images)
        if kind == "same_video":
            return plan_same_video_selection(images)
        if kind == "same_photo":
            return plan_same_photo_selection(images)
        return plan_similar_selection(images, group_data['md5_count'])

    def select_same_photo_images(self):
        """Seleciona, em todos os grupos PENDENTES, as versões piores de cada
           classe "Mesma foto" (mantém a melhor por qualidade; referência é a
           mantida; classes com cópia 'ampliada?' ficam de fora)."""
        selected_count = 0
        skipped_suspect = 0
        ref_worse = 0
        for idx in self.pending_idx:
            group_data = self.group_check_vars[idx]
            images = group_data['images']
            if not any(im.get('same_photo') is not None for im in images):
                continue
            self._ensure_metrics(images)
            classes = {}
            for im in images:
                if im.get('same_photo') is not None:
                    classes.setdefault(im['same_photo'], []).append(im)
            for members in classes.values():
                if any(im.get('same_photo_suspect') for im in members):
                    skipped_suspect += 1
                refs = [im for im in members if im.get('is_reference')]
                others = [im for im in members if not im.get('is_reference')]
                if refs and others and max((im.get('pixels') or 0) for im in others) > max((im.get('pixels') or 0) for im in refs):
                    ref_worse += 1
            for i in plan_same_photo_selection(images):
                images[i]['var'].set(1)
                selected_count += 1
        msg = (f"{selected_count} imagens 'Mesma foto' foram selecionadas (mantendo, em cada classe, "
               "a de melhor qualidade: maior resolução, depois maior arquivo, depois mais antiga).")
        if skipped_suspect:
            msg += f"\n\n{skipped_suspect} classe(s) com cópia 'ampliada?' foram deixadas para você decidir."
        if ref_worse:
            msg += (f"\n\nAtenção: em {ref_worse} classe(s) a cópia do acervo de referência tem resolução "
                    "MENOR que a do alvo; a referência é mantida mesmo assim (revise se quiser).")
        messagebox.showinfo("Seleção Concluída", msg + self._verified_note() + self._reference_selection_note())

    def select_same_video_images(self):
        """Em todos os grupos PENDENTES com "Mesmo vídeo": mantém um arquivo por
           grupo (o mais antigo; no empate, o maior) e seleciona os outros."""
        selected_count = 0
        for idx in self.pending_idx:
            group_data = self.group_check_vars[idx]
            if not any(im.get('same_video') for im in group_data['images']):
                continue
            for i in self._plan_for_group(group_data, "same_video"):
                group_data['images'][i]['var'].set(1)
                selected_count += 1
        messagebox.showinfo("Seleção Concluída",
                            f"{selected_count} arquivos 'Mesmo vídeo' foram selecionados (mantendo, em cada "
                            "grupo, o arquivo mais antigo; no empate, o maior)."
                            + self._verified_note() + self._t(self._reference_selection_note(), files=True))

    def select_group(self, group_idx, kind):
        """Seleção automática (idênticas ou semelhantes) só de UM grupo,
           sem diálogo: as caixas refletem na hora."""
        group_data = self.group_check_vars[group_idx]
        for i in self._plan_for_group(group_data, kind):
            group_data['images'][i]['var'].set(1)

    def select_identical_images(self):
        """Seleciona automaticamente imagens idênticas (mesmo MD5) em todos os
           grupos PENDENTES (os já verificados são decisão tomada). Sem pasta de referência: mantém a mais antiga de cada
           subgrupo. Com referência: mantém a cópia do acervo (a lógica de
           decisão está em plan_identical_selection, testável sem interface)."""
        selected_count = 0
        for idx in self.pending_idx:
            group_data = self.group_check_vars[idx]
            for i in self._plan_for_group(group_data, "identical"):
                group_data['images'][i]['var'].set(1)
                selected_count += 1

        messagebox.showinfo("Seleção Concluída", self._t(
                           f"{selected_count} imagens idênticas foram selecionadas (mantendo a mais antiga de cada grupo)."
                           + self._verified_note() + self._reference_selection_note()))

    def select_similar_images(self):
        """Seleciona automaticamente imagens semelhantes (MD5 diferente) em
           todos os grupos PENDENTES (os já verificados são decisão tomada). Sem pasta de referência: mantém a mais antiga de
           cada grupo. Com referência: mantém a versão do acervo (a lógica de
           decisão está em plan_similar_selection, testável sem interface)."""
        selected_count = 0
        # Dimensões desconhecidas são lidas do cabeçalho; com muitas, mostra progresso
        unknown = sum(1 for idx in self.pending_idx
                      for im in self.group_check_vars[idx]['images']
                      if im.get('pixels') is None and im['filepath'] not in self.image_dims
                      and im.get('kind', KIND_PHOTO) == KIND_PHOTO)
        show_progress = unknown > 500
        if show_progress:
            self.scan_cancelled = False
            self.create_progress_window()
            self.progress_window.title("Lendo dimensões das imagens")
            self.progress_label.config(text="Lendo dimensões (só o cabeçalho de cada arquivo)...")
        try:
            done = 0
            for n, idx in enumerate(self.pending_idx):
                group_data = self.group_check_vars[idx]
                done += self._ensure_metrics(group_data['images'])
                if show_progress:
                    self.update_progress(done, unknown, "dimensões", unit="arquivos")
                    if self.scan_cancelled:
                        break
                for i in self._plan_for_group(group_data, "similar"):
                    group_data['images'][i]['var'].set(1)
                    selected_count += 1
        finally:
            if show_progress:
                self._close_progress_window()

        messagebox.showinfo("Seleção Concluída",
                           f"{selected_count} imagens semelhantes foram selecionadas (mantendo a de melhor "
                           "qualidade de cada grupo: maior resolução, depois maior arquivo, depois mais antiga)."
                           + self._verified_note() + self._reference_selection_note())

    def _verified_note(self):
        n = len(getattr(self, 'verified_idx', []))
        return f"\n\n{n} grupo(s) já verificados não foram alterados." if n else ""

    def _reference_selection_note(self):
        """Complemento das mensagens de seleção no modo comparação
           (string vazia no modo normal: mensagens intocadas)."""
        if not self.reference_folder:
            return ""
        return ("\n\nModo comparação: quando existe cópia no acervo de referência, todas as "
                "cópias da pasta alvo são selecionadas. Imagens da referência nunca são "
                "selecionadas.")

    def _changed_since_scan(self, item):
        """
        Para arquivos comparados por bytes: True se o arquivo mudou depois da
        varredura (tamanho diferente, ou data diferindo mais de 2 s, que é a
        precisão de um pendrive FAT). A prova de "cópia exata" vale para o
        conteúdo lido na varredura; se mudou, o arquivo não é tocado.
        Fotos ficam de fora: seguem exatamente a regra de sempre.
        """
        filepath = item['filepath'] if isinstance(item, dict) else item
        if classify_file(filepath) == KIND_PHOTO:
            return False
        size, mtime_ns = getattr(self, 'file_stats', {}).get(filepath, (None, None))
        if size is None:
            return False
        try:
            st = os.stat(filepath)
        except OSError:
            return False   # sumiu: o erro normal de mover/excluir trata
        if st.st_size != size or (mtime_ns is not None and abs(st.st_mtime_ns - mtime_ns) > 2_000_000_000):
            log.warning("Alterado desde a varredura, não será tocado: %s", filepath)
            return True
        return False

    def _stale_proofs(self, selected_paths):
        """Reconfere as provas que vieram do cache, de bytes (_stale_md5_proofs) e
           de "Mesmo vídeo" (_stale_stream_proofs). Retorna o set de selecionados
           que NÃO podem ser tocados, ou None se o usuário cancelou."""
        selected_paths = list(selected_paths)
        stale = self._stale_md5_proofs(selected_paths)
        if stale is None:
            return None
        more = self._stale_stream_proofs([fp for fp in selected_paths if fp not in stale])
        return None if more is None else stale | more

    def _stale_stream_proofs(self, selected_paths):
        """
        Como _stale_md5_proofs, para "Mesmo vídeo": um selecionado que NÃO tem
        cópia exata ficando depende da prova por hash de fluxos. Se essa prova
        (a dele ou a de quem fica) veio do cache, o ffmpeg a refaz agora; só sai
        se o hash ainda é o registrado e sobra um mantido com o mesmo hash
        confirmado.
        """
        cached = getattr(self, 'byte_cached_stream', None)
        if not cached or not self.ffmpeg_path:
            return set()
        selected = set(selected_paths)
        plan = []    # (selecionado, mantidos com "Mesmo vídeo")
        for group_data in self.group_check_vars.values():
            members = [im for im in group_data['images'] if im.get('same_video')]
            kept = [im['filepath'] for im in members if im['filepath'] not in selected]
            for im in members:
                fp = im['filepath']
                if fp not in selected or not kept:
                    continue
                if any(k['md5'] == im['md5'] and k['filepath'] not in selected for k in members
                       if k is not im):
                    continue   # tem cópia EXATA ficando: já coberto pela reconferência de bytes
                if cache_key(fp) in cached or any(cache_key(k) in cached for k in kept):
                    plan.append((fp, kept))
        if not plan:
            return set()
        cancel_event = threading.Event()
        self.scan_cancelled = False
        self.create_progress_window()
        self.progress_window.title("Reconferindo Cópias")
        self.progress_label.config(text="Refazendo a comparação de vídeo e áudio (ffmpeg)...")
        self.progress_window.update()
        results = {}
        state = {"n": 0}
        total = len({fp for fp, _ in plan}) + len(plan)

        def pump():
            self.progress_window.update()
            if self.scan_cancelled:
                cancel_event.set()

        def confirmed(fp):
            key = cache_key(fp)
            if key not in cached:
                return True          # calculado nesta varredura
            if fp not in results:
                state["n"] += 1
                self.update_progress(state["n"], max(total, state["n"]), fp, force=True, unit="vídeos")
                now = ffmpegtools.stream_hashes(self.ffmpeg_path, fp, cancel_event, tick=pump)
                if cancel_event.is_set():
                    raise ScanCancelled()
                results[fp] = now is not None and now == self.byte_stream.get(key)
                if not results[fp]:
                    log.warning("Prova de 'Mesmo vídeo' do cache NÃO confirmada: %s", fp)
            return results[fp]

        stale = set()
        try:
            for fp, kept in plan:
                kept_ok = any(confirmed(k) for k in sorted(kept, key=lambda k: cache_key(k) in cached))
                if not kept_ok or not confirmed(fp):
                    stale.add(fp)
        except (ScanCancelled, tk.TclError):
            return None
        finally:
            self._close_progress_window()
        for fp, ok in results.items():
            if ok:
                cached.discard(cache_key(fp))
        return stale

    def _stale_md5_proofs(self, selected_paths):
        """
        Última trava antes de mover/excluir arquivos comparados por bytes.
        O cache vale por caminho + tamanho + data, e há programas que mudam o
        conteúdo preservando os três (contêiner VeraCrypt, editor de tags com
        "manter a data"): a prova guardada pode ter caducado sem sinal nenhum.
        Para cada conjunto de cópias em que ALGUMA prova veio do cache, relê
        agora os arquivos envolvidos e só libera um selecionado se (a) o MD5
        dele ainda é o registrado e (b) sobra ao menos um exemplar NÃO
        selecionado com esse mesmo MD5 confirmado. Provas lidas nesta varredura
        já estão cobertas por _changed_since_scan e não são relidas.

        Retorna o set de caminhos selecionados que NÃO podem ser tocados, ou
        None se o usuário cancelou a reconferência (nada deve ser feito).
        """
        cached = getattr(self, 'byte_cached_proof', None)
        if not cached:
            return set()
        selected = set(selected_paths)
        plan = []    # (md5, selecionados, mantidos) de cada conjunto com prova do cache
        for group_data in self.group_check_vars.values():
            by_md5 = {}
            for im in group_data['images']:
                if im.get('kind', KIND_PHOTO) != KIND_PHOTO:
                    by_md5.setdefault(im['md5'], []).append(im['filepath'])
            for md5, members in by_md5.items():
                chosen = [fp for fp in members if fp in selected]
                kept = [fp for fp in members if fp not in selected]
                # Tudo selecionado: não sobra cópia de qualquer jeito (decisão
                # explícita do usuário); nada selecionado: nada a conferir
                if chosen and kept and any(cache_key(fp) in cached for fp in members):
                    plan.append((md5, chosen, kept))
        if not plan:
            return set()

        def needs_read(fp):
            return cache_key(fp) in cached

        def size_of(fp):
            return self.file_stats.get(fp, (None, None))[0]

        to_read = [fp for _, chosen, kept in plan for fp in chosen if needs_read(fp)]
        to_read += [kept[0] for _, _, kept in plan if all(needs_read(fp) for fp in kept)]
        total = sum(size_of(fp) or 0 for fp in to_read)
        done = [0]
        cancel_event = threading.Event()
        self.scan_cancelled = False
        self.create_progress_window()
        self.progress_window.title("Reconferindo Cópias")
        self.progress_label.config(text="Relendo os arquivos cuja prova veio do cache...")
        self.progress_window.update()
        div = 1024 * 1024
        current = {"fp": ""}

        def on_bytes(n):
            done[0] += n
            self.update_progress(-(-done[0] // div), max(1, -(-total // div)), current["fp"], unit="MB")
            if self.scan_cancelled:
                cancel_event.set()

        results = {}     # caminho -> True (MD5 confirmado) / False
        fixes = {}       # caminho -> MD5 de verdade, para corrigir o cache

        def confirmed(fp, md5):
            if not needs_read(fp):
                return True          # lido nesta varredura
            if fp not in results:
                size = size_of(fp)
                current["fp"] = fp
                ok = False
                if size:
                    ok, actual = verify_cached_proof(fp, size, md5, cancel_event, on_bytes)
                    if not ok and actual:
                        fixes[fp] = actual
                results[fp] = ok
                if not ok:
                    log.warning("Prova do cache NÃO confirmada (conteúdo mudou sem mudar tamanho "
                                "nem data?): %s", fp)
            return results[fp]

        stale = set()
        try:
            for md5, chosen, kept in plan:
                # um mantido confirmado basta; os lidos nesta varredura vêm primeiro
                kept_ok = any(confirmed(fp, md5) for fp in sorted(kept, key=needs_read))
                for fp in chosen:
                    if not kept_ok or not confirmed(fp, md5):
                        stale.add(fp)
        except ScanCancelled:
            return None
        finally:
            self._close_progress_window()
        # O que foi confirmado agora passa a valer como lido nesta sessão
        for fp, ok in results.items():
            if ok:
                cached.discard(cache_key(fp))
        if fixes and self.use_cache_var.get() == 1:
            # Conserta o cache: a próxima varredura não repete o engano
            fix_cache = FileHashCache()
            try:
                for fp, actual in fixes.items():
                    size, mtime_ns = self.file_stats.get(fp, (None, None))
                    fix_cache.store(fp, size, mtime_ns, md5=actual)
            finally:
                fix_cache.close()
        return stale

    @staticmethod
    def _stale_note(stale):
        return (f"\n{stale} arquivo(s) NÃO foram tocados: a prova de cópia exata vinha do cache e não "
                "se confirmou ao reler agora (o conteúdo mudou sem mudar tamanho nem data). "
                "Escaneie de novo sem o cache." if stale else "")

    @staticmethod
    def _live_moved_note(count, verb):
        return f"\n+ {count} vídeo(s) de Live Photo {verb} junto com a foto." if count else ""

    @staticmethod
    def _changed_note(changed):
        return (f"\n{changed} arquivo(s) mudaram depois da varredura e NÃO serão tocados "
                "(a comparação valia para o conteúdo antigo; escaneie de novo)."
                if changed else "")

    @staticmethod
    def _protected_note(protected):
        return (f"\n{protected} imagem(ns) da referência foram ignoradas (protegidas)."
                if protected else "")

    def open_folder(self, path):
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Abrir pasta", f"Não foi possível abrir:\n{path}\n\n{e}")

    def _group_index_of(self, group):
        """Índice original do grupo (lista de tuplas) dentro de self.groups."""
        for idx, data in self.group_check_vars.items():
            if data['group'] is group:
                return idx
        return None

    def _image_meta(self, group_idx, filepath):
        """(status, origem, tamanho) de uma imagem, para o relatório."""
        status = origem = ""
        size = self.file_stats.get(filepath, (None, None))[0]
        data = self.group_check_vars.get(group_idx)
        if data:
            for info in data['images']:
                if info['filepath'] == filepath:
                    status = image_status(info, data['md5_count'])
                    origem = "REF" if info.get('is_reference') else "ALVO"
                    break
        return status, origem, size

    def _live_photo_step(self, selected_paths):
        """
        Antes de mover/excluir: acha os vídeos de Live Photo que podem
        acompanhar as fotos selecionadas (plan_live_companions: só os que têm
        cópia idêntica ficando) e pergunta UMA vez. Sem nenhum par, não há
        diálogo algum (o fluxo de sempre não muda).
        Retorna None se o usuário cancelou tudo; senão um dict:
          'pairs':  {cache_key(foto): (foto, mov)} a levar junto (vazio se "Não")
          'note':   texto para a mensagem final (vídeos que ficaram, fotos órfãs)
          'warn':   texto para a confirmação (fotos que ficarão sem o vídeo)
          'labels': {mov: (status, tamanho)} para o relatório CSV
        """
        selected_paths = list(selected_paths)
        acting_keys = {cache_key(p) for p in selected_paths}
        # "Fica" = não está marcado em NENHUM grupo (não só no grupo desta ação)
        selected_keys = acting_keys | {cache_key(im['filepath']) for d in self.group_check_vars.values()
                                       for im in d['images'] if im['var'].get() == 1}
        groups = [[im['filepath'] for im in d['images']] for d in self.group_check_vars.values()]
        started = time.time()
        state = {"window": False}

        def tick(n):
            # Conferir os vídeos lê os dois arquivos de cada par: se demorar,
            # aparece uma janela de progresso com Cancelar
            if not state["window"] and time.time() - started > 0.7:
                self.scan_cancelled = False
                self.create_progress_window()
                self.progress_window.title("Live Photos")
                state["window"] = True
            if state["window"]:
                self.progress_label.config(text=f"Conferindo os vídeos das Live Photos... {n}")
                self.progress_window.update()
                if self.scan_cancelled:
                    raise ScanCancelled()

        try:
            plan, without_copy = plan_live_companions(groups, selected_keys, self._is_protected,
                                                      acting_keys, tick)
        except ScanCancelled:
            return None
        finally:
            if state["window"]:
                self._close_progress_window()
        orphans = orphaned_live_photos(selected_paths, selected_keys)
        out = {'pairs': {}, 'note': "", 'warn': "", 'labels': {}}
        if orphans:
            out['warn'] = (f"\n\nAtenção: {orphans} vídeo(s) selecionado(s) são a parte em vídeo de Live "
                           "Photos cuja foto NÃO está selecionada: a foto ficará sem o vídeo dela.")
        if without_copy:
            out['note'] = (f"\n{without_copy} vídeo(s) de Live Photo (.MOV ao lado da foto) ficaram onde "
                           "estão: não há cópia idêntica deles ao lado das fotos mantidas.")
        if not plan:
            return out
        answer = messagebox.askyesnocancel(
            "Live Photos",
            f"{len(plan)} das fotos selecionadas são Live Photos do iPhone: cada uma tem ao lado um "
            "vídeo curto (.MOV) com o mesmo nome.\n\n"
            f"Esses {len(plan)} vídeo(s) têm cópia idêntica, conferida agora, ao lado da foto que "
            "será mantida.\n\n"
            "Levar esses vídeos junto com as fotos?\n"
            "(Não = só as fotos; os vídeos ficam soltos na pasta.)")
        if answer is None:
            return None
        if answer:
            for photo, mov in plan:
                out['pairs'][cache_key(photo)] = (photo, mov)
                try:
                    size = os.path.getsize(mov)
                except OSError:
                    size = None
                out['labels'][mov] = ("Live Photo (par)", size)
        else:
            out['note'] += f"\n{len(plan)} vídeo(s) de Live Photo ficaram na pasta, a seu pedido."
        return out

    def _record_batch(self, action, items, labels=None):
        """Registra um lote no relatório CSV e, se for reversível, no log de
           ações. items: lista de (group_idx, caminho, destino). labels:
           {caminho: (status, tamanho)} para itens que não são linha de grupo
           (o .MOV que acompanhou uma Live Photo)."""
        if not items:
            return
        rows = []
        for group_idx, src, dst in items:
            status, origem, size = self._image_meta(group_idx, src)
            if labels and src in labels:
                status, size = labels[src]
                origem = "ALVO"
            rows.append({"acao": action, "grupo": (group_idx + 1) if group_idx is not None else "",
                         "status": status, "origem": origem, "caminho": src,
                         "destino": dst or "", "tamanho": size if size is not None else ""})
        self.session_report.record(rows)
        if action == "mover":
            self.action_log.append({"type": "move", "items": [(src, dst) for _, src, dst in items]})
        elif action == "lixeira":
            self.action_log.append({"type": "trash", "items": [(src, None) for _, src, _ in items]})

    def _report_note(self):
        path = self.session_report.path
        return f"\n\nRelatório da sessão: {path}" if path else ""

    def undo_last_action(self):
        """Desfaz o último lote de movimentos (devolve os arquivos à origem e
           os re-seleciona). Exclusões foram para a Lixeira: restaure por lá."""
        if not self.action_log:
            messagebox.showinfo("Desfazer", "Nenhuma ação para desfazer nesta sessão.")
            return
        last = self.action_log[-1]
        if last["type"] == "trash":
            messagebox.showinfo(
                "Desfazer",
                self._t(f"O último lote enviou {len(last['items'])} imagem(ns) para a Lixeira do Windows.\n"
                        "Para restaurá-las, abra a Lixeira, selecione os arquivos e use 'Restaurar'.")
            )
            return
        if not messagebox.askyesno(
                "Desfazer",
                self._t(f"Devolver {len(last['items'])} imagem(ns) movida(s) para a pasta de origem?")):
            return
        self.action_log.pop()
        by_path = {info['filepath']: info for data in self.group_check_vars.values()
                   for info in data['images']}
        restored, conflicts, errors = 0, 0, []
        report_items = []
        for src, dst in last["items"]:
            if not os.path.exists(dst) or os.path.exists(src):
                conflicts += 1
                continue
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                restored += 1
                if src in by_path:
                    by_path[src]['var'].set(1)
                report_items.append((None, src, dst))
            except Exception as e:
                errors.append(f"{src}: {e}")
        rows = [{"acao": "desfazer_mover", "grupo": "", "status": "", "origem": "",
                 "caminho": src, "destino": dst, "tamanho": ""} for _, src, dst in report_items]
        self.session_report.record(rows)
        self.render_page()
        msg = self._t(f"{restored} imagem(ns) devolvida(s) à origem (e selecionada(s) de novo).")
        if conflicts:
            msg += self._t(f"\n{conflicts} não puderam ser devolvidas (arquivo já existe na origem ou sumiu do destino).")
        if errors:
            msg += "\n\nErros:\n" + "\n".join(errors[:5])
        messagebox.showinfo("Desfazer", msg + self._report_note())

    def _trash_refused(self):
        """Sem send2trash: recusa excluir (nunca exclui definitivamente)."""
        if trash_available():
            return False
        messagebox.showerror(
            "Lixeira indisponível",
            "A exclusão usa a Lixeira do Windows (biblioteca send2trash), que não está\n"
            "disponível nesta instalação. Nada foi excluído.\n\n"
            "Instale com: python -m pip install send2trash\n"
            "Ou use 'Mover Selecionadas' para uma pasta temporária."
        )
        return True

    def move_all_selected(self):
        """Move todas as imagens selecionadas de todos os grupos"""
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return
        if self._dest_under_reference(dest_folder):
            messagebox.showerror(
                "Destino protegido",
                "A pasta de destino é (ou está dentro de) a pasta de referência protegida.\n"
                "Escolha outro destino."
            )
            return

        moved_count = 0
        skipped_count = 0
        protected = 0
        changed = 0
        errors = []
        batch = []

        wanted = [im['filepath'] for d in self.group_check_vars.values() for im in d['images']
                  if im['var'].get() == 1 and not self._is_protected(im['filepath'])]
        # Live Photos: o .MOV ao lado da foto pode ir junto (só pergunta se houver par)
        live = self._live_photo_step(wanted)
        if live is None:
            return   # cancelado na pergunta das Live Photos: nada foi movido
        # Provas de "cópia exata" que vieram do cache são relidas agora
        stale = self._stale_proofs(wanted)
        if stale is None:
            return   # reconferência cancelada: nada foi movido
        live_moved = 0

        # Itera sobre todos os grupos
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']

            # Move cada imagem selecionada
            for img_info in images:
                if img_info['var'].get() == 1:
                    filepath = img_info['filepath']
                    if self._is_protected(filepath):
                        protected += 1
                        img_info['var'].set(0)
                        log.warning("Bloqueado: tentativa de mover arquivo da referência: %s", filepath)
                        continue
                    if filepath in stale:
                        img_info['var'].set(0)
                        continue
                    if self._changed_since_scan(img_info):
                        changed += 1
                        img_info['var'].set(0)
                        continue
                    try:
                        pair = live['pairs'].get(cache_key(filepath))
                        new_mov = mov_err = None
                        if pair:
                            new_path, new_mov, mov_err = self._move_pair(filepath, pair[1], dest_folder)
                        else:
                            new_path = self._move_file(filepath, dest_folder)
                        if new_path is None:
                            skipped_count += 1  # já estava na pasta de destino
                        else:
                            moved_count += 1
                            batch.append((group_idx, filepath, new_path))
                            img_info['var'].set(0)  # Desmarca após mover
                            if new_mov:
                                live_moved += 1
                                batch.append((group_idx, pair[1], new_mov))
                            if mov_err:
                                errors.append(f"{pair[1]}: {mov_err}")
                    except Exception as e:
                        errors.append(f"{filepath}: {str(e)}")

        self._record_batch("mover", batch, live['labels'])
        # Recarrega a página atual para atualizar a visualização
        self.render_page()

        skipped_msg = self._t(f"\n{skipped_count} imagem(ns) ignorada(s): já estavam na pasta de destino "
                              f"(continuam selecionadas)." if skipped_count else "")
        skipped_msg += (self._t(self._protected_note(protected)) + self._changed_note(changed)
                        + self._stale_note(len(stale)) + self._live_moved_note(live_moved, "movido(s)")
                        + live['note'] + live['warn'])
        undo_msg = "\n\nPara devolver: menu 'Mais' > 'Desfazer último lote'." if batch else ""
        if errors:
            error_msg = (self._t(f"{moved_count} imagens movidas.") + f"{skipped_msg}\n\nErros:\n"
                         + "\n".join(errors[:5]))
            if len(errors) > 5:
                error_msg += f"\n... e mais {len(errors) - 5} erros."
            messagebox.showwarning("Mover - Concluído com Erros", error_msg + undo_msg + self._report_note())
        else:
            messagebox.showinfo("Mover", self._t(f"{moved_count} imagens movidas com sucesso!") + skipped_msg
                                + undo_msg + self._report_note())

    def delete_all_selected(self):
        """Exclui todas as imagens selecionadas de todos os grupos"""
        # Conta quantas imagens estão selecionadas (as protegidas não contam:
        # o número prometido na confirmação tem que ser o número executado)
        selected_count = 0
        protected = 0
        changed_paths = set()   # mudaram desde a varredura: a prova de "idêntico" caducou
        for group_data in self.group_check_vars.values():
            for img_info in group_data['images']:
                if img_info['var'].get() == 1:
                    if self._is_protected(img_info['filepath']):
                        protected += 1
                    elif self._changed_since_scan(img_info):
                        changed_paths.add(img_info['filepath'])
                    else:
                        selected_count += 1

        if selected_count == 0:
            messagebox.showinfo("Excluir", self._t("Nenhuma imagem selecionada."
                                                   + self._protected_note(protected))
                                + self._changed_note(len(changed_paths)))
            return
        if self._trash_refused():
            return

        wanted = [im['filepath'] for d in self.group_check_vars.values() for im in d['images']
                  if im['var'].get() == 1 and not self._is_protected(im['filepath'])
                  and im['filepath'] not in changed_paths]
        # Live Photos: o .MOV ao lado da foto pode ir junto (só pergunta se houver
        # par), ANTES da confirmação, para o número prometido ser o executado
        live = self._live_photo_step(wanted)
        if live is None:
            return
        live_line = (f"\n+ {len(live['pairs'])} vídeo(s) de Live Photo (.MOV ao lado da foto)."
                     if live['pairs'] else "")

        confirm = messagebox.askyesno("Excluir", self._t(
                                     f"Enviar {selected_count} imagens selecionadas para a Lixeira do Windows?"
                                     + self._protected_note(protected)) + self._changed_note(len(changed_paths))
                                     + live_line + live['warn'])
        if not confirm:
            return

        # Provas de "cópia exata" que vieram do cache são relidas agora, antes
        # de qualquer arquivo ir para a Lixeira
        stale = self._stale_proofs(wanted)
        if stale is None:
            return   # reconferência cancelada: nada foi excluído

        deleted_count = 0
        live_deleted = 0
        errors = []
        batch = []

        # Itera sobre todos os grupos
        for group_idx, group_data in self.group_check_vars.items():
            images = group_data['images']

            # Exclui cada imagem selecionada
            for img_info in images:
                if img_info['var'].get() == 1:
                    filepath = img_info['filepath']
                    if self._is_protected(filepath):
                        img_info['var'].set(0)
                        log.warning("Bloqueado: tentativa de excluir arquivo da referência: %s", filepath)
                        continue
                    # o diálogo pode ter ficado aberto por minutos: confere de novo
                    if (filepath in changed_paths or filepath in stale
                            or self._changed_since_scan(img_info)):
                        img_info['var'].set(0)
                        continue
                    try:
                        trash_file(filepath)
                        deleted_count += 1
                        batch.append((group_idx, filepath, None))
                        img_info['var'].set(0)  # Desmarca após excluir
                    except Exception as e:
                        errors.append(f"{filepath}: {str(e)}")
                        continue
                    pair = live['pairs'].get(cache_key(filepath))
                    if pair:   # a foto foi: o vídeo de Live Photo dela vai junto
                        try:
                            trash_file(pair[1])
                            live_deleted += 1
                            batch.append((group_idx, pair[1], None))
                        except Exception as e:
                            errors.append(f"{pair[1]}: {str(e)}")

        self._record_batch("lixeira", batch, live['labels'])
        # Recarrega a página atual para atualizar a visualização
        self.render_page()

        extra = (self._stale_note(len(stale)) + self._live_moved_note(live_deleted, "enviado(s) à Lixeira")
                 + live['note'])
        if errors:
            error_msg = (self._t(f"{deleted_count} imagens enviadas para a Lixeira.") + "\n\nErros:\n"
                         + "\n".join(errors[:5]))
            if len(errors) > 5:
                error_msg += f"\n... e mais {len(errors) - 5} erros."
            messagebox.showwarning("Excluir - Concluído com Erros", error_msg + extra + self._report_note())
        else:
            messagebox.showinfo("Excluir", self._t(f"{deleted_count} imagens enviadas para a Lixeira do Windows!"
                                                   + self._protected_note(protected))
                                + extra + self._report_note())

    @staticmethod
    def _move_file(filepath, dest_folder):
        """Move um arquivo para a pasta de destino. Se já existir um arquivo com
           o mesmo nome, adiciona sufixo _1, _2, ... Funciona entre unidades
           diferentes (shutil.move), ao contrário do os.rename."""
        basename = os.path.basename(filepath)
        new_path = os.path.join(dest_folder, basename)
        # Destino é o próprio arquivo (mover para a pasta onde já está):
        # nada a fazer; retorna None para o chamador não contar como movida.
        if cache_key(new_path) == cache_key(filepath):
            return None
        if os.path.exists(new_path):
            name, ext = os.path.splitext(basename)
            counter = 1
            while os.path.exists(new_path):
                new_path = os.path.join(dest_folder, f"{name}_{counter}{ext}")
                counter += 1
        ImageCleaner._move_to(filepath, new_path)
        return new_path

    @staticmethod
    def _move_pair(photo, mov, dest_folder):
        """
        Move uma Live Photo (foto + .MOV) mantendo o par: os dois recebem o
        MESMO sufixo de colisão (IMG_1_2.JPG e IMG_1_2.MOV), senão o vínculo
        pelo nome se perderia no destino. Retorna (novo_foto, novo_mov, erro_mov);
        novo_foto None = a foto já estava na pasta de destino (nada é movido).
        Erro ao mover a FOTO propaga; erro só no vídeo volta em erro_mov (a
        foto já foi e tem de ser registrada).
        """
        name, ext = os.path.splitext(os.path.basename(photo))
        mov_ext = os.path.splitext(mov)[1]
        if cache_key(os.path.join(dest_folder, name + ext)) == cache_key(photo):
            return None, None, None
        counter = 0
        while True:
            suffix = f"_{counter}" if counter else ""
            new_photo = os.path.join(dest_folder, name + suffix + ext)
            new_mov = os.path.join(dest_folder, name + suffix + mov_ext)
            if not os.path.exists(new_photo) and not os.path.exists(new_mov):
                break
            counter += 1
        ImageCleaner._move_to(photo, new_photo)
        try:
            ImageCleaner._move_to(mov, new_mov)
        except Exception as e:
            return new_photo, None, e
        return new_photo, new_mov, None

    @staticmethod
    def _move_to(filepath, new_path):
        """shutil.move com limpeza da cópia parcial se falhar no meio."""
        try:
            shutil.move(filepath, new_path)
        except Exception:
            # Entre unidades o shutil.move copia e depois apaga a origem. Se a
            # origem ainda existe, a cópia no destino é descartável (completa,
            # se só o apagar falhou; parcial, se a cópia foi interrompida):
            # remove para não deixar duplicata nem arquivo truncado para trás.
            if os.path.exists(filepath) and os.path.exists(new_path):
                try:
                    os.remove(new_path)
                except OSError:
                    pass
            raise
        return new_path

    def move_images(self, group, check_vars):
        dest_folder = filedialog.askdirectory(title="Selecione a pasta de destino")
        if not dest_folder:
            return
        if self._dest_under_reference(dest_folder):
            messagebox.showerror(
                "Destino protegido",
                "A pasta de destino é (ou está dentro de) a pasta de referência protegida.\n"
                "Escolha outro destino."
            )
            return
        moved = skipped = failed = protected = changed = 0
        batch = []
        group_idx = self._group_index_of(group)
        files = any(is_file_kind(classify_file(fp)) for (fp, _, _) in group)
        wanted = [fp for (fp, _, _), var in zip(group, check_vars)
                  if var.get() == 1 and not self._is_protected(fp)]
        live = self._live_photo_step(wanted)
        if live is None:
            return   # cancelado na pergunta das Live Photos: nada foi movido
        stale = self._stale_proofs(wanted)
        if stale is None:
            return   # reconferência cancelada: nada foi movido
        live_moved = 0
        for (filepath, _, _), var in zip(group, check_vars):
            if var.get() == 1:
                if self._is_protected(filepath):
                    protected += 1
                    var.set(0)
                    log.warning("Bloqueado: tentativa de mover arquivo da referência: %s", filepath)
                    continue
                if filepath in stale:
                    var.set(0)
                    continue
                if self._changed_since_scan(filepath):
                    changed += 1
                    var.set(0)
                    continue
                try:
                    pair = live['pairs'].get(cache_key(filepath))
                    new_mov = mov_err = None
                    if pair:
                        new_path, new_mov, mov_err = self._move_pair(filepath, pair[1], dest_folder)
                    else:
                        new_path = self._move_file(filepath, dest_folder)
                    if new_path is None:
                        skipped += 1  # já estava na pasta de destino
                    else:
                        moved += 1
                        batch.append((group_idx, filepath, new_path))
                        if new_mov:
                            live_moved += 1
                            batch.append((group_idx, pair[1], new_mov))
                        if mov_err:
                            failed += 1
                            log.warning("Erro ao mover o vídeo da Live Photo %s: %s", pair[1], mov_err)
                except Exception as e:
                    failed += 1
                    log.warning("Erro ao mover %s: %s", filepath, e)
        self._record_batch("mover", batch, live['labels'])
        msg = self._t(f"{moved} imagem(ns) movida(s).", files=files)
        if skipped:
            msg += self._t(f"\n{skipped} ignorada(s): já estavam na pasta de destino.", files=files)
        if failed:
            msg += f"\n{failed} com erro (detalhes no log)."
        msg += (self._t(self._protected_note(protected), files=files) + self._changed_note(changed)
                + self._stale_note(len(stale)) + self._live_moved_note(live_moved, "movido(s)")
                + live['note'] + live['warn'])
        if batch:
            msg += "\n\nPara devolver: menu 'Mais' > 'Desfazer último lote'."
        messagebox.showinfo("Mover", msg + self._report_note())

    def delete_images(self, group, check_vars):
        selected_count = 0
        protected = 0
        changed_paths = set()   # mudaram desde a varredura: a prova de "idêntico" caducou
        files = any(is_file_kind(classify_file(fp)) for (fp, _, _) in group)
        for (filepath, _, _), var in zip(group, check_vars):
            if var.get() == 1:
                if self._is_protected(filepath):
                    protected += 1
                elif self._changed_since_scan(filepath):
                    changed_paths.add(filepath)
                else:
                    selected_count += 1
        if selected_count == 0:
            messagebox.showinfo("Excluir", self._t("Nenhuma imagem selecionada neste grupo."
                                                   + self._protected_note(protected), files=files)
                                + self._changed_note(len(changed_paths)))
            return
        if self._trash_refused():
            return
        wanted = [fp for (fp, _, _), var in zip(group, check_vars)
                  if var.get() == 1 and not self._is_protected(fp) and fp not in changed_paths]
        live = self._live_photo_step(wanted)
        if live is None:
            return
        live_line = (f"\n+ {len(live['pairs'])} vídeo(s) de Live Photo (.MOV ao lado da foto)."
                     if live['pairs'] else "")
        # A contagem importa: em grupos grandes a seleção pode incluir imagens
        # que não estão na faixa exibida no momento.
        confirm = messagebox.askyesno(
            "Excluir",
            self._t(f"Enviar {selected_count} imagem(ns) selecionada(s) deste grupo para a Lixeira do Windows?"
                    + self._protected_note(protected), files=files) + self._changed_note(len(changed_paths))
            + live_line + live['warn']
        )
        if not confirm:
            return
        stale = self._stale_proofs(wanted)
        if stale is None:
            return   # reconferência cancelada: nada foi excluído
        deleted = failed = live_deleted = 0
        batch = []
        group_idx = self._group_index_of(group)
        for (filepath, _, _), var in zip(group, check_vars):
            if var.get() == 1:
                if self._is_protected(filepath):
                    var.set(0)
                    log.warning("Bloqueado: tentativa de excluir arquivo da referência: %s", filepath)
                    continue
                # o diálogo pode ter ficado aberto por minutos: confere de novo
                if filepath in changed_paths or filepath in stale or self._changed_since_scan(filepath):
                    var.set(0)
                    continue
                try:
                    trash_file(filepath)
                    deleted += 1
                    batch.append((group_idx, filepath, None))
                    var.set(0)
                except Exception as e:
                    failed += 1
                    log.warning("Erro ao excluir %s: %s", filepath, e)
                    continue
                pair = live['pairs'].get(cache_key(filepath))
                if pair:   # a foto foi: o vídeo de Live Photo dela vai junto
                    try:
                        trash_file(pair[1])
                        live_deleted += 1
                        batch.append((group_idx, pair[1], None))
                    except Exception as e:
                        failed += 1
                        log.warning("Erro ao excluir o vídeo da Live Photo %s: %s", pair[1], e)
        self._record_batch("lixeira", batch, live['labels'])
        msg = self._t(f"{deleted} imagem(ns) enviada(s) para a Lixeira do Windows.", files=files)
        if failed:
            msg += f"\n{failed} com erro (detalhes no log)."
        messagebox.showinfo("Excluir", msg + self._t(self._protected_note(protected), files=files)
                            + self._stale_note(len(stale))
                            + self._live_moved_note(live_deleted, "enviado(s) à Lixeira")
                            + live['note'] + self._report_note())

def _report_callback_exception(exc_type, exc_value, exc_tb):
    """Erros dentro de callbacks do Tk (cliques de botão etc.) iam para o stderr,
       que não existe no .exe: agora vão para o log e para uma mensagem."""
    log.error("Erro inesperado na interface:\n%s",
              "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    try:
        messagebox.showerror(
            "Erro inesperado",
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"Detalhes no log: {get_log_path()}"
        )
    except Exception:
        pass


def run_selftest(folder, reference=None, confirm_similar=None, same_photo=None,
                 videos=False, others=False, same_video=None):
    """
    Modo de diagnóstico sem interface: `ImageCleaner.exe --selftest PASTA`
    ou `--selftest PASTA --ref REFERENCIA` (modo de comparação), opcionalmente
    com `--no-confirm` (desliga a confirmação de semelhantes por dhash;
    confirm_similar=None usa o padrão CONFIRM_SIMILAR) e `--no-same-photo`
    (same_photo=None usa o padrão SAME_PHOTO_ENABLED). `--videos` e `--others`
    ligam a comparação por bytes desses tipos (como as caixas da tela inicial);
    o resumo ganha o trecho "| BYTES: ..." só quando há algo comparado por bytes.
    Roda o pipeline completo (listar, hash, agrupar, MD5) e escreve o resumo
    no log (e no console, quando houver). Útil para validar o executável e
    para diagnosticar problemas em campo. Não usa o cache e não altera nada.

    No modo de comparação também simula a seleção automática e verifica, com
    assert, que nenhuma imagem da referência seria selecionada.
    """
    setup_logging()
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Pasta não encontrada ou inacessível: {folder}")
    if reference is not None:
        if not os.path.isdir(reference):
            raise FileNotFoundError(f"Pasta de referência não encontrada ou inacessível: {reference}")
        reason = folder_conflict(folder, reference)
        if reason:
            raise ValueError(f"Pastas em conflito: {reason}")
    t0 = time.time()
    log.info("SELFTEST em %s (threads=%d, draft=%s)", folder, HASH_WORKERS, USE_FAST_JPEG_DECODE)
    accept = make_scan_accept(True, videos, others)
    entries = list_image_files(folder, True, VALID_EXTENSIONS, accept=accept)
    ref_keys = set()
    if reference is not None:
        ref_entries = list_image_files(reference, True, VALID_EXTENSIONS, accept=accept)
        log.info("SELFTEST referência %s: %d arquivos", reference, len(ref_entries))
        entries, ref_keys = merge_scan_entries(entries, ref_entries)
    # Como na interface: fotos no pipeline de aparência, o resto só por bytes
    byte_entries = [e for e in entries if classify_file(e[0]) != KIND_PHOTO]
    entries = [e for e in entries if classify_file(e[0]) == KIND_PHOTO]
    log.info("SELFTEST listagem: %d arquivos em %.1fs", len(entries), time.time() - t0)
    byte_groups, byte_stats, sv_stats = [], None, None
    if byte_entries:
        b_idx, b_md5, byte_stats, _ = find_identical_files(
            byte_entries, None, HASH_WORKERS,
            reference_keys=ref_keys if reference is not None else None)
        ffmpeg_path = ffmpegtools.find_ffmpeg(load_settings().get("ffmpeg_path") or None,
                                              ImageCleaner._app_dir()) if videos else None
        if same_video is None:
            same_video = bool(ffmpeg_path)
        if videos and same_video and ffmpeg_path and ffmpegtools.check_version(ffmpeg_path):
            b_idx, b_md5, _, _, sv_stats, _ = find_same_videos(
                byte_entries, b_idx, b_md5, ffmpeg_path, ffmpegtools.ffprobe_beside(ffmpeg_path),
                None, HASH_WORKERS, reference_keys=ref_keys if reference is not None else None)
            log.info("SELFTEST mesmo vídeo: %s", {k: v for k, v in sv_stats.items() if k != "errors"})
        byte_groups = build_byte_groups(byte_entries, b_idx, b_md5)
        log.info("SELFTEST bytes: %s", {k: v for k, v in byte_stats.items() if k != "errors"})
    results, errors_by_idx, _ = hash_files(entries, None, HASH_WORKERS)
    images_data = [r for r in results if r is not None]
    log.info("SELFTEST hash: %d ok, %d erros em %.1fs", len(images_data), len(errors_by_idx), time.time() - t0)
    groups_idx = find_similar_groups([h for (_, h, _) in images_data], SIMILARITY_THRESHOLD)
    if reference is not None:
        groups_idx = filter_groups_for_reference(groups_idx, images_data, ref_keys)
    stats = [entries[i][1:] for i, r in enumerate(results) if r is not None]
    md5_by_idx, _ = md5_for_groups(images_data, stats, groups_idx, None, HASH_WORKERS)
    if confirm_similar is None:
        confirm_similar = CONFIRM_SIMILAR
    confirm_stats = None
    dhash_by_idx = None
    if confirm_similar and groups_idx:
        dhash_by_idx, _ = dhash_for_groups(images_data, stats, groups_idx, md5_by_idx, None, HASH_WORKERS)
        groups_idx, confirm_stats = confirm_similar_groups(
            images_data, groups_idx, md5_by_idx, dhash_by_idx, SIMILARITY_THRESHOLD, DHASH_THRESHOLD)
        if reference is not None:
            groups_idx = filter_groups_for_reference(groups_idx, images_data, ref_keys)
        log.info("SELFTEST confirmação: %s", confirm_stats)
    if same_photo is None:
        same_photo = SAME_PHOTO_ENABLED
    class_by_idx, suspect_ids, sp_stats = {}, set(), None
    if same_photo and groups_idx:
        class_by_idx, suspect_ids, sp_stats, _ = same_photo_stage(
            images_data, stats, groups_idx, md5_by_idx, dhash_by_idx, None, None, HASH_WORKERS)
        log.info("SELFTEST mesma foto: %s", sp_stats)
    class_by_path = {images_data[i][0]: c for i, c in class_by_idx.items()}
    groups = build_groups(images_data, groups_idx, md5_by_idx)
    n_ident = 0
    for g in groups:
        counts = {}
        for (_, _, m) in g:
            counts[m] = counts.get(m, 0) + 1
        n_ident += sum(1 for (_, _, m) in g if counts[m] > 1)
    summary = (f"SELFTEST OK: {len(entries)} arquivos, {len(images_data)} hashes, "
               f"{len(errors_by_idx)} erros, {len(groups)} grupos, {n_ident} idênticas, "
               f"{time.time() - t0:.1f}s")
    if confirm_stats is not None:
        summary += (f" | CONF(dhash<={DHASH_THRESHOLD}): {confirm_stats['groups_dropped']} grupos e "
                    f"{confirm_stats['images_dropped']} imagens descartados pela confirmação, "
                    f"{confirm_stats['groups_split']} divididos, "
                    f"{confirm_stats['pairs_rejected_dhash']} pares rejeitados, "
                    f"{confirm_stats['images_degenerate']} degeneradas")
    if sp_stats is not None:
        rej = ", ".join(f"{k} {v}" for k, v in sorted(sp_stats["rejected"].items())) or "nenhum"
        summary += (f" | MESMA FOTO: {sp_stats['classes']} classes, {sp_stats['images_in_classes']} imagens, "
                    f"{sp_stats['classes_suspect']} suspeitas, {sp_stats['pairs_hash_rule']} candidatos, "
                    f"{sp_stats['pairs_confirmed']} confirmados, rejeitados: {rej}")
    if reference is not None:
        # Simula a seleção automática (mesmas funções puras da interface)
        mtime_by_path = {fp: (mt / 1e9 if mt is not None else float("inf"))
                         for (fp, _, mt) in entries}
        size_by_path = {fp: sz for (fp, sz, _) in entries}
        n_protected = n_sel_ident = n_sel_simil = n_sel_same = 0
        for g in groups:
            counts = {}
            for (_, _, m) in g:
                counts[m] = counts.get(m, 0) + 1
            images = []
            for (fp, _, m) in g:
                try:
                    with Image.open(fp) as im:
                        pixels = im.size[0] * im.size[1]
                except Exception:
                    pixels = None
                cid = class_by_path.get(fp)
                images.append({'filepath': fp, 'md5': m, 'mtime': mtime_by_path.get(fp, float("inf")),
                               'is_reference': cache_key(fp) in ref_keys,
                               'pixels': pixels, 'size': size_by_path.get(fp),
                               'same_photo': cid, 'same_photo_suspect': cid in suspect_ids})
            n_protected += sum(1 for im in images if im['is_reference'])
            sel_i = plan_identical_selection(images)
            sel_s = plan_similar_selection(images, counts)
            sel_p = plan_same_photo_selection(images)
            for i in sel_i + sel_s + sel_p:
                assert not images[i]['is_reference'], \
                    f"BUG: imagem da referência selecionada: {images[i]['filepath']}"
            n_sel_ident += len(sel_i)
            n_sel_simil += len(sel_s)
            n_sel_same += len(sel_p)
        summary += (f" | REF: {len(groups)} grupos, {n_protected} protegidas, "
                    f"{n_sel_ident} selecionáveis (idênticas), {n_sel_simil} (semelhantes)")
        if sp_stats is not None:
            summary += f", {n_sel_same} (mesma foto)"
    if byte_stats is not None:
        n_sel_bytes = 0
        byte_mtime = {fp: mt / 1e9 for (fp, _, mt) in byte_entries if mt is not None}
        for g in byte_groups:
            images = [{'filepath': fp, 'md5': m, 'is_reference': cache_key(fp) in ref_keys,
                       'mtime': byte_mtime.get(fp, float("inf"))} for (fp, _, m) in g]
            sel = plan_identical_selection(images)
            for i in sel:
                assert not images[i]['is_reference'], \
                    f"BUG: arquivo da referência selecionado: {images[i]['filepath']}"
            n_sel_bytes += len(sel)
        summary += (f" | BYTES: {byte_stats['files']} arquivos, {byte_stats['groups']} grupos, "
                    f"{byte_stats['identical_files']} idênticos, {n_sel_bytes} selecionáveis, "
                    f"{format_bytes(byte_stats['bytes_read'])} lidos, {len(byte_stats['errors'])} erros")
        if sv_stats is not None:
            summary += (f" | MESMO VIDEO: {sv_stats['merged_groups']} grupos, "
                        f"{sv_stats['files_in_merged']} arquivos, {sv_stats['hashed']} hashes de fluxos")
    log.info(summary)
    return summary


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        try:
            reference = None
            confirm_similar = None
            same_photo = None
            videos = others = False
            same_video = None
            extra = sys.argv[3:]
            while extra:
                opt = extra.pop(0)
                if opt == "--ref" and extra:
                    reference = extra.pop(0)
                elif opt == "--no-confirm":
                    confirm_similar = False
                elif opt == "--no-same-photo":
                    same_photo = False
                elif opt == "--same-photo":
                    same_photo = True
                elif opt == "--videos":
                    videos = True
                elif opt == "--others":
                    others = True
                elif opt == "--no-same-video":
                    same_video = False
                else:
                    raise ValueError(f"Opção desconhecida: {opt} (uso: --selftest PASTA "
                                     "[--ref REFERENCIA] [--no-confirm] [--same-photo | --no-same-photo] "
                                     "[--videos] [--others] [--no-same-video])")
            result = run_selftest(sys.argv[2], reference, confirm_similar, same_photo, videos, others,
                                  same_video)
            code = 0
        except (FileNotFoundError, ValueError) as e:
            log.error("SELFTEST: %s", e)
            result = f"SELFTEST FALHOU: {e}"
            code = 2
        except Exception:
            log.exception("SELFTEST FALHOU")
            result = "SELFTEST FALHOU (veja o log)"
            code = 1
        try:
            print(result)
        except Exception:
            pass
        sys.exit(code)

    setup_logging()
    log.info("ImageCleaner iniciado (Python %s)", sys.version.split()[0])
    set_windows_app_identity()
    root = tk.Tk()
    root.report_callback_exception = _report_callback_exception
    app = ImageCleaner(root)
    root.protocol("WM_DELETE_WINDOW", app._on_main_window_close)
    root.mainloop()
