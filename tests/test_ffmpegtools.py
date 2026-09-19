"""
ffmpegtools: localizar o ffmpeg e tirar o hash dos fluxos. Os testes que
precisam de um ffmpeg de verdade são pulados se não houver um na máquina; os
vídeos de teste são gerados pelo próprio ffmpeg (1 s, 64x64).
"""
import os
import subprocess
import sys
import threading
import time

import pytest

import ffmpegtools as ft

FFMPEG = ft.find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg não encontrado nesta máquina")


def make_videos(folder):
    """original.mp4 + o mesmo conteúdo em outro contêiner/metadados + um recodificado."""
    os.makedirs(folder, exist_ok=True)
    run = lambda *args: subprocess.run([FFMPEG, "-y", "-v", "error", *args], check=True,
                                       stdin=subprocess.DEVNULL, creationflags=ft._NO_WINDOW)
    original = os.path.join(folder, "original.mp4")
    run("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", original)
    out = {"original": original}
    out["remux_mov"] = os.path.join(folder, "remux_outro_nome.mov")
    run("-i", original, "-c", "copy", "-metadata", "title=regravado", out["remux_mov"])
    out["remux_mkv"] = os.path.join(folder, "remux.mkv")
    run("-i", original, "-c", "copy", out["remux_mkv"])
    out["recodificado"] = os.path.join(folder, "recodificado.mp4")
    run("-i", original, "-c:v", "libx264", "-crf", "40", "-c:a", "copy", out["recodificado"])
    out["outro"] = os.path.join(folder, "outro_video.mp4")
    run("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=1", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", out["outro"])
    return out


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    return make_videos(str(tmp_path_factory.mktemp("videos")))


def _same(a, b):
    """Mesmo caminho, ignorando maiúsculas (o shutil.which devolve 'ffmpeg.EXE')."""
    return a is not None and os.path.normcase(a) == os.path.normcase(b)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()
    return path


def test_ordem_de_busca_e_primeiro_que_existe(tmp_path):
    exe = ft._EXE
    env = {"PATH": str(tmp_path / "no_path"), "LOCALAPPDATA": str(tmp_path / "local"),
           "SystemDrive": str(tmp_path / "c"), "ProgramFiles": str(tmp_path / "pf"),
           "ProgramData": str(tmp_path / "pd"), "USERPROFILE": str(tmp_path / "user")}
    app_dir = str(tmp_path / "app")
    assert ft.find_ffmpeg(None, app_dir, env) is None
    winget = _touch(os.path.join(env["LOCALAPPDATA"], "Microsoft", "WinGet", "Packages",
                                 "Gyan.FFmpeg_x", "ffmpeg-8-full_build", "bin", exe))
    if sys.platform == "win32":
        assert _same(ft.find_ffmpeg(None, app_dir, env), winget)
        choco = _touch(os.path.join(env["ProgramData"], "chocolatey", "bin", exe))
        assert _same(ft.find_ffmpeg(None, app_dir, env), winget)          # WinGet vem antes do Chocolatey
        assert choco in ft.candidate_paths(None, app_dir, env)
    on_path = _touch(os.path.join(env["PATH"], exe))
    if sys.platform != "win32":
        os.chmod(on_path, 0o755)
    assert _same(ft.find_ffmpeg(None, app_dir, env), on_path)             # o PATH vem antes das instalações comuns
    beside = _touch(os.path.join(app_dir, "ffmpeg", "bin", exe))
    assert _same(ft.find_ffmpeg(None, app_dir, env), beside)              # a pasta do programa vem antes do PATH
    saved = _touch(str(tmp_path / "escolhido" / exe))
    assert _same(ft.find_ffmpeg(saved, app_dir, env), saved)              # o escolhido à mão vem antes de tudo
    assert _same(ft.find_ffmpeg(str(tmp_path / "sumiu" / exe), app_dir, env), beside)   # salvo que sumiu: segue a busca
    paths = ft.candidate_paths(saved, app_dir, env)
    assert len(paths) == len({os.path.normcase(os.path.abspath(p)) for p in paths})   # sem repetidos
    probe = _touch(os.path.join(os.path.dirname(saved), ft._PROBE_EXE))
    assert ft.ffprobe_beside(saved) == probe and ft.ffprobe_beside(beside) is None and ft.ffprobe_beside(None) is None


def test_check_version_recusa_o_que_nao_e_ffmpeg(tmp_path):
    assert ft.check_version(str(tmp_path / "nao_existe.exe")) is None
    assert ft.check_version(sys.executable) is None                  # executa, mas não é ffmpeg
    fake = str(tmp_path / ft._EXE)
    with open(fake, "wb") as f:
        f.write(b"isto nao e um executavel")
    assert ft.check_version(fake) is None


@needs_ffmpeg
def test_check_version_de_verdade():
    version = ft.check_version(FFMPEG)
    assert version and version[0].isdigit() or version.startswith(("n", "N", "git"))


@needs_ffmpeg
def test_mesmos_fluxos_em_outro_conteiner_dao_o_mesmo_hash(videos):
    base = ft.stream_hashes(FFMPEG, videos["original"])
    assert base and base.startswith("a=") and ";v=" in base
    assert ft.stream_hashes(FFMPEG, videos["remux_mov"]) == base     # outro nome, contêiner e metadados
    assert ft.stream_hashes(FFMPEG, videos["remux_mkv"]) == base
    import hashlib
    md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest()
    assert md5(videos["original"]) != md5(videos["remux_mov"])       # os ARQUIVOS são diferentes
    recod = ft.stream_hashes(FFMPEG, videos["recodificado"])
    assert recod and recod != base                                   # vídeo recomprimido: outro fluxo
    assert recod.split(";")[0] == base.split(";")[0]                 # (o áudio copiado é o mesmo)
    outro = ft.stream_hashes(FFMPEG, videos["outro"])
    assert outro and outro != base and outro.startswith("v=")        # sem áudio


@needs_ffmpeg
def test_falhas_viram_none(videos, tmp_path):
    txt = str(tmp_path / "nao_e_video.mp4")
    with open(txt, "w") as f:
        f.write("texto")
    assert ft.stream_hashes(FFMPEG, txt) is None
    assert ft.stream_hashes(FFMPEG, str(tmp_path / "sumiu.mp4")) is None
    assert ft.stream_hashes(str(tmp_path / "ffmpeg_que_nao_existe.exe"), videos["original"]) is None


@needs_ffmpeg
def test_cancelar_mata_o_processo_e_tick_roda(videos):
    ev = threading.Event()
    ev.set()
    t0 = time.time()
    assert ft.stream_hashes(FFMPEG, videos["original"], cancel_event=ev) in (None, ft.stream_hashes(FFMPEG, videos["original"]))
    assert time.time() - t0 < 30
    ticks = []
    ft._run([sys.executable, "-c", "import time; time.sleep(1)"], tick=lambda: ticks.append(1))
    assert len(ticks) >= 2                                           # a interface respiraria
    ev2 = threading.Event()
    t0 = time.time()
    done = ft._run([sys.executable, "-c", "import time; time.sleep(30)"], cancel_event=ev2,
                   tick=ev2.set)                                     # cancela no 1º tique
    assert done is None and time.time() - t0 < 10                    # não esperou os 30 s


@needs_ffmpeg
def test_ffprobe_para_conteiner_que_o_mp4probe_nao_le(videos):
    probe = ft.ffprobe_beside(FFMPEG)
    if probe is None:
        pytest.skip("ffprobe não está ao lado do ffmpeg")
    info = ft.probe_video(probe, videos["remux_mkv"])
    assert (info["width"], info["height"], info["codec"]) == (64, 64, "h264")
    assert 0.8 < info["duration"] < 1.3
    assert ft.probe_video(probe, videos["original"] + "_sumiu") is None
