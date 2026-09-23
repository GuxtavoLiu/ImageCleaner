"""
Live Photos na interface (Tk real, diálogos simulados): o .MOV par acompanha a
foto que sai só quando sobra cópia idêntica dele; a pergunta só existe quando
há par; o par mantém o mesmo nome ao ser movido; Desfazer e CSV cobrem os dois.
"""
import csv
import gc
import os
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

import main as ic
from conftest import T0, _write
from test_live_photos import live_mov, plain_mov


def build_live_fixture(base, mov_in_a=True):
    """A/IMG_0001.JPG (mais antiga, mantida) e B/IMG_0001.JPG (cópia), cada uma
       com o seu IMG_0001.MOV de Live Photo idêntico; mais um par de fotos comum."""
    root = os.path.join(base, "fx")
    a, b = os.path.join(root, "A"), os.path.join(root, "B")
    _write(a, "IMG_0001.JPG", kind="A", mtime=T0 + 1)
    _write(b, "IMG_0001.JPG", copy_of=os.path.join("..", "A", "IMG_0001.JPG"), mtime=T0 + 50)
    _write(a, "comum.jpg", kind="B", mtime=T0 + 2)
    _write(b, "comum_copia.jpg", copy_of=os.path.join("..", "A", "comum.jpg"), mtime=T0 + 60)
    for folder in ((a, b) if mov_in_a else (b,)):
        with open(os.path.join(folder, "IMG_0001.MOV"), "wb") as f:
            f.write(live_mov())
    with open(os.path.join(b, "comum_copia.mov"), "wb") as f:      # vídeo comum: nunca é par
        f.write(plain_mov())
    return root


class _Dialogs:
    def __init__(self, monkeypatch, folder, live_answer=True, dest=None):
        self.msgs = []
        self.live_asked = []
        for name in ("showinfo", "showerror", "showwarning"):
            monkeypatch.setattr(messagebox, name,
                                lambda title, msg, _n=name: self.msgs.append((_n, title, msg)))
        monkeypatch.setattr(messagebox, "askyesno",
                            lambda title, msg: self.msgs.append(("askyesno", title, msg)) or True)

        def ask_live(title, msg):
            self.live_asked.append(msg)
            return live_answer

        monkeypatch.setattr(messagebox, "askyesnocancel", ask_live)
        self.folders = [folder]
        monkeypatch.setattr(filedialog, "askdirectory", lambda **k: self.folders[-1])


def _start(root, monkeypatch, dialogs, videos=0):
    for w in root.winfo_children():
        w.destroy()
    gc.collect()
    app = ic.ImageCleaner(root)
    app.select_folder()
    app.use_cache_var.set(0)
    app.scan_videos_var.set(videos)
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 60 and getattr(app, "groups_window", None) is None:
        root.update()
        time.sleep(0.01)
    t1 = time.time()
    while time.time() - t1 < 0.4:
        root.update()
        time.sleep(0.01)
    assert app.groups, dialogs.msgs
    return app


@pytest.fixture
def env(tk_root, tmp_path, monkeypatch):
    trash = tmp_path / "lixeira"
    trash.mkdir()
    monkeypatch.setattr(ic, "_send2trash",
                        lambda p: shutil.move(p, str(trash / (os.path.basename(os.path.dirname(p)) + "_" + os.path.basename(p)))))
    yield tk_root, str(tmp_path), str(trash)
    for w in tk_root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass


def _labels(widget, out):
    for c in widget.winfo_children():
        if isinstance(c, tk.Label):
            out.append(str(c.cget("text")))
        _labels(c, out)
    return out


def test_excluir_leva_o_video_par_e_registra_no_csv(env, monkeypatch):
    root, base, trash = env
    fx = build_live_fixture(base)
    d = _Dialogs(monkeypatch, fx, live_answer=True)
    app = _start(root, monkeypatch, d)
    assert sum("LIVE PHOTO" in t for t in _labels(app.content_frame, [])) == 2     # as duas IMG_0001
    app.select_identical_images()
    app.delete_all_selected()
    assert len(d.live_asked) == 1 and "1 das fotos selecionadas são Live Photos" in d.live_asked[0]
    confirm = [m for m in d.msgs if m[0] == "askyesno"][-1][2]
    assert "Enviar 2 imagens selecionadas para a Lixeira" in confirm            # o texto de sempre...
    assert "+ 1 vídeo(s) de Live Photo (.MOV ao lado da foto)." in confirm      # ...mais o prometido
    assert "+ 1 vídeo(s) de Live Photo enviado(s) à Lixeira junto com a foto." in d.msgs[-1][2]
    assert sorted(os.listdir(trash)) == ["B_IMG_0001.JPG", "B_IMG_0001.MOV", "B_comum_copia.jpg"]
    assert os.path.exists(os.path.join(fx, "A", "IMG_0001.MOV"))                 # a cópia que fica
    assert os.path.exists(os.path.join(fx, "B", "comum_copia.mov"))              # vídeo comum: intocado
    with open(app.session_report.path, encoding="utf-8-sig") as f:
        rows = {os.path.basename(r["caminho"]): r for r in csv.DictReader(f, delimiter=";")}
    assert rows["IMG_0001.MOV"]["status"] == "Live Photo (par)" and rows["IMG_0001.MOV"]["acao"] == "lixeira"
    assert rows["IMG_0001.JPG"]["status"] == "Cópia exata"


def test_nao_e_cancelar_na_pergunta(env, monkeypatch):
    root, base, trash = env
    fx = build_live_fixture(base)
    d = _Dialogs(monkeypatch, fx, live_answer=False)          # "Não": só as fotos
    app = _start(root, monkeypatch, d)
    app.select_identical_images()
    app.delete_all_selected()
    assert sorted(os.listdir(trash)) == ["B_IMG_0001.JPG", "B_comum_copia.jpg"]
    assert "1 vídeo(s) de Live Photo ficaram na pasta, a seu pedido." in d.msgs[-1][2]
    assert "+ 1 vídeo(s)" not in [m for m in d.msgs if m[0] == "askyesno"][-1][2]

    fx2 = build_live_fixture(os.path.join(base, "dois"))
    d = _Dialogs(monkeypatch, fx2, live_answer=None)          # "Cancelar": nada acontece
    app = _start(root, monkeypatch, d)
    app.select_identical_images()
    before = len(os.listdir(trash))
    app.delete_all_selected()
    assert len(os.listdir(trash)) == before and not [m for m in d.msgs if m[0] == "askyesno"]
    assert os.path.exists(os.path.join(fx2, "B", "IMG_0001.JPG"))


def test_sem_par_nenhuma_pergunta_nova_e_sem_copia_o_video_fica(env, monkeypatch):
    root, base, trash = env
    fx = build_live_fixture(base, mov_in_a=False)             # a foto mantida NÃO tem vídeo ao lado
    d = _Dialogs(monkeypatch, fx)
    app = _start(root, monkeypatch, d)
    app.select_identical_images()
    app.delete_all_selected()
    assert d.live_asked == []                                 # nada a oferecer: nenhum diálogo novo
    assert sorted(os.listdir(trash)) == ["B_IMG_0001.JPG", "B_comum_copia.jpg"]
    assert os.path.exists(os.path.join(fx, "B", "IMG_0001.MOV"))
    assert "1 vídeo(s) de Live Photo (.MOV ao lado da foto) ficaram onde estão" in d.msgs[-1][2]


def test_mover_mantem_o_par_com_o_mesmo_nome_e_desfazer_devolve_os_dois(env, monkeypatch):
    root, base, trash = env
    fx = build_live_fixture(base)
    d = _Dialogs(monkeypatch, fx)
    app = _start(root, monkeypatch, d)
    dest = os.path.join(base, "destino")
    os.makedirs(dest)
    with open(os.path.join(dest, "IMG_0001.MOV"), "wb") as f:  # só o nome do VÍDEO já existe no destino
        f.write(b"outro arquivo")
    d.folders.append(dest)
    names = [[os.path.basename(fp) for fp, _, _ in g] for g in app.groups]
    idx = names.index(["IMG_0001.JPG", "IMG_0001.JPG"])
    app.select_group(idx, "identical")
    group = app.group_check_vars[idx]
    app.move_images(group['group'], group['check_vars'])
    assert sorted(os.listdir(dest)) == ["IMG_0001.MOV", "IMG_0001_1.JPG", "IMG_0001_1.MOV"]   # par junto
    assert "+ 1 vídeo(s) de Live Photo movido(s) junto com a foto." in d.msgs[-1][2]
    app.undo_last_action()
    assert sorted(os.listdir(dest)) == ["IMG_0001.MOV"]
    assert os.path.exists(os.path.join(fx, "B", "IMG_0001.JPG")) and os.path.exists(os.path.join(fx, "B", "IMG_0001.MOV"))
    assert "2 imagem(ns) devolvida(s)" in d.msgs[-1][2]


def test_com_videos_marcado_avisa_da_foto_que_ficaria_sem_o_video(env, monkeypatch):
    root, base, trash = env
    fx = build_live_fixture(base)
    d = _Dialogs(monkeypatch, fx)
    app = _start(root, monkeypatch, d, videos=1)
    names = [[os.path.basename(fp) for fp, _, _ in g] for g in app.groups]
    idx = names.index(["IMG_0001.MOV", "IMG_0001.MOV"])
    app.select_group(idx, "identical")                         # só o vídeo de B; a foto de B fica
    group = app.group_check_vars[idx]
    app.delete_images(group['group'], group['check_vars'])
    confirm = [m for m in d.msgs if m[0] == "askyesno"][-1][2]
    assert "Atenção: 1 vídeo(s) selecionado(s) são a parte em vídeo de Live Photos cuja foto NÃO" in confirm
    assert d.live_asked == []
    # agora a foto de A sai (fica a de B): o vídeo de A é a ÚLTIMA cópia e não pode ir junto
    for im in app.group_check_vars[names.index(["IMG_0001.JPG", "IMG_0001.JPG"])]['images']:
        im['var'].set(1 if os.path.basename(os.path.dirname(im['filepath'])) == "A" else 0)
    app.delete_all_selected()
    assert d.live_asked == []
    assert os.path.exists(os.path.join(fx, "A", "IMG_0001.MOV"))
    assert "ficaram onde estão: não há cópia idêntica" in d.msgs[-1][2]
