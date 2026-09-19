"""
Motor de comparação por bytes (find_identical_files) e seu cache
(FileHashCache). Regra de ouro verificada aqui: só vira grupo o que tem MD5 do
arquivo INTEIRO igual; amostragem elimina, nunca confirma; erro nunca vira
"Idêntica"; e o disco é lido o mínimo possível.
"""
import hashlib
import os
import sqlite3
import threading

import pytest

import main as ic
from conftest import T0, WORKERS

CHUNK = ic.BYTE_QUICK_CHUNK
BIG = 1024 * 1024          # acima de 3 blocos: passa pelas amostras e pelo MD5 completo


def _blob(size, seed=1):
    """Conteúdo determinístico e não periódico."""
    out = bytearray()
    x = seed * 2654435761 % (1 << 32) or 1
    while len(out) < size:
        x = (x * 1103515245 + 12345) % (1 << 31)
        out += x.to_bytes(4, "little")
    return bytes(out[:size])


def _put(root, rel, data, mtime=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    t = T0 if mtime is None else mtime
    os.utime(path, (t, t))
    return path


def _entries(*paths):
    out = []
    for p in paths:
        st = os.stat(p)
        out.append((p, st.st_size, st.st_mtime_ns))
    return out


def _names(entries, groups_idx):
    return [[os.path.basename(entries[i][0]) for i in g] for g in groups_idx]


class _Spy:
    """Registra as chamadas às duas funções de leitura (por atributo de módulo)."""
    def __init__(self, monkeypatch):
        self.samples = []      # (nome, parte) de cada amostra lida
        self.full = []
        orig_quick, orig_full = ic.file_quick_key, ic.file_full_md5

        def quick(fp, size, on_bytes=None, mtime_ns=None, part="head"):
            self.samples.append((os.path.basename(fp), part))
            return orig_quick(fp, size, on_bytes, mtime_ns=mtime_ns, part=part)

        def full(fp, size, cancel_event=None, on_bytes=None, mtime_ns=None):
            self.full.append(os.path.basename(fp))
            return orig_full(fp, size, cancel_event, on_bytes, mtime_ns=mtime_ns)

        monkeypatch.setattr(ic, "file_quick_key", quick)
        monkeypatch.setattr(ic, "file_full_md5", full)

    def part(self, which):
        return sorted(name for name, part in self.samples if part == which)


def test_miolo_diferente_fora_das_amostras_nao_vira_identica(tmp_path, monkeypatch):
    root = str(tmp_path)
    base = _blob(BIG)
    a = _put(root, "a.mp4", base)
    a2 = _put(root, "a_copia.mp4", base)
    broken = bytearray(base)
    broken[100_000:100_512] = b"\x00" * 512      # setores zerados, fora das 3 janelas
    b = _put(root, "b_setor_zerado.mp4", bytes(broken))
    c = _put(root, "c_tamanho_unico.mp4", _blob(BIG + 7, seed=3))
    entries = _entries(a, a2, b, c)
    spy = _Spy(monkeypatch)
    groups, md5s, stats, cancelled = ic.find_identical_files(entries, None, WORKERS)
    assert cancelled is False
    assert _names(entries, groups) == [["a.mp4", "a_copia.mp4"]]
    assert md5s[0] == md5s[1] and set(md5s) == {0, 1}
    # as amostras NÃO separaram o arquivo danificado: quem separou foi o MD5 completo
    todos = ["a.mp4", "a_copia.mp4", "b_setor_zerado.mp4"]
    assert spy.part("head") == todos and spy.part("rest") == todos and sorted(spy.full) == todos
    assert not [n for n, _ in spy.samples if n == "c_tamanho_unico.mp4"]   # tamanho único: nem foi aberto
    assert stats["size_candidates"] == 3 and stats["groups"] == 1 and not stats["errors"]
    assert stats["md5_from_cache"] == []


def test_o_comeco_do_arquivo_elimina_com_uma_leitura_so(tmp_path, monkeypatch):
    root = str(tmp_path)
    base = _blob(BIG)
    head_diff = bytearray(base)
    head_diff[0] ^= 0xFF                              # difere no 1º byte
    tail_diff = bytearray(base)
    tail_diff[-1] ^= 0xFF                             # igual no começo, difere no fim
    paths = [_put(root, "x.bin", base), _put(root, "x_copia.bin", base),
             _put(root, "y_comeco.bin", bytes(head_diff)), _put(root, "z_fim.bin", bytes(tail_diff))]
    entries = _entries(*paths)
    spy = _Spy(monkeypatch)
    groups, _, stats, _ = ic.find_identical_files(entries, None, WORKERS)
    assert _names(entries, groups) == [["x.bin", "x_copia.bin"]]
    assert spy.part("head") == ["x.bin", "x_copia.bin", "y_comeco.bin", "z_fim.bin"]
    assert spy.part("rest") == ["x.bin", "x_copia.bin", "z_fim.bin"]    # y caiu com UMA leitura
    assert sorted(spy.full) == ["x.bin", "x_copia.bin"]                 # z caiu no meio/fim
    assert stats["bytes_read"] == 4 * CHUNK + 3 * 2 * CHUNK + 2 * BIG


@pytest.mark.parametrize("size", [1, CHUNK, 3 * CHUNK, 3 * CHUNK + 1, 3 * CHUNK + 2])
def test_limites_das_janelas(tmp_path, monkeypatch, size):
    root = str(tmp_path)
    base = _blob(size, seed=9)
    last = bytearray(base)
    last[-1] ^= 0x01
    mid = bytearray(base)
    mid[size // 2] ^= 0x02          # outro bit: com 1 byte, "meio" e "último" são o mesmo byte
    paths = [_put(root, "p.dat", base), _put(root, "p_copia.dat", base),
             _put(root, "ultimo_byte.dat", bytes(last)), _put(root, "byte_do_meio.dat", bytes(mid))]
    entries = _entries(*paths)
    spy = _Spy(monkeypatch)
    groups, md5s, _, _ = ic.find_identical_files(entries, None, WORKERS)
    assert _names(entries, groups) == [["p.dat", "p_copia.dat"]]
    assert md5s[0] == hashlib.md5(base).hexdigest()     # sempre o MD5 real do conteúdo inteiro
    if size <= 3 * CHUNK:
        assert spy.full == [] and spy.part("rest") == []   # arquivo pequeno: uma leitura só
    else:
        assert sorted(spy.full) == ["p.dat", "p_copia.dat"]


def test_vazio_e_tamanho_desconhecido_ficam_fora(tmp_path):
    root = str(tmp_path)
    e1 = _put(root, "vazio1.txt", b"")
    e2 = _put(root, "vazio2.txt", b"")
    k = _put(root, "k.txt", b"abc")
    entries = _entries(e1, e2) + [(k, None, None), (k + "_fantasma", None, None)]
    groups, _, stats, _ = ic.find_identical_files(entries, None, WORKERS)
    assert groups == [] and stats["no_size"] == 4


def test_hardlink_nao_e_duplicata(tmp_path):
    root = str(tmp_path)
    data = _blob(5000)
    a = _put(root, "a.pdf", data)
    link = os.path.join(root, "a_hardlink.pdf")
    try:
        os.link(a, link)
    except OSError as e:
        pytest.skip(f"sem suporte a hardlink aqui: {e}")
    entries = _entries(a, link)
    groups, _, stats, _ = ic.find_identical_files(entries, None, WORKERS)
    assert groups == [] and stats["hardlinks"] == 1
    copy = _put(root, "a_copia.pdf", data)
    entries = _entries(a, link, copy)
    groups, _, _, _ = ic.find_identical_files(entries, None, WORKERS)
    assert _names(entries, groups) == [["a.pdf", "a_copia.pdf"]]


def test_arquivo_que_mudou_desde_a_listagem_vira_erro_e_nao_vai_para_o_cache(tmp_path):
    root = str(tmp_path)
    data = _blob(BIG)
    paths = [_put(root, n, data) for n in ("v1.mov", "v2.mov", "v3_cresceu.mov", "v4_regravado.mov")]
    entries = _entries(*paths)
    with open(paths[2], "ab") as f:                    # cresce depois de listado
        f.write(b"mais")
    other = bytearray(data)
    other[500_000] ^= 0xFF                             # mesmo tamanho, outro conteúdo, data nova
    _put(root, "v4_regravado.mov", bytes(other), mtime=T0 + 3600)
    cache = ic.FileHashCache(str(tmp_path / "c.sqlite"))
    groups, _, stats, _ = ic.find_identical_files(entries, cache, WORKERS)
    assert _names(entries, groups) == [["v1.mov", "v2.mov"]]
    erros = {os.path.basename(e['filepath']): e['message'] for e in stats["errors"]}
    assert "tamanho mudou" in erros["v3_cresceu.mov"]
    assert "data de modificação mudou" in erros["v4_regravado.mov"]
    found = cache.lookup_many(entries)
    assert paths[2] not in found and paths[3] not in found     # nada do arquivo mudado foi gravado
    cache.close()


def test_erro_de_permissao_nao_derruba_o_resto(tmp_path, monkeypatch):
    root = str(tmp_path)
    data = _blob(3000)
    paths = [_put(root, n, data) for n in ("ok1.txt", "ok2.txt", "travado.txt")]
    entries = _entries(*paths)
    orig = ic.file_quick_key

    def quick(fp, size, on_bytes=None, **kw):
        if fp.endswith("travado.txt"):
            raise PermissionError("acesso negado")
        return orig(fp, size, on_bytes, **kw)

    monkeypatch.setattr(ic, "file_quick_key", quick)
    groups, _, stats, _ = ic.find_identical_files(entries, None, WORKERS)
    assert _names(entries, groups) == [["ok1.txt", "ok2.txt"]]
    assert len(stats["errors"]) == 1 and "acesso negado" in stats["errors"][0]['message']


def test_referencia_balde_so_da_referencia_nao_le_nenhum_byte(tmp_path, monkeypatch):
    alvo, ref = str(tmp_path / "alvo"), str(tmp_path / "ref")
    data = _blob(BIG, seed=4)
    r1, r2 = _put(ref, "r1.mp4", data), _put(ref, "r2.mp4", data)
    t = _put(alvo, "sozinho.mp4", _blob(BIG + 1, seed=5))
    entries = _entries(t, r1, r2)
    ref_keys = {ic.cache_key(r1), ic.cache_key(r2)}
    spy = _Spy(monkeypatch)
    groups, _, stats, _ = ic.find_identical_files(entries, None, WORKERS, reference_keys=ref_keys)
    assert groups == [] and spy.samples == [] and spy.full == []
    assert stats["bytes_read"] == 0


def test_referencia_filtro_reaplicado_depois_de_dividir_o_balde(tmp_path):
    alvo, ref = str(tmp_path / "alvo"), str(tmp_path / "ref")
    data = _blob(BIG, seed=6)
    t1, t2 = _put(alvo, "t1.mp4", data), _put(alvo, "t2.mp4", data)
    r = _put(ref, "r_mesmo_tamanho.mp4", _blob(BIG, seed=7))     # mesmo tamanho, outro conteúdo
    entries = _entries(t1, t2, r)
    ref_keys = {ic.cache_key(r)}
    groups, _, _, _ = ic.find_identical_files(entries, None, WORKERS, reference_keys=ref_keys)
    assert _names(entries, groups) == [["t1.mp4", "t2.mp4"]]      # duplicata interna do alvo
    groups, _, _, _ = ic.find_identical_files(entries, None, WORKERS, reference_keys=ref_keys,
                                              hide_target_only=True)
    assert groups == []                                           # virou só-alvo: oculto
    # com a cópia de verdade na referência, o grupo fica nos dois modos
    r_dup = _put(ref, "r_dup.mp4", data)
    entries = _entries(t1, t2, r, r_dup)
    ref_keys.add(ic.cache_key(r_dup))
    groups, _, _, _ = ic.find_identical_files(entries, None, WORKERS, reference_keys=ref_keys,
                                              hide_target_only=True)
    assert _names(entries, groups) == [["t1.mp4", "t2.mp4", "r_dup.mp4"]]


def test_segunda_rodada_com_cache_nao_le_nada_e_avisa_de_onde_veio_a_prova(tmp_path, monkeypatch):
    root = str(tmp_path / "dados")
    big, small = _blob(BIG, seed=2), _blob(900, seed=3)
    paths = [_put(root, "g1.mkv", big), _put(root, "g2.mkv", big),
             _put(root, "p1.txt", small), _put(root, "p2.txt", small)]
    entries = _entries(*paths)
    db = str(tmp_path / "cache.sqlite")
    cache = ic.FileHashCache(db)
    first = ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    assert first[2]["md5_from_cache"] == []                  # tudo lido nesta rodada
    spy = _Spy(monkeypatch)
    cache = ic.FileHashCache(db)
    second = ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    assert spy.samples == [] and spy.full == []
    assert second[0] == first[0] and second[1] == first[1]
    assert second[2]["bytes_read"] == 0
    assert sorted(second[2]["md5_from_cache"]) == sorted(paths)   # quem apagar tem de reconferir
    # arquivo alterado (mtime novo) é relido por inteiro
    os.utime(paths[0], (T0 + 99, T0 + 99))
    entries = _entries(*paths)
    cache = ic.FileHashCache(db)
    third = ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    assert spy.samples == [("g1.mkv", "head"), ("g1.mkv", "rest")] and spy.full == ["g1.mkv"]
    assert paths[0] not in third[2]["md5_from_cache"] and paths[1] in third[2]["md5_from_cache"]


def test_conteudo_alterado_sem_mudar_tamanho_nem_data_e_pego_na_reconferencia(tmp_path):
    """O cache vale por caminho + tamanho + data. Um programa que altera o
       conteúdo preservando os três (contêiner VeraCrypt, editor de tags com
       "manter a data") engana o cache: o motor avisa que a prova é antiga
       (md5_from_cache) e verify_cached_proof, relendo agora, desmente."""
    root = str(tmp_path / "dados")
    data = _blob(BIG, seed=21)
    a, b = _put(root, "cofre.hc", data), _put(root, "cofre_backup.hc", data)
    entries = _entries(a, b)
    db = str(tmp_path / "cache.sqlite")
    cache = ic.FileHashCache(db)
    ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    changed = bytearray(data)
    changed[400_000:400_016] = b"dado novo de hoje"[:16]      # fora das amostras
    with open(a, "r+b") as f:
        f.write(bytes(changed))
    os.utime(a, ns=(entries[0][2], entries[0][2]))            # data preservada
    assert _entries(a, b) == entries                          # para o cache, nada mudou
    cache = ic.FileHashCache(db)
    groups, md5s, stats, _ = ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    assert _names(entries, groups) == [["cofre.hc", "cofre_backup.hc"]]   # o cache foi enganado...
    assert sorted(stats["md5_from_cache"]) == sorted([a, b])              # ...mas avisou
    ok_a, actual = ic.verify_cached_proof(a, BIG, md5s[0])
    assert ok_a is False and actual == hashlib.md5(bytes(changed)).hexdigest()
    assert ic.verify_cached_proof(b, BIG, md5s[1]) == (True, md5s[1])
    assert ic.verify_cached_proof(str(tmp_path / "sumiu.hc"), BIG, md5s[0]) == (False, None)
    sem_cache, _, _, _ = ic.find_identical_files(entries, None, WORKERS)
    assert sem_cache == []                                                # lendo de verdade, não são iguais


def test_chave_gravada_com_outro_tamanho_de_bloco_nao_e_comparada(tmp_path, monkeypatch):
    root = str(tmp_path / "dados")
    data = _blob(BIG, seed=31)
    paths = [_put(root, "m1.mp4", data), _put(root, "m2.mp4", data)]
    entries = _entries(*paths)
    db = str(tmp_path / "cache.sqlite")
    cache = ic.FileHashCache(db)
    cache.store(*entries[0], head="4096:" + "0" * 32, quick="4096:" + "f" * 32)   # versão com bloco de 4 KB
    cache.flush()
    spy = _Spy(monkeypatch)
    groups, _, _, _ = ic.find_identical_files(entries, cache, WORKERS)
    cache.close()
    assert _names(entries, groups) == [["m1.mp4", "m2.mp4"]]       # a duplicata NÃO se perdeu
    assert spy.part("head") == ["m1.mp4", "m2.mp4"]                # a chave antiga foi ignorada e refeita
    assert ic.file_quick_key(paths[0], BIG)[1].startswith(f"{CHUNK}:")


def test_cache_preserva_campos_e_zera_quando_o_arquivo_muda(tmp_path):
    db = str(tmp_path / "c.sqlite")
    cache = ic.FileHashCache(db)
    fp = str(tmp_path / "v.mp4")
    vazio = dict.fromkeys(ic.FileHashCache.FIELDS)
    cache.store(fp, 100, 5, md5="m1")
    cache.store(fp, 100, 5, stream="s1")               # mesmo arquivo: soma os campos
    cache.flush()
    assert cache.lookup_many([(fp, 100, 5)])[fp] == {**vazio, "md5": "m1", "stream": "s1"}
    assert cache.lookup_many([(fp, 100, 6)]) == {}       # data diferente: inválido
    cache.store(fp, 100, 6, quick="q2")                  # arquivo mudou: zera o resto
    cache.flush()
    assert cache.lookup_many([(fp, 100, 6)])[fp] == {**vazio, "quick": "q2"}
    with pytest.raises(ValueError):
        cache.store(fp, 100, 6, phash="x")
    cache.store(fp + "_sem_data", 100, None, md5="m9")   # sem data não dá para validar depois: recusa
    cache.flush()
    assert cache.lookup_many([(fp + "_sem_data", 100, None)]) == {}
    cache.close()
    # convive com o HashCache no mesmo arquivo SQLite
    hc = ic.HashCache(db)
    assert hc.active
    hc.close()
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert ic.FileHashCache.TABLE in tables and "hashes_v1_full" in tables


def test_cache_de_versao_com_menos_colunas_ganha_as_que_faltam(tmp_path):
    db = str(tmp_path / "antigo.sqlite")
    conn = sqlite3.connect(db)
    conn.execute(f"CREATE TABLE {ic.FileHashCache.TABLE} (path TEXT PRIMARY KEY, size INTEGER, "
                 "mtime_ns INTEGER, quick TEXT, md5 TEXT)")
    conn.execute(f"INSERT INTO {ic.FileHashCache.TABLE} VALUES (?,?,?,?,?)",
                 (ic.cache_key(str(tmp_path / "v.mp4")), 10, 20, None, "antigo"))
    conn.commit()
    conn.close()
    cache = ic.FileHashCache(db)
    assert cache.active
    fp = str(tmp_path / "v.mp4")
    assert cache.lookup_many([(fp, 10, 20)])[fp]["md5"] == "antigo"    # nada se perdeu
    cache.store(fp, 10, 20, head="h")
    cache.flush()
    assert cache.lookup_many([(fp, 10, 20)])[fp]["head"] == "h"
    cache.close()


def test_cancelar_no_meio_do_md5_completo_preserva_as_amostras_e_nao_grava_md5(tmp_path):
    root = str(tmp_path / "dados")
    data = _blob(BIG, seed=8)
    paths = [_put(root, f"c{k}.mp4", data) for k in range(4)]
    entries = _entries(*paths)
    cache = ic.FileHashCache(str(tmp_path / "c.sqlite"))
    state = {"phase": None}
    groups, md5s, stats, cancelled = ic.find_identical_files(
        entries, cache, WORKERS,
        phase_cb=lambda phase, n, total: state.update(phase=phase),
        cancel_check=lambda: state["phase"] == "full")        # só cancela quando o MD5 completo começa
    assert cancelled is True and groups == [] and md5s == {}
    assert state["phase"] == "full" and stats["head_computed"] == 4 and stats["quick_computed"] == 4
    found = cache.lookup_many(entries)
    assert all(v["head"] and v["quick"] for v in found.values())    # o trabalho feito ficou
    cache.close()
    ev = threading.Event()
    ev.set()
    with pytest.raises(ic.ScanCancelled):
        ic.file_full_md5(paths[0], len(data), ev)


def test_ordem_deterministica_um_caminho_em_um_so_grupo_e_progresso(tmp_path):
    root = str(tmp_path)
    a, b = _blob(BIG, seed=11), _blob(2000, seed=12)
    paths = [_put(root, "1_b.txt", b), _put(root, "2_a.mp4", a), _put(root, "3_b.txt", b),
             _put(root, "4_a.mp4", a), _put(root, "5_a.mp4", a)]
    entries = _entries(*paths)
    progress, phases = [], []
    groups, md5s, stats, _ = ic.find_identical_files(
        entries, None, WORKERS,
        progress_cb=lambda done, total, fp: progress.append((done, total)),
        phase_cb=lambda phase, n, total: phases.append((phase, n, total)))
    assert _names(entries, groups) == [["1_b.txt", "3_b.txt"], ["2_a.mp4", "4_a.mp4", "5_a.mp4"]]
    flat = [i for g in groups for i in g]
    assert len(flat) == len(set(flat))
    assert phases == [("head", 5, 2 * 2000 + 3 * CHUNK), ("quick", 3, 3 * 2 * CHUNK), ("full", 3, 3 * BIG)]
    assert progress[-1] == (3 * BIG, 3 * BIG)             # termina em 100%
    assert all(done <= total for done, total in progress)
    built = ic.build_byte_groups(entries, groups, md5s)
    assert built[0] == [(paths[0], None, md5s[0]), (paths[2], None, md5s[2])]
    assert stats["identical_files"] == 5
