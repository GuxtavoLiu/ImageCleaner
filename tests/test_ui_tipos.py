"""
Interface com as caixas Fotos / Vídeos / Outros (Tk real, diálogos simulados):
grupos de bytes depois dos de fotos, vocabulário "arquivos", linhas sem
"Resolução", nenhuma tentativa de abrir vídeo com o Pillow, exclusão pela
Lixeira, trava "mudou depois da varredura", cancelamento e configurações.
"""
import csv
import gc
import os
import shutil
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

import main as ic
from conftest import T0, build_single_fixture


def _blob(size, seed):
    out = bytearray()
    x = seed * 2654435761 % (1 << 32) or 1
    while len(out) < size:
        x = (x * 1103515245 + 12345) % (1 << 31)
        out += x.to_bytes(4, "little")
    return bytes(out[:size])


def build_mixed_fixture(root):
    """Fixture de fotos de sempre + vídeos, documentos e uma foto HEIC, todos
       com cópia idêntica, e um vídeo sem par."""
    build_single_fixture(root)
    files = {
        "clip.mp4": _blob(300_000, 1), "notas.txt": _blob(4_000, 2),
        "foto.heic": _blob(50_000, 3), "sozinho.mov": _blob(123_457, 4),
    }
    for k, (name, data) in enumerate(sorted(files.items())):
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.write(data)
        os.utime(path, (T0 + 1000 + k, T0 + 1000 + k))
        if name != "sozinho.mov":
            base, ext = os.path.splitext(path)
            copy = os.path.join(root, "sub", os.path.basename(base) + "_copia" + ext)
            shutil.copyfile(path, copy)
            os.utime(copy, (T0 + 2000 + k, T0 + 2000 + k))
    return root


def _pump(root, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        root.update()
        time.sleep(0.01)


def _start(root, monkeypatch, folder, photos=1, videos=1, others=1, before_scan=None, use_cache=0):
    for w in root.winfo_children():
        w.destroy()
    # Lixo cíclico com variáveis do Tk tem de ser recolhido AQUI, na thread
    # principal: recolhido por acaso numa thread de trabalho, o __del__ do Tk
    # reclama (nos testes não há mainloop para repassar a chamada).
    gc.collect()
    msgs = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(messagebox, name, lambda title, msg, _n=name: msgs.append((_n, title, msg)))
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg: msgs.append(("askyesno", title, msg)) or True)
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: folder)
    done = {}
    orig = ic.ImageCleaner.scan_folder

    def scan_folder(self):
        try:
            orig(self)
        finally:
            done["ok"] = True

    monkeypatch.setattr(ic.ImageCleaner, "scan_folder", scan_folder)
    app = ic.ImageCleaner(root)
    app.select_folder()
    app.use_cache_var.set(use_cache)
    app.scan_photos_var.set(photos)
    app.scan_videos_var.set(videos)
    app.scan_others_var.set(others)
    if before_scan:
        before_scan(app)
    app.start_scan()
    t0 = time.time()
    while time.time() - t0 < 60 and not done.get("ok") and not any(m[0] == "showerror" for m in msgs):
        root.update()
        time.sleep(0.01)
    _pump(root, 0.5)      # lotes de renderização e miniaturas em segundo plano
    return app, msgs


@pytest.fixture
def cleanup(tk_root):
    yield tk_root
    for w in tk_root.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass


def _group_names(app):
    return [[os.path.basename(fp) for (fp, _, _) in g] for g in app.groups]


def _titles(app):
    return [str(c.cget("text")) for c in app.content_frame.winfo_children() if isinstance(c, tk.LabelFrame)]


def _row_text(app, idx, pos):
    return str(app.row_widgets[(idx, pos)][5].cget("text"))


def _buttons(widget, out):
    for c in widget.winfo_children():
        if isinstance(c, tk.Button):
            out.append(str(c.cget("text")))
        _buttons(c, out)
    return out


def test_tudo_marcado_grupos_de_bytes_depois_dos_de_fotos(cleanup, tmp_path, monkeypatch):
    root = cleanup
    opened = []
    orig_open = ic.Image.open

    def spy_open(fp, *a, **k):
        opened.append(str(fp))
        return orig_open(fp, *a, **k)

    monkeypatch.setattr(ic.Image, "open", spy_open)
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    app, msgs = _start(root, monkeypatch, fx)
    assert _group_names(app) == [
        ["a.jpg", "a_copy.jpg", "a_q30.jpg", "a_q60.jpg"], ["b.png", "b_copy.png"],   # fotos, como sempre
        ["clip.mp4", "clip_copia.mp4"], ["foto.heic", "foto_copia.heic"], ["notas.txt", "notas_copia.txt"],
    ]
    assert _titles(app)[:4] == ["Grupo 1", "Grupo 2", "Grupo 3 (vídeos)", "Grupo 4 (fotos)"]
    assert str(app.groups_window.title()) == "Grupos de Arquivos Duplicados"
    assert app.groups_same_photo is not None and len(app.groups_same_photo) == len(app.groups)
    assert app.groups_same_photo[2] == [None, None]
    # resumo: fotos contadas como imagens; o resto, à parte
    resumo = [m for m in msgs if m[1] == "Escaneamento Concluído"][0][2]
    assert "7 de 7 imagens processadas" in resumo
    assert "7 arquivo(s) comparados só por conteúdo" in resumo and "3 grupo(s) de cópias exatas" in resumo
    # linha de um vídeo: sem "Resolução", com o tipo
    assert "Status: Cópia exata" in _row_text(app, 2, 0) and "Vídeo MP4" in _row_text(app, 2, 0)
    assert "Resolução" not in _row_text(app, 2, 0)
    assert "Resolução: 128 x 128" in _row_text(app, 0, 0)          # foto: intocada
    # a placa aparece na hora (o Label sempre tem imagem)
    assert str(app.row_widgets[(2, 0)][3].cget("image"))

    app.select_similar_images()
    app.select_identical_images()
    assert "arquivos idênticos foram selecionados" in msgs[-1][2]
    app.open_preview(2, 0)
    root.update()
    assert "Abrir no programa padrão" in _buttons(app.preview_window, [])
    app.preview_window.destroy()
    # NUNCA o Pillow em quem não é foto
    assert not [p for p in opened if os.path.splitext(p)[1].lower() in (".mp4", ".heic", ".txt", ".mov")]
    # seleção de idênticas: mantém o mais antigo de cada grupo de bytes
    sel = sorted(os.path.basename(im['filepath']) for d in app.group_check_vars.values()
                 for im in d['images'] if im['var'].get() == 1)
    assert [s for s in sel if not s.endswith((".jpg", ".png"))] == \
        ["clip_copia.mp4", "foto_copia.heic", "notas_copia.txt"]
    assert str(app.selection_label.cget("text")).startswith("Selecionados: ")
    assert "arquivo(s)" in str(app.selection_label.cget("text"))


def test_excluir_vai_para_a_lixeira_com_vocabulario_de_arquivos_e_csv(cleanup, tmp_path, monkeypatch):
    root = cleanup
    trash = tmp_path / "lixeira"
    trash.mkdir()
    monkeypatch.setattr(ic, "_send2trash", lambda p: shutil.move(p, str(trash / os.path.basename(p))))
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    app, msgs = _start(root, monkeypatch, fx, photos=0)
    assert _group_names(app) == [["clip.mp4", "clip_copia.mp4"], ["notas.txt", "notas_copia.txt"]]
    assert _titles(app) == ["Grupo 1 (vídeos)", "Grupo 2 (arquivos)"]
    app.select_group(0, "identical")
    # o vídeo selecionado muda DEPOIS da varredura: não pode ser tocado
    app.select_group(1, "identical")
    changed = os.path.join(fx, "sub", "notas_copia.txt")
    with open(changed, "ab") as f:
        f.write(b"editado depois")
    app.delete_all_selected()
    confirm = [m for m in msgs if m[0] == "askyesno"][-1][2]
    assert "Enviar 1 arquivos selecionados para a Lixeira" in confirm
    assert "1 arquivo(s) mudaram depois da varredura e NÃO serão tocados" in confirm
    assert "1 arquivos enviados para a Lixeira do Windows!" in msgs[-1][2]
    assert sorted(os.listdir(trash)) == ["clip_copia.mp4"]
    assert os.path.exists(changed) and os.path.exists(os.path.join(fx, "clip.mp4"))
    with open(app.session_report.path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert [(r["acao"], r["status"], os.path.basename(r["caminho"])) for r in rows] == \
        [("lixeira", "Idêntica", "clip_copia.mp4")]
    # por grupo, com o grupo já sem nada selecionado
    app.delete_images(app.group_check_vars[1]['group'], app.group_check_vars[1]['check_vars'])
    assert "Nenhum arquivo selecionado neste grupo." in msgs[-1][2]


def test_so_videos_sem_duplicata_e_pasta_sem_nada(cleanup, tmp_path, monkeypatch):
    root = cleanup
    fx = str(tmp_path / "fx")
    os.makedirs(fx)
    for k in range(3):
        with open(os.path.join(fx, f"v{k}.mp4"), "wb") as f:
            f.write(_blob(10_000 + k, k + 1))
    app, msgs = _start(root, monkeypatch, fx, photos=0, others=0)
    assert [m[2] for m in msgs if m[1] == "Resultado"] == ["Nenhuma duplicata encontrada."]
    assert getattr(app, "groups_window", None) is None
    assert app.scan_in_progress is False
    empty = str(tmp_path / "vazia")
    os.makedirs(empty)
    app, msgs = _start(root, monkeypatch, empty, photos=0, others=0)
    assert [m[2] for m in msgs if m[1] == "Resultado"] == ["Nenhum arquivo encontrado."]


def test_cancelar_durante_a_comparacao_por_bytes(cleanup, tmp_path, monkeypatch):
    root = cleanup
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    holder = {}
    orig = ic.find_identical_files

    def cancelling(*a, **k):
        holder["app"].scan_cancelled = True
        return orig(*a, **k)

    monkeypatch.setattr(ic, "find_identical_files", cancelling)
    app, msgs = _start(root, monkeypatch, fx, before_scan=lambda a: holder.update(app=a))
    assert msgs[-1][1] == "Escaneamento Cancelado"
    assert "Comparação de arquivos cancelada." in msgs[-1][2]
    assert getattr(app, "groups_window", None) is None and app.scan_in_progress is False


def test_arquivo_ilegivel_gera_um_aviso_e_fica_fora(cleanup, tmp_path, monkeypatch):
    root = cleanup
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    orig = ic.file_quick_key

    def quick(fp, size, on_bytes=None, **kw):
        if fp.endswith("clip_copia.mp4"):
            raise PermissionError("acesso negado")
        return orig(fp, size, on_bytes, **kw)

    monkeypatch.setattr(ic, "file_quick_key", quick)
    app, msgs = _start(root, monkeypatch, fx, photos=0)
    avisos = [m for m in msgs if m[1] == "Comparação incompleta"]
    assert len(avisos) == 1 and "1 arquivo(s) não puderam ser lidos" in avisos[0][2]
    assert _group_names(app) == [["notas.txt", "notas_copia.txt"]]       # o vídeo sem prova não aparece


def test_configuracoes_e_validacao_das_caixas(cleanup, tmp_path, monkeypatch):
    root = cleanup
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    app, msgs = _start(root, monkeypatch, fx, photos=0, videos=0, others=0)
    assert msgs[-1][0] == "showerror" and "Marque pelo menos um tipo" in msgs[-1][2]
    assert str(app.confirm_check.cget("state")) == "normal"    # só muda pelo clique na caixa
    app.scan_photos_var.set(0)
    app._on_kinds_changed()
    assert str(app.confirm_check.cget("state")) == "disabled"
    assert str(app.same_photo_check.cget("state")) == "disabled"
    app, _ = _start(root, monkeypatch, fx, photos=1, videos=1, others=0)
    saved = ic.load_settings()
    assert (saved["scan_photos"], saved["scan_videos"], saved["scan_others"]) == (1, 1, 0)
    for w in root.winfo_children():
        w.destroy()
    again = ic.ImageCleaner(root)                               # a sessão seguinte lembra
    assert (again.scan_photos_var.get(), again.scan_videos_var.get(), again.scan_others_var.get()) == (1, 1, 0)
    assert ic.DEFAULT_SETTINGS["scan_photos"] == 1 and ic.DEFAULT_SETTINGS["scan_videos"] == 0


def test_vocabulario():
    assert ic.file_wording("Enviar 3 imagens selecionadas para a Lixeira do Windows?") == \
        "Enviar 3 arquivos selecionados para a Lixeira do Windows?"
    assert ic.file_wording("2 imagem(ns) devolvida(s) à origem (e selecionada(s) de novo).") == \
        "2 arquivo(s) devolvido(s) à origem (e selecionado(s) de novo)."
    assert ic.file_wording("Selecionadas: 1 imagem(ns), 3,0 KB") == "Selecionados: 1 arquivo(s), 3,0 KB"
    assert ic.media_summary(ic.KIND_VIDEO, "a.MOV", {"width": 1280, "height": 720, "duration": 73.4}) == \
        "Vídeo MOV: 1280 x 720, 1:13"
    assert ic.media_summary(ic.KIND_VIDEO, "a.avi", None) == "Vídeo AVI"
    assert ic.media_summary(ic.KIND_OTHER, "doc.pdf", None) == "Arquivo PDF"
    plate = ic.extension_plate("filme.mkv", ic.KIND_VIDEO, 200, 124)
    assert plate.size == (200, 124) and len(plate.getcolors(1 << 20)) > 1  # tem texto desenhado


def test_selftest_com_e_sem_bytes(tmp_path):
    so_fotos = build_single_fixture(str(tmp_path / "fotos"))
    base = ic.run_selftest(so_fotos)
    assert base.startswith("SELFTEST OK: 7 arquivos, 7 hashes") and "BYTES" not in base
    assert "BYTES" not in ic.run_selftest(so_fotos, videos=True, others=True)   # nada a comparar por bytes
    misto = build_mixed_fixture(str(tmp_path / "misto"))
    padrao = ic.run_selftest(misto)
    # o trecho de fotos não muda com outros arquivos na pasta; a HEIC (caixa Fotos) entra por bytes
    assert padrao.split(" hashes")[0] == base.split(" hashes")[0]
    assert "| BYTES: 2 arquivos, 1 grupos, 2 idênticos, 1 selecionáveis" in padrao
    tudo = ic.run_selftest(misto, videos=True, others=True)
    assert "| BYTES: 7 arquivos, 3 grupos, 6 idênticos, 3 selecionáveis" in tudo and "0 erros" in tudo
    so_videos = str(tmp_path / "so_videos")
    os.makedirs(so_videos)
    for name in ("x.mp4", "y.mp4"):
        with open(os.path.join(so_videos, name), "wb") as f:
            f.write(_blob(9_000, 5))
    out = ic.run_selftest(so_videos, videos=True)                  # pasta sem nenhuma foto
    assert out.startswith("SELFTEST OK: 0 arquivos, 0 hashes") and "1 grupos, 2 idênticos" in out


def test_prova_vinda_do_cache_e_relida_antes_de_excluir(cleanup, tmp_path, monkeypatch):
    """Conteúdo alterado SEM mudar tamanho nem data (contêiner VeraCrypt, editor
       de tags com "manter a data") engana o cache. Antes de excluir, o app relê
       os arquivos cuja prova é antiga: a cópia que deixou de ser cópia fica."""
    root = cleanup
    monkeypatch.setattr(ic, "get_hash_cache_path", lambda: str(tmp_path / "cache_teste.sqlite"))
    trash = tmp_path / "lixeira"
    trash.mkdir()
    monkeypatch.setattr(ic, "_send2trash", lambda p: shutil.move(p, str(trash / os.path.basename(p))))
    fx = str(tmp_path / "fx")
    os.makedirs(fx)
    data = _blob(400_000, 7)
    cofre, backup = os.path.join(fx, "cofre.hc"), os.path.join(fx, "cofre_backup.hc")
    outro, outro2 = os.path.join(fx, "lista.txt"), os.path.join(fx, "lista_copia.txt")
    for k, (path, blob) in enumerate(((cofre, data), (backup, data),
                                      (outro, _blob(3_000, 8)), (outro2, _blob(3_000, 8)))):
        with open(path, "wb") as f:
            f.write(blob)
        os.utime(path, (T0 + k, T0 + k))
    app, msgs = _start(root, monkeypatch, fx, photos=0, use_cache=1)      # 1ª varredura: grava o cache
    assert _group_names(app) == [["cofre.hc", "cofre_backup.hc"], ["lista.txt", "lista_copia.txt"]]
    assert app.byte_cached_proof == set()                                  # tudo lido agora

    novo = bytearray(data)
    novo[200_000:200_010] = b"dados novos"[:10]                            # fora das amostras
    st = os.stat(cofre)
    with open(cofre, "r+b") as f:
        f.write(bytes(novo))
    os.utime(cofre, ns=(st.st_atime_ns, st.st_mtime_ns))                   # data preservada

    app, msgs = _start(root, monkeypatch, fx, photos=0, use_cache=1)      # 2ª: o cache é enganado
    assert _group_names(app)[0] == ["cofre.hc", "cofre_backup.hc"]
    assert len(app.byte_cached_proof) == 4
    app.select_identical_images()                                          # mantém o mais antigo de cada grupo
    app.delete_all_selected()
    assert "Enviar 2 arquivos selecionados" in [m for m in msgs if m[0] == "askyesno"][-1][2]
    final = msgs[-1][2]
    assert "1 arquivos enviados para a Lixeira" in final
    assert "1 arquivo(s) NÃO foram tocados: a prova de cópia exata vinha do cache" in final
    assert sorted(os.listdir(trash)) == ["lista_copia.txt"]                # a cópia de verdade foi
    assert os.path.exists(backup) and os.path.exists(cofre)               # o backup, que não é mais cópia, ficou
    # o cache foi consertado: a varredura seguinte já não agrupa os dois
    app, msgs = _start(root, monkeypatch, fx, photos=0, use_cache=1)
    assert [m[2] for m in msgs if m[1] == "Resultado"] == ["Nenhuma duplicata encontrada."]


def test_so_fotos_com_heic_mantem_o_vocabulario_de_imagens(cleanup, tmp_path, monkeypatch):
    root = cleanup
    fx = build_mixed_fixture(str(tmp_path / "fx"))
    app, msgs = _start(root, monkeypatch, fx, photos=1, videos=0, others=0)
    assert _group_names(app)[2:] == [["foto.heic", "foto_copia.heic"]]
    assert _titles(app) == ["Grupo 1", "Grupo 2", "Grupo 3 (fotos)"]
    assert str(app.groups_window.title()) == "Grupos de Imagens Similares"   # HEIC é foto: nada de "arquivos"
    assert str(app.selection_label.cget("text")).startswith("Selecionadas: 0 imagem(ns)")
    assert "Foto HEIC (comparada só por conteúdo)" in _row_text(app, 2, 0)
    # pasta só com HEIC sem duplicata: achou imagens, não achou duplicata
    so_heic = str(tmp_path / "so_heic")
    os.makedirs(so_heic)
    for k in range(2):
        with open(os.path.join(so_heic, f"f{k}.heic"), "wb") as f:
            f.write(_blob(20_000, 40 + k))
    app, msgs = _start(root, monkeypatch, so_heic, photos=1, videos=0, others=0)
    assert [m[2] for m in msgs if m[1] == "Resultado"] == ["Nenhuma duplicata encontrada."]


def test_aviso_do_segundo_hash_nao_some_quando_ha_grupos_de_arquivos(cleanup, tmp_path, monkeypatch):
    import conftest as cf
    root = cleanup
    fx = str(tmp_path / "fx")
    os.makedirs(fx)
    cf._make_fake_pair(fx)                       # fotos que o 2º hash descarta como coincidência
    for name in ("v.mp4", "v_copia.mp4"):
        with open(os.path.join(fx, name), "wb") as f:
            f.write(_blob(50_000, 3))
    app, msgs = _start(root, monkeypatch, fx, photos=1, videos=1, others=0)
    aviso = [m[2] for m in msgs if m[1] == "Resultado"]
    assert len(aviso) == 1 and "Nenhuma imagem similar confirmada" in aviso[0]
    assert "Os grupos exibidos a seguir são de arquivos comparados por conteúdo." in aviso[0]
    assert _group_names(app) == [["v.mp4", "v_copia.mp4"]]
