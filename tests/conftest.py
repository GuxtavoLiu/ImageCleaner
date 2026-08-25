"""
Infraestrutura compartilhada dos testes do ImageCleaner.

- build_single_fixture: gera uma pasta determinística de imagens sintéticas
  (idênticas, semelhantes e não relacionadas) com mtimes fixos.
- build_reference_fixture: gera o par de pastas (alvo + referência) para o
  modo de comparação de duas pastas.
- run_pipeline / snapshot_single_mode: rodam o pipeline puro do main.py e
  produzem uma estrutura serializável (grupos, rótulos, seleções) usada pelo
  teste de regressão contra o snapshot dourado.
- oracle_identical_selection / oracle_similar_selection: cópia fiel da lógica
  de seleção da versão pré-refatoração (linhas 1684-1701 e 1717-1727 do
  main.py original), usada como oráculo de equivalência.
"""
import json
import os
import shutil
import sys

import numpy as np
import pytest
from PIL import Image

# Permite importar o main.py da raiz do projeto
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as ic  # noqa: E402

# Base fixa de mtime (2020-09-13): valores absolutos e determinísticos
T0 = 1_600_000_000

# Threads reduzidas nos testes: fixtures pequenas, sem ganho com 8 threads
WORKERS = 2


# Seeds por "motivo" de imagem: layouts de retângulos bem diferentes entre si
# (gradientes suaves não servem: o phash deles degenera e tudo se agrupa).
_KIND_SEEDS = {"A": 11, "B": 47, "C": 83}


def _make_image(kind):
    """Imagem sintética determinística 128x128 RGB: retângulos em posições
       pseudo-aleatórias com seed fixa por motivo. Estrutura grande o
       suficiente para sobreviver a recompressão JPEG (phash estável)."""
    rng = np.random.RandomState(_KIND_SEEDS[kind])
    w = h = 128
    arr = np.full((h, w), int(rng.randint(0, 256)), dtype=np.uint8)
    for _ in range(12):
        x0, y0 = rng.randint(0, w - 32), rng.randint(0, h - 32)
        rw, rh = rng.randint(24, 64), rng.randint(24, 64)
        arr[y0:y0 + rh, x0:x0 + rw] = rng.randint(0, 256)
    return Image.fromarray(arr).convert("RGB")


def _write(root, relpath, kind=None, quality=95, copy_of=None, mtime=None):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if copy_of is not None:
        shutil.copyfile(os.path.join(root, copy_of), path)
    else:
        img = _make_image(kind)
        if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg"):
            img.save(path, quality=quality)
        else:
            img.save(path)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def build_single_fixture(root):
    """
    Pasta única com:
      - a.jpg + a_copy.jpg (idênticas) + a_q30.jpg e a_q60.jpg (semelhantes)
      - sub/b.png + sub/b_copy.png (idênticas, mtime empatado de propósito)
      - c.jpg (não relacionada, não deve agrupar com nada)
    """
    os.makedirs(root, exist_ok=True)
    _write(root, "a.jpg", kind="A", quality=95, mtime=T0 + 100)
    _write(root, "a_copy.jpg", copy_of="a.jpg", mtime=T0 + 200)
    _write(root, "a_q30.jpg", kind="A", quality=30, mtime=T0 + 50)
    _write(root, "a_q60.jpg", kind="A", quality=60, mtime=T0 + 250)
    _write(root, "c.jpg", kind="C", quality=95, mtime=T0 + 400)
    _write(root, os.path.join("sub", "b.png"), kind="B", mtime=T0 + 300)
    _write(root, os.path.join("sub", "b_copy.png"),
           copy_of=os.path.join("sub", "b.png"), mtime=T0 + 300)
    return root


def build_reference_fixture(base):
    """
    Par (alvo, referência) para o modo de comparação:
      alvo/dup_a.jpg      idêntica a ref/acervo_a.jpg (cópia bit a bit)
      alvo/quase_a.jpg    semelhante a ref/acervo_a.jpg (quality diferente)
      alvo/interna1.jpg   idêntica a alvo/interna2.jpg (dupla SÓ do alvo; é
                          semelhante, não idêntica, a ref/acervo_b.jpg)
      alvo/unica.jpg      sem par em lugar nenhum (xadrez)
      ref/acervo_a.jpg    original protegido
      ref/acervo_b.jpg + ref/acervo_b2.jpg  dupla SÓ da referência
    """
    alvo = os.path.join(base, "alvo")
    ref = os.path.join(base, "ref")
    os.makedirs(alvo, exist_ok=True)
    os.makedirs(ref, exist_ok=True)
    _write(ref, "acervo_a.jpg", kind="A", quality=95, mtime=T0 + 10)
    shutil.copyfile(os.path.join(ref, "acervo_a.jpg"),
                    os.path.join(alvo, "dup_a.jpg"))
    os.utime(os.path.join(alvo, "dup_a.jpg"), (T0 + 500, T0 + 500))
    _write(alvo, "quase_a.jpg", kind="A", quality=30, mtime=T0 + 600)
    # quality 80: mesmo desenho de acervo_b (semelhante), mas MD5 diferente
    _write(alvo, "interna1.jpg", kind="B", quality=80, mtime=T0 + 700)
    _write(alvo, "interna2.jpg", copy_of="interna1.jpg", mtime=T0 + 800)
    _write(alvo, "unica.jpg", kind="C", quality=95, mtime=T0 + 900)
    _write(ref, "acervo_b.jpg", kind="B", quality=95, mtime=T0 + 20)
    _write(ref, "acervo_b2.jpg", copy_of="acervo_b.jpg", mtime=T0 + 30)
    return alvo, ref


def build_confirm_fixture(root):
    """
    Pasta para a confirmação por segundo hash e para hashes degenerados:
      - alpha_text.png (RGBA transparente com barras pretas) + alpha_text_copy.png
        (idênticas): antes da composição sobre branco tinham phash zero;
      - black.png e black2.png: pretas, bytes diferentes (degeneradas: NÃO devem
        ficar juntas com a confirmação ligada);
      - white.jpg: branca lisa (degenerada);
      - fake_a.png / fake_b.png: "falso semelhante" (phash próximo, dhash longe);
      - true_a.jpg / true_a_q30.jpg: recompressão (phash e dhash próximos).
    """
    os.makedirs(root, exist_ok=True)
    alpha = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    a = np.zeros((128, 128, 4), dtype=np.uint8)
    for x0 in (10, 40, 70, 100):
        a[20:110, x0:x0 + 12] = (0, 0, 0, 255)
    a[60:70, 10:118] = (0, 0, 0, 255)
    alpha = Image.fromarray(a, "RGBA")
    alpha.save(os.path.join(root, "alpha_text.png"))
    os.utime(os.path.join(root, "alpha_text.png"), (T0 + 10, T0 + 10))
    _write(root, "alpha_text_copy.png", copy_of="alpha_text.png", mtime=T0 + 20)
    Image.new("RGB", (128, 128), (0, 0, 0)).save(os.path.join(root, "black.png"))
    Image.new("RGB", (96, 96), (0, 0, 0)).save(os.path.join(root, "black2.png"))
    Image.new("RGB", (128, 128), (255, 255, 255)).save(os.path.join(root, "white.jpg"), quality=95)
    _make_fake_pair(root)
    _write(root, "true_a.jpg", kind="A", quality=95, mtime=T0 + 100)
    _write(root, "true_a_q30.jpg", kind="A", quality=30, mtime=T0 + 200)
    for name, t in (("black.png", 30), ("black2.png", 40), ("white.jpg", 50),
                    ("fake_a.png", 60), ("fake_b.png", 70)):
        os.utime(os.path.join(root, name), (T0 + t, T0 + t))
    return root


def _make_fake_pair(root, amp=15):
    """Par de imagens diferentes com phash próximo e dhash distante: mesma
       estrutura grossa de luz/sombra (gradiente + disco) somada a faixas
       verticais de polaridade OPOSTA. O phash (DCT baixa frequência) vê a
       estrutura compartilhada; o dhash (gradiente entre colunas vizinhas)
       vê as faixas invertidas."""
    h, w = 128, 144
    yy, xx = np.mgrid[0:h, 0:w]
    base = 128 + 60 * (yy / h - 0.5) * 2
    base = base - 70 * (((xx - 90) ** 2 + (yy - 50) ** 2) < 35 ** 2)
    stripes = np.array([+1, +1, -1, -1, +1, +1, -1, -1, +1])[xx * 9 // w] * amp
    for name, sign in (("fake_a.png", +1), ("fake_b.png", -1)):
        arr = np.clip(base + sign * stripes, 0, 255).astype(np.uint8)
        Image.fromarray(arr).convert("RGB").save(os.path.join(root, name))


def _confirm_stage(images_data, stats, groups_idx, md5_by_idx):
    dhash_by_idx, _ = ic.dhash_for_groups(images_data, stats, groups_idx, md5_by_idx, None, WORKERS)
    groups_idx, cstats = ic.confirm_similar_groups(
        images_data, groups_idx, md5_by_idx, dhash_by_idx,
        ic.SIMILARITY_THRESHOLD, ic.DHASH_THRESHOLD)
    return groups_idx, cstats


def run_pipeline(root, confirm=False):
    """Pipeline puro completo do modo de uma pasta, como o run_selftest faz.
       confirm=True acrescenta a confirmação por segundo hash."""
    entries = ic.list_image_files(root, True, ic.VALID_EXTENSIONS)
    results, errors_by_idx, _ = ic.hash_files(entries, None, WORKERS)
    images_data = [r for r in results if r is not None]
    hashes = [h for (_, h, _) in images_data]
    groups_idx = ic.find_similar_groups(hashes, ic.SIMILARITY_THRESHOLD)
    stats = [entries[i][1:] for i, r in enumerate(results) if r is not None]
    md5_by_idx, _ = ic.md5_for_groups(images_data, stats, groups_idx, None, WORKERS)
    if confirm:
        groups_idx, _ = _confirm_stage(images_data, stats, groups_idx, md5_by_idx)
    groups = ic.build_groups(images_data, groups_idx, md5_by_idx)
    return entries, images_data, groups


def group_to_images_dicts(group):
    """Converte um grupo do build_groups nos dicts que a seleção consome
       (mesmos campos de initialize_all_groups + métricas de qualidade)."""
    out = []
    for (fp, _, md5_val) in group:
        try:
            with Image.open(fp) as im:
                pixels = im.size[0] * im.size[1]
        except Exception:
            pixels = None
        out.append({
            'filepath': fp,
            'md5': md5_val,
            'mtime': os.path.getmtime(fp),
            'is_reference': False,
            'pixels': pixels,
            'size': os.path.getsize(fp),
        })
    return out


def md5_count_of(group):
    counts = {}
    for (_, _, m) in group:
        counts[m] = counts.get(m, 0) + 1
    return counts


def oracle_identical_selection(images):
    """Cópia fiel da lógica original de select_identical_images."""
    selected = []
    md5_groups = {}
    for i, img in enumerate(images):
        md5_groups.setdefault(img['md5'], []).append(i)
    for md5, idxs in md5_groups.items():
        if len(idxs) > 1:
            ordered = sorted(idxs, key=lambda i: images[i]['mtime'])
            selected.extend(ordered[1:])
    return selected


def oracle_similar_selection(images, md5_count):
    """Cópia fiel da lógica original de select_similar_images."""
    candidates = [i for i, img in enumerate(images) if md5_count[img['md5']] == 1]
    if len(candidates) > 1:
        candidates.sort(key=lambda i: images[i]['mtime'])
        return candidates[1:]
    return []


def snapshot_single_mode(root, confirm=False, priority=None):
    """
    Estrutura serializável do resultado completo do modo de uma pasta:
    grupos (caminhos relativos + rótulo Idêntica/Semelhante, na ordem) e as
    seleções automáticas (como listas ordenadas de caminhos relativos).
    """
    _, _, groups = run_pipeline(root, confirm=confirm)

    def rel(fp):
        return os.path.relpath(fp, root).replace("\\", "/")

    snap_groups = []
    sel_identical = []
    sel_similar = []
    for group in groups:
        counts = md5_count_of(group)
        snap_groups.append([
            [rel(fp), "Identica" if counts[m] > 1 else "Semelhante"]
            for (fp, _, m) in group
        ])
        images = group_to_images_dicts(group)
        # Usa o mecanismo de seleção vigente no main.py: as funções puras se
        # existirem (pós-refatoração), senão o oráculo (pré-refatoração).
        if hasattr(ic, "plan_identical_selection"):
            ident = ic.plan_identical_selection(images)
            simil = (ic.plan_similar_selection(images, counts, priority) if priority
                     else ic.plan_similar_selection(images, counts))
        else:
            ident = oracle_identical_selection(images)
            simil = oracle_similar_selection(images, counts)
        sel_identical.extend(sorted(rel(images[i]['filepath']) for i in ident))
        sel_similar.extend(sorted(rel(images[i]['filepath']) for i in simil))
    return {
        "groups": snap_groups,
        "selected_identical": sel_identical,
        "selected_similar": sel_similar,
    }


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GOLDEN_PATH = os.path.join(TESTS_DIR, "golden_single_mode.json")
GOLDEN_CONFIRM_PATH = os.path.join(TESTS_DIR, "golden_single_mode_confirm.json")
GOLDEN_FIXTURE_CONFIRM_PATH = os.path.join(TESTS_DIR, "golden_confirm_fixture.json")


GOLDEN_QUALITY_PATH = os.path.join(TESTS_DIR, "golden_single_mode_quality.json")
GOLDEN_FIXTURE_CONFIRM_QUALITY_PATH = os.path.join(TESTS_DIR, "golden_confirm_fixture_quality.json")
LEGACY_PRIORITY = ("mtime",)   # regra original: mantém a mais antiga


def generate_golden(tmp_root, confirm=False, path=GOLDEN_PATH, builder=build_single_fixture,
                    priority=None):
    """Gera um snapshot dourado (rodar UMA vez, de forma deliberada)."""
    builder(tmp_root)
    snap = snapshot_single_mode(tmp_root, confirm=confirm, priority=priority)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)
    return snap


@pytest.fixture(autouse=True)
def _isolated_app_data(tmp_path, monkeypatch):
    """Nenhum teste lê ou grava o settings.json/relatórios reais do usuário."""
    monkeypatch.setattr(ic, "get_settings_path", lambda: str(tmp_path / "settings_test.json"))
    monkeypatch.setattr(ic, "get_reports_dir", lambda: str(tmp_path / "relatorios_test"))


@pytest.fixture(scope="session")
def tk_root():
    """Um único interpretador Tk por SESSÃO de testes: criar um segundo Tk()
       no mesmo processo após destruir o primeiro falha de forma intermitente
       no Windows ("invalid command name tcl_findLibrary")."""
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"Tk indisponível: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


# ---------------------------------------------------------------------------
# Fixture "Mesma foto": base com textura (o dhash precisa de gradientes
# estáveis) e variantes que devem (ou não) ser reconhecidas como a mesma foto.
# ---------------------------------------------------------------------------

def _textured_base(w=320, h=240, seed=5):
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    arr = 90 + 60 * (xx / w) + 40 * (yy / h) + rng.normal(0, 12, (h, w))
    for _ in range(10):
        x0, y0 = rng.randint(0, w - 40), rng.randint(0, h - 40)
        arr[y0:y0 + rng.randint(20, 60), x0:x0 + rng.randint(20, 60)] += rng.randint(-70, 70)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    rgb = np.stack([arr, np.clip(arr * 0.9, 0, 255).astype(np.uint8),
                    np.clip(arr * 1.1, 0, 255).astype(np.uint8)], axis=-1)
    return Image.fromarray(rgb, "RGB")


def _exif(dto=None, make="Canon", model="EOS", orientation=None, subsec=None):
    ex = Image.Exif()
    if make:
        ex[271] = make
    if model:
        ex[272] = model
    if orientation:
        ex[274] = orientation
    if dto:
        ex.get_ifd(0x8769)[0x9003] = dto
    if subsec:
        ex.get_ifd(0x8769)[0x9291] = subsec
    return ex.tobytes()


def build_same_photo_fixture(root):
    """
    p.jpg (320x240, q95, EXIF 2020:09:13 10:00:00) e variantes:
      mesma foto: p_half (50%, sem EXIF), p_q30 (recompressão), p_exif_edit
        (pixels de p_q30, data +3 anos: edição de data, não veta);
      NÃO mesma foto: p_border (borda 10 px: proporção), p_crop (recorte 10%),
        p_rot (pixels rotacionados + Orientation=8), p_flip (espelhada),
        p_sticker (quadrado preto 40x40: pior região), p_exif_burst (pixels de
        p_q30, data +2 s: veto EXIF), p_tiny (48 px: dimensão mínima).
    """
    os.makedirs(root, exist_ok=True)
    base = _textured_base()
    base.save(os.path.join(root, "p.jpg"), quality=95, exif=_exif("2020:09:13 10:00:00"))
    base.resize((160, 120), Image.LANCZOS).save(os.path.join(root, "p_half.jpg"), quality=90)
    base.save(os.path.join(root, "p_q30.jpg"), quality=30, exif=_exif("2020:09:13 10:00:00"))
    base.save(os.path.join(root, "p_exif_edit.jpg"), quality=30, exif=_exif("2023:09:13 10:00:00"))
    base.save(os.path.join(root, "p_exif_burst.jpg"), quality=30, exif=_exif("2020:09:13 10:00:02"))
    bordered = Image.new("RGB", (340, 260), (255, 255, 255))
    bordered.paste(base, (10, 10))
    bordered.save(os.path.join(root, "p_border.jpg"), quality=95)
    base.crop((16, 12, 304, 228)).save(os.path.join(root, "p_crop.jpg"), quality=95)
    base.transpose(Image.ROTATE_90).save(os.path.join(root, "p_rot.jpg"), quality=95,
                                         exif=_exif("2020:09:13 10:00:00", orientation=8))
    base.transpose(Image.FLIP_LEFT_RIGHT).save(os.path.join(root, "p_flip.jpg"), quality=95)
    stick = base.copy()
    stick.paste((0, 0, 0), (190, 125, 250, 185))
    stick.save(os.path.join(root, "p_sticker.jpg"), quality=95)
    base.resize((48, 36), Image.LANCZOS).save(os.path.join(root, "p_tiny.jpg"), quality=95)
    for k, name in enumerate(sorted(os.listdir(root))):
        os.utime(os.path.join(root, name), (T0 + 10 * k, T0 + 10 * k))
    return root
