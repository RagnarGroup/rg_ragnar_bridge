"""Robustez del ciclo de vida de un turno y de la conexion: errores que no
pueden matar la tarea en silencio, tope de tiempo, procesos huerfanos y
reconexion (RAG-201, RAG-202, RAG-208)."""

import asyncio
import json
import os
import signal
import sys
import time
import uuid

import pytest

from helpers import FAKE, FAKE_HERMES, TOKEN, run
from ragnar_bridge.agents.claude import ClaudeAdaptador
from ragnar_bridge.bridge import ejecutar
from ragnar_bridge.config import Config, ConfigError, cargar


def _cfg(tmp_path, ragnar, **kw) -> Config:
    base = dict(
        url=ragnar.url,
        token=TOKEN,
        agents=["claude"],
        claude_cmd=[sys.executable, FAKE],
        workdir=str(tmp_path / "work"),
        config_dir=str(tmp_path / "claude"),
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture
async def arrancar(tmp_path, ragnar):
    (tmp_path / "work").mkdir()
    tareas = []

    async def _arrancar(**kw):
        tarea = asyncio.create_task(ejecutar(_cfg(tmp_path, ragnar, **kw), tmp_path))
        tareas.append(tarea)
        await asyncio.wait_for(ragnar.conectado.wait(), 10)

    yield _arrancar
    for t in tareas:
        t.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)


# ---- RAG-201: errores y tope de tiempo


async def test_una_excepcion_al_preparar_manda_error_sin_el_texto(arrancar, ragnar, monkeypatch):
    """Un TypeError (no OSError) en `preparar` mataba la task sin `error` ni
    `done`: la tarea quedaba "ejecutando" para siempre en Ragnar."""

    def roto(self, turno, username):
        raise TypeError("ragagt_secreto no deberia salir")

    monkeypatch.setattr(ClaudeAdaptador, "preparar", roto)
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "error" and "TypeError" in fin["message"]
    assert "ragagt_secreto" not in fin["message"]


async def test_estado_de_hermes_corrupto_no_cuelga_el_turno(tmp_path, ragnar, monkeypatch):
    """El caso del ticket: `hermes_session_id` que no es string en el JSON de
    estado rompe el spawn con TypeError."""
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("FAKE_HERMES_ARGV_LOG", str(tmp_path / "argvs.jsonl"))
    tid = str(uuid.uuid4())
    (tmp_path / "hermes-estado.json").write_text(json.dumps({"sesiones": {tid: {"hermes_session_id": 123}}}))
    cfg = Config(
        url=ragnar.url,
        token=TOKEN,
        agents=["hermes"],
        hermes_cmd=[sys.executable, FAKE_HERMES],
        workdir=str(tmp_path / "work"),
    )
    tarea = asyncio.create_task(ejecutar(cfg, tmp_path))
    try:
        await asyncio.wait_for(ragnar.conectado.wait(), 10)
        await ragnar.enviar({**run(tid), "agent": "hermes"})
        _, fin = await ragnar.hasta_done(tid)
        assert fin["type"] == "error" and "TypeError" in fin["message"]
    finally:
        tarea.cancel()
        await asyncio.gather(tarea, return_exceptions=True)


async def test_tope_por_turno_corta_el_cli_y_avisa(arrancar, ragnar):
    await arrancar(timeout_turno=1.5)
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="SLEEP"))
    _, fin = await ragnar.hasta_done(tid, timeout=20)
    assert fin["type"] == "error" and "timeout_turno" in fin["message"]

    # El lugar del turno se libera: la conversacion acepta otro.
    await ragnar.enviar(run(tid, prompt="hola"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "done" and fin["exit_code"] == 0


def test_config_valida_timeout_turno(tmp_path):
    ruta = tmp_path / "config.json"
    base = {"url": "wss://x/api/v1/bridge/ws", "token": "t"}
    ruta.write_text(json.dumps(base))
    assert cargar(ruta).timeout_turno == 7200
    for malo in (0, -1, "2h", True):
        ruta.write_text(json.dumps({**base, "timeout_turno": malo}))
        with pytest.raises(ConfigError, match="timeout_turno"):
            cargar(ruta)


# ---- RAG-202: procesos huerfanos y cancelaciones

posix = pytest.mark.skipif(os.name != "posix", reason="grupos de procesos: POSIX-only")


async def _murio(pid: int, espera: float = 8.0) -> bool:
    """¿Termino `pid`? Un zombie (ya muerto, esperando que init lo recoja)
    cuenta como muerto."""
    limite = time.monotonic() + espera
    while time.monotonic() < limite:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        await asyncio.sleep(0.1)
    return False


async def _pid_de(ruta, espera: float = 10.0) -> int:
    limite = time.monotonic() + espera
    while time.monotonic() < limite:
        if ruta.exists() and ruta.read_text().strip():
            return int(ruta.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError("el CLI de prueba no dejo su hijo")


@pytest.fixture
def pid_file(tmp_path, monkeypatch):
    ruta = tmp_path / "hijo.pid"
    monkeypatch.setenv("FAKE_CLAUDE_PID_FILE", str(ruta))
    yield ruta
    # En Windows (sin grupos) el hijo puede sobrevivir: que no ensucie la maquina.
    if ruta.exists() and os.name != "posix":
        try:
            os.kill(int(ruta.read_text().strip()), signal.SIGTERM)
        except OSError:
            pass


@posix
async def test_turno_normal_no_deja_procesos_huerfanos(arrancar, ragnar, pid_file):
    """Un turno que termina BIEN tambien limpia su grupo: antes sobrevivian los
    nietos (servidores MCP, shells en background)."""
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="HIJO"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert await _murio(await _pid_de(pid_file))


async def test_un_nieto_con_stdout_abierto_no_cuelga_el_turno(arrancar, ragnar, pid_file, monkeypatch):
    import ragnar_bridge.bridge as modulo

    monkeypatch.setattr(modulo, "ESPERA_DRENAJE", 1.0)
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="HIJO_STDOUT"))
    eventos, fin = await ragnar.hasta_done(tid, timeout=15)
    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert [e["type"] for e in eventos] == ["system", "result"]
    if os.name == "posix":
        assert await _murio(await _pid_de(pid_file))


@posix
async def test_un_nieto_que_ignora_sigterm_muere_con_sigkill(arrancar, ragnar, pid_file, monkeypatch):
    import ragnar_bridge.bridge as modulo

    monkeypatch.setattr(modulo, "ESPERA_DRENAJE", 1.0)
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="HIJO_TERCO"))
    _, fin = await ragnar.hasta_done(tid, timeout=20)
    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert await _murio(await _pid_de(pid_file))


@posix
async def test_cancel_mata_al_grupo_del_turno(arrancar, ragnar, pid_file):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="HIJO SLEEP"))
    pid = await _pid_de(pid_file)
    await ragnar.enviar({"type": "cancel", "task_id": tid})
    _, fin = await ragnar.hasta_done(tid, timeout=15)
    assert fin["type"] == "done" and fin["exit_code"] != 0
    assert await _murio(pid)


@posix
async def test_matar_proceso_no_sale_temprano_si_el_lider_ya_murio(tmp_path, pid_file):
    """El lider ya termino pero su nieto sigue en el grupo: un `cancel` tiene
    que limpiarlo igual (antes salia en cuanto veia el returncode)."""
    from ragnar_bridge.bridge import Bridge

    codigo = (
        "import os, subprocess, sys;"
        "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        "open(os.environ['FAKE_CLAUDE_PID_FILE'], 'w').write(str(h.pid))"
    )
    proceso = await asyncio.create_subprocess_exec(sys.executable, "-c", codigo, start_new_session=True)
    assert await proceso.wait() == 0
    pid = await _pid_de(pid_file)
    assert not await _murio(pid, espera=0.3)

    bridge = Bridge(Config(url="ws://x", token="t", agents=["claude"]), tmp_path)
    await bridge._matar_proceso(proceso)
    assert await _murio(pid)


async def test_cancel_que_llega_antes_de_que_arranque_el_cli_se_aplica(arrancar, ragnar, monkeypatch):
    """El `cancel` llega mientras el CLI todavia no esta registrado: antes se
    ignoraba y el turno corria entero."""
    import ragnar_bridge.bridge as modulo

    original = modulo.asyncio.create_subprocess_exec

    async def lento(*args, **kwargs):
        await asyncio.sleep(1.0)
        return await original(*args, **kwargs)

    await arrancar()
    monkeypatch.setattr(modulo.asyncio, "create_subprocess_exec", lento)
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="SLEEP"))
    await asyncio.sleep(0.2)
    await ragnar.enviar({"type": "cancel", "task_id": tid})
    # SLEEP duerme 60 s: si el cancel se perdiera, esto no llega a tiempo.
    _, fin = await ragnar.hasta_done(tid, timeout=20)
    assert fin["type"] == "done" and fin["exit_code"] != 0


@posix
async def test_sigterm_al_bridge_corta_los_turnos_y_sus_grupos(tmp_path, ragnar, pid_file):
    (tmp_path / "work").mkdir()
    tarea = asyncio.create_task(ejecutar(_cfg(tmp_path, ragnar), tmp_path, senales=True))
    await asyncio.wait_for(ragnar.conectado.wait(), 10)
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="HIJO SLEEP"))
    pid = await _pid_de(pid_file)

    os.kill(os.getpid(), signal.SIGTERM)
    resultado = await asyncio.wait_for(asyncio.gather(tarea, return_exceptions=True), 20)
    assert isinstance(resultado[0], asyncio.CancelledError)
    assert await _murio(pid)
