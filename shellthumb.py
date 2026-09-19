"""
Miniatura de QUALQUER arquivo, pedida ao shell do Windows: a mesma que o
Explorer mostra (um quadro do vídeo, a primeira página do PDF, a capa do MP3,
ou o ícone do tipo quando não há miniatura de verdade).

ctypes puro, sem dependência nova: SHCreateItemFromParsingName ->
IShellItemImageFactory::GetImage -> HBITMAP -> PIL.Image.

Módulo-folha: não importa nada do main.py. Fora do Windows, available() é
False e get_thumbnail levanta ThumbnailError.

COM: cada thread que chama get_thumbnail é inicializada uma vez como
apartment-threaded. A thread principal do Tk já é STA (o Tk chama
OleInitialize); inicializá-la de outro modo quebraria os diálogos nativos,
então RPC_E_CHANGED_MODE é aceito em silêncio.
"""
import os
import queue
import sys
import threading

from PIL import Image

SIIGBF_RESIZETOFIT = 0x0
SIIGBF_BIGGERSIZEOK = 0x1
SIIGBF_THUMBNAILONLY = 0x8      # falha (em vez de devolver o ícone) se não há miniatura real

_S_OK, _S_FALSE = 0, 1
_RPC_E_CHANGED_MODE = 0x80010106


class ThumbnailError(OSError):
    """Não foi possível obter a miniatura (arquivo sumiu, shell recusou...)."""


def available():
    return sys.platform == "win32"


_local = threading.local()
_api = None


def _load_api():
    """Carrega as DLLs e declara os protótipos uma única vez."""
    global _api
    if _api is not None:
        return _api
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                    ("d4", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    u = uuid.UUID("bcc18b79-ba16-442f-80c4-8a59c30c463b")   # IID_IShellItemImageFactory
    iid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    gdi32 = ctypes.WinDLL("gdi32")
    user32 = ctypes.WinDLL("user32")
    shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HANDLE, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    _api = {
        "ctypes": ctypes, "wintypes": wintypes, "iid": iid, "SIZE": SIZE, "BITMAP": BITMAP,
        "BITMAPINFOHEADER": BITMAPINFOHEADER, "shell32": shell32, "ole32": ole32,
        "gdi32": gdi32, "user32": user32,
        # vtable de IShellItemImageFactory: QueryInterface, AddRef, Release, GetImage
        "GetImage": ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                       ctypes.POINTER(wintypes.HANDLE)),
        "Release": ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p),
    }
    return _api


def _ensure_com(api):
    """CoInitializeEx uma vez por thread. Na thread principal do Tk nunca é
       desfeito (ela vive com o app); threads de trabalho chamam release_com()
       ao terminar."""
    if getattr(_local, "com_ready", False):
        return
    hr = api["ole32"].CoInitializeEx(None, 0x2) & 0xFFFFFFFF      # COINIT_APARTMENTTHREADED
    if hr not in (_S_OK, _S_FALSE, _RPC_E_CHANGED_MODE):
        raise ThumbnailError(f"CoInitializeEx falhou (hr={hr:08X})")
    _local.com_ready = True
    # S_OK e S_FALSE pedem um CoUninitialize de volta; CHANGED_MODE não contou
    _local.com_owned = hr in (_S_OK, _S_FALSE)


def release_com():
    """Desfaz o CoInitializeEx desta thread (para threads de trabalho que vão
       terminar). Sem efeito se esta thread nunca pediu miniatura."""
    if getattr(_local, "com_owned", False) and _api is not None:
        _api["ole32"].CoUninitialize()
    _local.com_ready = False
    _local.com_owned = False


def get_thumbnail(path, size, thumbnail_only=False, background=(255, 255, 255)):
    """
    Miniatura de `path` cabendo em size x size, como PIL.Image RGB (ícones
    com transparência são compostos sobre `background`). Com
    thumbnail_only=True, levanta ThumbnailError quando o shell só teria o
    ícone genérico do tipo para oferecer.
    """
    if not available():
        raise ThumbnailError("miniaturas do shell só existem no Windows")
    api = _load_api()
    ctypes, wintypes = api["ctypes"], api["wintypes"]
    _ensure_com(api)
    factory = ctypes.c_void_p()
    hr = api["shell32"].SHCreateItemFromParsingName(
        os.path.normpath(os.path.abspath(path)), None, ctypes.byref(api["iid"]), ctypes.byref(factory))
    if hr < 0 or not factory:
        raise ThumbnailError(f"shell não abriu o arquivo (hr={hr & 0xFFFFFFFF:08X})")
    vtable = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                         ctypes.POINTER(ctypes.c_void_p))
    hbitmap = wintypes.HANDLE()
    try:
        flags = SIIGBF_THUMBNAILONLY if thumbnail_only else SIIGBF_RESIZETOFIT
        hr = api["GetImage"](vtable[3])(factory, api["SIZE"](int(size), int(size)), flags,
                                        ctypes.byref(hbitmap))
    finally:
        api["Release"](vtable[2])(factory)
    if hr < 0 or not hbitmap:
        raise ThumbnailError(f"sem miniatura (hr={hr & 0xFFFFFFFF:08X})")
    try:
        bm = api["BITMAP"]()
        if not api["gdi32"].GetObjectW(hbitmap, ctypes.sizeof(bm), ctypes.byref(bm)):
            raise ThumbnailError("GetObject falhou")
        w, h = bm.bmWidth, bm.bmHeight
        if w <= 0 or h <= 0 or w * h > 64_000_000:
            raise ThumbnailError(f"dimensões inválidas ({w} x {h})")
        header = api["BITMAPINFOHEADER"](ctypes.sizeof(api["BITMAPINFOHEADER"]), w, -h, 1, 32,
                                         0, 0, 0, 0, 0, 0)      # altura negativa: de cima para baixo
        buf = ctypes.create_string_buffer(w * h * 4)
        dc = api["user32"].GetDC(None)
        try:
            lines = api["gdi32"].GetDIBits(dc, hbitmap, 0, h, buf, ctypes.byref(header), 0)
        finally:
            api["user32"].ReleaseDC(None, dc)
        if lines != h:
            raise ThumbnailError("GetDIBits falhou")
        rgba = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).copy()
    finally:
        api["gdi32"].DeleteObject(hbitmap)     # limite de 10 mil handles GDI por processo
    lo, hi = rgba.getchannel("A").getextrema()
    if hi == 0:
        return rgba.convert("RGB")             # alfa todo zero = bitmap sem canal alfa de verdade
    if lo == 255:
        return rgba.convert("RGB")
    out = Image.new("RGB", rgba.size, background)
    out.paste(rgba, mask=rgba.getchannel("A"))
    return out


class ThumbnailLoader:
    """
    Carrega miniaturas numa thread dedicada, para nunca travar a interface
    (um vídeo 4K a frio ou uma extensão de shell lenta pode levar segundos).
    A thread principal pede com request() e recolhe com drain(); criar o
    PhotoImage do Tk continua sendo trabalho da thread principal.

    job(path, size) -> valor qualquer; o padrão é get_thumbnail.
    """
    def __init__(self, job=None):
        self._job = job or get_thumbnail
        self._todo = queue.Queue()
        self._done = queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()
        self._thread = None

    def request(self, key, path, size):
        with self._lock:
            # put sob o lock: a thread só decide encerrar (fila vazia) com o
            # mesmo lock, então um pedido nunca fica órfão na fila
            self._todo.put((key, path, size))
            self._pending += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="miniaturas", daemon=True)
                self._thread.start()

    def discard_pending(self):
        """Descarta os pedidos que ainda não começaram (a página mudou: ninguém
           mais vai ver essas miniaturas) e devolve as chaves descartadas. Sem
           isto a fila é FIFO: quem pagina rápido esperaria centenas de
           miniaturas de páginas que já saíram da tela."""
        dropped = []
        with self._lock:
            while True:
                try:
                    dropped.append(self._todo.get_nowait()[0])
                except queue.Empty:
                    break
            self._pending -= len(dropped)
        return dropped

    @property
    def pending(self):
        """Pedidos ainda não recolhidos por drain()."""
        with self._lock:
            return self._pending

    def drain(self):
        """Lista de (key, valor_ou_None, erro_ou_None) prontos desde a última chamada."""
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                break
        if out:
            with self._lock:
                self._pending -= len(out)
        return out

    def _run(self):
        while True:
            try:
                key, path, size = self._todo.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if self._todo.empty():
                        self._thread = None      # ociosa: encerra; request() cria outra
                        release_com()
                        return
                continue
            try:
                self._done.put((key, self._job(path, size), None))
            except Exception as e:                # nunca derruba a thread
                self._done.put((key, None, e))
