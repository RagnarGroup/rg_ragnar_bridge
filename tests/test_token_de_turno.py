"""El MCP de tickets usa el token del usuario que escribio en ese turno, no el de la
config, y cada conversacion tiene su propio archivo de MCP."""

import json
import uuid

from ragnar_bridge.agents.claude import escribir_mcp_config, token_de_tickets
from ragnar_bridge.config import Config
from ragnar_bridge.protocolo import validar_run


def _run(**extra) -> dict:
    return {"session_id": str(uuid.uuid4()), "prompt": "hola", **extra}


def _cfg(**extra) -> Config:
    return Config(url="wss://panel.ejemplo.app/api/v1/bridge/ws", token="ragbrg_x", **extra)


def test_run_sin_el_campo_es_un_ragnar_viejo():
    turno = validar_run(_run())
    assert turno.token_de_turno is False
    assert token_de_tickets(_cfg(tickets_token="ragagt_config"), turno) == "ragagt_config"


def test_el_token_de_turno_gana_al_de_la_config():
    turno = validar_run(_run(tickets_token="ragagt_turno"))
    assert token_de_tickets(_cfg(tickets_token="ragagt_config"), turno) == "ragagt_turno"


def test_si_ragnar_no_pudo_emitirlo_el_turno_queda_sin_tickets():
    turno = validar_run(_run(tickets_token=None))
    assert turno.token_de_turno is True
    assert token_de_tickets(_cfg(tickets_token="ragagt_config"), turno) is None


def test_un_token_que_no_es_texto_se_rechaza():
    import pytest

    from ragnar_bridge.protocolo import RunInvalido

    with pytest.raises(RunInvalido):
        validar_run(_run(tickets_token=123))


def test_cada_conversacion_tiene_su_propio_archivo_de_mcp(tmp_path):
    cfg = _cfg()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ruta_a = escribir_mcp_config(cfg, tmp_path, "bridge", "ragagt_de_ana", a)
    ruta_b = escribir_mcp_config(cfg, tmp_path, "bridge", "ragagt_de_beto", b)
    assert ruta_a != ruta_b
    leer = lambda ruta: json.loads(open(ruta, encoding="utf-8").read())["mcpServers"]["tickets"]
    assert leer(ruta_a)["headers"]["Authorization"] == "Bearer ragagt_de_ana"
    assert leer(ruta_b)["headers"]["Authorization"] == "Bearer ragagt_de_beto"
    assert leer(ruta_a)["url"] == "https://panel.ejemplo.app/api/v1/mcp/"


def test_sin_token_no_se_escribe_ninguna_config(tmp_path):
    assert escribir_mcp_config(_cfg(), tmp_path, "bridge", None, str(uuid.uuid4())) is None
    assert list(tmp_path.iterdir()) == []
