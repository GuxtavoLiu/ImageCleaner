"""Lixeira (send2trash), relatório CSV da sessão e desfazer (mover)."""
import csv
import os
import shutil

import pytest

import main as ic


def test_session_report_cria_arquivo_no_primeiro_registro(tmp_path):
    rep = ic.SessionReport(str(tmp_path / "rel"))
    assert rep.path is None
    rep.record([])
    assert rep.path is None
    rep.record([{"acao": "mover", "grupo": 3, "status": "Idêntica", "origem": "ALVO",
                 "caminho": r"E:\a\x.jpg", "destino": r"E:\b\x.jpg", "tamanho": 123}])
    rep.record([{"acao": "lixeira", "grupo": 4, "status": "Semelhante", "origem": "ALVO",
                 "caminho": r"E:\a\y.jpg", "destino": "", "tamanho": 5}])
    assert rep.path and os.path.exists(rep.path)
    with open(rep.path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert [r["acao"] for r in rows] == ["mover", "lixeira"]
    assert rows[0]["caminho"] == r"E:\a\x.jpg" and rows[0]["tamanho"] == "123"
    assert set(rows[0]) == set(ic.SessionReport.COLUMNS)


def test_trash_file_usa_send2trash_ou_recusa(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ic, "_send2trash", lambda p: calls.append(p))
    ic.trash_file(str(tmp_path / "sub" / ".." / "a.jpg"))
    assert calls == [os.path.normpath(str(tmp_path / "sub" / ".." / "a.jpg"))]
    assert ic.trash_available()
    monkeypatch.setattr(ic, "_send2trash", None)
    assert not ic.trash_available()
    with pytest.raises(RuntimeError):
        ic.trash_file(str(tmp_path / "a.jpg"))
