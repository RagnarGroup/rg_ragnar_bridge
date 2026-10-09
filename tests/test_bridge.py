import asyncio
import json
import sys
import uuid

import pytest
from websockets.asyncio.server import serve

from ragnar_bridge import PROTOCOLO
from ragnar_bridge.bridge import Bridge, ErrorFatal, ejecutar
from ragnar_bridge.config import Config
from helpers import FAKE, TOKEN, FakeRagnar, run as _run
from ragnar_bridge.agents.claude import conceder_permiso, ruta_permisos, url_mcp_tickets
from ragnar_bridge.protocolo import permiso_es_catastrofico, regla_concedible


def _cfg(tmp_path, ragnar, **kw) -> Config:
    return Config(
        url=ragnar.url,
        token=TOKEN,
        agents=["claude"],
        claude_cmd=[sys.executable, FAKE],
        workdir=str(tmp_path / "work"),
        config_dir=str(tmp_path / "claude"),
        **kw,
    )


@pytest.fixture
async def conectado(tmp_path, ragnar):
    (tmp_path / "work").mkdir()
    cfg = _cfg(tmp_path, ragnar)
    tarea = asyncio.create_task(ejecutar(cfg, tmp_path))
    await asyncio.wait_for(ragnar.conectado.wait(), 10)
    yield cfg
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)


async def test_auth_lleva_protocolo_y_version(conectado, ragnar):
    assert ragnar.auth_frame["protocol"] == PROTOCOLO
    assert ragnar.auth_frame["agent"] == "claude"
    assert ragnar.auth_frame["token"] == TOKEN


async def test_turno_reenvia_stream_json_sin_traducir(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid))
    eventos, fin = await ragnar.hasta_done(tid)

    assert [e["type"] for e in eventos] == ["system", "result"]
    assert fin["type"] == "done" and fin["exit_code"] == 0
    argv = eventos[0]["argv"]
    assert "--print" in argv and "--output-format=stream-json" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "PROTOCOLO"


async def test_primer_turno_usa_session_id_y_fallback_luego_resume(conectado, ragnar, tmp_path):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid, prompt="corto", fallback="CON CONTEXTO"))
    eventos, _ = await ragnar.hasta_done(tid)
    argv = eventos[0]["argv"]
    assert "--session-id" in argv and "--resume" not in argv
    assert argv[-1] == "CON CONTEXTO"

    # Simula que el CLI dejo su sesion: <config_dir>/projects/<cwd saneado>/<id>.jsonl
    import re
    carpeta = re.sub(r"[^a-zA-Z0-9]", "-", str((tmp_path / "work").resolve()))
    destino = tmp_path / "claude" / "projects" / carpeta
    destino.mkdir(parents=True)
    (destino / f"{tid}.jsonl").write_text("{}\n")

    await ragnar.enviar(_run(tid, prompt="corto", fallback="CON CONTEXTO"))
    eventos, _ = await ragnar.hasta_done(tid)
    argv = eventos[0]["argv"]
    assert "--resume" in argv and "--session-id" not in argv
    assert argv[-1] == "corto"


async def test_prompt_que_empieza_con_guion_no_se_lee_como_flag(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid, prompt="--help"))
    eventos, _ = await ragnar.hasta_done(tid)
    assert eventos[0]["argv"][-1] == " --help"


async def test_fallo_del_cli_reporta_codigo_y_stderr(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid, prompt="FAIL"))
    _, fin = await ragnar.hasta_done(tid)
    assert fin["exit_code"] == 3 and "boom" in fin["stderr"]


async def test_cancel_corta_el_proceso(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid, prompt="SLEEP"))
    await asyncio.sleep(1)
    await ragnar.enviar({"type": "cancel", "task_id": tid})
    _, fin = await ragnar.hasta_done(tid, timeout=15)
    assert fin["type"] == "done" and fin["exit_code"] != 0


async def test_session_id_invalido_se_rechaza(conectado, ragnar):
    run = _run("x")
    run["session_id"] = "../../etc"
    await ragnar.enviar(run)
    _, fin = await ragnar.hasta_done("x")
    assert fin["type"] == "error"


async def test_conceder_es_de_la_conversacion_y_no_toca_tu_settings(conectado, ragnar, tmp_path):
    """RAG-200: la regla aprobada va a un settings PROPIO de la conversacion
    (`--settings`), nunca al ~/.claude/settings.json del usuario."""
    tid, otra = str(uuid.uuid4()), str(uuid.uuid4())
    await ragnar.enviar(_run(tid, conceder="Bash(docker ps:*)"))
    eventos, _ = await ragnar.hasta_done(tid)
    assert not (tmp_path / "claude" / "settings.json").exists()
    propio = ruta_permisos(tmp_path, tid)
    assert json.loads(propio.read_text())["permissions"]["allow"] == ["Bash(docker ps:*)"]
    argv = eventos[0]["argv"]
    assert argv[argv.index("--settings") + 1] == str(propio)

    # Los turnos siguientes de ESA conversacion siguen con el permiso...
    await ragnar.enviar(_run(tid, prompt="sigue"))
    eventos, _ = await ragnar.hasta_done(tid)
    assert eventos[0]["argv"][eventos[0]["argv"].index("--settings") + 1] == str(propio)
    # ...y otra conversacion no lo hereda.
    await ragnar.enviar(_run(otra))
    eventos, _ = await ragnar.hasta_done(otra)
    assert "--settings" not in eventos[0]["argv"]


@pytest.mark.parametrize("regla", ["Bash(*)", "Bash", "Bash(:*)", "Bash(sh:*)", "Bash(python3 *)"])
async def test_conceder_rechaza_reglas_amplias(conectado, ragnar, tmp_path, regla):
    tid = str(uuid.uuid4())
    await ragnar.enviar(_run(tid, conceder=regla))
    eventos, _ = await ragnar.hasta_done(tid)
    assert not (tmp_path / "claude" / "settings.json").exists()
    assert not ruta_permisos(tmp_path, tid).exists()
    assert "--settings" not in eventos[0]["argv"]


async def test_desconexion_mata_los_turnos_en_curso(tmp_path, ragnar):
    (tmp_path / "work").mkdir()
    cfg = _cfg(tmp_path, ragnar)
    bridge = Bridge(cfg, tmp_path)
    tid = str(uuid.uuid4())

    async def sesion():
        async with __import__("websockets").asyncio.client.connect(ragnar.url) as ws:
            await bridge.sesion(ws)

    tarea = asyncio.create_task(sesion())
    await asyncio.wait_for(ragnar.conectado.wait(), 10)
    await ragnar.enviar(_run(tid, prompt="SLEEP"))
    await asyncio.sleep(1)
    assert tid in bridge._procesos
    proceso = bridge._procesos[tid]

    await ragnar._ws.close()
    await asyncio.wait_for(asyncio.gather(tarea, return_exceptions=True), 15)
    assert proceso.returncode is not None


async def test_token_invalido_es_error_fatal(tmp_path):
    servidor = FakeRagnar(token_valido="otro")
    async with serve(servidor._handler, "127.0.0.1", 0) as srv:
        url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        cfg = Config(url=url, token=TOKEN, agents=["claude"], claude_cmd=[sys.executable, FAKE])
        with pytest.raises(ErrorFatal):
            await asyncio.wait_for(ejecutar(cfg, tmp_path), 10)


async def test_protocolo_incompatible_es_error_fatal(tmp_path):
    servidor = FakeRagnar(protocolo=99)
    async with serve(servidor._handler, "127.0.0.1", 0) as srv:
        url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        cfg = Config(url=url, token=TOKEN, agents=["claude"], claude_cmd=[sys.executable, FAKE])
        with pytest.raises(ErrorFatal):
            await asyncio.wait_for(ejecutar(cfg, tmp_path), 10)


def test_permiso_catastrofico():
    assert permiso_es_catastrofico("Bash(*)")
    assert permiso_es_catastrofico("Bash(rm -rf /)")
    assert permiso_es_catastrofico("Bash(mkfs.ext4 /dev/sda)")
    assert not permiso_es_catastrofico("Bash(docker *)")
    assert not permiso_es_catastrofico("Bash(rm -rf /home/x/tmp)")


def test_conceder_no_duplica(tmp_path):
    sid = str(uuid.uuid4())
    assert conceder_permiso(tmp_path, sid, "WebFetch")
    assert conceder_permiso(tmp_path, sid, " WebFetch ")
    datos = json.loads(ruta_permisos(tmp_path, sid).read_text())
    assert datos["permissions"]["allow"] == ["WebFetch"]


@pytest.mark.parametrize(
    "regla",
    [
        "Bash", "bash", "Bash()", "Bash(*)", "Bash(**)", "Bash(* )", "Bash(:*)", "Bash( :* )",
        "Bash(sh:*)", "Bash(bash -c *)", "Bash(python:*)", "Bash(python3.12 *)", "Bash(node *)",
        "Bash(/usr/bin/python3 *)", "Bash(sudo:*)", "Bash(env *)", "Bash(xargs:*)", "Bash(*docker*)",
        "PowerShell", "PowerShell(*)", "PowerShell(pwsh:*)", "*", "mcp__*", "Bash(rm -rf /)", "",
        "Bash(ls)\nBash", "Bash(docker; rm:*)",
    ],
)
def test_regla_concedible_rechaza_reglas_amplias(regla):
    assert not regla_concedible(regla)


@pytest.mark.parametrize(
    "regla",
    ["WebFetch", "Read", "Edit", "Bash(docker *)", "Bash(docker ps:*)", "Bash(npm run test:*)",
     "Bash(git status)", "Bash(python3 manage.py check)", "mcp__tickets__ver_ticket"],
)
def test_regla_concedible_acepta_reglas_concretas(regla):
    assert regla_concedible(regla)


def test_url_mcp_tickets():
    assert url_mcp_tickets("wss://panel.ragnargroup.app/api/v1/bridge/ws") == "https://panel.ragnargroup.app/api/v1/mcp/"
    assert url_mcp_tickets("ws://localhost:8000/api/v1/bridge/ws") == "http://localhost:8000/api/v1/mcp/"
    assert url_mcp_tickets("wss://otro/ws") is None
