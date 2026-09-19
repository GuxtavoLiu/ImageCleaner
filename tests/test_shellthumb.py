"""
Miniatura do Explorer (shellthumb) e o carregador em segundo plano. Os testes
que falam com o shell do Windows são pulados fora dele.
"""
import gc
import threading
import time

import pytest
from PIL import Image

import shellthumb

windows = pytest.mark.skipif(not shellthumb.available(), reason="só no Windows")


def _png(tmp_path, name="foto.png", size=(640, 360)):
    path = str(tmp_path / name)
    img = Image.new("RGB", size, (200, 30, 30))
    img.paste((30, 30, 200), (0, 0, size[0] // 2, size[1]))
    img.save(path)
    return path


@windows
def test_miniatura_de_imagem_cabe_no_tamanho_pedido(tmp_path):
    thumb = shellthumb.get_thumbnail(_png(tmp_path), 200)
    assert thumb.mode == "RGB"
    assert max(thumb.size) <= 200 and min(thumb.size) > 0
    assert thumb.size[0] > thumb.size[1]                  # manteve a proporção 16:9
    big = shellthumb.get_thumbnail(_png(tmp_path, "g.png", (1600, 900)), 900)
    assert 200 < max(big.size) <= 900


@windows
def test_arquivo_sem_miniatura_de_verdade_devolve_o_icone_ou_recusa(tmp_path):
    txt = str(tmp_path / "notas.txt")
    with open(txt, "w") as f:
        f.write("oi")
    icon = shellthumb.get_thumbnail(txt, 96)
    assert icon.mode == "RGB" and max(icon.size) <= 96    # ícone do tipo, composto sobre branco
    with pytest.raises(shellthumb.ThumbnailError):
        shellthumb.get_thumbnail(txt, 96, thumbnail_only=True)
    with pytest.raises(shellthumb.ThumbnailError):
        shellthumb.get_thumbnail(str(tmp_path / "nao_existe.mp4"), 96)


def _gdi_handles():
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    u32 = ctypes.WinDLL("user32")
    u32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    return u32.GetGuiResources(k32.GetCurrentProcess(), 0)     # 0 = GR_GDIOBJECTS


@windows
def test_funciona_em_thread_de_trabalho_e_nao_vaza_handles(tmp_path):
    path = _png(tmp_path)
    out = []
    shellthumb.get_thumbnail(path, 64)                    # aquece (DLLs, COM) antes de medir
    before = _gdi_handles()
    # Restos de Tk de testes anteriores são recolhidos AQUI, na thread principal:
    # recolhidos por acaso dentro da thread abaixo, o __del__ do Tk reclamaria
    # (nos testes não há mainloop para repassar a chamada).
    gc.collect()

    def work():
        for _ in range(300):                              # 10 mil handles GDI é o teto do processo
            out.append(shellthumb.get_thumbnail(path, 64).size)

    t = threading.Thread(target=work)
    t.start()
    t.join(60)
    assert len(out) == 300 and len(set(out)) == 1
    # Contagem real de objetos GDI do processo: 300 HBITMAPs vazados apareceriam aqui
    assert _gdi_handles() - before < 20


def test_loader_descarta_pedidos_que_ainda_nao_comecaram():
    gate = threading.Event()
    started = []

    def job(path, size):
        started.append(path)
        gate.wait(10)
        return path

    loader = shellthumb.ThumbnailLoader(job)
    for key in ("p1", "p2", "p3", "p4"):
        loader.request(key, key, 10)
    t0 = time.time()
    while not started and time.time() - t0 < 10:
        time.sleep(0.01)
    dropped = loader.discard_pending()                    # a página mudou
    assert dropped == ["p2", "p3", "p4"] and loader.pending == 1
    gate.set()
    got = []
    t0 = time.time()
    while not got and time.time() - t0 < 10:
        got += loader.drain()
        time.sleep(0.01)
    assert [(k, v) for k, v, _ in got] == [("p1", "p1")] and loader.pending == 0
    assert started == ["p1"]                              # os descartados nunca rodaram


def test_loader_entrega_resultados_e_erros_sem_derrubar_a_thread():
    def job(path, size):
        if path == "ruim":
            raise ValueError("falhou")
        return f"{path}@{size}"

    loader = shellthumb.ThumbnailLoader(job)
    for key in ("a", "ruim", "b"):
        loader.request(key, key, 10)
    got = []
    t0 = time.time()
    while len(got) < 3 and time.time() - t0 < 10:
        got += loader.drain()
        time.sleep(0.01)
    assert [(k, v) for k, v, _ in got] == [("a", "a@10"), ("ruim", None), ("b", "b@10")]
    assert isinstance(got[1][2], ValueError)
    assert loader.pending == 0
    loader.request("c", "c", 5)                           # a thread continua servindo
    t0 = time.time()
    while not (res := loader.drain()) and time.time() - t0 < 10:
        time.sleep(0.01)
    assert res == [("c", "c@5", None)]


def test_fora_do_windows_recusa_sem_quebrar(monkeypatch):
    monkeypatch.setattr(shellthumb, "available", lambda: False)
    with pytest.raises(shellthumb.ThumbnailError):
        shellthumb.get_thumbnail("qualquer.mp4", 100)
