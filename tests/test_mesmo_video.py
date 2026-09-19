"""
Estágio "Mesmo vídeo" (find_same_videos): mesmos fluxos de áudio e vídeo em
arquivos diferentes. A escolha de candidatos é pura; o estágio completo usa o
ffmpeg de verdade (pulado se não houver) com vídeos gerados na hora.
"""
import os
import shutil

import pytest

import ffmpegtools as ft
import main as ic
from conftest import T0, WORKERS
from test_ffmpegtools import FFMPEG, make_videos, needs_ffmpeg


def _info(w=1280, h=720, codec="avc1", duration=10.0):
    return {"width": w, "height": h, "codec": codec, "duration": duration}


def test_candidatos_mesmo_balde_e_duracao_proxima():
    infos = {
        "a": _info(duration=10.00), "b": _info(duration=10.08),          # dentro da tolerância
        "c": _info(duration=25.0),                                        # mesma resolução, outra duração
        "d": _info(w=1920, h=1080, duration=10.0),                        # outra resolução
        "e": _info(codec="hvc1", duration=10.0),                          # outro codec
        "f": None, "g": {"width": None, "height": None, "codec": None, "duration": None},
    }
    assert ic.same_video_candidates(infos) == {"a", "b"}
    # contêineres diferentes dão nomes diferentes ao mesmo codec
    assert ic.same_video_candidates({"mp4": _info(codec="avc1"), "mkv": _info(codec="h264")}) == {"mp4", "mkv"}
    # duração desconhecida (MP4 fragmentado) é coringa: o balde inteiro entra
    wild = {"x": _info(duration=None), "y": _info(duration=99.0), "z": _info(duration=3.0)}
    assert ic.same_video_candidates(wild) == {"x", "y", "z"}
    assert ic.same_video_candidates({"so": _info()}) == set()


def test_cadeia_de_duracoes_proximas_entra_inteira():
    infos = {k: _info(duration=d) for k, d in (("a", 10.0), ("b", 10.08), ("c", 10.16), ("d", 10.5))}
    assert ic.same_video_candidates(infos) == {"a", "b", "c"}


def _entries(*paths):
    out = []
    for p in paths:
        st = os.stat(p)
        out.append((p, st.st_size, st.st_mtime_ns))
    return out


@pytest.fixture(scope="module")
def acervo(tmp_path_factory):
    if FFMPEG is None:
        pytest.skip("ffmpeg não encontrado nesta máquina")
    root = str(tmp_path_factory.mktemp("acervo"))
    v = make_videos(root)
    v["copia"] = os.path.join(root, "copia_exata.mp4")
    shutil.copyfile(v["original"], v["copia"])
    for k, name in enumerate(sorted(os.listdir(root))):
        os.utime(os.path.join(root, name), (T0 + k, T0 + k))
    return v


def _names(entries, groups):
    return [sorted(os.path.basename(entries[i][0]) for i in g) for g in groups]


@needs_ffmpeg
def test_remux_entra_no_grupo_da_copia_exata(acervo):
    order = ["original", "copia", "remux_mov", "remux_mkv", "recodificado", "outro"]
    entries = _entries(*(acervo[k] for k in order))
    groups, md5s, _, _ = ic.find_identical_files(entries, None, WORKERS)
    assert _names(entries, groups) == [["copia_exata.mp4", "original.mp4"]]
    phases = []
    out = ic.find_same_videos(entries, groups, md5s, FFMPEG, ft.ffprobe_beside(FFMPEG), None, WORKERS,
                              phase_cb=lambda ph, n, total: phases.append(ph))
    new_groups, new_md5, same_idx, streams, stats, cancelled = out
    assert cancelled is False and [p for p in phases] == ["probe", "stream"]
    esperado = ["copia_exata.mp4", "original.mp4", "remux_outro_nome.mov"]
    if ft.ffprobe_beside(FFMPEG):
        esperado = sorted(esperado + ["remux.mkv"])                     # o mkv precisa do ffprobe
    assert _names(entries, new_groups) == [esperado]                    # recodificado e outro ficam de fora
    assert same_idx == set(new_groups[0]) and len(set(streams.values())) == 1
    assert new_md5[0] == new_md5[1] == md5s[0]                          # cópia exata continua "Idêntica"
    assert new_md5[2].startswith("SIZE:")                               # o remux não é cópia exata
    assert len({new_md5[i] for i in new_groups[0]}) == len(new_groups[0]) - 1   # só um par de MD5 igual
    assert stats["merged_groups"] == 1 and stats["hashed"] == stats["candidates"] and not stats["errors"]
    # a regra de seleção mantém UM arquivo do grupo inteiro: o mais antigo
    images = [{'filepath': entries[i][0], 'md5': new_md5[i], 'same_video': True, 'is_reference': False,
               'mtime': entries[i][2] / 1e9, 'size': entries[i][1]} for i in new_groups[0]]
    sel = ic.plan_same_video_selection(images)
    assert len(sel) == len(images) - 1
    kept = [im for k, im in enumerate(images) if k not in sel][0]
    assert kept['mtime'] == min(im['mtime'] for im in images)
    images[1]['is_reference'] = True                                    # com referência: saem todos os do alvo
    assert sorted(ic.plan_same_video_selection(images)) == [k for k in range(len(images)) if k != 1]


@needs_ffmpeg
def test_cache_referencia_cancelamento_e_sem_ffmpeg(acervo, tmp_path):
    entries = _entries(acervo["original"], acervo["remux_mov"], acervo["outro"])
    assert ic.find_identical_files(entries, None, WORKERS)[0] == []     # nenhum é cópia exata
    same = lambda **kw: ic.find_same_videos(entries, [], {}, FFMPEG, None, workers=WORKERS, **kw)
    cache = ic.FileHashCache(str(tmp_path / "c.sqlite"))
    first = same(cache=cache)
    assert _names(entries, first[0]) == [["original.mp4", "remux_outro_nome.mov"]]
    assert first[4]["hashed"] == 3 and first[4]["stream_from_cache"] == []
    second = same(cache=cache)                                          # 2ª rodada: nada é relido
    assert second[0] == first[0] and second[4]["hashed"] == 0 and second[4]["probed"] == 0
    assert sorted(os.path.basename(p) for p in second[4]["stream_from_cache"]) == \
        ["original.mp4", "remux_outro_nome.mov"]
    cache.close()
    # vídeo sem cópia exata entrou com sentinela: nunca será "Idêntica"
    assert all(str(m).startswith("SIZE:") for m in first[1].values())
    # grupo só do alvo some com "ocultar duplicatas internas"; com a referência no grupo, fica
    ref = {ic.cache_key(acervo["outro"])}
    assert same(reference_keys=ref, hide_target_only=True)[0] == []
    ref = {ic.cache_key(acervo["remux_mov"])}
    assert len(same(reference_keys=ref, hide_target_only=True)[0]) == 1
    # cancelado e sem ffmpeg: devolve o que recebeu
    out = same(cancel_check=lambda: True)
    assert out[5] is True and out[0] == [] and out[2] == set()
    out = ic.find_same_videos(entries, [], {}, None)
    assert out[0] == [] and out[5] is False and out[4]["hashed"] == 0


def test_status_mesmo_video():
    base = {'md5': 'SIZE:1:0', 'same_photo': None}
    assert ic.image_status({**base, 'same_video': True}, {'SIZE:1:0': 1}) == "Mesmo vídeo"
    assert ic.image_status({'md5': 'm', 'same_video': True}, {'m': 2}) == "Idêntica"      # cópia exata vence
    assert ic.image_status(base, {'SIZE:1:0': 1}) == "Semelhante"
    assert ic.plan_same_video_selection([{'same_video': True, 'mtime': 1, 'size': 1}]) == []
