"""
"Mesmo vídeo" na interface (Tk real, diálogos simulados, ffmpeg de verdade):
o remux entra no grupo da cópia exata, os botões mantêm um arquivo por grupo,
a prova vinda do cache é refeita antes de excluir, sem ffmpeg nada muda, e o
"Localizar..." só aceita um ffmpeg que funciona.
"""
import gc
import os
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

import ffmpegtools as ft
import main as ic
from conftest import T0
from test_ffmpegtools import FFMPEG, make_videos, needs_ffmpeg


def build_video_fixture(base):
    root = os.path.join(base, "fx")
    v = make_videos(root)
    shutil.copyfile(v["original"], os.path.join(root, "copia_exata.mp4"))
    os.remove(v["remux_mkv"])                 # o mkv depende de haver ffprobe: fora desta fixture
    for k, name in enumerate(["original.mp4", "copia_exata.mp4", "remux_outro_nome.mov",
                              "recodificado.mp4", "outro_video.mp4"]):
        os.utime(os.path.join(root, name), (T0 + k, T0 + k))
    return root


def _start(root, monkeypatch, folder, use_cache=0, ffmpeg=True, same_video=1):
    for w in root.winfo_children():
        w.destroy()
    gc.collect()
    msgs = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda title, msg, _n=name: msgs.append((_n, title, msg)))
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg: msgs.append(("askyesno", title, msg)) or True)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: folder)
    done = {}
    orig = ic.ImageCleaner.scan_folder

    def scan_folder(self):
        try:
            orig(self)
        finally:
            done["ok"] = True

    monkeypatch.setattr(ic.ImageCleaner, "scan_folder", scan_folder)
    app = ic.ImageCleaner(root)
    app.select_folder()
    app.use_cache_var.set(use_cache)
    app.scan_photos_var.set(0)
    app.scan_videos_var.set(1)
    app.same_video_var.set(same_video)
    if not ffmpeg:
        app.ffmpeg_path = None
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 120 and not done.get("ok"):
        root.update()
        time.sleep(0.01)
    t1 = time.time()
    while time.time() - t1 < 0.4:
        root.update()
        time.sleep(0.01)
    return app, msgs


@pytest.fixture
def env(tk_root, tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_hash_cache_path", lambda: str(tmp_path / "cache_teste.sqlite"))
    trash = tmp_path / "lixeira"
    trash.mkdir()
    monkeypatch.setattr(ic, "_send2trash", lambda p: shutil.move(p, str(trash / os.path.basename(p))))
    yield tk_root, str(tmp_path), str(trash)
    for w in tk_root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass


def _names(app):
    return [sorted(os.path.basename(fp) for fp, _, _ in g) for g in app.groups]


def _buttons(widget, out):
    for c in widget.winfo_children():
        if isinstance(c, tk.Button):
            out.append(str(c.cget("text")))
        _buttons(c, out)
    return out


def _statuses(app, idx=0):
    data = app.group_check_vars[idx]
    return {os.path.basename(im['filepath']): ic.image_status(im, data['md5_count']) for im in data['images']}


@needs_ffmpeg
def test_remux_entra_no_grupo_e_a_selecao_mantem_um_arquivo(env, monkeypatch):
    root, base, trash = env
    fx = build_video_fixture(base)
    app, msgs = _start(root, monkeypatch, fx)
    assert _names(app) == [["copia_exata.mp4", "original.mp4", "remux_outro_nome.mov"]]
    assert _statuses(app) == {"original.mp4": "Idêntica", "copia_exata.mp4": "Idêntica",
                              "remux_outro_nome.mov": "Mesmo vídeo"}
    buttons = _buttons(app.groups_window, [])
    assert "Selecionar Todos Mesmo Vídeo" in buttons and "Selecionar Mesmo vídeo" in buttons
    assert "Mesmo vídeo: 1 grupo(s), 3 arq" in str(app.page_info_label.cget("text"))
    rows = [str(w[5].cget("text")) for _, w in sorted(app.row_widgets.items())]
    assert any("Status: Mesmo vídeo" in r for r in rows) and any("Status: Cópia exata" in r for r in rows)
    app.select_same_video_images()
    assert "2 arquivos 'Mesmo vídeo' foram selecionados" in msgs[-1][2]
    sel = sorted(os.path.basename(im['filepath']) for im in app.group_check_vars[0]['images'] if im['var'].get())
    assert sel == ["copia_exata.mp4", "remux_outro_nome.mov"]        # fica o mais antigo: original.mp4
    app.delete_all_selected()
    assert sorted(os.listdir(trash)) == ["copia_exata.mp4", "remux_outro_nome.mov"]
    assert os.path.exists(os.path.join(fx, "original.mp4"))


@needs_ffmpeg
def test_sem_ffmpeg_ou_com_a_opcao_desligada_so_copias_exatas(env, monkeypatch):
    root, base, trash = env
    fx = build_video_fixture(base)
    for kw in ({"ffmpeg": False}, {"same_video": 0}):
        app, msgs = _start(root, monkeypatch, fx, **kw)
        assert _names(app) == [["copia_exata.mp4", "original.mp4"]]
        assert "Selecionar Todos Mesmo Vídeo" not in _buttons(app.groups_window, [])
        assert app.byte_same_video == set()


@needs_ffmpeg
def test_prova_de_mesmo_video_vinda_do_cache_e_refeita_antes_de_excluir(env, monkeypatch):
    root, base, trash = env
    fx = build_video_fixture(base)
    os.remove(os.path.join(fx, "copia_exata.mp4"))                    # só o remux é "cópia"
    app, _ = _start(root, monkeypatch, fx, use_cache=1)               # 1ª varredura grava o cache
    assert _names(app) == [["original.mp4", "remux_outro_nome.mov"]] and app.byte_cached_stream == set()
    # o remux é alterado no lugar, sem mudar tamanho nem data (os dados do vídeo mudam)
    remux = os.path.join(fx, "remux_outro_nome.mov")
    st = os.stat(remux)
    with open(remux, "r+b") as f:
        data = bytearray(f.read())
        pos = data.find(b"mdat") + 64
        data[pos:pos + 32] = bytes(255 - b for b in data[pos:pos + 32])
        f.seek(0)
        f.write(data)
    os.utime(remux, ns=(st.st_atime_ns, st.st_mtime_ns))
    app, msgs = _start(root, monkeypatch, fx, use_cache=1)            # 2ª: o cache é enganado
    assert _names(app) == [["original.mp4", "remux_outro_nome.mov"]]
    assert len(app.byte_cached_stream) == 2
    app.select_same_video_images()                                    # seleciona o remux (mais novo)
    app.delete_all_selected()
    assert os.listdir(trash) == [] and os.path.exists(remux)          # a prova não se confirmou: fica
    assert "1 arquivo(s) NÃO foram tocados" in msgs[-1][2]


def test_linha_do_ffmpeg_na_tela_inicial_e_localizar(env, monkeypatch, tmp_path):
    root, base, trash = env
    for w in root.winfo_children():
        w.destroy()
    msgs = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg: msgs.append(msg))
    app = ic.ImageCleaner(root)
    app.scan_videos_var.set(0)
    app._on_kinds_changed()
    assert str(app.ffmpeg_btn.cget("state")) == "disabled" and "marque Vídeos" in str(app.ffmpeg_label.cget("text"))
    app.ffmpeg_path, app.ffmpeg_version = None, None
    app.scan_videos_var.set(1)
    app._on_kinds_changed()
    assert "ffmpeg não encontrado" in str(app.ffmpeg_label.cget("text"))
    assert str(app.same_video_check.cget("state")) == "disabled" and str(app.ffmpeg_btn.cget("state")) == "normal"
    assert app._same_video_ready() is False
    # "Localizar..." recusa o que não é ffmpeg e não salva nada
    fake = str(tmp_path / "ffmpeg.exe")
    with open(fake, "wb") as f:
        f.write(b"nada")
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **k: fake)
    app.locate_ffmpeg()
    assert msgs and "não é um ffmpeg que funciona" in msgs[-1] and app.ffmpeg_path is None
    assert ic.load_settings()["ffmpeg_path"] == ""
    if FFMPEG is None:
        return
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **k: FFMPEG)
    app.locate_ffmpeg()                                                # aceita, salva e libera a opção
    assert os.path.normcase(app.ffmpeg_path) == os.path.normcase(os.path.normpath(FFMPEG))
    assert os.path.normcase(ic.load_settings()["ffmpeg_path"]) == os.path.normcase(os.path.normpath(FFMPEG))
    assert str(app.same_video_check.cget("state")) == "normal" and "encontrado" in str(app.ffmpeg_label.cget("text"))
    assert app._same_video_ready() is True
    # validação em segundo plano: a linha sai de "conferindo" sozinha
    app.ffmpeg_version = None
    app._refresh_ffmpeg_status()
    assert "conferindo" in str(app.ffmpeg_label.cget("text"))
    t0 = time.time()
    while app.ffmpeg_version is None and time.time() - t0 < 30:
        root.update()
        time.sleep(0.02)
    assert app.ffmpeg_version and "encontrado" in str(app.ffmpeg_label.cget("text"))
