"""Un bridge, dos agentes (claude y agy) en el mismo servidor: los detecta, le
cuenta a Ragnar cuales funcionan, corre cada conversacion con el que Ragnar
pida, y se entera solo si cambia el estado (login, instalacion)."""

import asyncio
import json
import sys
import uuid

import pytest

from helpers import FAKE, FAKE_AGY, TOKEN, run
from ragnar_bridge.bridge import ejecutar
from ragnar_bridge.config import Config


def _cfg(tmp_path, ragnar, **kw) -> Config:
    datos = dict(
        url=ragnar.url,
        token=TOKEN,
        claude_cmd=[sys.executable, FAKE],
        agy_cmd=[sys.executable, FAKE_AGY],
        codex_cmd=["/no/existe/codex"],
        agy_dir=str(tmp_path / "agy"),
        config_dir=str(tmp_path / "claude"),
        workdir=str(tmp_path / "work"),
    )
    datos.update(kw)
    return Config(**datos)


@pytest.fixture
async def arrancar(tmp_path, ragnar, monkeypatch):
    (tmp_path / "work").mkdir()
    (tmp_path / "agy").mkdir()
    monkeypatch.setenv("AGY_FAKE_DIR", str(tmp_path / "agy"))
    tareas = []

    async def _arrancar(**kw):
        tarea = asyncio.create_task(ejecutar(_cfg(tmp_path, ragnar, **kw), tmp_path))
        tareas.append(tarea)
        await asyncio.wait_for(ragnar.conectado.wait(), 10)

    yield _arrancar
    for t in tareas:
        t.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)


def _por_nombre(agentes):
    return {a["name"]: a for a in agentes}


async def test_detecta_los_dos_agentes_y_los_anuncia(arrancar, ragnar):
    await arrancar()
    agentes = _por_nombre(ragnar.auth_frame["agents"])
    assert set(agentes) == {"claude", "agy"}
    assert agentes["claude"]["cli_version"] == "fake-claude 1.0" and agentes["claude"]["login"] is True
    assert agentes["agy"]["cli_version"] == "fake-agy 1.0" and agentes["agy"]["login"] is True
    assert ragnar.auth_frame["agent"] == "claude"


async def test_solo_anuncia_los_que_estan_instalados(arrancar, ragnar):
    await arrancar(agy_cmd=["agy-que-no-existe-xyz"])
    assert [a["name"] for a in ragnar.auth_frame["agents"]] == ["claude"]


async def test_cada_conversacion_corre_con_el_agente_que_ragnar_pide(arrancar, ragnar):
    await arrancar()
    con_claude, con_agy = str(uuid.uuid4()), str(uuid.uuid4())

    await ragnar.enviar(run(con_claude, agent="claude"))
    eventos, _ = await ragnar.hasta_done(con_claude)
    assert eventos[0]["type"] == "system"

    await ragnar.enviar(run(con_agy, agent="agy"))
    eventos, _ = await ragnar.hasta_done(con_agy)
    assert [e["type"] for e in eventos][-1] == "result"
    assert any(e["type"] == "stream_event" for e in eventos)


async def test_sin_agente_usa_el_primero_instalado(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    eventos, _ = await ragnar.hasta_done(tid)
    assert eventos[0]["type"] == "system"


async def test_pedir_un_agente_que_no_esta_es_un_error_claro(arrancar, ragnar):
    await arrancar(agy_cmd=["agy-que-no-existe-xyz"])
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, agent="agy"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["type"] == "error"
    assert "agy" in fin["message"] and "claude" in fin["message"]


async def test_un_agente_sin_sesion_se_anuncia_con_login_false(arrancar, ragnar, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_LOGIN", "0")
    await arrancar()
    agentes = _por_nombre(ragnar.auth_frame["agents"])
    assert agentes["agy"]["login"] is False and agentes["claude"]["login"] is True


async def test_el_bridge_avisa_solo_si_cambia_el_estado(arrancar, ragnar, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_LOGIN", "0")
    await arrancar(reprobar_cada=1)
    monkeypatch.setenv("FAKE_AGY_LOGIN", "1")

    while True:
        msg = await asyncio.wait_for(ragnar.recibidos.get(), 15)
        if msg["type"] == "agents":
            break
    assert _por_nombre(msg["agents"])["agy"]["login"] is True


async def test_el_frame_auth_es_json_serializable(arrancar, ragnar):
    await arrancar()
    json.dumps(ragnar.auth_frame)


async def test_probe_de_ragnar_re_sondea_ya_y_responde_con_agents(arrancar, ragnar, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_LOGIN", "0")
    await arrancar(reprobar_cada=3600)
    monkeypatch.setenv("FAKE_AGY_LOGIN", "1")

    await ragnar.enviar({"type": "probe"})
    while True:
        msg = await asyncio.wait_for(ragnar.recibidos.get(), 15)
        if msg["type"] == "agents":
            break
    assert _por_nombre(msg["agents"])["agy"]["login"] is True
