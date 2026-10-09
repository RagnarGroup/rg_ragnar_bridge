"""Robustez del ciclo de vida de un turno y de la conexion: errores que no
pueden matar la tarea en silencio, tope de tiempo, procesos huerfanos y
reconexion (RAG-201, RAG-202, RAG-208)."""

import asyncio
import json
import sys
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
