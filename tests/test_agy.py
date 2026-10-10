"""Adaptador de Antigravity (agy): traduccion de eventos, sesiones por
conversacion y flujo de permisos. Corre contra tests/fake_agy.py, que emite los
eventos reales de agy 1.1.27."""

import asyncio
import json
import sys
import uuid

import pytest

from helpers import FAKE_AGY, TOKEN, run
from ragnar_bridge import PROTOCOLO
from ragnar_bridge.bridge import ErrorFatal, ejecutar, probar_conexion
from ragnar_bridge.config import Config, ConfigError, cargar


def _cfg(tmp_path, ragnar, **kw) -> Config:
    return Config(
        url=ragnar.url,
        token=TOKEN,
        agents=["agy"],
        agy_cmd=[sys.executable, FAKE_AGY],
        agy_dir=str(tmp_path / "agy"),
        workdir=str(tmp_path / "work"),
        **kw,
    )


@pytest.fixture
async def arrancar(tmp_path, ragnar, monkeypatch):
    """Fabrica: levanta un bridge agy con la config pedida y lo apaga al final."""
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


def _argvs(tmp_path):
    lineas = (tmp_path / "agy" / "argvs.jsonl").read_text(encoding="utf-8").splitlines()
    argvs = [json.loads(x) for x in lineas]
    # El bridge corre `agy --version` al conectar: no es un turno.
    return [a for a in argvs if a != ["--version"]]


def _prompt(argv) -> str:
    return next(a for a in argv if a.startswith("-p="))[3:]


def _texto(eventos) -> str:
    return "".join(
        e["event"]["delta"]["text"]
        for e in eventos
        if e.get("type") == "stream_event"
    )


async def test_el_bridge_se_anuncia_como_agy(arrancar, ragnar):
    await arrancar()
    assert ragnar.auth_frame["agent"] == "agy"
    assert [a["name"] for a in ragnar.auth_frame["agents"]] == ["agy"]


async def test_traduce_los_eventos_de_agy_al_formato_de_claude(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    eventos, fin = await ragnar.hasta_done(tid)

    assert fin["type"] == "done" and fin["exit_code"] == 0
    assert _texto(eventos) == "Hola mundo\n"
    resultado = eventos[-1]
    assert resultado["type"] == "result" and resultado["is_error"] is False
    # El uso es la SUMA de los pasos de este turno (100/5/2), no el
    # `result.usage` de agy (9999), que es acumulado de toda la conversacion.
    assert resultado["usage"]["input_tokens"] == 100
    assert resultado["usage"]["output_tokens"] == 5
    assert resultado["usage"]["cache_read_input_tokens"] == 2


async def test_primer_turno_abre_conversacion_con_las_instrucciones_de_ragnar(arrancar, ragnar, tmp_path):
    await arrancar(agy_model="gemini-3.7-flash-high")
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="corto", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)

    argv = _argvs(tmp_path)[0]
    assert "--conversation" not in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-high"
    assert argv[argv.index("--print-timeout") + 1] == "30m"
    assert "--dangerously-skip-permissions" not in argv
    # agy no tiene --append-system-prompt: las instrucciones van en el prompt.
    assert _prompt(argv).startswith("PROTOCOLO") and _prompt(argv).endswith("CON CONTEXTO")


async def test_segundo_turno_retoma_la_conversacion_de_agy(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)

    primero, segundo = _argvs(tmp_path)
    assert "--conversation" in segundo
    # El id lo asigno agy en el primer turno; el bridge lo recordo.
    estado = json.loads((tmp_path / "agy-estado.json").read_text())
    assert segundo[segundo.index("--conversation") + 1] == estado["sesiones"][tid]["conversation_id"]
    # Retomando, agy ya recuerda: va solo el mensaje nuevo, sin instrucciones.
    assert _prompt(segundo) == "dos"


async def test_si_la_conversacion_ya_no_existe_se_rehace_con_el_fallback(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    for archivo in (tmp_path / "agy" / "conversations").iterdir():
        archivo.unlink()

    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)
    segundo = _argvs(tmp_path)[1]
    assert "--conversation" not in segundo and _prompt(segundo).endswith("CON CONTEXTO")


async def test_el_prompt_va_pegado_al_flag_p(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    raro = "--help\nsegunda linea"
    await ragnar.enviar(run(tid, prompt=raro, fallback=raro))
    await ragnar.hasta_done(tid)
    # Un solo argumento `-p=<prompt>`: ni un prompt que empieza con "-" ni uno
    # con saltos de linea se confunden con un flag de agy.
    argv = _argvs(tmp_path)[0]
    assert sum(a.startswith("-p=") for a in argv) == 1 and "-p" not in argv
    assert _prompt(argv).endswith(raro)


async def test_herramienta_denegada_pide_permiso_con_el_marcador_de_ragnar(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="hace TOOL"))
    eventos, fin = await ragnar.hasta_done(tid)

    assert fin["exit_code"] == 0
    assert "--dangerously-skip-permissions" not in _argvs(tmp_path)[0]

    tipos = [e["type"] for e in eventos]
    assert "assistant" in tipos and "user" in tipos
    tool_use = next(e for e in eventos if e["type"] == "assistant")["message"]["content"][0]
    assert tool_use["name"] == "run_command" and tool_use["input"] == {"CommandLine": "echo hola-bridge"}
    tool_result = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert tool_result["is_error"] is True and tool_result["tool_use_id"] == tool_use["id"]

    # Ragnar solo toma el marcador si es lo ULTIMO del texto del turno.
    texto = _texto(eventos)
    assert texto.rstrip().endswith("[[/PERMISO]]")
    marcador = json.loads(texto.split("[[PERMISO]]")[1].split("[[/PERMISO]]")[0])
    assert marcador["herramienta"] == "agy:permisos"
    assert "RunCommand" in marcador["motivo"] and "echo hola-bridge" in marcador["motivo"]


async def test_aprobar_relanza_la_conversacion_con_las_herramientas_aprobadas(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="hace TOOL"))
    await ragnar.hasta_done(tid)

    await ragnar.enviar(run(tid, prompt="Se concedio el permiso, hace TOOL", conceder="agy:permisos"))
    eventos, _ = await ragnar.hasta_done(tid)
    segundo = _argvs(tmp_path)[1]
    assert "--dangerously-skip-permissions" in segundo and "--conversation" in segundo
    resultado_tool = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert resultado_tool["is_error"] is False and "hola-bridge" in resultado_tool["content"]
    assert "[[PERMISO]]" not in _texto(eventos)

    # El permiso es de la conversacion: los turnos siguientes siguen aprobados
    # sin volver a preguntar.
    await ragnar.enviar(run(tid, prompt="otra vez TOOL"))
    await ragnar.hasta_done(tid)
    assert "--dangerously-skip-permissions" in _argvs(tmp_path)[2]


async def test_el_permiso_de_una_conversacion_no_pasa_a_otra(arrancar, ragnar, tmp_path):
    await arrancar()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await ragnar.enviar(run(a, prompt="TOOL", conceder="agy:permisos"))
    await ragnar.hasta_done(a)
    await ragnar.enviar(run(b, prompt="TOOL"))
    await ragnar.hasta_done(b)
    assert "--dangerously-skip-permissions" in _argvs(tmp_path)[0]
    assert "--dangerously-skip-permissions" not in _argvs(tmp_path)[1]


async def test_modo_denegar_nunca_concede(arrancar, ragnar, tmp_path):
    await arrancar(agy_permisos="denegar")
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="TOOL", conceder="agy:permisos"))
    eventos, _ = await ragnar.hasta_done(tid)
    assert "--dangerously-skip-permissions" not in _argvs(tmp_path)[0]
    texto = _texto(eventos)
    assert "[[PERMISO]]" not in texto and "agy_permisos='denegar'" in texto


async def test_modo_auto_aprueba_desde_el_primer_turno(arrancar, ragnar, tmp_path):
    await arrancar(agy_permisos="auto")
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="TOOL"))
    await ragnar.hasta_done(tid)
    assert "--dangerously-skip-permissions" in _argvs(tmp_path)[0]


async def test_status_distinto_de_success_es_error(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="STATUS_ERROR"))
    eventos, _ = await ragnar.hasta_done(tid)
    assert eventos[-1]["is_error"] is True and "algo fallo" in eventos[-1]["result"]


async def test_fallo_del_cli_reporta_codigo_y_stderr(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="FAIL"))
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


# ---- config y doctor


def test_config_rechaza_agente_y_permisos_invalidos(tmp_path):
    ruta = tmp_path / "config.json"
    base = {"url": "wss://x/api/v1/bridge/ws", "token": "t"}
    ruta.write_text(json.dumps({**base, "agents": ["gemini"]}))
    with pytest.raises(ConfigError, match="gemini"):
        cargar(ruta)
    ruta.write_text(json.dumps({**base, "agents": []}))
    with pytest.raises(ConfigError, match="vacio"):
        cargar(ruta)
    ruta.write_text(json.dumps({**base, "agents": ["agy"], "agy_permisos": "siempre"}))
    with pytest.raises(ConfigError, match="agy_permisos"):
        cargar(ruta)
    ruta.write_text(json.dumps({**base, "agents": ["agy"], "agy_cmd": "agy"}))
    assert cargar(ruta).agy_cmd == ["agy"]
    # Config de la 0.2 (un solo agente) y sin ninguno (detecta todos).
    ruta.write_text(json.dumps({**base, "agent": "agy"}))
    assert cargar(ruta).agentes_pedidos() == ["agy"]
    ruta.write_text(json.dumps(base))
    assert cargar(ruta).agentes_pedidos() == ["claude", "agy", "codex", "hermes"]


async def test_probar_conexion_devuelve_el_nombre_del_servidor(ragnar, tmp_path):
    cfg = Config(url=ragnar.url, token=TOKEN, agents=["agy"], agy_cmd=[sys.executable, FAKE_AGY])
    assert await probar_conexion(cfg) == "t"
    assert ragnar.auth_frame["protocol"] == PROTOCOLO


async def test_probar_conexion_con_token_invalido_es_error_fatal(ragnar):
    cfg = Config(url=ragnar.url, token="ragbrg_otro", agents=["agy"], agy_cmd=[sys.executable, FAKE_AGY])
    with pytest.raises(ErrorFatal):
        await probar_conexion(cfg)


# ---- cuota (/usage)


def _adaptador(tmp_path, monkeypatch):
    from ragnar_bridge.agents.agy import AgyAdaptador

    (tmp_path / "agy").mkdir(exist_ok=True)
    monkeypatch.setenv("AGY_FAKE_DIR", str(tmp_path / "agy"))
    cfg = Config(url="ws://x", token="t", agents=["agy"], agy_cmd=[sys.executable, FAKE_AGY], agy_dir=str(tmp_path / "agy"))
    return AgyAdaptador(cfg, tmp_path)


def test_cuota_de_agy_se_normaliza_a_bloques(tmp_path, monkeypatch):
    cuota = _adaptador(tmp_path, monkeypatch).cuota("1.2.11")
    assert cuota == {
        "plan": None,
        "bloques": [
            {"grupo": "Gemini Models", "ventana": "semana", "usado": 25, "resetea": "2026-10-04T02:10:32Z"},
            {"grupo": "Gemini Models", "ventana": "5h", "usado": 0, "resetea": "2026-09-27T09:31:35Z"},
            {"grupo": "Claude and GPT models", "ventana": "5h", "usado": 50, "resetea": "2026-09-27T09:31:35Z"},
        ],
    }


def test_un_agy_anterior_a_1_1_11_ni_se_consulta(tmp_path, monkeypatch):
    # Ahi `-p "/usage"` iria al modelo como un prompt (y gastaria cuota).
    adaptador = _adaptador(tmp_path, monkeypatch)
    assert adaptador.cuota("1.1.10") is None and adaptador.cuota("desconocida") is None
    assert not (tmp_path / "agy" / "usage-llamado").exists()


async def test_la_cuota_llega_en_un_frame_agents_despues_del_auth(arrancar, ragnar, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_VERSION", "fake-agy 1.2.11")
    await arrancar()
    # El auth no la lleva (no demora la conexion)...
    assert "quota" not in ragnar.auth_frame["agents"][0]
    # ...llega enseguida en un frame `agents`.
    while True:
        msg = await asyncio.wait_for(ragnar.recibidos.get(), 15)
        if msg["type"] == "agents":
            break
    agy = msg["agents"][0]
    assert agy["name"] == "agy" and len(agy["quota"]["bloques"]) == 3
