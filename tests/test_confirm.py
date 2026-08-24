"""
Testes da confirmação de "Semelhante" por segundo hash (dhash) e do
tratamento de hashes degenerados (transparência composta sobre branco).
"""
import os
import sqlite3
import time

import imagehash
import numpy as np
import pytest
from PIL import Image

import main as ic
from conftest import build_confirm_fixture, build_reference_fixture, T0, WORKERS

# --------------------------------------------------------------------------
# Hashes: composição sobre branco, degenerados, dhash
# --------------------------------------------------------------------------

def _pc(x):
    return bin(x).count("1")


def _bits(n, seed=0):
    """Inteiro de 64 bits com exatamente n bits ligados (determinístico)."""
    rng = np.random.RandomState(seed)
    pos = rng.choice(64, n, replace=False)
    v = 0
    for p in pos:
        v |= 1 << int(p)
    return v


def test_is_degenerate_hash():
    assert ic.is_degenerate_hash(0)
    assert ic.is_degenerate_hash((1 << 64) - 1)
    assert ic.is_degenerate_hash(_bits(4))
    assert ic.is_degenerate_hash(_bits(60))
    assert not ic.is_degenerate_hash(_bits(5))
    assert not ic.is_degenerate_hash(_bits(59))
    assert not ic.is_degenerate_hash(_bits(32))
    assert ic.is_degenerate_hash(imagehash.hex_to_hash("0000000000000000"))
    assert not ic.is_degenerate_hash(imagehash.hex_to_hash("f0f0f0f0f0f0f0f0"))


def test_flatten_alpha_muda_phash_de_png_transparente(tmp_path):
    root = build_confirm_fixture(str(tmp_path))
    p = os.path.join(root, "alpha_text.png")
    with Image.open(p) as im:
        antigo = imagehash.phash(im)              # comportamento antigo: tudo preto
        novo = imagehash.phash(ic._flatten_alpha(im))
    assert str(antigo) == "0000000000000000"
    assert ic.is_degenerate_hash(antigo)
    assert not ic.is_degenerate_hash(novo)
    assert ic.compute_phash(p) == novo


def test_flatten_alpha_nao_toca_imagem_sem_alfa(tmp_path):
    root = build_confirm_fixture(str(tmp_path))
    p = os.path.join(root, "true_a.jpg")
    with Image.open(p) as im:
        assert ic._flatten_alpha(im) is im
        assert ic.compute_phash(p) == imagehash.phash(im)
        assert ic.compute_dhash(p) == imagehash.dhash(im)


def test_flatten_alpha_paleta_com_transparencia():
    # Paleta: índice 0 = magenta (transparente), índice 1 = preto
    a = np.zeros((64, 64), dtype=np.uint8)
    a[16:48, 16:48] = 1
    im = Image.fromarray(a, "P")
    im.putpalette([255, 0, 255, 0, 0, 0] + [0] * (768 - 6))
    im.info["transparency"] = 0
    flat = ic._flatten_alpha(im)
    g = np.asarray(flat.convert("L"))
    assert g.max() == 255 and g.min() == 0        # fundo virou branco, não magenta
    assert (g == 255).sum() > (g == 0).sum()


def test_fixture_confirm_distancias(tmp_path):
    root = build_confirm_fixture(str(tmp_path))
    ph = lambda n: ic.hash_to_int(ic.compute_phash(os.path.join(root, n)))
    dh = lambda n: ic.hash_to_int(ic.compute_dhash(os.path.join(root, n)))
    # degeneradas
    assert ic.is_degenerate_hash(ph("black.png"))
    assert ic.is_degenerate_hash(ph("black2.png"))
    assert ic.is_degenerate_hash(ph("white.jpg"))
    assert not ic.is_degenerate_hash(ph("alpha_text.png"))
    # falso semelhante: phash junto, dhash longe
    assert _pc(ph("fake_a.png") ^ ph("fake_b.png")) <= ic.SIMILARITY_THRESHOLD
    assert _pc(dh("fake_a.png") ^ dh("fake_b.png")) >= ic.DHASH_THRESHOLD + 4
    # verdadeiro: ambos juntos
    assert _pc(ph("true_a.jpg") ^ ph("true_a_q30.jpg")) <= ic.SIMILARITY_THRESHOLD
    assert _pc(dh("true_a.jpg") ^ dh("true_a_q30.jpg")) <= ic.DHASH_THRESHOLD


def test_fixture_referencia_quase_a_confirma(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    dh = lambda p: ic.hash_to_int(ic.compute_dhash(p))
    assert _pc(dh(os.path.join(alvo, "quase_a.jpg")) ^ dh(os.path.join(ref, "acervo_a.jpg"))) <= ic.DHASH_THRESHOLD


# --------------------------------------------------------------------------
# confirm_similar_groups: propriedades com hashes fabricados (sem disco)
# --------------------------------------------------------------------------

def _H(v):
    return imagehash.hex_to_hash(f"{v:016x}")


def _flip(v, nbits, offset=0):
    """Inverte nbits bits a partir da posição offset."""
    for b in range(offset, offset + nbits):
        v ^= 1 << b
    return v


BASE = _bits(32, seed=1)


def _data(specs):
    """specs: lista de (phash_int, md5, dhash_int_ou_None).
       Devolve (images_data, md5_by_idx, dhash_by_idx)."""
    images_data = [(f"img{i}.jpg", _H(p), m) for i, (p, m, _) in enumerate(specs)]
    md5_by_idx = {i: m for i, (_, m, _) in enumerate(specs)}
    dhash_by_idx = {i: d for i, (_, _, d) in enumerate(specs) if d is not None}
    return images_data, md5_by_idx, dhash_by_idx


def _confirm(specs, groups, D=14, T=10):
    images_data, md5_by_idx, dhash_by_idx = _data(specs)
    return ic.confirm_similar_groups(images_data, groups, md5_by_idx, dhash_by_idx, T, D)


def test_md5_igual_nunca_separa_mesmo_com_hashes_longe():
    out, st = _confirm([(BASE, "m", 0), (_flip(BASE, 40), "m", (1 << 64) - 1),
                        (0, "m", 0)], [[0, 1, 2]])
    assert out == [[0, 1, 2]]
    assert st["groups_unchanged"] == 1 and st["images_dropped"] == 0


def test_dhash_longe_com_md5_diferente_descarta_par():
    out, st = _confirm([(BASE, "a", 0), (_flip(BASE, 4), "b", _flip(0, 20))], [[0, 1]])
    assert out == []
    assert st["groups_dropped"] == 1 and st["images_dropped"] == 2
    assert st["pairs_rejected_dhash"] == 1


def test_dhash_perto_confirma():
    out, st = _confirm([(BASE, "a", 0), (_flip(BASE, 4), "b", _flip(0, 6))], [[0, 1]])
    assert out == [[0, 1]]
    assert st["pairs_checked"] == 1 and st["pairs_rejected_dhash"] == 0


def test_degenerado_com_md5_unico_some_e_com_md5_repetido_fica():
    d0 = 0                                    # phash zero: degenerado
    out, st = _confirm([(d0, "x", None), (d0, "y", None), (d0, "x", None)], [[0, 1, 2]])
    assert out == [[0, 2]]
    assert st["images_degenerate"] == 3 and st["pairs_rejected_degenerate"] == 2


def test_dhash_ausente_cai_no_criterio_antigo():
    out, st = _confirm([(BASE, "a", None), (_flip(BASE, 4), "b", 0)], [[0, 1]])
    assert out == [[0, 1]]
    assert st["pairs_without_dhash"] == 1


def test_D64_sem_degenerados_saida_igual_entrada():
    # cadeia conexa sob pd <= 10 com dhashes arbitrários
    rng = np.random.RandomState(3)
    specs, p = [], BASE
    for i in range(30):
        p = _flip(p, 4, offset=(i * 4) % 60)
        specs.append((p, f"m{i}", int.from_bytes(rng.bytes(8), "big")))
    groups = [[0, 1, 2], list(range(3, 30))]
    out, st = _confirm(specs, groups, D=64)
    assert out == groups
    assert st["groups_unchanged"] == 2 and st["pairs_rejected_dhash"] == 0


def test_ordem_dos_subgrupos_e_contencao():
    # grupo [0..5]: {0,3} juntos por dhash, {1,4,5} juntos, 2 sozinho
    specs = [(BASE, "a", 0), (BASE, "b", _flip(0, 30)), (BASE, "c", _flip(0, 20, 32)),
             (BASE, "d", _flip(0, 2)), (BASE, "e", _flip(0, 30) ^ 1), (BASE, "f", _flip(0, 30) ^ 2)]
    # índice 6 fora de qualquer grupo; 7 e 8 formam um grupo idêntico
    specs += [(0, "z", None), (BASE, "g", 0), (BASE, "g", 0)]
    out, st = _confirm(specs, [[0, 1, 2, 3, 4, 5], [7, 8]], D=14)
    assert out == [[0, 3], [1, 4, 5], [7, 8]]
    assert st["groups_split"] == 1 and st["groups_unchanged"] == 1
    assert st["images_dropped"] == 1
    # contenção e unicidade
    seen = set()
    for g in out:
        assert not (set(g) & seen) and len(g) > 1
        seen |= set(g)
        assert set(g) <= {0, 1, 2, 3, 4, 5} or set(g) <= {7, 8}


def test_transitividade_domada():
    # a-b confirmados, b-c com dhash longe, a-c com phash longe: c some
    a = BASE
    b = _flip(a, 8, 0)
    c = _flip(b, 8, 8)          # pd(a,c) = 16 > 10
    specs = [(a, "a", 0), (b, "b", _flip(0, 2)), (c, "c", _flip(0, 30))]
    out, _ = _confirm(specs, [[0, 1, 2]])
    assert out == [[0, 1]]


@pytest.mark.parametrize("n", [3000])
def test_grupo_gigante_intacto_e_dividido(n):
    # intacto: todos com o mesmo phash e dhash 0 (md5 distintos)
    specs = [(BASE, f"m{i}", 0) for i in range(n)]
    t0 = time.time()
    out, st = _confirm(specs, [list(range(n))])
    assert out == [list(range(n))] and st["groups_unchanged"] == 1
    # dividido ao meio pelo dhash
    specs = [(BASE, f"m{i}", 0 if i % 2 == 0 else _flip(0, 30)) for i in range(n)]
    out, st = _confirm(specs, [list(range(n))])
    assert [len(g) for g in out] == [n // 2, n // 2] and st["groups_split"] == 1
    assert out[0] == list(range(0, n, 2)) and out[1] == list(range(1, n, 2))
    assert time.time() - t0 < 20


def test_cancelamento_levanta_scan_cancelled():
    specs = [(BASE, "a", 0), (BASE, "b", 0)]
    images_data, md5_by_idx, dhash_by_idx = _data(specs)
    with pytest.raises(ic.ScanCancelled):
        ic.confirm_similar_groups(images_data, [[0, 1]], md5_by_idx, dhash_by_idx, 10, 14,
                                  cancel_check=lambda: True)


# --------------------------------------------------------------------------
# Cache com coluna dhash, hash_files recalculando degenerados, dhash_for_groups
# --------------------------------------------------------------------------

def test_hashcache_migra_banco_antigo_sem_coluna_dhash(tmp_path):
    db = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE hashes_v1_full (path TEXT PRIMARY KEY, size INTEGER, "
                "mtime_ns INTEGER, phash TEXT, md5 TEXT)")
    fa = str(tmp_path / "a" / "x.jpg")
    con.execute("INSERT INTO hashes_v1_full VALUES (?, 1, 11, 'aa', 'm')", (ic.cache_key(fa),))
    con.commit(); con.close()

    cache = ic.HashCache(path=db)
    assert cache.active and cache.has_dhash
    cols = {r[1] for r in cache.conn.execute("PRAGMA table_info(hashes_v1_full)")}
    assert "dhash" in cols
    assert cache.lookup(fa, 1, 11) == ("aa", "m")          # contrato antigo intacto
    assert cache.lookup_dhash(fa, 1, 11) is None
    cache.update_dhash(fa, "dd"); cache.flush()
    assert cache.lookup_dhash(fa, 1, 11) == "dd"
    assert cache.lookup(fa, 1, 11) == ("aa", "m")
    # load_prefix carrega dhash também
    cache.load_prefix(str(tmp_path / "a"))
    assert cache.lookup_dhash(fa, 1, 11) == "dd"
    assert cache.lookup_dhash(fa, 2, 11) is None            # tamanho mudou
    # store com dhash persiste; store sem dhash zera
    fb = str(tmp_path / "a" / "y.jpg")
    cache.store(fb, 5, 55, "bb", "m2", "d2"); cache.flush()
    cache.load_prefix(str(tmp_path / "a"))
    assert cache.lookup_dhash(fb, 5, 55) == "d2"
    cache.store(fb, 5, 55, "bb"); cache.flush(); cache.load_prefix(str(tmp_path / "a"))
    assert cache.lookup_dhash(fb, 5, 55) is None and cache.lookup(fb, 5, 55) == ("bb", None)
    cache.close()
    # reabrir: ALTER idempotente
    cache2 = ic.HashCache(path=db)
    assert cache2.has_dhash
    cache2.close()


def test_hash_files_recalcula_phash_degenerado_do_cache_preservando_md5(tmp_path):
    root = build_confirm_fixture(str(tmp_path / "fx"))
    db = str(tmp_path / "cache.sqlite")
    cache = ic.HashCache(path=db)
    png = os.path.join(root, "alpha_text.png")
    jpg = os.path.join(root, "white.jpg")
    entries = [(p, os.path.getsize(p), os.stat(p).st_mtime_ns) for p in (png, jpg)]
    # cache "antigo": phash zero para os dois, md5 conhecido, dhash antigo no png
    cache.store(png, entries[0][1], entries[0][2], "0000000000000000", "md5png", "velho")
    cache.store(jpg, entries[1][1], entries[1][2], "0000000000000000", "md5jpg", None)
    cache.flush()

    calls = []
    original = ic.compute_phash
    ic.compute_phash = lambda fp: calls.append(fp) or original(fp)
    try:
        results, errors, cancelled = ic.hash_files(entries, cache, 2)
    finally:
        ic.compute_phash = original
    assert not errors and not cancelled
    assert calls == [png]                                  # só o PNG foi recalculado
    fp, h, md5 = results[0]
    assert not ic.is_degenerate_hash(h) and md5 == "md5png"
    assert str(results[1][1]) == "0000000000000000" and results[1][2] == "md5jpg"
    # banco: phash novo, md5 preservado, dhash antigo descartado
    cache.load_prefix(root)
    assert cache.lookup(png, entries[0][1], entries[0][2]) == (str(h), "md5png")
    assert cache.lookup_dhash(png, entries[0][1], entries[0][2]) is None
    cache.close()


def test_dhash_for_groups_calcula_so_o_necessario_e_usa_cache(tmp_path):
    root = build_confirm_fixture(str(tmp_path / "fx"))
    entries = ic.list_image_files(root, True, ic.VALID_EXTENSIONS)
    results, _, _ = ic.hash_files(entries, None, WORKERS)
    images_data = [r for r in results if r is not None]
    stats = [entries[i][1:] for i, r in enumerate(results) if r is not None]
    name = lambda i: os.path.basename(images_data[i][0])
    idx = {name(i): i for i in range(len(images_data))}
    # grupo 1: idênticas (alpha_text + copy) -> nenhum dhash
    # grupo 2: fake_a + fake_b (md5 diferentes) -> 2 dhashes
    # grupo 3: black + black2 (degeneradas, md5 diferentes) -> nenhum
    g1 = [idx["alpha_text.png"], idx["alpha_text_copy.png"]]
    g2 = [idx["fake_a.png"], idx["fake_b.png"]]
    g3 = [idx["black.png"], idx["black2.png"]]
    md5_by_idx, _ = ic.md5_for_groups(images_data, stats, [g1, g2, g3], None, WORKERS)

    db = str(tmp_path / "cache.sqlite")
    cache = ic.HashCache(path=db)
    # No app, hash_files já gravou a linha (phash) de cada imagem; o dhash é
    # um UPDATE sobre ela. Reproduz isso aqui.
    for (fp, h, _), (size, mt) in zip(images_data, stats):
        cache.store(fp, size, mt, str(h))
    cache.flush()
    calls = []
    original = ic.compute_dhash
    ic.compute_dhash = lambda fp: calls.append(fp) or original(fp)
    try:
        d1, c1 = ic.dhash_for_groups(images_data, stats, [g1, g2, g3], md5_by_idx, cache, WORKERS)
        assert sorted(os.path.basename(c) for c in calls) == ["fake_a.png", "fake_b.png"]
        assert set(d1) == set(g2) and not c1
        calls.clear()
        cache.load_prefix(root)
        d2, _ = ic.dhash_for_groups(images_data, stats, [g1, g2, g3], md5_by_idx, cache, WORKERS)
        assert calls == [] and d2 == d1                      # segunda vez: tudo do cache
    finally:
        ic.compute_dhash = original
        cache.close()


def test_confirmacao_ponta_a_ponta_na_fixture(tmp_path):
    from conftest import run_pipeline
    root = build_confirm_fixture(str(tmp_path / "fx"))
    names = lambda groups: sorted(sorted(os.path.basename(fp) for (fp, _, _) in g) for g in groups)
    _, _, sem = run_pipeline(root, confirm=False)
    _, _, com = run_pipeline(root, confirm=True)
    # Sem confirmação (comportamento antigo): pretos e branca podem se misturar por
    # coincidência de hash; fake_a/fake_b juntos.
    assert ["fake_a.png", "fake_b.png"] in names(sem)
    # Com confirmação: só pares legítimos
    assert names(com) == [["alpha_text.png", "alpha_text_copy.png"],
                          ["true_a.jpg", "true_a_q30.jpg"]]
