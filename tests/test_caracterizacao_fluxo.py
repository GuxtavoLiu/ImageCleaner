"""
Caracterização do fluxo completo da interface (Tk real, diálogos simulados):
do "Iniciar escaneamento" até a tela de grupos ou a mensagem final.

Os goldens das funções puras (test_regression_single_mode) não passam por
_scan_folder_impl nem por group_images. Este teste fotografa exatamente o que
o usuário vê nesse caminho: a sequência de diálogos (tipo, título, texto), os
títulos das janelas de progresso, os grupos, as classes "Mesma foto", as
estatísticas, a barra de status e os botões. Qualquer reestruturação do fluxo
tem de manter este snapshot intacto quando só "Fotos" está em jogo.

Os cenários "misto" colocam vídeos e outros arquivos na pasta: o resultado
tem de ser IDÊNTICO ao do cenário sem eles.

Gerar o golden (rodar UMA vez, de forma deliberada, com o main.py de referência):
    python tests/test_caracterizacao_fluxo.py --gerar
"""
import json
import gc
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as ic  # noqa: E402
import conftest as cf  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_caracterizacao_fluxo.json")

# Funções de módulo onde o cancelamento é injetado (uma por fase do fluxo)
CANCEL_POINTS = {
    "listagem": "list_image_files",
    "hash": "hash_files",
    "agrupamento": "find_similar_groups",
    "md5": "md5_for_groups",
    "dhash": "dhash_for_groups",
    "mesma_foto": "same_photo_stage",
}


class _Patcher:
    """monkeypatch mínimo, para o gerador do golden rodar fora do pytest."""
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo = []


def _add_mixed_files(folder):
    """Vídeos e outros arquivos (com cópias idênticas) que o modo Fotos ignora."""
    os.makedirs(os.path.join(folder, "sub"), exist_ok=True)
    blobs = {
        "clip.mp4": b"\x00\x00\x00\x18ftypmp42" + b"video-falso-1" * 4000,
        "notas.txt": b"lista de compras\n" * 300,
        os.path.join("sub", "outro.mov"): b"\x00\x00\x00\x14ftypqt  " + b"video-falso-2" * 5000,
        "planilha.xlsx": b"PK\x03\x04" + b"conteudo" * 2000,
    }
    for rel, data in blobs.items():
        path = os.path.join(folder, rel)
        with open(path, "wb") as f:
            f.write(data)
        base, ext = os.path.splitext(path)
        shutil.copyfile(path, base + "_copia" + ext)
    for k, (dirpath, _, files) in enumerate(os.walk(folder)):
        for j, name in enumerate(sorted(files)):
            if os.path.splitext(name)[1].lower() in (".mp4", ".mov", ".txt", ".xlsx"):
                t = cf.T0 + 5000 + 10 * j + 100 * k
                os.utime(os.path.join(dirpath, name), (t, t))


def _build(name, base):
    """Monta as pastas do cenário. Retorna (alvo, referência_ou_None, opções, cancelar_em)."""
    options = {}
    cancel_at = None
    reference = None
    mixed = name.endswith("__misto")
    core = name[:-len("__misto")] if mixed else name
    target = os.path.join(base, "alvo")
    if core == "normal":
        cf.build_single_fixture(target)
    elif core == "normal_sem_pos_filtros":
        cf.build_single_fixture(target)
        options = {"confirm_similar": 0, "same_photo": 0}
    elif core == "pasta_vazia":
        os.makedirs(target)
    elif core == "sem_semelhantes":
        cf._write(target, "a.jpg", kind="A", mtime=cf.T0 + 1)
        cf._write(target, "c.jpg", kind="C", mtime=cf.T0 + 2)
    elif core in ("referencia", "referencia_oculta_internas"):
        target, reference = cf.build_reference_fixture(base)
        if core == "referencia_oculta_internas":
            options = {"show_target_only": 0}
    elif core == "referencia_sem_duplicata":
        # grupo só na referência (acervo_b + cópia); alvo sem par nenhum
        reference = os.path.join(base, "ref")
        cf._write(target, "unica.jpg", kind="C", mtime=cf.T0 + 1)
        cf._write(reference, "acervo_b.jpg", kind="B", mtime=cf.T0 + 2)
        cf._write(reference, "acervo_b2.jpg", copy_of="acervo_b.jpg", mtime=cf.T0 + 3)
    elif core == "referencia_sem_grupos":
        reference = os.path.join(base, "ref")
        cf._write(target, "unica.jpg", kind="C", mtime=cf.T0 + 1)
        cf._write(reference, "acervo_a.jpg", kind="A", mtime=cf.T0 + 2)
    elif core == "confirmacao_descarta_tudo":
        os.makedirs(target)
        cf._make_fake_pair(target)
        for k, n in enumerate(("fake_a.png", "fake_b.png")):
            os.utime(os.path.join(target, n), (cf.T0 + k, cf.T0 + k))
    elif core == "confirmacao_fixture":
        cf.build_confirm_fixture(target)
    elif core == "mesma_foto":
        cf.build_same_photo_fixture(target)
    elif core.startswith("cancelar_"):
        cf.build_single_fixture(target)
        cancel_at = core[len("cancelar_"):]
    else:
        raise ValueError(name)
    if mixed:
        _add_mixed_files(target)
        if reference:
            _add_mixed_files(reference)
    return target, reference, options, cancel_at


SCENARIOS = [
    "normal", "normal_sem_pos_filtros", "pasta_vazia", "sem_semelhantes",
    "referencia", "referencia_oculta_internas", "referencia_sem_duplicata",
    "referencia_sem_grupos", "confirmacao_descarta_tudo", "confirmacao_fixture", "mesma_foto",
] + ["cancelar_" + k for k in CANCEL_POINTS]
MIXED = ["normal", "referencia", "referencia_sem_duplicata", "confirmacao_descarta_tudo",
         "mesma_foto", "pasta_vazia", "cancelar_md5"]


def _pattern(values):
    """Valores -> inteiros pela ordem de primeira aparição (None fica None)."""
    seen = {}
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            out.append(seen.setdefault(v, len(seen)))
    return out


def _walk(widget, cls, out):
    for c in widget.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        _walk(c, cls, out)
    return out


def run_scenario(root, name, base):
    """Roda um cenário ponta a ponta e devolve o snapshot serializável."""
    for w in root.winfo_children():
        w.destroy()
    gc.collect()   # lixo do Tk recolhido na thread principal, nunca numa thread de trabalho
    target, reference, options, cancel_at = _build(name, base)
    p = _Patcher()
    dialogs = []
    progress_titles = []
    holder = {}

    def norm(text):
        text = str(text)
        for path, tag in ((reference, "<REF>"), (target, "<ALVO>"), (ic.get_log_path(), "<LOG>")):
            if path:
                text = text.replace(path, tag).replace(path.replace("\\", "/"), tag)
        return text

    try:
        p.setattr(ic, "get_settings_path", lambda: os.path.join(base, "settings_test.json"))
        p.setattr(ic, "get_reports_dir", lambda: os.path.join(base, "relatorios_test"))
        for kind in ("showinfo", "showerror", "showwarning"):
            p.setattr(messagebox, kind,
                      lambda title, msg, _k=kind: dialogs.append([_k, norm(title), norm(msg)]))
        p.setattr(messagebox, "askyesno",
                  lambda title, msg: dialogs.append(["askyesno", norm(title), norm(msg)]) or True)
        p.setattr(filedialog, "askdirectory", lambda **k: target)

        orig_close = ic.ImageCleaner._close_progress_window

        def close_progress(self):
            try:
                if hasattr(self, "progress_window") and self.progress_window.winfo_exists():
                    progress_titles.append(str(self.progress_window.title()))
            except tk.TclError:
                pass
            orig_close(self)

        p.setattr(ic.ImageCleaner, "_close_progress_window", close_progress)

        orig_scan = ic.ImageCleaner.scan_folder

        def scan_folder(self):
            try:
                orig_scan(self)
            finally:
                holder["done"] = True

        p.setattr(ic.ImageCleaner, "scan_folder", scan_folder)

        if cancel_at:
            fn_name = CANCEL_POINTS[cancel_at]
            orig_fn = getattr(ic, fn_name)

            def cancelling(*a, **k):
                holder["app"].scan_cancelled = True
                return orig_fn(*a, **k)

            p.setattr(ic, fn_name, cancelling)

        app = ic.ImageCleaner(root)
        holder["app"] = app
        app._apply_target_folder(target)
        if reference:
            app._apply_reference_folder(reference)
        app.use_cache_var.set(0)
        for key, var in (("confirm_similar", "confirm_similar_var"), ("same_photo", "same_photo_var"),
                         ("show_target_only", "show_target_only_var")):
            if key in options:
                getattr(app, var).set(options[key])
        app.start_scan()
        t0 = time.time()
        while time.time() - t0 < 60 and not holder.get("done"):
            root.update()
            time.sleep(0.01)
        assert holder.get("done"), f"{name}: o fluxo não terminou em 60 s"
        # deixa os lotes de renderização (after) terminarem
        t1 = time.time()
        while time.time() - t1 < 0.4:
            root.update()
            time.sleep(0.01)

        snap = {
            "dialogs": dialogs,
            "progress_titles": progress_titles,
            "scan_in_progress": bool(getattr(app, "scan_in_progress", False)),
            "has_groups_window": getattr(app, "groups_window", None) is not None,
        }
        if snap["has_groups_window"]:
            def rel(fp):
                for rootdir, tag in ((reference, "REF"), (target, "ALVO")):
                    if rootdir and ic.cache_key(fp).startswith(ic.folder_prefix(rootdir)):
                        return tag + "/" + os.path.relpath(fp, rootdir).replace("\\", "/")
                return fp

            snap["groups"] = []
            for g in app.groups:
                pat = _pattern([m for (_, _, m) in g])
                snap["groups"].append([[rel(fp), k] for (fp, _, _), k in zip(g, pat)])
            sp = app.groups_same_photo
            snap["groups_same_photo"] = None if sp is None else [_pattern(row) for row in sp]
            snap["same_photo_suspect_count"] = len(app.same_photo_suspect)
            snap["confirm_stats"] = app.confirm_stats
            snap["same_photo_stats"] = app.same_photo_stats
            snap["window_title"] = str(app.groups_window.title())
            snap["labels"] = {
                "review": str(app.review_label.cget("text")),
                "selection": str(app.selection_label.cget("text")),
                "page_info": str(app.page_info_label.cget("text")),
            }
            snap["group_titles"] = [str(c.cget("text")) for c in app.content_frame.winfo_children()
                                    if isinstance(c, tk.LabelFrame)]
            snap["buttons"] = [str(b.cget("text")) for b in _walk(app.groups_window, tk.Button, [])]
            rows = {}
            for (idx, pos), widgets in sorted(app.row_widgets.items()):
                info = str(widgets[5].cget("text")).split("\n")
                rows[f"{idx}:{pos}"] = info[:2]
            snap["rows"] = rows
            snap["statuses"] = [
                [ic.image_status(im, data["md5_count"]) for im in data["images"]]
                for _, data in sorted(app.group_check_vars.items())
            ]
        return snap
    finally:
        p.undo()
        for w in root.winfo_children():
            try:
                w.destroy()
            except tk.TclError:
                pass


def _load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def _roundtrip(snap):
    return json.loads(json.dumps(snap, ensure_ascii=False))


@pytest.mark.parametrize("name", SCENARIOS)
def test_fluxo_igual_ao_golden(name, tk_root, tmp_path):
    snap = run_scenario(tk_root, name, str(tmp_path))
    assert _roundtrip(snap) == _load_golden()[name]


@pytest.mark.parametrize("name", MIXED)
def test_fluxo_com_videos_e_outros_na_pasta_nao_muda_nada(name, tk_root, tmp_path):
    """Só Fotos marcado: vídeos e outros arquivos na pasta são invisíveis."""
    snap = run_scenario(tk_root, name + "__misto", str(tmp_path))
    assert _roundtrip(snap) == _load_golden()[name]


if __name__ == "__main__":
    if "--gerar" not in sys.argv:
        sys.exit("uso: python tests/test_caracterizacao_fluxo.py --gerar")
    tk_root_ = tk.Tk()
    tk_root_.withdraw()
    out = {}
    for scenario in SCENARIOS:
        tmp = tempfile.mkdtemp(prefix="ic_caract_")
        try:
            out[scenario] = _roundtrip(run_scenario(tk_root_, scenario, tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"{scenario}: {len(out[scenario]['dialogs'])} diálogo(s), "
              f"grupos={len(out[scenario].get('groups', []))}")
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("golden gravado em", GOLDEN)
