"""Adaptador de Cursor: traduccion de stream-json, sesion y login, contra tests/fake_cursor.py."""

import asyncio
import json
import os
import sys
import uuid

import pytest

from helpers import FAKE_CURSOR, TOKEN, run
from ragnar_bridge import PROTOCOLO
from ragnar_bridge.agents.cursor import CursorAdaptador, sesion_desde_status
from ragnar_bridge.bridge import ejecutar, probar_conexion
from ragnar_bridge.config import Config


def _cfg(tmp_path, ragnar, **kw) -> Config:
    return Config(
        url=ragnar.url + "/api/v1/bridge/ws",
        token=TOKEN,
        agents=["cursor"],
        cursor_cmd=[sys.executable, FAKE_CURSOR],
        workdir=str(tmp_path / "work"),
        **kw,
    )


@pytest.fixture
async def arrancar(tmp_path, ragnar, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("FAKE_CURSOR_ARGV_LOG", str(tmp_path / "argvs.jsonl"))
    tareas = []

    async def _arrancar(**kw):
        tarea = asyncio.create_task(ejecutar(_cfg(tmp_path, ragnar, **kw), tmp_path))
        tareas.append(tarea)
        await asyncio.wait_for(ragnar.conectado.wait(), 10)

    yield _arrancar
    for tarea in tareas:
        tarea.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)


def _invocaciones(tmp_path):
    return [json.loads(linea) for linea in (tmp_path / "argvs.jsonl").read_text(encoding="utf-8").splitlines()]


def _texto(eventos) -> str:
    return "".join(e["event"]["delta"]["text"] for e in eventos if e.get("type") == "stream_event")


def test_status_json_y_texto():
    assert sesion_desde_status('{"loggedIn": true}', "", 0) is True
    assert sesion_desde_status('{"loggedIn": false}', "", 1) is False
    assert sesion_desde_status("Not logged in\n", "", 1) is False
    assert sesion_desde_status("Logged in as ada@ragnar.dev\n", "", 0) is True
    assert sesion_desde_status("", "boom", 2) is False
    assert sesion_desde_status("", "", 0) is None


def test_el_login_imprime_el_link_sin_abrir_el_navegador(tmp_path):
    cfg = Config(url="ws://x", token="t", agents=["cursor"], cursor_cmd=[sys.executable, FAKE_CURSOR])
    receta = CursorAdaptador(cfg, tmp_path).receta_login()
    assert receta.argv[-1] == "login"
    assert receta.env["NO_OPEN_BROWSER"] == "1"
    assert receta.pide_codigo is False
    assert receta.patron_url.fullmatch("https://cursor.com/loginDeepControl?challenge=abc&uuid=def&mode=login")


async def test_el_bridge_se_anuncia_como_cursor(arrancar, ragnar):
    await arrancar()
    assert ragnar.auth_frame["agent"] == "cursor"
    agentes = ragnar.auth_frame["agents"]
    assert [a["name"] for a in agentes] == ["cursor"]
    assert agentes[0]["cli_version"] == "2026.10.22-27dcf5f"
    assert agentes[0]["login"] is True
    assert agentes[0]["conectable"] is (os.name == "posix")


async def test_traduce_deltas_y_omite_el_cierre_duplicado(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    eventos, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert _texto(eventos) == "Hola mundo"
    assert eventos[-1]["type"] == "result" and eventos[-1]["is_error"] is False
    assert "result" not in eventos[-1]


async def test_herramienta_se_traduce_a_tool_use_y_tool_result(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="hace TOOL"))
    eventos, _ = await ragnar.hasta_done(tid)
    uso = next(e for e in eventos if e["type"] == "assistant")["message"]["content"][0]
    resultado = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert uso == {"type": "tool_use", "id": "call-1", "name": "Read", "input": {"path": "README.md"}}
    assert resultado["tool_use_id"] == "call-1"
    assert resultado["content"] == "hola archivo" and resultado["is_error"] is False
    assert _texto(eventos) == "Leo el archivo. "


async def test_primer_turno_lleva_las_instrucciones_y_el_segundo_reanuda(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno", fallback="--help"))
    await ragnar.hasta_done(tid)
    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)

    primero, segundo = [i["argv"] for i in _invocaciones(tmp_path)]
    estado = json.loads((tmp_path / "cursor-estado.json").read_text(encoding="utf-8"))
    sesion = estado["sesiones"][tid]["cursor_session_id"]
    assert primero[primero.index("--") + 1] == "PROTOCOLO\n\n---\n\n--help"
    assert "--resume" not in primero
    assert "--force" in primero and "disabled" in primero
    assert segundo[segundo.index("--resume") + 1] == sesion
    assert segundo[segundo.index("--") + 1] == "dos"


async def test_resultado_con_error_se_reporta(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="FAILED"))
    eventos, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 0
    assert eventos[-1]["is_error"] is True
    assert eventos[-1]["result"] == "Cursor se quedo sin cuota."


async def test_caida_del_cli_reporta_codigo_y_stderr(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="CRASH"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 3 and "boom" in fin["stderr"]


async def test_conceder_deja_un_warning(arrancar, ragnar, caplog):
    await arrancar()
    tid = str(uuid.uuid4())
    with caplog.at_level("WARNING", logger="ragnar_bridge"):
        await ragnar.enviar(run(tid, prompt="hola", conceder="Bash(docker ps:*)"))
        _, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "done"
    assert any("Cursor" in r.getMessage() and "docker ps" in r.getMessage() for r in caplog.records)


async def test_probar_conexion_anuncia_cursor(ragnar):
    cfg = Config(url=ragnar.url, token=TOKEN, agents=["cursor"], cursor_cmd=[sys.executable, FAKE_CURSOR])
    assert await probar_conexion(cfg) == "t"
    assert ragnar.auth_frame["protocol"] == PROTOCOLO
    assert ragnar.auth_frame["agents"][0]["name"] == "cursor"
