"""Adaptador de Codex CLI: traduccion de eventos, sesiones por hilo, comando
seguro y config aislada. Corre contra tests/fake_codex.py, que emite el JSONL de
`codex exec --json` (codex-cli 0.157.0)."""

import asyncio
import json
import os
import sys
import time
import uuid

import pytest

from helpers import FAKE_CODEX, TOKEN, run
from ragnar_bridge import PROTOCOLO
from ragnar_bridge.bridge import ejecutar, probar_conexion
from ragnar_bridge.config import Config, ConfigError, cargar


def _cfg(tmp_path, ragnar, **kw) -> Config:
    return Config(
        # Con la ruta real del bridge, para que se pueda derivar la URL del MCP de tickets.
        url=ragnar.url + "/api/v1/bridge/ws",
        token=TOKEN,
        agents=["codex"],
        codex_cmd=[sys.executable, FAKE_CODEX],
        codex_home=str(tmp_path / "codex"),
        workdir=str(tmp_path / "work"),
        **kw,
    )


@pytest.fixture
async def arrancar(tmp_path, ragnar):
    (tmp_path / "work").mkdir()
    (tmp_path / "codex").mkdir()
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
    lineas = (tmp_path / "codex" / "argvs.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lineas]


def _overrides(argv) -> dict:
    """Los pares `-c clave=valor` del comando, con el valor ya decodificado."""
    pares = {}
    for i, a in enumerate(argv):
        if a == "-c":
            clave, valor = argv[i + 1].split("=", 1)
            pares[clave] = json.loads(valor)
    return pares


def _prompt(argv) -> str:
    return argv[-1]


def _texto(eventos) -> str:
    return "".join(e["event"]["delta"]["text"] for e in eventos if e.get("type") == "stream_event")


async def test_el_bridge_se_anuncia_como_codex(arrancar, ragnar):
    await arrancar()
    assert ragnar.auth_frame["agent"] == "codex"
    agentes = ragnar.auth_frame["agents"]
    assert [a["name"] for a in agentes] == ["codex"]
    assert agentes[0]["cli_version"] == "codex-cli 0.157.0" and agentes[0]["login"] is True


async def test_sin_sesion_se_anuncia_login_false(arrancar, ragnar, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LOGIN", "0")
    await arrancar()
    assert ragnar.auth_frame["agents"][0]["login"] is False


async def test_traduce_texto_y_uso_al_formato_de_claude(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    eventos, fin = await ragnar.hasta_done(tid)

    assert fin["type"] == "done" and fin["exit_code"] == 0
    # Cada mensaje entero de Codex se separa del anterior; el reasoning no sale.
    assert _texto(eventos) == "Hola\n\nmundo"
    resultado = eventos[-1]
    assert resultado["type"] == "result" and resultado["is_error"] is False
    # input_tokens de Codex INCLUYE lo cacheado; Ragnar suma cada campo aparte.
    assert resultado["usage"] == {
        "input_tokens": 600,
        "output_tokens": 20,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 0,
    }


async def test_comandos_se_traducen_a_tool_use_y_tool_result(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="hace TOOL"))
    eventos, _ = await ragnar.hasta_done(tid)

    usos = [e["message"]["content"][0] for e in eventos if e["type"] == "assistant"]
    resultados = [e["message"]["content"][0] for e in eventos if e["type"] == "user"]
    # item.started + item.completed del mismo comando = UN tool_use y UN tool_result.
    assert [u["id"] for u in usos] == ["item_1", "item_2"]
    assert usos[0]["name"] == "Bash" and usos[0]["input"] == {"command": 'bash -lc "echo hola-bridge"'}
    assert [r["tool_use_id"] for r in resultados] == ["item_1", "item_2"]
    assert resultados[0]["content"] == "hola-bridge\n" and resultados[0]["is_error"] is False
    assert resultados[1]["is_error"] is True
    assert _texto(eventos) == "Voy a correrlo.\n\nListo."


async def test_cambios_de_archivo_y_tools_mcp(arrancar, ragnar):
    await arrancar()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await ragnar.enviar(run(a, prompt="FILE"))
    eventos, _ = await ragnar.hasta_done(a)
    uso = next(e for e in eventos if e["type"] == "assistant")["message"]["content"][0]
    res = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert uso["name"] == "apply_patch" and res["content"] == "update a.txt" and res["is_error"] is False

    await ragnar.enviar(run(b, prompt="MCP"))
    eventos, _ = await ragnar.hasta_done(b)
    uso = next(e for e in eventos if e["type"] == "assistant")["message"]["content"][0]
    res = next(e for e in eventos if e["type"] == "user")["message"]["content"][0]
    assert uso["name"] == "mcp__ragnar-tickets__listar" and uso["input"] == {"q": 1}
    assert res["content"] == "RAG-1"


async def test_primer_turno_lleva_las_instrucciones_y_el_prompt_va_tras_el_separador(arrancar, ragnar, tmp_path):
    await arrancar(codex_model="gpt-x")
    tid = str(uuid.uuid4())
    raro = "--help\nsegunda linea"
    await ragnar.enviar(run(tid, prompt="corto", fallback=raro))
    await ragnar.hasta_done(tid)

    inv = _invocaciones(tmp_path)[0]
    argv = inv["argv"]
    assert argv[0] == "exec" and "--json" in argv and "resume" not in argv
    # Un prompt que empieza como un flag no se lee como flag: va tras `--`.
    assert argv[-2] == "--" and _prompt(argv).startswith("PROTOCOLO") and _prompt(argv).endswith(raro)
    assert _overrides(argv)["model"] == "gpt-x"
    assert _overrides(argv)["model_reasoning_effort"] == "medium"
    assert inv["cwd"] == str(tmp_path / "work")


async def test_modelo_y_razonamiento_se_fijan_siempre_por_defecto(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    await ragnar.hasta_done(tid)
    await ragnar.enviar(run(tid, prompt="dos"))
    await ragnar.hasta_done(tid)
    # En el primer turno y tambien al retomar el hilo (exec resume).
    for inv in _invocaciones(tmp_path):
        ov = _overrides(inv["argv"])
        assert ov["model"] == "gpt-5.6-terra" and ov["model_reasoning_effort"] == "medium"


async def test_vacio_no_fija_modelo_ni_razonamiento(arrancar, ragnar, tmp_path):
    await arrancar(codex_model="", codex_reasoning="")
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    await ragnar.hasta_done(tid)
    ov = _overrides(_invocaciones(tmp_path)[0]["argv"])
    assert "model" not in ov and "model_reasoning_effort" not in ov


async def test_segundo_turno_retoma_el_hilo(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)

    _, segundo = [i["argv"] for i in _invocaciones(tmp_path)]
    estado = json.loads((tmp_path / "codex-estado.json").read_text())
    hilo = estado["sesiones"][tid]["thread_id"]
    assert segundo[0] == "exec" and segundo[1] == "resume" and "--json" in segundo
    # Retomando, Codex ya recuerda: va solo el mensaje nuevo, sin instrucciones.
    assert segundo[-3:] == ["--", hilo, "dos"]


async def test_si_el_hilo_ya_no_existe_se_rehace_con_el_fallback(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    for archivo in (tmp_path / "codex" / "sessions").rglob("rollout-*.jsonl"):
        archivo.unlink()

    await ragnar.enviar(run(tid, prompt="dos", fallback="CON CONTEXTO"))
    await ragnar.hasta_done(tid)
    segundo = _invocaciones(tmp_path)[1]["argv"]
    assert "resume" not in segundo and _prompt(segundo).endswith("CON CONTEXTO")


async def test_un_thread_id_raro_en_el_estado_no_llega_al_comando(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    raro = "--malicious-flag-injection"
    (tmp_path / "codex-estado.json").write_text(json.dumps({"sesiones": {tid: {"thread_id": raro}}}))
    await ragnar.enviar(run(tid, prompt="uno"))
    await ragnar.hasta_done(tid)
    argv = _invocaciones(tmp_path)[0]["argv"]
    assert "resume" not in argv and raro not in argv


async def test_sandbox_por_defecto_es_read_only(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    await ragnar.hasta_done(tid)
    argv = _invocaciones(tmp_path)[0]["argv"]
    assert _overrides(argv)["sandbox_mode"] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "sandbox_workspace_write.writable_roots" not in _overrides(argv)


async def test_workspace_write_suma_los_add_dirs_como_raices_escribibles(arrancar, ragnar, tmp_path):
    await arrancar(codex_sandbox="workspace-write", add_dirs=[str(tmp_path / "extra")])
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    await ragnar.hasta_done(tid)
    ov = _overrides(_invocaciones(tmp_path)[0]["argv"])
    assert ov["sandbox_mode"] == "workspace-write"
    assert ov["sandbox_workspace_write.writable_roots"] == [str(tmp_path / "extra")]


async def test_codex_home_movido_se_exporta(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid))
    await ragnar.hasta_done(tid)
    assert _invocaciones(tmp_path)[0]["env"]["CODEX_HOME"] == str(tmp_path / "codex")


async def test_el_token_de_tickets_viaja_por_entorno_y_no_por_argv(arrancar, ragnar, tmp_path):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, tickets_token="ragagt_turno"))
    await ragnar.hasta_done(tid)

    inv = _invocaciones(tmp_path)[0]
    # Ni el token ni la URL en el comando (`ps` los mostraria)...
    assert "ragagt_turno" not in json.dumps(inv["argv"])
    ov = _overrides(inv["argv"])
    assert ov["mcp_servers.ragnar-tickets.args"] == ["-m", "ragnar_bridge.mcp_proxy"]
    assert ov["mcp_servers.ragnar-tickets.env_vars"] == ["RAGNAR_TICKETS_URL", "RAGNAR_TICKETS_TOKEN", "RAGNAR_TICKETS_AGENT"]
    # ...van en el entorno de ESE proceso, y no en el config.toml de nadie.
    assert inv["env"]["RAGNAR_TICKETS_TOKEN"] == "ragagt_turno"
    assert inv["env"]["RAGNAR_TICKETS_AGENT"] == "codex"
    assert inv["env"]["RAGNAR_TICKETS_URL"].endswith("/api/v1/mcp/")
    assert not (tmp_path / "codex" / "config.toml").exists()
    # El modelo tampoco ve esas variables en sus comandos de shell.
    assert ov["shell_environment_policy.exclude"] == ["RAGNAR_*"]


async def test_sin_token_de_tickets_no_se_declara_el_mcp(arrancar, ragnar, tmp_path, monkeypatch):
    # Ni un token del entorno del bridge se hereda al turno.
    monkeypatch.setenv("RAGNAR_TICKETS_TOKEN", "ragagt_del_entorno")
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, tickets_token=None))
    await ragnar.hasta_done(tid)
    inv = _invocaciones(tmp_path)[0]
    assert not any(k.startswith("mcp_servers.") for k in _overrides(inv["argv"]))
    assert "RAGNAR_TICKETS_TOKEN" not in inv["env"]


async def test_turn_failed_es_error_con_el_mensaje_de_codex(arrancar, ragnar):
    await arrancar()
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, prompt="FAILED"))
    eventos, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 1
    assert eventos[-1]["type"] == "result" and eventos[-1]["is_error"] is True
    assert eventos[-1]["result"] == "cuota agotada"


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


# ---- config y conexion


def test_config_valida_codex(tmp_path):
    ruta = tmp_path / "config.json"
    base = {"url": "wss://x/api/v1/bridge/ws", "token": "t", "agents": ["codex"]}
    ruta.write_text(json.dumps({**base, "codex_sandbox": "todo"}))
    with pytest.raises(ConfigError, match="codex_sandbox"):
        cargar(ruta)
    ruta.write_text(json.dumps({**base, "codex_cmd": "codex"}))
    cfg = cargar(ruta)
    assert cfg.codex_cmd == ["codex"] and cfg.codex_sandbox == "read-only" and cfg.codex_home == "~/.codex"
    assert cfg.codex_model == "gpt-5.6-terra" and cfg.codex_reasoning == "medium"
    # `null` (lo que escribe init) vale el default; "" es no fijarlo.
    ruta.write_text(json.dumps({**base, "codex_model": None, "codex_reasoning": None}))
    assert cargar(ruta).codex_model == "gpt-5.6-terra"
    ruta.write_text(json.dumps({**base, "codex_model": "", "codex_reasoning": ""}))
    assert (cargar(ruta).codex_model, cargar(ruta).codex_reasoning) == ("", "")
    ruta.write_text(json.dumps({**base, "codex_reasoning": "enorme"}))
    with pytest.raises(ConfigError, match="codex_reasoning"):
        cargar(ruta)


async def test_probar_conexion_anuncia_codex(ragnar, tmp_path):
    (tmp_path / "codex").mkdir()
    cfg = Config(url=ragnar.url, token=TOKEN, agents=["codex"], codex_cmd=[sys.executable, FAKE_CODEX], codex_home=str(tmp_path / "codex"))
    assert await probar_conexion(cfg) == "t"
    assert ragnar.auth_frame["protocol"] == PROTOCOLO and ragnar.auth_frame["agents"][0]["name"] == "codex"


# ---- cuota (/usage)


def test_cuota_de_codex_se_lee_de_app_server(tmp_path):
    from ragnar_bridge.agents.codex import CodexAdaptador

    (tmp_path / "codex").mkdir()
    cfg = Config(url="ws://x", token="t", agents=["codex"], codex_cmd=[sys.executable, FAKE_CODEX], codex_home=str(tmp_path / "codex"))
    cuota = CodexAdaptador(cfg, tmp_path).cuota("codex-cli 0.157.0")
    assert cuota["plan"] == "team"
    assert cuota["bloques"] == [
        {"grupo": None, "ventana": "5h", "usado": 12, "resetea": "2026-09-27T09:28:31Z"},
        {"grupo": None, "ventana": "semana", "usado": 40, "resetea": "2026-10-03T19:01:13Z"},
        {"grupo": "Spark", "ventana": "5h", "usado": 3, "resetea": "2026-09-27T09:28:31Z"},
    ]


def test_si_app_server_no_existe_la_cuota_es_none_y_no_lanza(tmp_path):
    from ragnar_bridge.agents.codex import CodexAdaptador

    cfg = Config(url="ws://x", token="t", agents=["codex"], codex_cmd=["/no/existe/codex"])
    assert CodexAdaptador(cfg, tmp_path).cuota("x") is None


@pytest.mark.skipif(os.name != "posix", reason="matar el grupo de procesos es POSIX-only")
def test_cuota_no_deja_huerfanos_de_app_server(tmp_path, monkeypatch):
    """Regresion del incidente 2026-09-28: `app-server` puede dejar un
    descendiente vivo que ignora que maten solo su PID (openai/codex#15379).
    Sondear la cuota cada `reprobar_cada` sin esto acumula un proceso por
    ciclo hasta agotar la RAM del host."""
    from ragnar_bridge.agents.codex import CodexAdaptador

    (tmp_path / "codex").mkdir()
    pid_file = tmp_path / "huerfano.pid"
    monkeypatch.setenv("FAKE_CODEX_SPAWN_ORPHAN", str(pid_file))
    cfg = Config(url="ws://x", token="t", agents=["codex"], codex_cmd=[sys.executable, FAKE_CODEX], codex_home=str(tmp_path / "codex"))

    assert CodexAdaptador(cfg, tmp_path).cuota("x") is not None

    pid = int(pid_file.read_text().strip())
    time.sleep(0.5)  # darle tiempo al kill del grupo a propagarse
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
