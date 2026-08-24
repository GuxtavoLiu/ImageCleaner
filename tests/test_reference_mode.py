"""
Testes do modo de comparação de duas pastas: funções puras de suporte,
cache com múltiplas raízes e ponta a ponta headless via run_selftest.
"""
import os

import pytest

import main as ic
from conftest import build_reference_fixture, run_pipeline, T0


# --------------------------------------------------------------------------
# folder_conflict
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a, b, motivo", [
    (r"E:\Fotos", r"E:\Fotos", "mesma"),
    (r"E:\Fotos", r"e:\fotos\\", "mesma"),               # caixa e barra final
    (r"E:\Fotos\2020", r"E:\Fotos", "alvo está dentro"),
    (r"E:\Fotos", r"E:\Fotos\2020", "referência está dentro"),
])
def test_folder_conflict_detecta(a, b, motivo):
    reason = ic.folder_conflict(a, b)
    assert reason is not None and motivo in reason


@pytest.mark.parametrize("a, b", [
    (r"E:\Fotos", r"E:\Fotos2"),          # prefixo de string, não de pasta
    (r"E:\Fotos2", r"E:\Fotos"),
    (r"E:\A\alvo", r"E:\A\ref"),
    (r"C:\x", r"D:\x"),
])
def test_folder_conflict_disjuntas(a, b):
    assert ic.folder_conflict(a, b) is None


# --------------------------------------------------------------------------
# merge_scan_entries / filter_groups_for_reference / is_protected_path
# --------------------------------------------------------------------------

def _e(path):
    return (path, 10, 20)


def test_merge_scan_entries_ordem_e_set():
    alvo = [_e(r"E:\alvo\1.jpg"), _e(r"E:\alvo\2.jpg")]
    ref = [_e(r"E:\ref\a.jpg")]
    entries, keys = ic.merge_scan_entries(alvo, ref)
    assert [p for (p, _, _) in entries] == [r"E:\alvo\1.jpg", r"E:\alvo\2.jpg", r"E:\ref\a.jpg"]
    assert keys == {ic.cache_key(r"E:\ref\a.jpg")}
    assert ic.cache_key(r"e:\REF\A.JPG") in keys  # normalização de caixa


def test_filter_groups_for_reference():
    paths = [r"E:\alvo\1.jpg", r"E:\alvo\2.jpg", r"E:\ref\a.jpg", r"E:\ref\b.jpg", r"E:\alvo\3.jpg"]
    images_data = [(p, None, None) for p in paths]
    keys = {ic.cache_key(paths[2]), ic.cache_key(paths[3])}
    so_ref = [2, 3]
    misto = [0, 2]
    so_alvo = [1, 4]
    groups = [so_ref, misto, so_alvo]
    assert ic.filter_groups_for_reference(groups, images_data, keys) == [misto, so_alvo]
    assert ic.filter_groups_for_reference(groups, images_data, keys, hide_target_only=True) == [misto]
    # ordem interna preservada
    assert ic.filter_groups_for_reference([[2, 0]], images_data, keys) == [[2, 0]]


def test_is_protected_path_set_e_prefixo():
    keys = {ic.cache_key(r"E:\ref\a.jpg")}
    prefix = ic.folder_prefix(r"E:\ref")
    assert ic.is_protected_path(r"E:\REF\A.JPG", keys, prefix)
    assert ic.is_protected_path(r"E:\ref\sub\novo.jpg", keys, prefix)   # só pelo prefixo
    assert ic.is_protected_path(r"E:\ref\a.jpg", keys, None)            # só pelo set
    assert not ic.is_protected_path(r"E:\alvo\a.jpg", keys, prefix)
    assert not ic.is_protected_path(r"E:\ref2\a.jpg", keys, prefix)     # prefixo de string
    assert not ic.is_protected_path(r"E:\alvo\a.jpg", set(), None)


# --------------------------------------------------------------------------
# HashCache com duas raízes
# --------------------------------------------------------------------------

def test_hashcache_load_prefix_merge(tmp_path):
    db = str(tmp_path / "cache.sqlite")
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    fa = os.path.join(a, "x.jpg")
    fb = os.path.join(b, "y.jpg")
    fora = str(tmp_path / "c" / "z.jpg")
    cache = ic.HashCache(path=db)
    assert cache.active
    cache.store(fa, 1, 11, "aa", None)
    cache.store(fb, 2, 22, "bb", "md5b")
    cache.store(fora, 3, 33, "cc", None)
    cache.flush()

    # Uma raiz (modo normal): comportamento de sempre
    cache.load_prefix(a)
    assert cache.lookup(fa, 1, 11) == ("aa", None)
    assert cache.lookup(fb, 2, 22) == ("bb", "md5b")      # via consulta individual
    assert cache.lookup(fa, 999, 11) is None              # tamanho mudou
    assert len(cache.loaded) == 1

    # Duas raízes (modo comparação)
    cache.load_prefix(a)
    cache.load_prefix(b, merge=True)
    assert len(cache.loaded) == 2 and len(cache.loaded_prefixes) == 2
    assert cache.lookup(fa, 1, 11) == ("aa", None)
    assert cache.lookup(fb, 2, 22) == ("bb", "md5b")
    assert cache.lookup(fora, 3, 33) == ("cc", None)       # fora das raízes: consulta o banco
    assert cache.lookup(os.path.join(a, "inexistente.jpg"), 1, 1) is None

    # Recarregar sem merge substitui
    cache.load_prefix(b)
    assert len(cache.loaded) == 1 and cache.loaded_prefixes == [ic.folder_prefix(b)]
    cache.close()
    assert cache.loaded_prefixes == []


# --------------------------------------------------------------------------
# Ponta a ponta headless (pipeline + filtragem + seleção)
# --------------------------------------------------------------------------

def _rel(base, fp):
    return os.path.relpath(fp, base).replace("\\", "/")


def test_fixture_de_referencia_agrupa_como_esperado(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    # Sanidade das distâncias de phash (a fixture precisa ser estável)
    h = lambda p: ic.compute_phash(p)
    assert abs(h(os.path.join(ref, "acervo_a.jpg")) - h(os.path.join(alvo, "quase_a.jpg"))) <= ic.SIMILARITY_THRESHOLD
    assert abs(h(os.path.join(ref, "acervo_a.jpg")) - h(os.path.join(alvo, "interna1.jpg"))) > ic.SIMILARITY_THRESHOLD
    assert abs(h(os.path.join(ref, "acervo_a.jpg")) - h(os.path.join(alvo, "unica.jpg"))) > ic.SIMILARITY_THRESHOLD
    assert abs(h(os.path.join(ref, "acervo_b.jpg")) - h(os.path.join(alvo, "unica.jpg"))) > ic.SIMILARITY_THRESHOLD


def test_modo_referencia_ponta_a_ponta(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    base = str(tmp_path)

    entries = ic.list_image_files(alvo, True, ic.VALID_EXTENSIONS)
    ref_entries = ic.list_image_files(ref, True, ic.VALID_EXTENSIONS)
    entries, keys = ic.merge_scan_entries(entries, ref_entries)
    results, errors, _ = ic.hash_files(entries, None, 2)
    assert not errors
    images_data = [r for r in results if r is not None]
    groups_idx = ic.find_similar_groups([hh for (_, hh, _) in images_data], ic.SIMILARITY_THRESHOLD)

    # Antes do filtro: 3 grupos (misto A, só-alvo B interna, só-ref B acervo).
    # interna1/2 (motivo B) e acervo_b/b2 (motivo B) caem no MESMO grupo,
    # pois são o mesmo desenho: logo há 2 grupos: A (misto) e B (misto).
    filtered = ic.filter_groups_for_reference(groups_idx, images_data, keys)
    stats = [entries[i][1:] for i, r in enumerate(results) if r is not None]
    md5_by_idx, _ = ic.md5_for_groups(images_data, stats, filtered, None, 2)
    groups = ic.build_groups(images_data, filtered, md5_by_idx)

    by_name = {}
    for g in groups:
        by_name[frozenset(_rel(base, fp) for (fp, _, _) in g)] = g
    nomes = [sorted(k) for k in by_name]
    assert sorted(nomes) == [
        ["alvo/dup_a.jpg", "alvo/quase_a.jpg", "ref/acervo_a.jpg"],
        ["alvo/interna1.jpg", "alvo/interna2.jpg", "ref/acervo_b.jpg", "ref/acervo_b2.jpg"],
    ]
    # 'unica.jpg' não aparece em grupo nenhum
    assert all("alvo/unica.jpg" not in k for k in by_name)

    # Seleção: a referência nunca; dup_a (idêntica a acervo_a) e quase_a
    # (semelhante, com referência no grupo) selecionadas; no grupo B,
    # interna1/2 são idênticas entre si mas SEM cópia na referência
    # (acervo_b é outro MD5): mantém a mais antiga (interna1).
    sel_ident, sel_simil = [], []
    for g in groups:
        counts = {}
        for (_, _, m) in g:
            counts[m] = counts.get(m, 0) + 1
        images = [{'filepath': fp, 'md5': m, 'mtime': os.path.getmtime(fp),
                   'is_reference': ic.cache_key(fp) in keys} for (fp, _, m) in g]
        sel_ident += [_rel(base, images[i]['filepath']) for i in ic.plan_identical_selection(images)]
        sel_simil += [_rel(base, images[i]['filepath']) for i in ic.plan_similar_selection(images, counts)]
    assert sorted(sel_ident) == ["alvo/dup_a.jpg", "alvo/interna2.jpg"]
    assert sorted(sel_simil) == ["alvo/quase_a.jpg"]
    assert not any(n.startswith("ref/") for n in sel_ident + sel_simil)


def test_run_selftest_com_referencia(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    summary = ic.run_selftest(alvo, ref)
    assert summary.startswith("SELFTEST OK")
    assert "REF: 2 grupos, 3 protegidas, 2 selecionáveis (idênticas), 1 (semelhantes)" in summary


def test_run_selftest_rejeita_pastas_em_conflito(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    with pytest.raises(ValueError):
        ic.run_selftest(alvo, alvo)
    with pytest.raises(ValueError):
        ic.run_selftest(str(tmp_path), ref)


def test_run_selftest_sem_referencia_inalterado(tmp_path):
    alvo, ref = build_reference_fixture(str(tmp_path))
    summary = ic.run_selftest(alvo)
    assert summary.startswith("SELFTEST OK") and "REF:" not in summary
