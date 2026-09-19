"""
Leitura mínima do cabeçalho de vídeos MP4/MOV (família ISO BMFF/QuickTime),
em Python puro: duração, dimensões, codec do vídeo e, para Live Photos do
iPhone, o identificador que liga o .MOV à foto.

Por que não o ffprobe: só iniciar o processo custa ~0,9 s por arquivo; ler os
átomos direto custa milissegundos (só o átomo 'moov' é lido, nunca o vídeo).

Módulo-folha: não importa nada do main.py. Nunca levanta exceção por arquivo
malformado: o que não der para ler volta como None.
"""
import io
import os
import struct

MP4_EXTENSIONS = (".mp4", ".mov", ".m4v", ".3gp", ".3g2")

# Chave de metadados que o iPhone grava no .MOV de uma Live Photo (o mesmo
# UUID vai, em ASCII, no MakerNote da foto)
LIVE_PHOTO_KEY = b"com.apple.quicktime.content.identifier"

_MAX_MOOV = 32 * 1024 * 1024     # moov maior que isto: lê só o começo (mvhd/tkhd ficam na frente)
_MAX_ATOMS = 20000               # trava contra arquivo malformado com átomos minúsculos em laço
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_VIDEO_CODECS = {b"avc1", b"avc3", b"hvc1", b"hev1", b"mp4v", b"jpeg", b"mjpa", b"apch", b"apcn",
                 b"apcs", b"apco", b"ap4h", b"vp09", b"av01", b"s263", b"h263", b"dvh1", b"dvhe"}


def _atoms(fh, start, end, budget):
    """Gera (tipo, início_do_conteúdo, fim) dos átomos entre start e end."""
    pos = start
    while pos + 8 <= end:
        budget[0] -= 1
        if budget[0] < 0:
            return
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        head = 8
        if size == 1:                      # tamanho de 64 bits
            big = fh.read(8)
            if len(big) < 8:
                return
            size = struct.unpack(">Q", big)[0]
            head = 16
        elif size == 0:                    # "até o fim do arquivo"
            size = end - pos
        if size < head or pos + size > end:
            if kind == b"moov" and pos + head < end:
                yield kind, pos + head, end    # moov truncado: aproveita o que houver
            return
        yield kind, pos + head, pos + size
        pos += size


def _find_moov(fh, file_size, budget):
    for kind, start, end in _atoms(fh, 0, file_size, budget):
        if kind == b"moov":
            return start, end
    return None


def _parse_mvhd(bio, start, end):
    bio.seek(start)
    head = bio.read(4)
    if len(head) < 4:
        return None
    if head[0] == 1:
        data = bio.read(28)
        if len(data) < 28:
            return None
        timescale, duration = struct.unpack(">IQ", data[16:28])
    else:
        data = bio.read(16)
        if len(data) < 16:
            return None
        timescale, duration = struct.unpack(">II", data[8:16])
    if not timescale or duration in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return None
    return duration / timescale


def _parse_meta(bio, start, end, budget, out):
    """moov/meta -> keys + ilst: valor da chave de Live Photo."""
    bio.seek(start)
    probe = bio.read(12)
    # QuickTime: 'meta' é um contêiner simples; ISO: tem 4 bytes de versão/flags antes
    if probe[4:8] == b"hdlr":
        first = start
    elif probe[8:12] == b"hdlr":
        first = start + 4
    else:
        return
    key_index = None
    ilst = None
    for kind, a, b in _atoms(bio, first, end, budget):
        if kind == b"keys":
            bio.seek(a)
            head = bio.read(8)
            if len(head) < 8:
                return
            count = struct.unpack(">I", head[4:8])[0]
            pos = a + 8
            for index in range(1, min(count, 4096) + 1):
                bio.seek(pos)
                entry = bio.read(8)
                if len(entry) < 8:
                    break
                size = struct.unpack(">I", entry[:4])[0]
                if size < 8 or pos + size > b:
                    break
                if bio.read(size - 8) == LIVE_PHOTO_KEY:
                    key_index = index
                pos += size
        elif kind == b"ilst":
            ilst = (a, b)
    if key_index is None:
        return
    out["live_photo"] = True
    if ilst is None:
        return
    for kind, a, b in _atoms(bio, ilst[0], ilst[1], budget):
        if struct.unpack(">I", kind)[0] != key_index:
            continue
        for sub, c, d in _atoms(bio, a, b, budget):
            if sub == b"data" and d - c > 8:
                bio.seek(c + 8)            # pula tipo (4) e localidade (4)
                value = bio.read(min(d - c - 8, 256))
                try:
                    out["content_id"] = value.decode("utf-8").strip("\x00 ").upper() or None
                except UnicodeDecodeError:
                    pass
                return


def _walk(bio, start, end, budget, out, depth=0):
    if depth > 8:
        return
    for kind, a, b in _atoms(bio, start, end, budget):
        if kind == b"mvhd":
            out["duration"] = _parse_mvhd(bio, a, b)
        elif kind == b"tkhd" and out["width"] is None and b - a >= 8:
            bio.seek(b - 8)
            w, h = struct.unpack(">II", bio.read(8))
            if w >> 16 and h >> 16:
                out["width"], out["height"] = w >> 16, h >> 16
        elif kind == b"stsd" and out["codec"] is None and b - a >= 16:
            bio.seek(a + 12)
            codec = bio.read(4)
            if codec in _VIDEO_CODECS:
                out["codec"] = codec.decode("ascii")
        elif kind == b"meta" and depth <= 1:
            # iPhone: moov/meta. Arquivos regravados (ffmpeg): moov/udta/meta.
            _parse_meta(bio, a, b, budget, out)
        elif kind in _CONTAINERS or (kind == b"udta" and depth == 0):
            _walk(bio, a, b, budget, out, depth + 1)


def probe(path):
    """
    Retorna um dict com 'duration' (segundos, float), 'width', 'height',
    'codec' (ex.: 'avc1'), 'live_photo' (bool: o .MOV traz a chave de Live
    Photo) e 'content_id' (UUID da Live Photo, em maiúsculas). Campos que não
    puderam ser lidos ficam None (live_photo fica False). Arquivo que não é
    MP4/MOV, ilegível ou truncado: tudo None, sem exceção.
    Atenção: MP4 fragmentado costuma ter duração 0 no cabeçalho: vem None.
    """
    out = {"duration": None, "width": None, "height": None, "codec": None,
           "live_photo": False, "content_id": None}
    try:
        file_size = os.path.getsize(path)
        budget = [_MAX_ATOMS]
        with open(path, "rb") as fh:
            moov = _find_moov(fh, file_size, budget)
            if moov is None:
                return out
            fh.seek(moov[0])
            blob = fh.read(min(moov[1] - moov[0], _MAX_MOOV))
        _walk(io.BytesIO(blob), 0, len(blob), budget, out)
        if out["duration"] is not None and out["duration"] <= 0:
            out["duration"] = None
    except (OSError, struct.error, ValueError):
        pass
    return out


def photo_content_id(path, limit=512 * 1024):
    """
    UUID de Live Photo gravado na FOTO (MakerNote da Apple, em ASCII), ou None.
    Lê só o começo do arquivo: o EXIF fica no início do JPEG/HEIC.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(limit)
    except OSError:
        return None
    return _find_uuid(head)


def _find_uuid(data):
    """Primeiro UUID 8-4-4-4-12 em ASCII dentro do bloco MakerNote da Apple."""
    anchor = data.find(b"Apple iOS")
    if anchor < 0:
        return None
    hexd = b"0123456789abcdefABCDEF"
    shape = (8, 4, 4, 4, 12)
    window = data[anchor:anchor + 64 * 1024]
    i = 0
    n = len(window)
    while i + 36 <= n:
        j = i
        ok = True
        for k, width in enumerate(shape):
            if any(window[j + t] not in hexd for t in range(width)):
                ok = False
                break
            j += width
            if k < 4:
                if window[j:j + 1] != b"-":
                    ok = False
                    break
                j += 1
        if ok:
            return window[i:i + 36].decode("ascii").upper()
        i += 1
    return None


def format_duration(seconds):
    """73.4 -> '1:13'; 3725 -> '1:02:05'; None -> ''."""
    if seconds is None:
        return ""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
