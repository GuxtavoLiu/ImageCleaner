"""
Classificação dos arquivos (foto, foto só por bytes, vídeo, outro) e a
listagem única com predicado (make_scan_accept): uma caminhada só pelo disco,
fotos na mesma ordem relativa de sempre, e nada perigoso entrando na
comparação por bytes.
"""
import os
import types

import pytest

import main as ic
from conftest import build_single_fixture


@pytest.mark.parametrize("name, kind", [
    ("a.jpg", ic.KIND_PHOTO), ("A.JPEG", ic.KIND_PHOTO), ("x.png", ic.KIND_PHOTO),
    ("IMG_1.HEIC", ic.KIND_PHOTO_BYTES), ("raw.cr2", ic.KIND_PHOTO_BYTES), ("r.DNG", ic.KIND_PHOTO_BYTES),
    ("v.mp4", ic.KIND_VIDEO), ("V.MOV", ic.KIND_VIDEO), ("f.mkv", ic.KIND_VIDEO),
    ("doc.pdf", ic.KIND_OTHER), ("sem_extensao", ic.KIND_OTHER), ("codigo.ts", ic.KIND_OTHER),
])
def test_classify_file(name, kind):
    assert ic.classify_file(name) == kind


def test_conjuntos_de_extensoes_nao_se_sobrepoem():
    photo, pbytes, video = (set(ic.VALID_EXTENSIONS), set(ic.PHOTO_BYTES_EXTENSIONS),
                            set(ic.VIDEO_EXTENSIONS))
    assert not (photo & pbytes) and not (photo & video) and not (pbytes & video)
    assert all(e == e.lower() and e.startswith(".") for e in photo | pbytes | video)


def _mixed(root):
    build_single_fixture(root)
    extras = {
        "clip.mp4": b"v" * 500, "clip_copia.mp4": b"v" * 500,
        "foto.heic": b"h" * 300, "notas.txt": b"n" * 200,
        "vazio.txt": b"", "Thumbs.db": b"t" * 100, "desktop.ini": b"d" * 50,
        "~$rascunho.docx": b"o" * 80,
        os.path.join("sub", "filme.mov"): b"m" * 700,
        os.path.join("$RECYCLE.BIN", "apagada.jpg"): b"x" * 10,
        os.path.join("$RECYCLE.BIN", "apagado.mp4"): b"x" * 10,
        os.path.join("System Volume Information", "x.dat"): b"x" * 10,
    }
    for rel, data in extras.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return root


def _rel(root, entries):
    return [os.path.relpath(fp, root).replace("\\", "/") for (fp, _, _) in entries]


# A lista que a versão anterior (só fotos, por extensão) produzia para a fixture
# de sempre, escrita à mão: comparar com o próprio código novo não provaria nada.
FOTOS_DE_SEMPRE = ["a.jpg", "a_copy.jpg", "a_q30.jpg", "a_q60.jpg", "c.jpg", "sub/b.png", "sub/b_copy.png"]


def test_so_fotos_da_a_mesma_lista_de_sempre_mais_as_fotos_so_por_bytes(tmp_path):
    root = _mixed(str(tmp_path / "fx"))
    sem_predicado = ic.list_image_files(root, True, ic.VALID_EXTENSIONS)
    assert _rel(root, sem_predicado) == FOTOS_DE_SEMPRE
    novo = ic.list_image_files(root, True, ic.VALID_EXTENSIONS, accept=ic.make_scan_accept(True, False, False))
    fotos = [e for e in novo if ic.classify_file(e[0]) == ic.KIND_PHOTO]
    assert fotos == sem_predicado                           # mesmas entradas, mesma ordem
    assert [r for r in _rel(root, novo) if r not in FOTOS_DE_SEMPRE] == ["foto.heic"]


def test_pastas_de_sistema_nao_sao_percorridas_nem_para_fotos(tmp_path):
    """Mudança DELIBERADA em relação à versão anterior (que listava a Lixeira
       ao varrer a raiz de um disco): vale para todos os tipos, inclusive
       sem predicado."""
    root = _mixed(str(tmp_path / "fx"))
    assert os.path.exists(os.path.join(root, "$RECYCLE.BIN", "apagada.jpg"))
    for entries in (ic.list_image_files(root, True, ic.VALID_EXTENSIONS),
                    ic.list_image_files(root, True, accept=ic.make_scan_accept(True, True, True))):
        assert not any(r.startswith(("$RECYCLE.BIN", "System Volume Information"))
                       for r in _rel(root, entries))


def test_todos_os_tipos_numa_caminhada_so(tmp_path):
    root = _mixed(str(tmp_path / "fx"))
    tudo = _rel(root, ic.list_image_files(root, True, accept=ic.make_scan_accept(True, True, True)))
    for esperado in ("clip.mp4", "clip_copia.mp4", "foto.heic", "notas.txt", "sub/filme.mov", "a.jpg"):
        assert esperado in tudo
    for proibido in ("vazio.txt", "Thumbs.db", "desktop.ini", "~$rascunho.docx"):
        assert proibido not in tudo
    assert not any(r.startswith(("$RECYCLE.BIN", "System Volume Information")) for r in tudo)
    so_videos = _rel(root, ic.list_image_files(root, True, accept=ic.make_scan_accept(False, True, False)))
    assert so_videos == ["clip.mp4", "clip_copia.mp4", "sub/filme.mov"]
    sem_sub = _rel(root, ic.list_image_files(root, False, accept=ic.make_scan_accept(False, True, False)))
    assert sem_sub == ["clip.mp4", "clip_copia.mp4"]


def _fake_entry(name, symlink=False):
    return types.SimpleNamespace(name=name, is_symlink=lambda: symlink)


def _fake_stat(size=10, attrs=0):
    return types.SimpleNamespace(st_size=size, st_file_attributes=attrs)


def test_byte_file_ok_recusa_o_que_nao_e_seguro_ler():
    ok = ic.byte_file_ok
    assert ok(_fake_entry("v.mp4"), _fake_stat())
    assert not ok(_fake_entry("v.mp4"), None)                       # sem stat
    assert not ok(_fake_entry("v.mp4"), _fake_stat(size=0))         # vazio
    assert not ok(_fake_entry("v.mp4", symlink=True), _fake_stat()) # link simbólico
    assert not ok(_fake_entry("v.mp4"), _fake_stat(attrs=0x4))      # sistema
    assert not ok(_fake_entry("v.mp4"), _fake_stat(attrs=0x1000))   # offline
    assert not ok(_fake_entry("v.mp4"), _fake_stat(attrs=0x400000)) # só na nuvem (OneDrive)
    assert ok(_fake_entry("v.mp4"), _fake_stat(attrs=0x2 | 0x20))   # oculto/arquivo: entra
    assert not ok(_fake_entry("THUMBS.DB"), _fake_stat())


def test_foto_do_pipeline_entra_mesmo_sem_stat_como_sempre():
    accept = ic.make_scan_accept(True, True, True)
    assert accept(_fake_entry("a.jpg"), None) is True
    assert not accept(_fake_entry("a.heic"), None)
    assert ic.make_scan_accept(False, True, True)(_fake_entry("a.jpg"), _fake_stat()) is False
    assert ic.make_scan_accept(False, False, True)(_fake_entry("a.heic"), _fake_stat()) is False
