"""
A armadilha da janela minimizada (Tk no Windows): um modal (transient + grab)
criado com a dona minimizada nasce invisível e prende o programa; minimizar a
dona com um modal aberto também a deixava sem volta pela barra de tarefas.
Tk real: a dona é restaurada antes do modal, o grab é solto enquanto ela está
minimizada e volta ao modal mais recente quando ela reaparece.
"""
import gc
import time
import tkinter as tk

import pytest

import main as ic


def _pump(root, seconds=0.3):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.01)


@pytest.fixture
def app(tk_root):
    for w in tk_root.winfo_children():
        w.destroy()
    gc.collect()
    tk_root.deiconify()
    _pump(tk_root)
    a = ic.ImageCleaner(tk_root)
    yield tk_root, a
    for w in tk_root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass
    tk_root.withdraw()


def test_relatorio_de_erros_com_a_principal_minimizada(app):
    root, a = app
    root.iconify()
    _pump(root)
    assert root.state() == "iconic"
    a.scan_errors = [{'type': 'Formato Inválido', 'filepath': 'x.jpg', 'message': 'm'}]
    a.scan_origin_counts = None
    a.byte_groups = []
    seen = {}

    def olha():
        win = root.grab_current()
        try:
            seen.update(root=root.state(), grab=win is not None,
                        viewable=bool(win is not None and win.winfo_viewable()),
                        title=win.title() if win is not None else None)
        finally:                                    # sem isto, wait_window nunca voltaria
            for w in root.winfo_children():
                if isinstance(w, tk.Toplevel):
                    w.destroy()

    root.after(500, olha)
    a.show_scan_summary(3, 2)                       # volta quando o relatório é destruído
    assert seen == {"root": "normal", "grab": True, "viewable": True,
                    "title": "Relatório de Escaneamento"}


@pytest.mark.parametrize("dona", ["principal", "toplevel"])
def test_minimizar_a_dona_com_modal_aberto_solta_o_grab_e_devolve(app, dona):
    root, a = app
    master = root if dona == "principal" else tk.Toplevel(root)
    _pump(root)
    win = tk.Toplevel(master)
    a._make_modal(win, master)
    _pump(root)
    assert root.grab_current() is win and win.winfo_viewable()

    master.iconify()
    _pump(root, 0.5)
    assert master.state() == "iconic"
    assert root.grab_current() is None
    master.deiconify()
    _pump(root, 0.5)
    assert master.state() == "normal"
    assert root.grab_current() is win and win.winfo_viewable()

    # um segundo modal por cima: ao voltar, o grab vai para o mais recente
    win2 = tk.Toplevel(master)
    a._make_modal(win2, master)
    _pump(root)
    master.iconify()
    _pump(root, 0.5)
    master.deiconify()
    _pump(root, 0.5)
    assert root.grab_current() is win2

    # sem modal vivo, minimizar e voltar não mexe em grab nenhum
    win2.destroy()
    win.destroy()
    _pump(root)
    master.iconify()
    _pump(root, 0.3)
    master.deiconify()
    _pump(root, 0.3)
    assert root.grab_current() is None
    assert a._modal_windows == []              # as mortas foram varridas nos eventos
