"""
ffmpeg OPCIONAL: achar o executável na máquina e tirar o "hash dos fluxos" de
um vídeo (MD5 dos pacotes de vídeo e de áudio, sem decodificar). Dois arquivos
com o mesmo hash de fluxos têm exatamente o mesmo vídeo e o mesmo áudio, mesmo
que os bytes do arquivo difiram (remux MP4 -> MOV/MKV, metadados ou datas
regravados). Sem ffmpeg o resto do programa funciona igual.

Custo medido: só iniciar o ffmpeg/ffprobe leva ~0,9 s (executável de 200 MB).
Por isso NADA aqui serve de pré-filtro: quem chama escolhe poucos candidatos
(ver mp4probe) e só então paga o processo.

Módulo-folha: não importa nada do main.py.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW: sem console piscando
_EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
_PROBE_EXE = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"


def candidate_paths(saved=None, app_dir=None, env=None):
    """
    Lugares onde procurar o ffmpeg, em ordem de preferência:
      1. o caminho salvo nas configurações (escolhido à mão);
      2. a pasta do próprio programa (e subpastas ffmpeg/ e ffmpeg/bin/);
      3. o PATH do sistema;
      4. instalações comuns no Windows: C:\\ffmpeg\\bin, Arquivos de Programas,
         WinGet (Links e Packages), Chocolatey e Scoop.
    Retorna a lista SEM checar existência (find_ffmpeg faz isso).
    """
    env = os.environ if env is None else env
    out = []
    if saved:
        out.append(saved)
    if app_dir:
        out += [os.path.join(app_dir, _EXE), os.path.join(app_dir, "ffmpeg", _EXE),
                os.path.join(app_dir, "ffmpeg", "bin", _EXE)]
    on_path = shutil.which("ffmpeg", path=env.get("PATH"))
    if on_path:
        out.append(on_path)
    if sys.platform == "win32":
        local = env.get("LOCALAPPDATA", "")
        for base in (env.get("SystemDrive", "C:") + os.sep, env.get("ProgramFiles", ""),
                     env.get("ProgramFiles(x86)", ""), env.get("ProgramW6432", "")):
            if base:
                out.append(os.path.join(base, "ffmpeg", "bin", _EXE))
        if local:
            out.append(os.path.join(local, "Microsoft", "WinGet", "Links", _EXE))
            out += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                                 "*FFmpeg*", "*", "bin", _EXE)), reverse=True)
        if env.get("ProgramData"):
            out.append(os.path.join(env["ProgramData"], "chocolatey", "bin", _EXE))
        if env.get("USERPROFILE"):
            out.append(os.path.join(env["USERPROFILE"], "scoop", "shims", _EXE))
    seen, unique = set(), []
    for p in out:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_ffmpeg(saved=None, app_dir=None, env=None):
    """Primeiro candidato que existe como arquivo, ou None. Não executa nada
       (instantâneo): a validação de verdade é check_version."""
    for path in candidate_paths(saved, app_dir, env):
        if os.path.isfile(path):
            return path
    return None


def ffprobe_beside(ffmpeg_path):
    """O ffprobe vem na mesma pasta do ffmpeg; None se não estiver lá."""
    if not ffmpeg_path:
        return None
    probe = os.path.join(os.path.dirname(ffmpeg_path), _PROBE_EXE)
    return probe if os.path.isfile(probe) else None


def check_version(ffmpeg_path, timeout=20):
    """Roda `ffmpeg -version` e devolve a versão ("8.1.1-full_build..."), ou
       None se não é um ffmpeg que funciona. Leva ~1 s: fora da thread principal."""
    try:
        done = subprocess.run([ffmpeg_path, "-version"], capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    first = done.stdout.decode("utf-8", "replace").splitlines()[:1]
    if done.returncode != 0 or not first or not first[0].lower().startswith("ffmpeg version"):
        return None
    return first[0].split()[2] if len(first[0].split()) > 2 else "?"


def _run(cmd, cancel_event=None, tick=None, timeout=None):
    """Roda cmd até o fim devolvendo (returncode, stdout). Consulta
       cancel_event a cada 0,2 s (mata o processo e devolve None) e chama tick()
       no mesmo ritmo, para quem chama manter a interface viva. A saída
       esperada é minúscula: ler o pipe só no fim não trava."""
    import time
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except OSError:
        return None
    started = time.time()
    try:
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if tick:
                tick()
            if (cancel_event is not None and cancel_event.is_set()) or \
                    (timeout and time.time() - started > timeout):
                proc.kill()
                proc.wait(timeout=10)
                return None
        out = proc.stdout.read()
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
    return proc.returncode, out


def stream_hashes(ffmpeg_path, filepath, cancel_event=None, tick=None, timeout=None):
    """
    Hash dos fluxos de vídeo e áudio de `filepath` (pacotes copiados, sem
    decodificar; lê o arquivo inteiro). Retorna uma string canônica, igual para
    arquivos com os mesmos fluxos em qualquer ordem ou contêiner, ex.:
    "a=9e10...;v=aa79...". None se cancelado, se o ffmpeg falhou ou se o
    arquivo não tem fluxo de vídeo. Fluxos de dados/legendas ficam de fora
    (o iPhone grava trilhas de metadados que um remux descarta).
    """
    cmd = [ffmpeg_path, "-nostdin", "-v", "error", "-i", filepath, "-map", "0:v", "-map", "0:a?",
           "-c", "copy", "-f", "streamhash", "-hash", "md5", "-"]
    done = _run(cmd, cancel_event, tick, timeout)
    if done is None or done[0] != 0:
        return None
    parts = []
    for line in done[1].decode("ascii", "replace").splitlines():
        # formato: "0,v,MD5=aa79ffd1..."
        fields = line.strip().split(",")
        if len(fields) == 3 and fields[1] in ("v", "a") and "=" in fields[2]:
            parts.append(f"{fields[1]}={fields[2].split('=', 1)[1].lower()}")
    if not any(p.startswith("v=") for p in parts):
        return None
    return ";".join(sorted(parts))


def probe_video(ffprobe_path, filepath, cancel_event=None, timeout=60):
    """Duração, dimensões e codec pelo ffprobe (para contêineres que o mp4probe
       não lê: mkv, avi, wmv...). Mesmo formato de dict do mp4probe.probe
       (só as chaves duration, width, height, codec); None se falhar."""
    cmd = [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "format=duration:stream=codec_name,width,height", "-of", "json", filepath]
    done = _run(cmd, cancel_event, None, timeout)
    if done is None or done[0] != 0:
        return None
    try:
        data = json.loads(done[1].decode("utf-8", "replace"))
        stream = (data.get("streams") or [{}])[0]
        duration = float(data.get("format", {}).get("duration") or 0) or None
        if not stream.get("width"):
            return None
        return {"duration": duration, "width": int(stream["width"]), "height": int(stream["height"]),
                "codec": str(stream.get("codec_name") or "")}
    except (ValueError, TypeError, KeyError):
        return None
