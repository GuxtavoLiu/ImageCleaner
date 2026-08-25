"""Status "Mesma foto": provas por par, classes, cache, estágio e seleção."""
import os
import time

import imagehash
import numpy as np
import pytest

import main as ic
from conftest import build_same_photo_fixture, WORKERS

F = None  # features por nome, preenchidas pela fixture de módulo


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    root = build_same_photo_fixture(str(tmp_path_factory.mktemp("sp")))
    feats = {n: ic.same_photo_features(os.path.join(root, n)) for n in os.listdir(root)}
    return root, feats


def _pc(x):
    return bin(x).count("1")


# --------------------------------------------------------------------------
# Fixture: quem é candidato pelos hashes e quem passa nas provas
# --------------------------------------------------------------------------

def test_fixture_hashes_e_provas(fx):
    root, feats = fx
    ph = {n: ic.hash_to_int(ic.compute_phash(os.path.join(root, n))) for n in feats}
    dh = {n: ic.hash_to_int(ic.compute_dhash(os.path.join(root, n))) for n in feats}
    def gate(a, b):
        return _pc(ph[a] ^ ph[b]) <= ic.SAME_PHOTO_PHASH_MAX and _pc(dh[a] ^ dh[b]) <= ic.SAME_PHOTO_DHASH_MAX
    # mesma foto de verdade: passa na porta e nas provas
    for other in ("p_half.jpg", "p_q30.jpg", "p_exif_edit.jpg"):
        assert gate("p.jpg", other), other
        assert ic.same_photo_pair_ok(feats["p.jpg"], feats[other]) == (True, "ok"), other
    # rotação nos pixels e espelho: nem candidatas
    assert _pc(ph["p.jpg"] ^ ph["p_rot.jpg"]) > ic.SAME_PHOTO_PHASH_MAX
    assert not gate("p.jpg", "p_flip.jpg")
    # rejeições com motivo
    assert ic.same_photo_pair_ok(feats["p.jpg"], feats["p_border.jpg"])[1] == "proporcao"
    assert ic.same_photo_pair_ok(feats["p.jpg"], feats["p_tiny.jpg"])[1] == "dimensao"
    assert ic.same_photo_pair_ok(feats["p.jpg"], feats["p_exif_burst.jpg"])[1] == "exif"
    ok, reason = ic.same_photo_pair_ok(feats["p.jpg"], feats["p_sticker.jpg"])
    assert not ok and reason in ("ncc", "bloco")
    ok, reason = ic.same_photo_pair_ok(feats["p.jpg"], feats["p_crop.jpg"])
    assert not ok or not gate("p.jpg", "p_crop.jpg")
    # conteúdo pixel-idêntico com EXIF diferente: o veto não se aplica
    assert ic.same_photo_pair_ok(feats["p_q30.jpg"], feats["p_exif_burst.jpg"]) == (True, "ok")
    # EXIF lido do sub-IFD
    assert feats["p.jpg"]["exif"]["dto"] == "2020:09:13 10:00:00" and feats["p.jpg"]["exif"]["make"] == "Canon"
    assert feats["p_half.jpg"]["exif"]["dto"] is None
    assert feats["p.jpg"]["dims"] == (320, 240) and feats["p_rot.jpg"]["dims"] == (240, 320)


# --------------------------------------------------------------------------
# Funções puras pequenas
# --------------------------------------------------------------------------

def test_aspect_close():
    assert ic.aspect_close((4608, 3456), (1280, 960))
    assert ic.aspect_close((1000, 750), (1005, 750))          # 0,5%
    assert not ic.aspect_close((1184, 864), (1280, 960))      # 3%
    assert not ic.aspect_close((720, 1280), (1280, 720))      # invertida: pixels brutos diferentes
    assert not ic.aspect_close(None, (1, 1)) and not ic.aspect_close((0, 5), (1, 1))


def test_exif_veto():
    e = lambda dto=None, sub=None, uid=None: {"dto": dto, "subsec": sub, "uid": uid}
    assert not ic.exif_veto(e(), e(), False)
    assert not ic.exif_veto(e("2020:01:01 10:00:00"), e(), False)                     # um só: sem veto
    assert ic.exif_veto(e("2020:01:01 10:00:00"), e("2020:01:01 10:00:02"), False)   # rajada
    assert ic.exif_veto(e("2020:01:01 10:00:00"), e("2020:01:01 10:59:00"), False)   # dentro da janela
    assert not ic.exif_veto(e("2020:01:01 10:00:00"), e("2023:01:01 10:00:00"), False)  # edição de data
    assert not ic.exif_veto(e("2020:01:01 10:00:00"), e("2020:01:01 10:00:02"), True)   # pixel-idêntico
    assert ic.exif_veto(e("x", "1"), e("y", "2"), False)                              # datas ilegíveis e diferentes
    assert ic.exif_veto(e("2020:01:01 10:00:00", "10"), e("2020:01:01 10:00:00", "20"), False)  # subsec
    assert not ic.exif_veto(e("2020:01:01 10:00:00", "10"), e("2020:01:01 10:00:00", None), False)
    assert ic.exif_veto(e(uid="a"), e(uid="b"), False)
    assert not ic.exif_veto(e(uid="a"), e(uid="a"), False)


def test_gray_array_normalize_e_metricas():
    n = ic.SAME_PHOTO_GRAY_SIZE
    assert ic.gray_array(b"\x00" * (n * n - 1)) is None                  # blob do tamanho errado
    flat = ic.gray_array(bytes([100]) * (n * n))
    assert ic.normalize_gray(flat) is None                                # quase lisa: sem prova
    rng = np.random.RandomState(0)
    a = rng.randint(0, 256, (n, n)).astype(np.float32)
    na = ic.normalize_gray(a)
    assert abs(ic.ncc_global(na, na) - 1.0) < 1e-5 and ic.block_max_error(na, na) < 1e-6
    b = a + rng.normal(0, 3, (n, n)).astype(np.float32)                  # recompressão leve
    nb = ic.normalize_gray(b)
    assert ic.ncc_global(na, nb) > 0.99 and ic.block_max_error(na, nb) < 0.05
    c = a.copy(); c[0:16, 0:16] = 255 - c[0:16, 0:16]                    # um bloco alterado
    nc = ic.normalize_gray(c)
    assert ic.ncc_global(na, nc) > 0.8                                   # a média esconde
    assert ic.block_max_error(na, nc) > ic.SAME_PHOTO_BLOCK_ERR_MAX      # a pior região não
    other = ic.normalize_gray(rng.randint(0, 256, (n, n)).astype(np.float32))
    assert ic.ncc_global(na, other) < 0.2 and ic.ncc_gradient(na, other) < 0.3
    assert ic.ncc_gradient(na, nb) > 0.8


def test_pair_ok_limiares_exatos(monkeypatch):
    n = ic.SAME_PHOTO_GRAY_SIZE
    rng = np.random.RandomState(1)
    a = rng.randint(0, 256, (n, n)).astype(np.uint8)
    fa = {"dims": (400, 300), "exif": {}, "gray": a.tobytes()}
    fb = {"dims": (800, 600), "exif": {}, "gray": a.tobytes()}
    assert ic.same_photo_pair_ok(fa, fb) == (True, "ok")
    assert ic.same_photo_pair_ok(fa, None)[1] == "sem_prova"
    assert ic.same_photo_pair_ok(fa, {"dims": None, "gray": a.tobytes()})[1] == "dimensao"
    # limiar exato de NCC: com ncc_min=1.0001 nada passa
    assert ic.same_photo_pair_ok(fa, fb, {"ncc_min": 1.0001})[1] == "ncc"
    assert ic.same_photo_pair_ok(fa, fb, {"block_err_max": -1})[1] == "bloco"
    assert ic.same_photo_pair_ok(fa, fb, {"grad_ncc_min": 1.5})[1] == "gradiente"
    assert ic.same_photo_pair_ok(fa, fb, {"min_side": 500})[1] == "dimensao"


# --------------------------------------------------------------------------
# Candidatos, classes, ligação completa, suspeitas
# --------------------------------------------------------------------------

def _H(v):
    return imagehash.hex_to_hash(f"{v:016x}")


BASE = 0xF0F0F0F0F0F0F0F0


def _flip(v, nbits, offset=0):
    for b in range(offset, offset + nbits):
        v ^= 1 << b
    return v


def test_candidate_pairs():
    images_data = [("a.jpg", _H(BASE), "m1"), ("b.jpg", _H(_flip(BASE, 2)), "m2"),
                   ("c.jpg", _H(_flip(BASE, 4)), "m3"), ("d.jpg", _H(BASE), "m1"),
                   ("e.jpg", _H(BASE), "ERR:e.jpg"), ("f.jpg", _H(0), "m6")]
    md5 = {i: d[2] for i, d in enumerate(images_data)}
    dh = {0: 0, 1: 1, 2: 0, 3: 0, 4: 0}          # f sem dhash
    pairs, st = ic.same_photo_candidate_pairs(images_data, [[0, 1, 2, 3, 4, 5]], md5, dh)
    assert pairs == [(0, 0, 1), (0, 1, 2), (0, 1, 3)]   # a~b, b~c (2 bits), b~d (cópia de a); a~c 4 bits; e ERR; f degenerado
    assert st["pairs_hash_rule"] == 3
    # grupo só de idênticas é pulado
    assert ic.same_photo_candidate_pairs(images_data, [[0, 3]], md5, dh)[0] == []


def _feats(seed, dims=(800, 600), exif=None, alter_block=False):
    n = ic.SAME_PHOTO_GRAY_SIZE
    rng = np.random.RandomState(seed)
    a = rng.randint(0, 256, (n, n)).astype(np.uint8)
    if alter_block:
        a[0:16, 0:16] = 255 - a[0:16, 0:16]
    return {"dims": dims, "exif": exif or {}, "gray": a.tobytes()}


def test_classes_ligacao_completa_e_md5():
    # grupo [0..4]: 0,1,2 mesma foto (seed 7); 3 cópia bit a bit de 0; 4 outra foto (seed 9)
    images_data = [(f"{i}.jpg", _H(BASE), m) for i, m in enumerate(["m0", "m1", "m2", "m0", "m4"])]
    stats = [(1000, 1)] * 5
    md5 = {i: d[2] for i, d in enumerate(images_data)}
    feats = {0: _feats(7), 1: _feats(7), 2: _feats(7), 3: _feats(7), 4: _feats(9)}
    pairs = [(0, i, j) for i in range(5) for j in range(i + 1, 5) if md5[i] != md5[j]]
    cls, suspect, sp = ic.same_photo_classes([[0, 1, 2, 3, 4]], pairs, images_data, stats, md5, feats)
    assert cls == {0: 0, 1: 0, 2: 0, 3: 0} and 4 not in cls
    assert sp["classes"] == 1 and sp["images_in_classes"] == 4 and sp["rejected"].get("ncc") == 4
    # cadeia: 0~1 e 1~2 confirmados, 0~2 nem candidato: ligação completa deixa 2 de fora
    feats = {0: _feats(7), 1: _feats(7), 2: _feats(7)}
    images_data = [(f"{i}.jpg", _H(BASE), f"m{i}") for i in range(3)]
    md5 = {i: f"m{i}" for i in range(3)}
    pairs = [(0, 0, 1), (0, 1, 2)]                       # (0, 2) não é candidato
    cls, _, sp = ic.same_photo_classes([[0, 1, 2]], pairs, images_data, [(1, 1)] * 3, md5, feats)
    assert cls == {0: 0, 1: 0} and sp["unions_refused_clique"] == 1
    # classe só de idênticas não é devolvida
    images_data = [("a", _H(BASE), "m"), ("b", _H(BASE), "m")]
    cls, _, _ = ic.same_photo_classes([[0, 1]], [], images_data, [(1, 1)] * 2, {0: "m", 1: "m"}, {})
    assert cls == {}


def test_classes_suspeita_de_ampliacao():
    # 0: 800x600 com 240 KB (0,5 bpp); 1: 3200x2400 "ampliada" com 480 KB (0,06 bpp)
    images_data = [("a.jpg", _H(BASE), "m0"), ("b.jpg", _H(BASE), "m1")]
    stats = [(240_000, 1), (480_000, 1)]
    feats = {0: _feats(3, dims=(800, 600)), 1: _feats(3, dims=(3200, 2400))}
    cls, suspect, sp = ic.same_photo_classes([[0, 1]], [(0, 0, 1)], images_data, stats, {0: "m0", 1: "m1"}, feats)
    assert cls == {0: 0, 1: 0} and suspect == {0} and sp["classes_suspect"] == 1
    # sem ampliação: bpp proporcional
    stats = [(240_000, 1), (3_800_000, 1)]
    _, suspect, _ = ic.same_photo_classes([[0, 1]], [(0, 0, 1)], images_data, stats, {0: "m0", 1: "m1"}, feats)
    assert suspect == set()


def test_classes_adversarial_duas_meias_cliques():
    k = 400
    images_data = [(f"{i}.jpg", _H(BASE), f"m{i}") for i in range(k)]
    md5 = {i: f"m{i}" for i in range(k)}
    feats = {i: _feats(11) for i in range(k)}
    feats[0] = _feats(11, alter_block=True)             # 0 não liga com ninguém
    pairs = [(0, i, j) for i in range(k) for j in range(i + 1, k)]
    t0 = time.time()
    cls, _, sp = ic.same_photo_classes([list(range(k))], pairs, images_data, [(1, 1)] * k, md5, feats)
    assert time.time() - t0 < 20
    assert 0 not in cls and len(cls) == k - 1 and sp["classes"] == 1


def test_cancelamento():
    with pytest.raises(ic.ScanCancelled):
        ic.same_photo_candidate_pairs([("a", _H(BASE), "m")], [[0]], {0: "m"}, {}, cancel_check=lambda: True)


# --------------------------------------------------------------------------
# Cache em tabela própria
# --------------------------------------------------------------------------

def test_same_photo_cache(tmp_path, fx):
    root, feats = fx
    db = str(tmp_path / "cache.sqlite")
    hc = ic.HashCache(path=db)                       # a tabela de hashes convive no mesmo arquivo
    cache = ic.SamePhotoCache(path=db)
    assert cache.active
    fp = os.path.join(root, "p.jpg")
    cache.store(fp, 100, 200, feats["p.jpg"]); cache.flush()
    got = cache.lookup_many([(fp, 100, 200), (fp + "x", 1, 1)])
    assert set(got) == {fp} and got[fp]["dims"] == (320, 240) and got[fp]["exif"]["dto"] == "2020:09:13 10:00:00"
    assert got[fp]["gray"] == feats["p.jpg"]["gray"]
    assert cache.lookup_many([(fp, 101, 200)]) == {}   # tamanho mudou
    # blob de outro tamanho de miniatura é ignorado
    cache.conn.execute(f"UPDATE {cache.TABLE} SET gray_size = 32"); cache.conn.commit()
    assert cache.lookup_many([(fp, 100, 200)]) == {}
    cache.close(); hc.close()
    assert ic.SamePhotoCache(path=db).active


# --------------------------------------------------------------------------
# Estágio completo na fixture real (com e sem dhash pré-calculado)
# --------------------------------------------------------------------------

def _pipeline(root):
    entries = ic.list_image_files(root, True, ic.VALID_EXTENSIONS)
    results, _, _ = ic.hash_files(entries, None, WORKERS)
    images_data = [r for r in results if r is not None]
    stats = [entries[i][1:] for i, r in enumerate(results) if r is not None]
    groups_idx = ic.find_similar_groups([h for (_, h, _) in images_data], ic.SIMILARITY_THRESHOLD)
    md5_by_idx, _ = ic.md5_for_groups(images_data, stats, groups_idx, None, WORKERS)
    return images_data, stats, groups_idx, md5_by_idx


def test_same_photo_stage_na_fixture(fx, tmp_path):
    root, _ = fx
    images_data, stats, groups_idx, md5 = _pipeline(root)
    name = lambda i: os.path.basename(images_data[i][0])
    cls, suspect, sp, cancelled = ic.same_photo_stage(images_data, stats, groups_idx, md5, None,
                                                      None, None, WORKERS)
    assert not cancelled
    classes = {}
    for i, c in cls.items():
        classes.setdefault(c, set()).add(name(i))
    assert list(classes.values()) == [{"p.jpg", "p_half.jpg", "p_q30.jpg", "p_exif_edit.jpg"}]
    assert suspect == set()
    # mesmo resultado com o dhash vindo "da confirmação"
    dh, _ = ic.dhash_for_groups(images_data, stats, groups_idx, md5, None, WORKERS)
    cls2, _, _, _ = ic.same_photo_stage(images_data, stats, groups_idx, md5, dh, None, None, WORKERS)
    assert cls2 == cls
    # com cache: segunda chamada não recalcula provas
    cache = ic.SamePhotoCache(path=str(tmp_path / "sp.sqlite"))
    ic.same_photo_stage(images_data, stats, groups_idx, md5, dh, None, cache, WORKERS)
    calls = []
    orig = ic.same_photo_features
    ic.same_photo_features = lambda fp, size=None: calls.append(fp) or orig(fp, size)
    try:
        cls3, _, _, _ = ic.same_photo_stage(images_data, stats, groups_idx, md5, dh, None, cache, WORKERS)
    finally:
        ic.same_photo_features = orig
        cache.close()
    assert cls3 == cls and calls == []


# --------------------------------------------------------------------------
# Status e seleção
# --------------------------------------------------------------------------

def test_image_status():
    assert ic.image_status({'md5': "m", 'same_photo': 0}, {"m": 2}) == "Idêntica"
    assert ic.image_status({'md5': "m", 'same_photo': 0}, {"m": 1}) == "Mesma foto"
    assert ic.image_status({'md5': "m"}, {"m": 1}) == "Semelhante"


def _img(cls, pixels, size, mtime, ref=False, suspect=False):
    return {'md5': f"{pixels}{size}{mtime}", 'same_photo': cls, 'pixels': pixels, 'size': size,
            'mtime': mtime, 'is_reference': ref, 'same_photo_suspect': suspect}


def test_plan_same_photo_selection():
    imgs = [_img(0, 100, 10, 5), _img(0, 400, 20, 9), _img(None, 900, 90, 1), _img(0, 400, 20, 2)]
    assert ic.plan_same_photo_selection(imgs) == [0, 1]           # mantém 3 (400 px, empate de tamanho, mais antiga)
    imgs = [_img(0, 100, 10, 5), _img(0, 400, 20, 9, ref=True)]
    assert ic.plan_same_photo_selection(imgs) == [0]              # referência mantida
    imgs = [_img(0, 100, 10, 5, ref=True), _img(0, 400, 20, 9, ref=True)]
    assert ic.plan_same_photo_selection(imgs) == []
    imgs = [_img(0, 100, 10, 5, suspect=True), _img(0, 4000, 20, 9, suspect=True)]
    assert ic.plan_same_photo_selection(imgs) == []               # suspeita: nada automático
    imgs = [_img(0, 100, 10, 5), _img(1, 100, 10, 5), _img(0, 100, 10, 5), _img(1, 100, 10, 6)]
    assert sorted(ic.plan_same_photo_selection(imgs)) == [2, 3]   # duas classes, empate total: primeira fica
