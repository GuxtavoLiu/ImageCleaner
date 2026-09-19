"""
Executor paralelo "com tique" (_run_parallel_ticking): a thread principal tem
de continuar viva (tick) enquanto um worker está preso numa tarefa longa, e o
cancelamento tem de sair por ali.
"""
import threading
import time

import pytest

import main as ic


def test_resultados_chegam_todos_na_thread_principal():
    main_thread = threading.current_thread()
    seen = []

    def on_result(value):
        assert threading.current_thread() is main_thread
        seen.append(value)

    cancelled = ic._run_parallel_ticking(list(range(50)), lambda x: x * 2, on_result, 4)
    assert cancelled is False
    assert sorted(seen) == [x * 2 for x in range(50)]


def test_lista_vazia_nao_faz_nada():
    assert ic._run_parallel_ticking([], lambda x: x, lambda r: None, 2, tick=lambda: None) is False


def test_tick_roda_enquanto_o_worker_esta_preso():
    release = threading.Event()
    ticks = []

    def worker(_):
        release.wait(5)
        return "ok"

    def tick():
        ticks.append(time.time())
        if len(ticks) >= 5:
            release.set()

    out = []
    ic._run_parallel_ticking([1], worker, out.append, 1, tick=tick, interval=0.02)
    assert out == ["ok"]
    assert len(ticks) >= 5          # acordou várias vezes sem nenhuma conclusão


def test_cancelar_pelo_tick_para_logo_e_solta_os_workers():
    event = threading.Event()
    started = []

    def worker(item):
        started.append(item)
        t0 = time.time()
        while not event.is_set() and time.time() - t0 < 5:
            time.sleep(0.005)
        return item

    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        if calls["n"] == 3:
            event.set()

    results = []
    t0 = time.time()
    cancelled = ic._run_parallel_ticking(list(range(100)), worker, results.append, 2,
                                         tick=tick, cancel_event=event, interval=0.02)
    assert cancelled is True
    assert time.time() - t0 < 2       # não esperou os 5 s de cada worker
    assert len(started) <= 4          # no máximo workers * 2 em voo; o resto nem começou


def test_excecao_do_on_result_propaga_e_liga_o_cancelamento():
    event = threading.Event()

    def on_result(_):
        raise RuntimeError("falha na thread principal")

    with pytest.raises(RuntimeError):
        ic._run_parallel_ticking([1, 2, 3], lambda x: x, on_result, 2, cancel_event=event)
    assert event.is_set()
