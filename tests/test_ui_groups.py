"""
Smoke da tela de grupos (Tk real, diálogos simulados): fila de revisão com
"Grupos verificados", seleção por grupo, ações globais. Pulado se o Tk não
conseguir abrir uma janela (ambiente sem display).
"""
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

import main as ic
from conftest import build_single_fixture


@pytest.fixture(scope="module")
def tk_root():
    """Um único interpretador Tk por módulo: criar um segundo Tk() no mesmo
       processo após destruir o primeiro falha de forma intermitente no
       Windows ("invalid command name tcl_findLibrary")."""
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


@pytest.fixture
def app_with_groups(tk_root, tmp_path, monkeypatch):
    root = tk_root
    for w in root.winfo_children():
        w.destroy()
    fx = build_single_fixture(str(tmp_path / "fx"))
    msgs = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda title, msg, _n=name: msgs.append((_n, title, msg)))
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg: msgs.append(("askyesno", title, msg)) or True)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: fx)
    app = ic.ImageCleaner(root)
    app.select_folder()
    app.use_cache_var.set(0)
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 60 and getattr(app, "groups_window", None) is None:
        root.update()
        time.sleep(0.02)
    assert app.groups, ("sem grupos", msgs)
    app.groups_per_page = 1          # força paginação com a fixture pequena (2 grupos)
    app.render_page()
    root.update()
    yield app, root, msgs
    for w in root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass


def _buttons(w, out):
    for c in w.winfo_children():
        if isinstance(c, tk.Button):
            out.append(str(c.cget("text")))
        _buttons(c, out)
    return out


def _titles(app):
    return [str(c.cget("text")) for c in app.content_frame.winfo_children()
            if isinstance(c, tk.LabelFrame)]


def _selected(app, idx):
    return sorted(os.path.basename(im['filepath'])
                  for im in app.group_check_vars[idx]['images'] if im['var'].get() == 1)


def test_fila_de_revisao(app_with_groups):
    app, root, msgs = app_with_groups
    assert app.pending_idx == [0, 1] and app.verified_idx == []
    assert _titles(app) == ["Grupo 1"]
    assert "Grupos verificados (0)" in _buttons(app.groups_window, [])

    # (a) selecionar + verificar o grupo 1: sai da fila, o grupo 2 sobe
    app.select_and_verify(0, "identical")
    root.update()
    assert app.pending_idx == [1] and app.verified_idx == [0]
    assert _titles(app) == ["Grupo 2"]
    assert app.current_page == 0
    # (b) a seleção feita permanece
    assert _selected(app, 0) == ["a_copy.jpg"]
    assert "Grupos verificados (1)" in _buttons(app.groups_window, [])

    # (g) "Selecionar Todas" não toca nos verificados
    app.group_check_vars[0]['images'][1]['var'].set(0)     # desmarca a_copy manualmente
    app.select_identical_images()
    assert _selected(app, 0) == []                         # verificado: intocado
    assert _selected(app, 1) == ["b_copy.png"]             # pendente: selecionado
    assert "1 grupo(s) já verificados não foram alterados" in msgs[-1][2]

    # (c) vista de verificados
    app.toggle_view()
    root.update()
    assert app.view_mode == "verified"
    assert _titles(app) == ["Grupo 1 ✓ verificado"]
    b = _buttons(app.groups_window, [])
    assert "Voltar para pendentes" in b and "← Voltar aos pendentes (1)" in b
    assert "Marcar verificado ✓" not in b

    # (d) desfazer reinsere na ordem original
    app.unverify_group(0)
    root.update()
    assert app.pending_idx == [0, 1] and app.verified_idx == []
    assert _titles(app) == []                              # vista verificados vazia
    app.toggle_view()
    root.update()
    assert app.view_mode == "pending" and _titles(app) == ["Grupo 1"]

    # (e) verificar o último grupo da última página volta uma página
    app.next_page(); root.update()
    assert app.current_page == 1 and _titles(app) == ["Grupo 2"]
    app.verify_group(1); root.update()
    assert app.current_page == 0 and _titles(app) == ["Grupo 1"]
    app.verify_group(0); root.update()
    assert app.pending_idx == [] and _titles(app) == []
    assert app.prev_btn.cget("state") == "disabled" and app.next_btn.cget("state") == "disabled"

    # (f) exclusão global conta imagens dos verificados
    assert _selected(app, 1) == ["b_copy.png"]
    app.delete_all_selected()
    confirm = [m for m in msgs if m[0] == "askyesno"][-1]
    assert "excluir 1 imagens" in confirm[2]
    assert not os.path.exists(os.path.join(os.path.dirname(app.selected_folder), "fx", "sub", "b_copy.png"))


def test_f1_f2_seguem_a_vista(app_with_groups):
    app, root, _ = app_with_groups
    app.verify_group(0); root.update()
    # só 1 pendente: F2 não avança
    app.next_page(); root.update()
    assert app.current_page == 0
    app.toggle_view(); root.update()
    assert _titles(app) == ["Grupo 1 ✓ verificado"]


def _row_labels(app, name):
    """Widgets da linha cujo rótulo de nome é `name`."""
    for (idx, pos), widgets in app.row_widgets.items():
        info = app.group_check_vars[idx]['images'][pos]
        if os.path.basename(info['filepath']) == name:
            return idx, pos, widgets
    raise AssertionError(f"linha {name} não encontrada")


def test_linha_resolucao_caminho_curto_e_cor(app_with_groups):
    app, root, _ = app_with_groups
    app.groups_per_page = 20
    app.render_page(); root.update()
    idx, pos, widgets = _row_labels(app, "a_copy.jpg")
    textos = [str(w.cget("text")) for w in widgets if isinstance(w, tk.Label)]
    assert "a_copy.jpg" in textos                       # nome em destaque
    assert "[ALVO] (raiz)" in textos                    # caminho curto com a tag
    info_text = next(t for t in textos if t.startswith("Status:"))
    assert "Resolução: 128 x 128 (0,0 MP)" in info_text
    assert "Idêntica" in info_text and "bytes)" in info_text
    # cor da linha acompanha a seleção
    var = app.group_check_vars[idx]['images'][pos]['var']
    assert widgets[0].cget("bg") == ic.PALETTE["bg"]
    var.set(1); root.update()
    assert widgets[0].cget("bg") == ic.PALETTE["selected_row"]
    assert widgets[2].cget("bg") == ic.PALETTE["selected_row"]   # checkbox também
    var.set(0); root.update()
    assert widgets[0].cget("bg") == ic.PALETTE["bg"]
    # re-render preserva a cor conforme o var
    var.set(1); app.render_page(); root.update()
    _, _, widgets2 = _row_labels(app, "a_copy.jpg")
    assert widgets2[0].cget("bg") == ic.PALETTE["selected_row"]
    # caminho em subpasta
    _, _, w_b = _row_labels(app, "b_copy.png")
    assert "[ALVO] sub" in [str(w.cget("text")) for w in w_b if isinstance(w, tk.Label)]


def test_menu_de_contexto_handlers(app_with_groups, monkeypatch):
    app, root, msgs = app_with_groups
    calls = []
    monkeypatch.setattr(os, "startfile", lambda p: calls.append(("start", p)), raising=False)
    monkeypatch.setattr(ic.subprocess, "Popen", lambda args, **k: calls.append(("popen", args)))
    fp = app.group_check_vars[0]['images'][0]['filepath']
    app.open_image(fp)
    app.open_in_explorer(fp)
    app.copy_path(fp)
    root.update()
    assert calls[0] == ("start", fp)
    assert calls[1][0] == "popen" and calls[1][1][:2] == ["explorer", "/select,"] and calls[1][1][2] == os.path.normpath(fp)
    assert root.clipboard_get() == fp
    # erro ao abrir vira mensagem, não exceção
    monkeypatch.setattr(os, "startfile", lambda p: (_ for _ in ()).throw(OSError("x")), raising=False)
    app.open_image(fp)
    assert msgs[-1][0] == "showerror"
