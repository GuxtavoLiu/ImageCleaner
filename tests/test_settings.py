"""Configurações lembradas entre sessões (settings.json)."""
import json
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

import main as ic
from conftest import build_reference_fixture


def test_load_save_e_corrompido(tmp_path):
    path = str(tmp_path / "settings.json")
    st = ic.load_settings(path)
    assert st == ic.DEFAULT_SETTINGS and st is not ic.DEFAULT_SETTINGS
    st["recent_targets"] = [r"E:\a"]
    st["use_cache"] = 0
    ic.save_settings(st, path)
    again = ic.load_settings(path)
    assert again["recent_targets"] == [r"E:\a"] and again["use_cache"] == 0
    # corrompido: padrões
    open(path, "w").write("{ nao é json")
    assert ic.load_settings(path) == ic.DEFAULT_SETTINGS
    # tipo errado é ignorado
    json.dump({"use_cache": "sim", "recent_targets": "x"}, open(path, "w"))
    assert ic.load_settings(path) == ic.DEFAULT_SETTINGS


def test_push_recent():
    assert ic.push_recent([], r"E:\a") == [r"E:\a"]
    assert ic.push_recent([r"E:\a", r"E:\b"], r"e:\B") == [r"e:\B", r"E:\a"]     # dedupe sem caixa
    lst = [f"E:\\{i}" for i in range(5)]
    assert ic.push_recent(lst, r"E:\novo") == [r"E:\novo"] + lst[:4]


def test_tela_inicial_lembra_pastas_e_opcoes(tmp_path, monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"Tk indisponível: {e}")
    root.withdraw()
    try:
        alvo, ref = build_reference_fixture(str(tmp_path))
        path = str(tmp_path / "settings.json")
        monkeypatch.setattr(ic, "get_settings_path", lambda: path)
        json.dump({"recent_targets": [str(tmp_path / "sumiu"), alvo], "recent_references": [ref],
                   "scan_subfolders": 0, "use_cache": 0, "confirm_similar": 1, "show_target_only": 0},
                  open(path, "w"))
        for name in ("showinfo", "showerror", "showwarning"):
            monkeypatch.setattr(messagebox, name, lambda *a, **k: None)
        app = ic.ImageCleaner(root)
        # pasta inexistente ignorada; a seguinte é usada; referência aplicada
        assert app.selected_folder == alvo and app.reference_folder == ref
        assert app.scan_subfolders_var.get() == 0 and app.use_cache_var.get() == 0
        assert app.show_target_only_var.get() == 0
        assert app.start_btn.winfo_manager() == "pack"        # já dá para iniciar
        # menu Recentes lista só pastas existentes
        menu = app.recent_targets_mb["menu"]
        menu_widget = root.nametowidget(menu)
        app._fill_recent_menu(menu_widget, "recent_targets", app._apply_target_folder)
        assert menu_widget.entrycget(0, "label") == alvo and menu_widget.index("end") == 0
        # iniciar grava: opção alterada e pastas na frente
        app.use_cache_var.set(1)
        monkeypatch.setattr(app, "scan_folder", lambda: None)
        app.start_scan(); root.update()
        saved = json.load(open(path, encoding="utf-8"))
        assert saved["use_cache"] == 1 and saved["recent_targets"][0] == alvo
        assert saved["recent_references"] == [ref]
        assert str(tmp_path / "sumiu") in saved["recent_targets"]   # histórico preservado
    finally:
        for w in root.winfo_children():
            w.destroy()
        root.destroy()
