"""
Testes das funções puras de seleção automática (plan_identical_selection e
plan_similar_selection):
  1. equivalência com a lógica original (oráculo) quando não há referência;
  2. regras novas do modo de comparação (referência nunca é selecionada).
"""
import itertools

import main as ic
from conftest import oracle_identical_selection, oracle_similar_selection

INF = float("inf")


def _img(md5, mtime, ref=False):
    return {'md5': md5, 'mtime': mtime, 'is_reference': ref}


def _md5_count(images):
    counts = {}
    for im in images:
        counts[im['md5']] = counts.get(im['md5'], 0) + 1
    return counts


# --------------------------------------------------------------------------
# 1) Equivalência com o comportamento antigo (modo de uma pasta)
# --------------------------------------------------------------------------

def _casos_sem_referencia():
    """Tabela ampla de grupos sintéticos: empates, inf, subgrupos misturados."""
    md5s = ["m1", "m1", "m2", "m3", "m1", "m2"]
    mtimes_opts = [[1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1], [1, 1, 1, 1, 1, 1],
                   [INF, 1, 2, INF, 3, 4], [2, 2, 1, 1, 3, 3], [5, INF, INF, 1, 1, 2]]
    for mtimes in mtimes_opts:
        yield [_img(m, t) for m, t in zip(md5s, mtimes)]
    # Grupos pequenos: todas as combinações de 2 e 3 md5s entre {a, b}
    for n in (2, 3):
        for combo in itertools.product(["a", "b"], repeat=n):
            for mtimes in itertools.permutations(range(n)):
                yield [_img(m, t) for m, t in zip(combo, mtimes)]


def test_identical_equivalente_ao_oraculo_sem_referencia():
    for images in _casos_sem_referencia():
        esperado = sorted(oracle_identical_selection(images))
        obtido = sorted(ic.plan_identical_selection(images))
        assert obtido == esperado, (images, esperado, obtido)


def test_similar_equivalente_ao_oraculo_sem_referencia():
    for images in _casos_sem_referencia():
        counts = _md5_count(images)
        esperado = sorted(oracle_similar_selection(images, counts))
        obtido = sorted(ic.plan_similar_selection(images, counts))
        assert obtido == esperado, (images, esperado, obtido)


def test_identical_empate_de_mtime_mantem_a_primeira():
    images = [_img("m", 10), _img("m", 10), _img("m", 10)]
    assert ic.plan_identical_selection(images) == [1, 2]


def test_identical_mtime_inf_nunca_e_a_preservada():
    images = [_img("m", INF), _img("m", 5)]
    assert ic.plan_identical_selection(images) == [0]


def test_similar_com_uma_unica_candidata_nao_seleciona():
    images = [_img("a", 1), _img("a", 2), _img("b", 0)]
    assert ic.plan_similar_selection(images, _md5_count(images)) == []


# --------------------------------------------------------------------------
# 2) Regras do modo de comparação
# --------------------------------------------------------------------------

def test_identical_com_copia_na_referencia_seleciona_todas_do_alvo():
    # referência é a mais NOVA de propósito: mesmo assim é a preservada
    images = [_img("m", 100), _img("m", 200), _img("m", 999, ref=True)]
    assert ic.plan_identical_selection(images) == [0, 1]


def test_identical_referencia_nunca_no_retorno():
    images = [_img("m", 1, ref=True), _img("m", 2, ref=True), _img("m", 3)]
    assert ic.plan_identical_selection(images) == [2]
    images = [_img("m", 1, ref=True), _img("m", 2, ref=True)]
    assert ic.plan_identical_selection(images) == []


def test_identical_subgrupo_sem_referencia_mantem_regra_antiga():
    # grupo misto: subgrupo m1 tem referência, subgrupo m2 é só do alvo
    images = [_img("m1", 50), _img("m1", 10, ref=True),
              _img("m2", 300), _img("m2", 200), _img("m2", 250)]
    assert sorted(ic.plan_identical_selection(images)) == [0, 2, 4]


def test_similar_com_referencia_no_grupo_seleciona_todas_as_candidatas():
    # uma única semelhante do alvo: sem referência não seria selecionada,
    # com referência no grupo é
    images = [_img("ref", 1, ref=True), _img("x", 5)]
    counts = _md5_count(images)
    assert ic.plan_similar_selection(images, counts) == [1]
    images = [_img("ref", 1, ref=True), _img("x", 5), _img("y", 2)]
    counts = _md5_count(images)
    assert sorted(ic.plan_similar_selection(images, counts)) == [1, 2]


def test_similar_referencia_nunca_e_candidata():
    images = [_img("r1", 1, ref=True), _img("r2", 2, ref=True)]
    assert ic.plan_similar_selection(images, _md5_count(images)) == []


def test_similar_grupo_so_alvo_mantem_regra_antiga():
    images = [_img("x", 5), _img("y", 2), _img("z", 9)]
    counts = _md5_count(images)
    assert sorted(ic.plan_similar_selection(images, counts)) == [0, 2]


def test_similar_identica_do_alvo_nao_e_candidata_mesmo_com_referencia():
    # dup_a (alvo) é idêntica a acervo_a (ref): fica para a seleção de idênticas
    images = [_img("A", 1, ref=True), _img("A", 2), _img("B", 3)]
    counts = _md5_count(images)
    assert ic.plan_similar_selection(images, counts) == [2]
    assert ic.plan_identical_selection(images) == [1]


# --------------------------------------------------------------------------
# 3) Regra de qualidade (SIMILAR_KEEP_PRIORITY): resolução > arquivo > data
# --------------------------------------------------------------------------

def _q(md5, mtime, pixels=None, size=None, ref=False):
    return {'md5': md5, 'mtime': mtime, 'pixels': pixels, 'size': size, 'is_reference': ref}


def test_qualidade_resolucao_decide():
    imgs = [_q("a", 1, pixels=100, size=999), _q("b", 9, pixels=400, size=10)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [0]     # mantém b (maior resolução)


def test_qualidade_empate_resolucao_tamanho_decide():
    imgs = [_q("a", 1, pixels=100, size=2_800_000), _q("b", 1, pixels=100, size=3_100_000)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [0]     # grupo 303: mantém o maior arquivo


def test_qualidade_empate_total_data_decide():
    imgs = [_q("a", 5, pixels=100, size=10), _q("b", 2, pixels=100, size=10), _q("c", 7, pixels=100, size=10)]
    assert sorted(ic.plan_similar_selection(imgs, _md5_count(imgs))) == [0, 2]   # mantém b (mais antiga)


def test_qualidade_empate_absoluto_mantem_a_primeira():
    imgs = [_q("a", 1, pixels=100, size=10), _q("b", 1, pixels=100, size=10)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [1]


def test_qualidade_resolucao_desconhecida_perde():
    imgs = [_q("a", 1, pixels=None, size=999), _q("b", 9, pixels=50, size=1)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [0]
    # todas desconhecidas: cai no tamanho
    imgs = [_q("a", 1, pixels=None, size=5), _q("b", 9, pixels=None, size=7)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [0]
    # sem métricas nenhuma (dicts antigos): cai na data, como antes
    imgs = [_img("a", 5), _img("b", 2)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == [0]


def test_qualidade_referencia_no_grupo_seleciona_todas_as_candidatas():
    imgs = [_q("r", 1, pixels=10, size=1, ref=True), _q("a", 1, pixels=900, size=900), _q("b", 1, pixels=800, size=800)]
    assert sorted(ic.plan_similar_selection(imgs, _md5_count(imgs))) == [1, 2]


def test_qualidade_identicas_nao_entram():
    imgs = [_q("m", 1, pixels=100, size=10), _q("m", 2, pixels=100, size=10), _q("x", 3, pixels=900, size=900)]
    assert ic.plan_similar_selection(imgs, _md5_count(imgs)) == []        # só 1 candidata
    assert ic.plan_identical_selection(imgs) == [1]                        # idênticas: mais antiga fica


def test_prioridade_mtime_reproduz_regra_antiga():
    for images in _casos_sem_referencia():
        for im in images:
            im['pixels'] = 100; im['size'] = 10       # métricas iguais: não interferem
        counts = _md5_count(images)
        esperado = sorted(oracle_similar_selection(images, counts))
        assert sorted(ic.plan_similar_selection(images, counts, priority=("mtime",))) == esperado
        assert sorted(ic.plan_similar_selection(images, counts)) == esperado
