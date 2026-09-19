"""
mp4probe: leitura do cabeçalho de MP4/MOV com átomos escritos à mão (sem
depender de ffmpeg): duração, dimensões, codec, marca de Live Photo nos dois
lugares onde ela aparece, e robustez a arquivo malformado.
"""
import os
import struct

import pytest

import mp4probe

UUID = "1A2B3C4D-1111-2222-3333-ABCDEF012345"


def atom(kind, payload=b"", big=False):
    if big:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mvhd(timescale=600, duration=1500, version=0):
    if version == 1:
        body = b"\x01\x00\x00\x00" + b"\x00" * 16 + struct.pack(">IQ", timescale, duration)
    else:
        body = b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", timescale, duration)
    return atom(b"mvhd", body + b"\x00" * 80)


def trak(width=1280, height=720, codec=b"avc1"):
    tkhd = atom(b"tkhd", b"\x00" * 76 + struct.pack(">II", width << 16, height << 16))
    stsd = atom(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">I", 86) + codec + b"\x00" * 78)
    return atom(b"trak", tkhd + atom(b"mdia", atom(b"minf", atom(b"stbl", stsd))))


def meta(uuid=UUID, iso_style=False, other_key_first=True):
    hdlr = atom(b"hdlr", b"\x00" * 8 + b"mdta" + b"\x00" * 13)
    names = ([b"com.apple.quicktime.make"] if other_key_first else []) + [mp4probe.LIVE_PHOTO_KEY]
    keys = atom(b"keys", b"\x00" * 4 + struct.pack(">I", len(names))
                + b"".join(struct.pack(">I4s", 8 + len(n), b"mdta") + n for n in names))
    values = ([b"Apple"] if other_key_first else []) + [uuid.encode()]
    ilst = atom(b"ilst", b"".join(
        atom(struct.pack(">I", i + 1), atom(b"data", struct.pack(">II", 1, 0) + v))
        for i, v in enumerate(values)))
    return atom(b"meta", (b"\x00" * 4 if iso_style else b"") + hdlr + keys + ilst)


def write(tmp_path, name, data):
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(data)
    return path


FTYP = atom(b"ftyp", b"qt  " + b"\x00" * 8)
MDAT = atom(b"mdat", b"\xAB" * 5000)


def test_video_comum_moov_no_fim(tmp_path):
    p = write(tmp_path, "v.mov", FTYP + MDAT + atom(b"moov", mvhd() + trak()))
    assert mp4probe.probe(p) == {"duration": 2.5, "width": 1280, "height": 720, "codec": "avc1",
                                 "live_photo": False, "content_id": None}


def test_moov_no_inicio_mvhd_v1_e_trilha_de_audio_antes(tmp_path):
    audio = atom(b"trak", atom(b"tkhd", b"\x00" * 84))            # largura/altura zero
    p = write(tmp_path, "v.mp4", FTYP + atom(b"moov", mvhd(1000, 90_000, version=1) + audio
                                           + trak(1920, 1080, b"hvc1")) + MDAT)
    info = mp4probe.probe(p)
    assert (info["duration"], info["width"], info["height"], info["codec"]) == (90.0, 1920, 1080, "hvc1")


def test_live_photo_do_iphone_meta_direto_no_moov(tmp_path):
    p = write(tmp_path, "IMG_0001.MOV", FTYP + MDAT + atom(b"moov", mvhd() + trak() + meta()))
    info = mp4probe.probe(p)
    assert info["live_photo"] is True and info["content_id"] == UUID


def test_live_photo_regravada_meta_dentro_de_udta_estilo_iso(tmp_path):
    moov = atom(b"moov", mvhd() + trak() + atom(b"udta", meta(uuid=UUID.lower(), iso_style=True)))
    p = write(tmp_path, "IMG_0002.MOV", FTYP + MDAT + moov)
    info = mp4probe.probe(p)
    assert info["live_photo"] is True and info["content_id"] == UUID     # normalizado em maiúsculas


def test_outra_chave_de_metadados_nao_e_live_photo(tmp_path):
    hdlr = atom(b"hdlr", b"\x00" * 8 + b"mdta" + b"\x00" * 13)
    keys = atom(b"keys", b"\x00" * 4 + struct.pack(">I", 1)
                + struct.pack(">I4s", 8 + 24, b"mdta") + b"com.apple.quicktime.make")
    p = write(tmp_path, "v.mov", FTYP + atom(b"moov", mvhd() + trak() + atom(b"meta", hdlr + keys)))
    assert mp4probe.probe(p)["live_photo"] is False


def test_tamanho_de_64_bits_e_mp4_fragmentado(tmp_path):
    big_mdat = atom(b"mdat", b"\x00" * 3000, big=True)
    p = write(tmp_path, "v.mp4", FTYP + big_mdat + atom(b"moov", mvhd(1000, 0) + trak()))
    info = mp4probe.probe(p)
    assert info["duration"] is None          # fragmentado: duração 0 no cabeçalho = desconhecida
    assert info["width"] == 1280


VAZIO = {"duration": None, "width": None, "height": None, "codec": None,
         "live_photo": False, "content_id": None}


@pytest.mark.parametrize("data", [
    b"", b"abc", b"\x00" * 64, os.urandom(4096),
    FTYP + struct.pack(">I4s", 999_999, b"mdat") + b"x" * 10,        # tamanho maior que o arquivo
    FTYP + struct.pack(">I4s", 4, b"free") * 50,                     # tamanho menor que o cabeçalho
    FTYP + atom(b"moov", struct.pack(">I4s", 8, b"trak") * 30000),   # laço de átomos minúsculos
], ids=["vazio", "tres_bytes", "zeros", "aleatorio", "tamanho_maior_que_o_arquivo",
        "tamanho_menor_que_o_cabecalho", "laco_de_atomos"])
def test_malformado_nunca_levanta(tmp_path, data):
    p = write(tmp_path, "x.mp4", data)
    assert mp4probe.probe(p) == VAZIO


def test_moov_truncado_aproveita_o_que_der_e_arquivo_ausente(tmp_path):
    full = FTYP + atom(b"moov", mvhd() + trak() + meta())
    p = write(tmp_path, "cortado.mov", full[:len(FTYP) + 8 + 116 + 40])   # mvhd inteiro, trak cortado
    info = mp4probe.probe(p)
    assert info["duration"] == 2.5 and info["width"] is None
    assert mp4probe.probe(str(tmp_path / "nao_existe.mov")) == VAZIO


def test_uuid_na_foto_e_formatacao(tmp_path):
    exif = b"\xff\xd8\xff\xe1" + b"Exif\x00\x00" + b"\x00" * 100 + b"Apple iOS\x00\x00\x01MM" \
        + b"\x00" * 60 + UUID.lower().encode() + b"\x00" + b"\xff" * 500
    assert mp4probe.photo_content_id(write(tmp_path, "IMG_0001.JPG", exif)) == UUID
    sem_apple = b"\xff\xd8" + UUID.encode() + b"\x00" * 50           # UUID solto não conta
    assert mp4probe.photo_content_id(write(tmp_path, "b.jpg", sem_apple)) is None
    assert mp4probe.photo_content_id(str(tmp_path / "nao_existe.jpg")) is None
    assert mp4probe.format_duration(73.4) == "1:13"
    assert mp4probe.format_duration(3725) == "1:02:05"
    assert mp4probe.format_duration(None) == ""
