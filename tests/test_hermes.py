"""Adaptador de Hermes Agent: traduccion de eventos, sesiones por conversacion
y comando. Corre contra tests/fake_hermes.py, que emite el JSONL real de
`hermes chat --format stream-json` (hermes-agent 0.21.5, capturado en vivo en
servidor-personal, RAG-187)."""

import asyncio
import json
import sys
import uuid

import pytest

from helpers import FAKE_HERMES, TOKEN, run
from ragnar_bridge import PROTOCOLO
from ragnar_bridge.bridge import ejecutar, probar_conexion
from ragnar_bridge.config import Config


def _cfg(tmp_path, ragnar, **kw) -> Config:
    return Config(
        url=ragnar.url + "/api/v1/bridge/ws",
        token=TOKEN,
        agents=["hermes"],
        hermes_cmd=[sys.executable, FAKE_HERMES],
        workdir=str(tmp_path / "work"),
        **kw,
    )


@pytest.fixture
async def arrancar(tmp_path, ragnar, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("FAKE_HERMES_ARGV_LOG", str(tmp_path / "argvs.jsonl"))
    tareas = []

    async def _arrancar(**kw):
        tarea = asyncio.create_task(ejecutar(_cfg(tmp_path, ragnar, **kw), tmp_path))
        tareas.append(tarea)
        await asyncio.wait_for(ragnar.conectado.wait(), 10)

    yield _arrancar
    for t in tareas:
        t.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)


def _invocaciones(tmp_path):
    lineas = (tmp_path / "argvs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lineas]


def _texto(eventos) -> str:
    return "".join(e["event"]["delta"]["text"] for e in eventos if e.get("type") == "stream_event")


async def test_el_bridge_se_anuncia_como_hermes(arrancar, ragnar):
    await arrancar()
    assert ragnar.auth_frame["agent"] == "hermes"
    agentes = ragnar.auth_frame["agents"]
    assert [a["name"] for a in agentes] == ["hermes"]
    # Hermes no tiene un comando de solo-estado equivalente a `codex login
    # status`/`agy models` (RAG-187): "sesion sin comprobar" es honesto,
    # nunca bloquea que se intente un turno.
    assert agentes[0]["cli_version"].startswith("Hermes Agent") and agentes[0]["login"] is None


async def test_traduce_texto_y_uso_al_formato_de_claude(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    eventos, fin = await ragnar.hasta_done(tid)

    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert _texto(eventos) == "Hola mundo"
    resultado = eventos[-1]
    assert resultado["type"] == "result" and resultado["is_error"] is False
    # cache_read se resta de input, igual que con Codex: Hermes lo incluye adentro.
    assert resultado["usage"] == {
        "input_tokens": 4556,
        "output_tokens": 136,
        "cache_read_input_tokens": 16228,
        "cache_creation_input_tokens": 0,
    }


async def test_herramienta_se_traduce_a_tool_use_y_tool_result(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="hace TOOL"))
    eventos, _ = await ragnar.hasta_done(tid)

    uso = next(e for e in eventos if e["type"] == "assistant")["message"]["content"][0]
    resultado = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert uso["name"] == "terminal" and uso["input"] == {"command": "echo $((40 + 2))"}
    assert resultado["tool_use_id"] == uso["id"]
    assert resultado["content"] == "42" and resultado["is_error"] is False
    assert _texto(eventos) == "\n\n42"


async def test_primer_turno_lleva_las_instrucciones(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    raro = "--help\nsegunda linea"
    await ragnar.enviar(run(tid, prompt="corto", fallback=raro))
    await ragnar.hasta_done(tid)

    argv = _invocaciones(tmp_path)[0]["argv"]
    assert argv[0] == "chat" and "--resume" not in argv
    query = next(a for a in argv if a.startswith("--query="))[len("--query=") :]
    assert query.startswith("PROTOCOLO") and query.endswith(raro)


async def test_segundo_turno_retoma_la_sesion(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)

    primero, segundo = [i["argv"] for i in _invocaciones(tmp_path)]
    estado = json.loads((tmp_path / "hermes-estado.json").read_text())
    sesion = estado["sesiones"][tid]["hermes_session_id"]
    assert "--resume" not in primero
    assert "--resume" in segundo and segundo[segundo.index("--resume") + 1] == sesion
    # Retomando, va solo el mensaje nuevo, sin las instrucciones de Ragnar.
    query = next(a for a in segundo if a.startswith("--query="))[len("--query=") :]
    assert query == "dos"


async def test_resultado_con_error_se_reporta(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="FAILED"))
    eventos, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 0  # el proceso de hermes sale limpio...
    assert eventos[-1]["type"] == "result" and eventos[-1]["is_error"] is True  # ...pero el turno fallo
    assert eventos[-1]["result"] == "Hermes se quedo sin cuota."


async def test_caida_del_cli_reporta_codigo_y_stderr(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="CRASH"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 3 and "boom" in fin["stderr"]


async def test_cancel_corta_el_proceso(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="SLEEP"))
    await asyncio.sleep(1)
    await ragnar.enviar({"type": "cancel", "task_id": tid})
    _, fin = await ragnar.hasta_done(tid, timeout=15)
    assert fin["type"] == "done" and fin["exit_code"] != 0


async def test_probar_conexion_anuncia_hermes(ragnar):
    cfg = Config(url=ragnar.url, token=TOKEN, agents=["hermes"], hermes_cmd=[sys.executable, FAKE_HERMES])
    assert await probar_conexion(cfg) == "t"
    assert ragnar.auth_frame["protocol"] == PROTOCOLO and ragnar.auth_frame["agents"][0]["name"] == "hermes"


def test_cuota_de_hermes_es_none(tmp_path):
    """Hermes no tiene hoy un comando de solo lectura para leer limites de
    cuota (a diferencia de codex app-server/agy -p=/usage)."""
    from ragnar_bridge.agents.hermes import HermesAdaptador

    cfg = Config(url="ws://x", token="t", agents=["hermes"], hermes_cmd=[sys.executable, FAKE_HERMES])
    assert HermesAdaptador(cfg, tmp_path).cuota("x") is None
