"""
Live Photos do iPhone: reconhecer o .MOV par de uma foto e decidir, com
segurança, quando ele pode acompanhar a foto que sai. Regra de ouro: o vídeo
só sai se sobra uma cópia BYTE A BYTE idêntica dele.

Os .MOV são escritos à mão (átomos do test_mp4probe); não há Live Photo real
no acervo de testes.
"""
import os

import main as ic
from test_mp4probe import FTYP, MDAT, UUID, atom, meta, mvhd, trak

OTHER_UUID = "99999999-AAAA-BBBB-CCCC-000000000001"


def live_mov(uuid=UUID, payload=b""):
    extra = atom(b"free", payload) if payload else b""      # conteúdo extra num átomo válido
    return FTYP + MDAT + extra + atom(b"moov", mvhd() + trak() + meta(uuid=uuid))


def plain_mov():
    return FTYP + MDAT + atom(b"moov", mvhd() + trak())


def jpg(uuid=None, filler=b"\x11"):
    head = b"\xff\xd8\xff\xe1" + b"Exif\x00\x00" + b"\x00" * 40
    if uuid:
        head += b"Apple iOS\x00\x00\x01MM" + b"\x00" * 30 + uuid.encode() + b"\x00"
    return head + filler * 600


def put(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def keys(*paths):
    return {ic.cache_key(p) for p in paths}


def test_reconhece_o_par_so_pela_marca_da_apple(tmp_path):
    root = str(tmp_path)
    foto = put(root, "IMG_0001.JPG", jpg(UUID))
    mov = put(root, "IMG_0001.MOV", live_mov())
    assert ic.cache_key(ic.live_photo_companion(foto)) == ic.cache_key(mov)
    assert ic.is_live_photo_video(mov)
    # vídeo comum com o mesmo número da foto NÃO é par
    put(root, "IMG_0002.JPG", jpg())
    comum = put(root, "IMG_0002.MOV", plain_mov())
    assert ic.live_photo_companion(os.path.join(root, "IMG_0002.JPG")) is None
    assert not ic.is_live_photo_video(comum)
    # identificadores presentes e diferentes: não é par; foto sem identificador: aceita
    put(root, "IMG_0003.JPG", jpg(OTHER_UUID))
    put(root, "IMG_0003.MOV", live_mov())
    assert ic.live_photo_companion(os.path.join(root, "IMG_0003.JPG")) is None
    put(root, "IMG_0004.HEIC", jpg())
    put(root, "IMG_0004.mov", live_mov())
    assert ic.live_photo_companion(os.path.join(root, "IMG_0004.HEIC")) is not None
    # sem .MOV ao lado, ou extensão que não é de Live Photo
    assert ic.live_photo_companion(put(root, "IMG_0005.JPG", jpg())) is None
    put(root, "IMG_0006.MOV", live_mov())
    assert ic.live_photo_companion(put(root, "IMG_0006.PNG", jpg())) is None


def _two_folders(tmp_path, mov_b=None, with_mov_a=True):
    a, b = str(tmp_path / "A"), str(tmp_path / "B")
    fa, fb = put(a, "IMG_0001.JPG", jpg(UUID)), put(b, "IMG_0001.JPG", jpg(UUID))
    ma = put(a, "IMG_0001.MOV", live_mov()) if with_mov_a else None
    mb = put(b, "IMG_0001.MOV", live_mov() if mov_b is None else mov_b)
    return fa, fb, ma, mb


def test_video_acompanha_a_foto_quando_sobra_copia_identica(tmp_path):
    fa, fb, ma, mb = _two_folders(tmp_path)
    plan, sem_copia = ic.plan_live_companions([[fa, fb]], keys(fb))
    assert [(os.path.basename(p), ic.cache_key(m)) for p, m in plan] == [("IMG_0001.JPG", ic.cache_key(mb))]
    assert sem_copia == 0
    assert ic.plan_live_companions([[fa, fb]], set()) == ([], 0)          # nada selecionado


def test_sem_copia_provada_o_video_fica(tmp_path):
    # a foto mantida não tem vídeo ao lado
    fa, fb, _, mb = _two_folders(tmp_path / "x", with_mov_a=False)
    assert ic.plan_live_companions([[fa, fb]], keys(fb)) == ([], 1)
    # a foto mantida tem vídeo, mas o conteúdo é outro (mesmo identificador!)
    fa, fb, ma, mb = _two_folders(tmp_path / "y", mov_b=live_mov(payload=b"outra gravacao"))
    assert not ic.same_content(ma, mb)
    assert ic.plan_live_companions([[fa, fb]], keys(fb)) == ([], 1)


def test_outra_foto_de_mesmo_nome_que_fica_segura_o_video(tmp_path):
    fa, fb, ma, mb = _two_folders(tmp_path)
    heic = put(os.path.dirname(fb), "IMG_0001.HEIC", jpg(UUID, filler=b"\x22"))
    assert ic.cache_key(heic) in keys(*ic.stills_with_same_stem(fb))
    assert ic.plan_live_companions([[fa, fb]], keys(fb)) == ([], 0)        # a HEIC ainda usa o .MOV
    plan, _ = ic.plan_live_companions([[fa, fb]], keys(fb, heic))          # as duas saem: o vídeo pode ir
    assert len(plan) == 1


def test_video_ja_selecionado_ou_protegido_nao_entra(tmp_path):
    fa, fb, ma, mb = _two_folders(tmp_path)
    assert ic.plan_live_companions([[fa, fb]], keys(fb, mb)) == ([], 0)
    protegido = ic.cache_key(mb)
    assert ic.plan_live_companions([[fa, fb]], keys(fb),
                                   is_protected=lambda p: ic.cache_key(p) == protegido) == ([], 0)


def test_nunca_apaga_a_ultima_copia_do_video(tmp_path):
    """Fotos: sai a de A (fica a de B). Vídeos: sai o de B (fica o de A).
       Levar o .MOV de A junto com a foto de A apagaria os DOIS vídeos."""
    fa, fb, ma, mb = _two_folders(tmp_path)
    groups = [[fa, fb], [ma, mb]]
    assert ic.plan_live_companions(groups, keys(fa, mb)) == ([], 1)
    # com o vídeo de B ficando, o de A pode acompanhar a foto de A
    plan, sem_copia = ic.plan_live_companions(groups, keys(fa))
    assert [ic.cache_key(m) for _, m in plan] == [ic.cache_key(ma)] and sem_copia == 0


def test_prova_tambem_pode_vir_de_um_grupo_de_videos(tmp_path):
    fa, fb, _, mb = _two_folders(tmp_path, with_mov_a=False)
    copia = put(str(tmp_path / "C"), "video_guardado.mov", live_mov())
    assert ic.plan_live_companions([[fa, fb]], keys(fb)) == ([], 1)               # sem o grupo de vídeos
    plan, _ = ic.plan_live_companions([[fa, fb], [mb, copia]], keys(fb))
    assert len(plan) == 1
    assert ic.plan_live_companions([[fa, fb], [mb, copia]], keys(fb, copia)) == ([], 1)   # a cópia também sai


def test_foto_que_ficaria_sem_o_video(tmp_path):
    fa, fb, ma, mb = _two_folders(tmp_path)
    comum = put(str(tmp_path / "B"), "VID_1.mov", plain_mov())
    put(str(tmp_path / "B"), "VID_1.jpg", jpg())
    assert ic.orphaned_live_photos([mb, comum], keys(mb, comum)) == 1      # a foto de B fica sem o vídeo
    assert ic.orphaned_live_photos([mb, fb], keys(mb, fb)) == 0            # foto e vídeo saem juntos
    assert ic.orphaned_live_photos([fa], keys(fa)) == 0


def test_same_content(tmp_path):
    root = str(tmp_path)
    a, b = put(root, "a.bin", b"x" * 3_000_000), put(root, "b.bin", b"x" * 3_000_000)
    c = put(root, "c.bin", b"x" * 2_999_999 + b"y")
    assert ic.same_content(a, b) and not ic.same_content(a, c)
    assert not ic.same_content(a, put(root, "d.bin", b"x"))
    assert not ic.same_content(a, os.path.join(root, "nao_existe.bin"))


def test_conferencia_avisa_o_progresso_e_pode_ser_cancelada(tmp_path):
    import pytest
    fa, fb, ma, mb = _two_folders(tmp_path)
    seen = []
    plan, _ = ic.plan_live_companions([[fa, fb]], keys(fb), tick=seen.append)
    assert seen == [1] and len(plan) == 1

    def cancel(n):
        raise ic.ScanCancelled()

    with pytest.raises(ic.ScanCancelled):
        ic.plan_live_companions([[fa, fb]], keys(fb), tick=cancel)
    assert os.path.exists(mb)                      # cancelar não toca em nada


def test_pasta_e_listada_uma_vez_so_por_acao(tmp_path, monkeypatch):
    fa, fb, ma, mb = _two_folders(tmp_path)
    folder_b = os.path.dirname(fb)
    extras = [put(folder_b, f"IMG_01{k:02d}.mov", plain_mov()) for k in range(30)]
    calls = []
    orig = os.listdir
    monkeypatch.setattr(ic.os, "listdir", lambda p: calls.append(p) or orig(p))
    assert ic.orphaned_live_photos([mb] + extras, keys(mb, *extras)) == 1
    assert len(calls) == 1                         # 31 vídeos na mesma pasta, uma listagem
