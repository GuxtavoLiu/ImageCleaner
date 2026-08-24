"""Funções puras de apresentação (sem Tk)."""
import pytest

import main as ic


def test_format_bytes():
    assert ic.format_bytes(0) == "0 bytes"
    assert ic.format_bytes(1023) == "1023 bytes"
    assert ic.format_bytes(1245) == "1,2 KB"
    assert ic.format_bytes(3_400_000_000) == "3,2 GB"
    assert ic.format_bytes(None) == "?"


def test_format_resolution():
    assert ic.format_resolution(None) == "desconhecida"
    assert ic.format_resolution((4608, 3456)) == "4608 x 3456 (16 MP)"
    assert ic.format_resolution((1920, 1080)) == "1920 x 1080 (2,1 MP)"
    assert ic.format_resolution((100, 100)) == "100 x 100 (0,0 MP)"


@pytest.mark.parametrize("fp, roots, esperado", [
    (r"E:\FOTOS\NB\Acervo\2014\foto.jpg", [("ALVO", r"E:\FOTOS\NB")], ("ALVO", r"Acervo\2014", "foto.jpg")),
    (r"e:\fotos\nb\foto.jpg", [("ALVO", r"E:\FOTOS\NB")], ("ALVO", "", "foto.jpg")),
    (r"E:\FOTOS\REF\2008\x.jpg", [("ALVO", r"E:\FOTOS\NB"), ("REF", r"E:\FOTOS\REF")], ("REF", "2008", "x.jpg")),
    (r"D:\outra\x.jpg", [("ALVO", r"E:\FOTOS\NB")], ("", r"D:\outra", "x.jpg")),
    (r"E:\FOTOS\NB2\x.jpg", [("ALVO", r"E:\FOTOS\NB")], ("", r"E:\FOTOS\NB2", "x.jpg")),
    (r"E:\FOTOS\NB\x.jpg", [("ALVO", ""), ("REF", r"E:\FOTOS\NB")], ("REF", "", "x.jpg")),
])
def test_shorten_path(fp, roots, esperado):
    assert ic.shorten_path(fp, roots) == esperado
