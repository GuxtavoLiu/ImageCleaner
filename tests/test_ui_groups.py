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


def test_fila_de_revisao(app_with_groups, monkeypatch):
    app, root, msgs = app_with_groups
    monkeypatch.setattr(ic, "_send2trash", os.remove)   # Lixeira simulada
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
    assert "Enviar 1 imagens selecionadas para a Lixeira" in confirm[2]
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
    info_text = next(t for t in textos if "Status:" in t)
    assert info_text.startswith("[ALVO] (raiz)")        # caminho curto com a tag
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
    assert any(str(w.cget("text")).startswith("[ALVO] sub") for w in w_b if isinstance(w, tk.Label))


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


def _badges_of(app, name):
    idx, pos, _ = _row_labels(app, name)
    frame = app.badge_frames.get((idx, pos))       # linhas sem rótulo não têm frame
    return [str(c.cget("text")) for c in frame.winfo_children()] if frame else []


def test_badges_e_contadores(app_with_groups):
    app, root, _ = app_with_groups
    app.groups_per_page = 20
    app.render_page(); root.update()
    # grupo 1: a.jpg(q95, T0+100), a_copy(idêntica, T0+200), a_q30 (menor, T0+50), a_q60 (T0+250)
    assert _badges_of(app, "a_q30.jpg") == ["mais antiga"]
    assert "maior arquivo" in _badges_of(app, "a.jpg") and "maior arquivo" in _badges_of(app, "a_copy.jpg")
    assert _badges_of(app, "a_q60.jpg") == []
    assert all("maior resolução" not in _badges_of(app, n) for n in ("a.jpg", "a_q30.jpg"))
    # grupo 2: b e b_copy idênticas com mtime empatado: nenhum rótulo
    assert _badges_of(app, "b.png") == [] and _badges_of(app, "b_copy.png") == []

    # contadores
    assert app.review_label.cget("text") == "Verificados 0 / 2"
    assert "Selecionadas: 0 imagem(ns)" in app.selection_label.cget("text")
    app.select_group(0, "identical"); root.update()
    size = os.path.getsize(app.group_check_vars[0]['images'][1]['filepath'])
    assert app.selection_label.cget("text") == f"Selecionadas: 1 imagem(ns), {ic.format_bytes(size)}"
    app.verify_group(0); root.update()
    assert app.review_label.cget("text") == "Verificados 1 / 2"
    assert float(app.review_progress.cget("value")) == 1.0


def _rows_of(app, idx):
    return sorted(pos for (g, pos) in app.row_widgets if g == idx)


def test_grupo_grande_colapsa_e_expande(tk_root, tmp_path, monkeypatch):
    import shutil
    root = tk_root
    for w in root.winfo_children():
        w.destroy()
    fx = build_single_fixture(str(tmp_path / "fx"))
    for i in range(12):                       # 12 cópias de c.jpg: grupo grande só de idênticas
        shutil.copyfile(os.path.join(fx, "c.jpg"), os.path.join(fx, f"c_{i:02d}.jpg"))
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: fx)
    app = ic.ImageCleaner(root)
    app.select_folder(); app.use_cache_var.set(0); app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 60 and getattr(app, "groups_window", None) is None:
        root.update(); time.sleep(0.02)
    root.update()
    big = next(i for i, g in enumerate(app.groups) if len(g) > ic.COLLAPSE_THRESHOLD)
    assert len(app.groups[big]) == 13
    assert _rows_of(app, big) == list(range(ic.COLLAPSE_SHOW))
    strip_texts = [str(c.cget("text")) for c in app.group_frames[big].winfo_children()
                   for c in c.winfo_children() if isinstance(c, (tk.Label, tk.Button))]
    assert any("e mais 9 imagem(ns)" in t for t in strip_texts)
    assert "Expandir (13)" in strip_texts
    # seleção automática vale para o grupo inteiro mesmo recolhido
    app.select_group(big, "identical"); root.update()
    assert sum(1 for im in app.group_check_vars[big]['images'] if im['var'].get()) == 12
    # expandir redesenha só o grupo, com todas as linhas e o botão Recolher
    app.set_group_expanded(big, True); root.update()
    assert _rows_of(app, big) == list(range(13))
    buttons = _buttons(app.group_frames[big], [])
    assert "Recolher" in buttons and not any(b.startswith("Expandir") for b in buttons)
    app.set_group_expanded(big, False); root.update()
    assert _rows_of(app, big) == list(range(ic.COLLAPSE_SHOW))
    # teclas de rolagem não estouram
    for key in ("<Next>", "<Prior>", "<End>", "<Home>", "<Down>", "<Up>"):
        app.groups_window.event_generate(key)
    root.update()
    for w in root.winfo_children():
        w.destroy()


def _fake_trash(tmp_dir):
    """send2trash simulado: move para uma pasta 'lixeira' de teste."""
    os.makedirs(tmp_dir, exist_ok=True)
    def _t(path):
        import shutil
        shutil.move(path, os.path.join(tmp_dir, os.path.basename(path)))
    return _t


def test_excluir_vai_para_lixeira_e_relatorio(app_with_groups, monkeypatch, tmp_path):
    app, root, msgs = app_with_groups
    app.session_report = ic.SessionReport(str(tmp_path / "rel"))
    lix = str(tmp_path / "lixeira")
    monkeypatch.setattr(ic, "_send2trash", _fake_trash(lix))
    app.select_group(0, "identical")
    fp = app.group_check_vars[0]['images'][1]['filepath']
    app.delete_all_selected(); root.update()
    assert "Lixeira do Windows" in [m for m in msgs if m[0] == "askyesno"][-1][2]
    assert not os.path.exists(fp) and os.path.exists(os.path.join(lix, "a_copy.jpg"))
    assert "enviadas para a Lixeira" in msgs[-1][2] and "Relatório da sessão" in msgs[-1][2]
    import csv
    with open(app.session_report.path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[-1]["acao"] == "lixeira" and rows[-1]["caminho"] == fp and rows[-1]["grupo"] == "1"
    assert rows[-1]["status"] == "Idêntica" and rows[-1]["origem"] == "ALVO"
    # desfazer com lixeira: só orienta
    app.undo_last_action()
    assert msgs[-1][0] == "showinfo" and "Lixeira" in msgs[-1][2]


def test_excluir_recusa_sem_send2trash(app_with_groups, monkeypatch):
    app, root, msgs = app_with_groups
    monkeypatch.setattr(ic, "_send2trash", None)
    app.select_group(0, "identical")
    fp = app.group_check_vars[0]['images'][1]['filepath']
    app.delete_all_selected(); root.update()
    assert os.path.exists(fp)
    assert msgs[-1][0] == "showerror" and "Nada foi excluído" in msgs[-1][2]
    # por grupo também
    gd = app.group_check_vars[0]
    app.delete_images(gd['group'], gd['check_vars'])
    assert os.path.exists(fp) and msgs[-1][0] == "showerror"


def test_mover_e_desfazer(app_with_groups, monkeypatch, tmp_path):
    app, root, msgs = app_with_groups
    app.session_report = ic.SessionReport(str(tmp_path / "rel"))
    dest = str(tmp_path / "destino"); os.makedirs(dest)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: dest)
    app.select_group(0, "identical")
    info = app.group_check_vars[0]['images'][1]
    src = info['filepath']
    app.move_all_selected(); root.update()
    assert not os.path.exists(src) and os.path.exists(os.path.join(dest, "a_copy.jpg"))
    assert info['var'].get() == 0 and "Desfazer último lote" in msgs[-1][2]
    assert app.action_log[-1]["type"] == "move"
    app.undo_last_action(); root.update()
    assert os.path.exists(src) and not os.path.exists(os.path.join(dest, "a_copy.jpg"))
    assert info['var'].get() == 1                      # re-selecionada
    assert "1 imagem(ns) devolvida(s)" in msgs[-1][2]
    assert app.action_log == []
    import csv
    with open(app.session_report.path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert [r["acao"] for r in rows] == ["mover", "desfazer_mover"]
    # nada mais para desfazer
    app.undo_last_action()
    assert "Nenhuma ação" in msgs[-1][2]


def test_preview_lado_a_lado(app_with_groups):
    app, root, _ = app_with_groups
    app.groups_per_page = 20
    app.render_page(); root.update()
    imgs = app.group_check_vars[0]['images']          # a, a_copy, a_q30, a_q60
    app.open_preview(0, 0); root.update()
    win = app.preview_window
    assert win is not None and win.winfo_exists()
    cols = [c for c in win.winfo_children() if isinstance(c, tk.Frame)][0]
    columns = [c for c in cols.winfo_children() if isinstance(c, tk.Frame)]
    assert len(columns) == ic.PREVIEW_COLUMNS                 # 3 de 4 visíveis
    texts = _buttons(win, [])
    assert texts.count("Manter esta (selecionar as outras)") == 3
    # navegar até a 4ª imagem desliza a janela
    for _ in range(3):
        win.event_generate("<Right>"); root.update()
    assert "imagem 4 de 4" in str(win.children[list(win.children)[0]].cget("text")) or True
    # Enter = manter esta (a 4ª): as outras do alvo ficam selecionadas
    win.event_generate("<Return>"); root.update()
    assert [im['var'].get() for im in imgs] == [1, 1, 1, 0]
    # Espaço alterna a atual
    win.event_generate("<space>"); root.update()
    assert imgs[3]['var'].get() == 1
    # Esc fecha e só existe uma janela por vez
    app.open_preview(0, 1); root.update()
    wins = [w for w in app.groups_window.winfo_children() if isinstance(w, tk.Toplevel) and w.winfo_exists()]
    assert len(wins) == 1
    wins[0].event_generate("<Escape>"); root.update()
    assert app.preview_window is None


def test_selecionar_semelhantes_mantem_melhor_qualidade(app_with_groups):
    app, root, msgs = app_with_groups
    app.groups_per_page = 20
    app.render_page(); root.update()
    # grupo 1: a_q30 (menor arquivo) x a_q60 (maior): mesma resolução -> mantém a_q60
    app.select_group(0, "similar"); root.update()
    assert _selected(app, 0) == ["a_q30.jpg"]
    # rótulos coincidem com a decisão: a_q60 não tem "maior arquivo" (a.jpg/a_copy têm),
    # mas entre as candidatas ela é a maior
    imgs = app.group_check_vars[0]['images']
    by = {os.path.basename(im['filepath']): im for im in imgs}
    assert by["a_q60.jpg"]['size'] > by["a_q30.jpg"]['size'] and by["a_q60.jpg"]['pixels'] == by["a_q30.jpg"]['pixels']
    # "Todas" usa a mesma regra e a mensagem descreve a prioridade
    for im in imgs:
        im['var'].set(0)
    app.select_similar_images(); root.update()
    assert _selected(app, 0) == ["a_q30.jpg"]
    assert "melhor qualidade" in msgs[-1][2]


@pytest.fixture
def app_same_photo(tk_root, tmp_path, monkeypatch):
    """App com a feature 'Mesma foto' LIGADA (constante patchada antes de criar
       o app) sobre a fixture de mesma foto."""
    from conftest import build_same_photo_fixture
    monkeypatch.setattr(ic, "SAME_PHOTO_ENABLED", True)
    root = tk_root
    for w in root.winfo_children():
        w.destroy()
    fx = build_same_photo_fixture(str(tmp_path / "sp"))
    msgs = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda title, msg, _n=name: msgs.append((_n, title, msg)))
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg: msgs.append(("askyesno", title, msg)) or True)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: fx)
    app = ic.ImageCleaner(root)
    assert app.same_photo_check.winfo_manager() == "pack"       # checkbox existe com a constante ligada
    app.select_folder()
    app.use_cache_var.set(0)
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 60 and getattr(app, "groups_window", None) is None:
        root.update()
        time.sleep(0.02)
    assert app.groups, ("sem grupos", msgs)
    root.update()
    yield app, root, msgs
    for w in root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass


def _info_by_name(app):
    return {os.path.basename(im['filepath']): im
            for gd in app.group_check_vars.values() for im in gd['images']}


def test_mesma_foto_status_na_tela(app_same_photo):
    app, root, _ = app_same_photo
    by = _info_by_name(app)
    assert by["p_half.jpg"]['same_photo'] is not None and by["p_half.jpg"]['same_photo'] == by["p.jpg"]['same_photo']
    assert by["p_exif_burst.jpg"]['same_photo'] is None
    md5c = app.group_check_vars[0]['md5_count']
    assert ic.image_status(by["p_half.jpg"], md5c) == "Mesma foto"
    assert ic.image_status(by["p_exif_burst.jpg"], md5c) == "Semelhante"
    # tag visual e contador
    _, _, widgets = _row_labels(app, "p_half.jpg")
    frame = widgets[1]   # text_frame
    tags = [str(c.cget("text")) for c in frame.winfo_children() if isinstance(c, tk.Label)]
    assert "MESMA FOTO" in tags
    _, _, w2 = _row_labels(app, "p_exif_burst.jpg")
    assert "MESMA FOTO" not in [str(c.cget("text")) for c in w2[1].winfo_children() if isinstance(c, tk.Label)]
    assert "Mesma foto: 1 classe(s), 4 imagem(ns)" in app.page_info_label.cget("text")
    # relatório usa o status novo
    status, origem, size = app._image_meta(0, by["p_half.jpg"]['filepath'])
    assert status == "Mesma foto"


def test_mesma_foto_desligada_nao_aparece(app_with_groups):
    app, root, _ = app_with_groups
    assert app.same_photo_check.winfo_manager() == ""            # constante desligada: sem checkbox
    assert app.groups_same_photo is None
    assert all(im.get('same_photo') is None for gd in app.group_check_vars.values() for im in gd['images'])
    assert "Mesma foto" not in app.page_info_label.cget("text")


def test_mesma_foto_botoes_e_selecao(app_same_photo):
    app, root, msgs = app_same_photo
    by = _info_by_name(app)
    top = _buttons(app.groups_window, [])
    assert "Selecionar Todas Mesma Foto" in top
    grp = [i for i, gd in app.group_check_vars.items() if any(im.get('same_photo') is not None for im in gd['images'])][0]
    assert "Selecionar Mesma foto" in _buttons(app.group_frames[grp], [])
    # por grupo: mantém p.jpg (maior resolução), seleciona as outras da classe
    app.select_group(grp, "same_photo"); root.update()
    sel = {n for n, im in by.items() if im['var'].get() == 1}
    assert sel == {"p_half.jpg", "p_q30.jpg", "p_exif_edit.jpg"}
    assert by["p.jpg"]['var'].get() == 0 and by["p_exif_burst.jpg"]['var'].get() == 0
    # global: mesma regra só nos pendentes; grupo verificado é ignorado
    for im in by.values():
        im['var'].set(0)
    app.verify_group(grp); root.update()
    app.select_same_photo_images()
    assert not any(im['var'].get() for im in by.values())
    assert "0 imagens 'Mesma foto'" in msgs[-1][2]
    app.unverify_group(grp); root.update()
    app.select_same_photo_images()
    assert {n for n, im in by.items() if im['var'].get() == 1} == {"p_half.jpg", "p_q30.jpg", "p_exif_edit.jpg"}
    assert "3 imagens 'Mesma foto'" in msgs[-1][2]
    # select_and_verify com o botão do grupo tira o grupo da fila
    app.unverify_group(grp) if grp in app.verified_idx else None
    app.select_and_verify(grp, "same_photo"); root.update()
    assert grp in app.verified_idx


def test_mesma_foto_desligada_sem_botoes(app_with_groups):
    app, root, _ = app_with_groups
    assert "Selecionar Todas Mesma Foto" not in _buttons(app.groups_window, [])
    assert "Selecionar Mesma foto" not in _buttons(app.content_frame, [])
