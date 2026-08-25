"""
Regressão do modo de uma pasta: o resultado completo (grupos, ordem, rótulos
Idêntica/Semelhante e seleções automáticas) tem que ser idêntico ao snapshot
dourado gerado com o main.py ANTES do modo de comparação existir.

Para regenerar o snapshot (só se uma mudança de comportamento for
intencional e validada): python -c "import sys; sys.path.insert(0,'tests');
import conftest, tempfile; conftest.generate_golden(tempfile.mkdtemp())"
"""
import json

import main as ic
from conftest import (GOLDEN_PATH, GOLDEN_QUALITY_PATH, GOLDEN_FIXTURE_CONFIRM_QUALITY_PATH,
                      LEGACY_PRIORITY, build_single_fixture, snapshot_single_mode)


def test_fixture_distancias(tmp_path):
    import os
    root = build_single_fixture(str(tmp_path / "fix"))
    h = lambda n: ic.compute_phash(os.path.join(root, n))
    assert abs(h("a.jpg") - h("a_q30.jpg")) <= ic.SIMILARITY_THRESHOLD
    assert abs(h("a.jpg") - h("a_q60.jpg")) <= ic.SIMILARITY_THRESHOLD
    assert abs(h("a.jpg") - h("c.jpg")) > ic.SIMILARITY_THRESHOLD
    assert abs(h("a.jpg") - h(os.path.join("sub", "b.png"))) > ic.SIMILARITY_THRESHOLD
    assert abs(h("c.jpg") - h(os.path.join("sub", "b.png"))) > ic.SIMILARITY_THRESHOLD


def test_modo_uma_pasta_igual_ao_snapshot_dourado(tmp_path):
    """Sem confirmação por segundo hash: comportamento original, bit a bit."""
    root = build_single_fixture(str(tmp_path / "fix"))
    atual = snapshot_single_mode(root, confirm=False, priority=LEGACY_PRIORITY)
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        dourado = json.load(f)
    assert atual == dourado


def test_modo_uma_pasta_confirmado_igual_ao_snapshot_dourado(tmp_path):
    """Com confirmação: snapshot próprio (gerado igual ao original: recompressões
       não são rejeitadas pelo dhash)."""
    from conftest import GOLDEN_CONFIRM_PATH
    root = build_single_fixture(str(tmp_path / "fix"))
    atual = snapshot_single_mode(root, confirm=True, priority=LEGACY_PRIORITY)
    with open(GOLDEN_CONFIRM_PATH, encoding="utf-8") as f:
        dourado = json.load(f)
    assert atual == dourado
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        assert atual == json.load(f)


def test_fixture_confirm_igual_ao_snapshot_dourado(tmp_path):
    from conftest import GOLDEN_FIXTURE_CONFIRM_PATH, build_confirm_fixture
    root = build_confirm_fixture(str(tmp_path / "fx"))
    atual = snapshot_single_mode(root, confirm=True, priority=LEGACY_PRIORITY)
    with open(GOLDEN_FIXTURE_CONFIRM_PATH, encoding="utf-8") as f:
        assert atual == json.load(f)


def test_regra_de_qualidade_igual_ao_snapshot_dourado(tmp_path):
    """Regra nova (SIMILAR_KEEP_PRIORITY): goldens próprios, gerados de forma
       deliberada. Na fixture única, a_q60 (maior arquivo) fica e a_q30 sai."""
    from conftest import build_confirm_fixture
    root = build_single_fixture(str(tmp_path / "fix"))
    atual = snapshot_single_mode(root, confirm=True)
    with open(GOLDEN_QUALITY_PATH, encoding="utf-8") as f:
        dourado = json.load(f)
    assert atual == dourado
    assert dourado["selected_similar"] == ["a_q30.jpg"]
    assert snapshot_single_mode(root, confirm=False) == dourado    # confirmação não muda a fixture
    root2 = build_confirm_fixture(str(tmp_path / "fx"))
    with open(GOLDEN_FIXTURE_CONFIRM_QUALITY_PATH, encoding="utf-8") as f:
        assert snapshot_single_mode(root2, confirm=True) == json.load(f)


def test_selftest_uma_pasta(tmp_path):
    root = build_single_fixture(str(tmp_path / "fix"))
    summary = ic.run_selftest(root)
    assert summary.startswith("SELFTEST OK: 7 arquivos, 7 hashes, 0 erros, 2 grupos, 4 idênticas")
    assert f"| CONF(dhash<={ic.DHASH_THRESHOLD}): 0 grupos e 0 imagens descartados" in summary
    sem = ic.run_selftest(root, confirm_similar=False)
    assert sem.startswith("SELFTEST OK: 7 arquivos, 7 hashes, 0 erros, 2 grupos, 4 idênticas")
    assert "CONF(" not in sem


def test_selftest_fixture_confirm(tmp_path):
    from conftest import build_confirm_fixture
    root = build_confirm_fixture(str(tmp_path / "fx"))
    com = ic.run_selftest(root)
    sem = ic.run_selftest(root, confirm_similar=False)
    assert "2 grupos, 2 idênticas" in com          # alpha_text par + true_a par
    assert "CONF(" in com and "descartados" in com
    assert "CONF(" not in sem
    n_sem = int(sem.split(" grupos")[0].split(", ")[-1])
    assert n_sem >= 3                              # sem confirmação: falsos e degenerados agrupam


def test_mesma_foto_goldens_e_semelhantes_inalterados(tmp_path):
    """Feature "Mesma foto" ligada explicitamente: goldens próprios; e a seleção
       de Semelhantes é IDÊNTICA à da regra de qualidade sem a feature."""
    from conftest import (GOLDEN_SAME_PHOTO_FIXTURE_PATH, GOLDEN_SINGLE_SAME_PHOTO_PATH,
                          build_same_photo_fixture)
    root = build_single_fixture(str(tmp_path / "fix"))
    com = snapshot_single_mode(root, confirm=True, same_photo=True)
    with open(GOLDEN_SINGLE_SAME_PHOTO_PATH, encoding="utf-8") as f:
        assert com == json.load(f)
    with open(GOLDEN_QUALITY_PATH, encoding="utf-8") as f:
        sem = json.load(f)
    assert com["selected_similar"] == sem["selected_similar"]
    assert com["selected_identical"] == sem["selected_identical"]
    assert "selected_same_photo" in com and "selected_same_photo" not in sem
    root2 = build_same_photo_fixture(str(tmp_path / "sp"))
    with open(GOLDEN_SAME_PHOTO_FIXTURE_PATH, encoding="utf-8") as f:
        dourado = json.load(f)
    assert snapshot_single_mode(root2, confirm=True, same_photo=True) == dourado
    # a fixture prova o que deve: p_half/p_q30/p_exif_edit são "MesmaFoto" e a
    # seleção mantém p.jpg (maior arquivo na mesma resolução? não: p_half é menor,
    # p.jpg tem mais pixels que p_half e mais bytes que p_q30)
    labels = {name: lab for g in dourado["groups"] for name, lab in g}
    assert labels["p_half.jpg"] == "MesmaFoto" and labels["p_q30.jpg"] == "MesmaFoto"
    assert labels["p_exif_burst.jpg"] == "Semelhante"
    assert "p.jpg" not in dourado["selected_same_photo"]
    assert {"p_half.jpg", "p_q30.jpg", "p_exif_edit.jpg"} <= set(dourado["selected_same_photo"])


def test_selftest_mesma_foto(tmp_path):
    from conftest import build_same_photo_fixture
    root = build_same_photo_fixture(str(tmp_path / "sp"))
    com = ic.run_selftest(root, same_photo=True)
    assert "MESMA FOTO: 1 classes, 4 imagens, 0 suspeitas" in com
    sem = ic.run_selftest(root, same_photo=False)
    assert "MESMA FOTO" not in sem
