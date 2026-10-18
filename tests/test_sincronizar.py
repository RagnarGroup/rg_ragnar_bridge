"""Bootstrap `auto` desde Ragnar, `sync --once` y `publicar`, incluidos sus comandos de CLI."""

import asyncio
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from ragnar_bridge import PROTOCOLO, cli
from ragnar_bridge.api import ClienteRagnar
from ragnar_bridge.bridge import Bridge, ejecutar
from ragnar_bridge.catalogo import clave_artefacto, publicar, recolectar, sync_once
from ragnar_bridge.config import Config
from ragnar_bridge.sincronizar import NOMBRE_REGISTRO, Registro
from helpers import FAKE, TOKEN

TOKEN_AGENTE = "ragagt_test"


def _sha(texto):
    return hashlib.sha256(texto.encode()).hexdigest()


def _archivo(path, content):
    return {"path": path, "content": content, "sha256": _sha(content)}


def _bridge(tmp_path, root):
    estado = tmp_path / "estado"
    estado.mkdir(exist_ok=True)
    return Bridge(Config(url="ws://localhost", token="t", project_paths={"RAG": str(root)}), estado)


def _auto(bridge, *archivos, cli_="claude"):
    return bridge._aplicar_bootstrap({"cli": cli_, "project_key": "RAG", "mode": "auto", "files": list(archivos)})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def test_auto_escribe_lo_nuevo_y_actualiza_lo_no_tocado(tmp_path, repo):
    bridge = _bridge(tmp_path, repo)
    ruta = ".claude/agents/rafael.md"

    primero = _auto(bridge, _archivo(ruta, "v1"))
    assert primero == {"ok": True, "mode": "auto", "files": [{"path": ruta, "status": "written"}]}
    assert (repo / ruta).read_text() == "v1"

    segundo = _auto(bridge, _archivo(ruta, "v2"))
    assert segundo["files"] == [{"path": ruta, "status": "updated"}]
    assert (repo / ruta).read_text() == "v2"

    assert _auto(bridge, _archivo(ruta, "v2"))["files"] == [{"path": ruta, "status": "unchanged"}]

    registro = json.loads((tmp_path / "estado" / NOMBRE_REGISTRO).read_text())
    assert registro == {str(repo.resolve()): {ruta: _sha("v2")}}
    assert not (repo / NOMBRE_REGISTRO).exists()


def test_auto_nunca_pisa_un_archivo_editado_a_mano(tmp_path, repo):
    bridge = _bridge(tmp_path, repo)
    ruta = ".claude/skills/revisar/SKILL.md"
    _auto(bridge, _archivo(ruta, "v1"))
    (repo / ruta).write_text("mi version")

    resultado = _auto(bridge, _archivo(ruta, "v2"), _archivo(".claude/agents/nuevo.md", "n"))
    assert resultado["ok"] is True
    assert resultado["files"] == [
        {"path": ruta, "status": "conflict"},
        {"path": ".claude/agents/nuevo.md", "status": "written"},
    ]
    assert (repo / ruta).read_text() == "mi version"
    assert _auto(bridge, _archivo(ruta, "v3"))["files"] == [{"path": ruta, "status": "conflict"}]


def test_auto_no_pisa_un_archivo_que_existia_antes_de_ragnar(tmp_path, repo):
    bridge = _bridge(tmp_path, repo)
    destino = repo / ".claude/agents/viejo.md"
    destino.parent.mkdir(parents=True)
    destino.write_text("hecho a mano")
    assert _auto(bridge, _archivo(".claude/agents/viejo.md", "catalogo"))["files"][0]["status"] == "conflict"
    assert destino.read_text() == "hecho a mano"


def test_un_apply_confirmado_deja_que_el_proximo_auto_actualice(tmp_path, repo):
    bridge = _bridge(tmp_path, repo)
    destino = repo / ".claude/agents/a.md"
    destino.parent.mkdir(parents=True)
    destino.write_text("a mano")
    bridge._aplicar_bootstrap(
        {"cli": "claude", "project_key": "RAG", "mode": "apply", "overwrite": True, "files": [_archivo(".claude/agents/a.md", "v1")]}
    )
    assert _auto(bridge, _archivo(".claude/agents/a.md", "v2"))["files"][0]["status"] == "updated"


@pytest.mark.parametrize(
    "ruta", [".claude/settings.json", "../afuera.md", ".claude/agents/../../x.md", "CLAUDE.md", ".agent/rules/a.md"]
)
def test_auto_rechaza_rutas_fuera_de_los_prefijos_sin_escribir_nada(tmp_path, repo, ruta):
    bridge = _bridge(tmp_path, repo)
    with pytest.raises(ValueError):
        _auto(bridge, _archivo(".claude/agents/ok.md", "ok"), _archivo(ruta, "x"))
    assert not (repo / ".claude/agents/ok.md").exists()


def test_auto_en_un_proyecto_sin_ruta_configurada_falla(tmp_path, repo):
    bridge = _bridge(tmp_path, repo)
    with pytest.raises(ValueError, match="ruta configurada"):
        bridge._aplicar_bootstrap({"cli": "claude", "project_key": "OTRO", "mode": "auto", "files": [_archivo(".claude/agents/a.md", "x")]})


@pytest.fixture
async def conectado(tmp_path, ragnar, repo):
    (tmp_path / "work").mkdir()
    cfg = Config(
        url=ragnar.url,
        token=TOKEN,
        agents=["claude"],
        claude_cmd=[sys.executable, FAKE],
        workdir=str(tmp_path / "work"),
        config_dir=str(tmp_path / "claude"),
        project_paths={"RAG": str(repo), "SGA": str(repo)},
    )
    tarea = asyncio.create_task(ejecutar(cfg, tmp_path))
    await asyncio.wait_for(ragnar.conectado.wait(), 10)
    yield cfg
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)


async def test_el_auth_declara_proyectos_y_plataforma(conectado, ragnar, repo):
    assert ragnar.auth_frame["protocol"] == PROTOCOLO == 2
    assert ragnar.auth_frame["projects"] == ["RAG", "SGA"]
    assert ragnar.auth_frame["platform"] in ("linux", "windows", "macos")
    assert str(repo) not in json.dumps(ragnar.auth_frame)


async def test_el_frame_bootstrap_auto_responde_por_el_socket(conectado, ragnar, repo):
    await ragnar.enviar(
        {
            "type": "bootstrap",
            "request_id": "r1",
            "mode": "auto",
            "cli": "claude",
            "project_key": "RAG",
            "overwrite": False,
            "files": [_archivo(".claude/agents/a.md", "x")],
        }
    )
    while True:
        msg = await asyncio.wait_for(ragnar.recibidos.get(), 10)
        if msg.get("type") == "bootstrap_result":
            break
    assert msg["request_id"] == "r1"
    assert msg["result"] == {"ok": True, "mode": "auto", "files": [{"path": ".claude/agents/a.md", "status": "written"}]}
    assert (repo / ".claude/agents/a.md").read_text() == "x"


class _Catalogo:
    def __init__(self):
        self.artefactos = []
        self.posts = 0
        self.autorizaciones = set()

    def vigentes(self, cli, project_key):
        elegidos = {}
        for a in sorted(self.artefactos, key=lambda a: -a["revision"]):
            if a["cli"] != cli or a["project_key"] not in (None, project_key):
                continue
            elegidos.setdefault(a["artifact_key"], a)
        return list(elegidos.values())

    def agregar(self, cuerpo):
        revision = 1 + max((a["revision"] for a in self.artefactos if a["artifact_key"] == cuerpo["artifact_key"]), default=0)
        artefacto = {**cuerpo, "revision": revision, "sha256": _sha(cuerpo["content"]), "published_by": "admin", "created_at": "2026-10-09T00:00:00Z"}
        self.artefactos.append(artefacto)
        return artefacto


@pytest.fixture
def api():
    catalogo = _Catalogo()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _responder(self, codigo, cuerpo):
            datos = json.dumps(cuerpo).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)

        def _autorizado(self):
            catalogo.autorizaciones.add(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != f"Bearer {TOKEN_AGENTE}":
                self._responder(401, {"detail": "Credencial ausente o inválida"})
                return False
            return True

        def do_GET(self):
            if not self._autorizado():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            vigentes = catalogo.vigentes(q["cli"], q.get("project_key"))
            if url.path == "/api/v1/bootstrap/manifest":
                return self._responder(200, vigentes)
            if url.path == "/api/v1/bootstrap/catalog":
                return self._responder(200, [{k: v for k, v in a.items() if k != "content"} for a in vigentes])
            self._responder(404, {"detail": "no"})

        def do_POST(self):
            if not self._autorizado():
                return
            cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            catalogo.posts += 1
            self._responder(201, catalogo.agregar(cuerpo))

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    catalogo.url = f"http://127.0.0.1:{servidor.server_address[1]}/api/v1"
    yield catalogo
    servidor.shutdown()
    servidor.server_close()


def _cliente(api):
    return ClienteRagnar(api.url, TOKEN_AGENTE)


def test_sync_once_contra_un_servidor_falso(api, tmp_path, repo):
    api.agregar({"artifact_key": "claude/RAG/.claude/agents/rafael.md", "title": "r", "cli": "claude", "project_key": "RAG", "platform": "any", "relative_path": ".claude/agents/rafael.md", "content": "v1"})
    api.agregar({"artifact_key": "claude/_global/.claude/skills/s/SKILL.md", "title": "s", "cli": "claude", "project_key": None, "platform": "any", "relative_path": ".claude/skills/s/SKILL.md", "content": "global"})
    registro = Registro.en(tmp_path / "estado")

    primero = sync_once(_cliente(api), "claude", "RAG", repo, registro, "windows")
    assert sorted((r["path"], r["status"]) for r in primero) == [
        (".claude/agents/rafael.md", "written"),
        (".claude/skills/s/SKILL.md", "written"),
    ]
    assert (repo / ".claude/agents/rafael.md").read_text() == "v1"

    (repo / ".claude/skills/s/SKILL.md").write_text("mio")
    api.agregar({"artifact_key": "claude/RAG/.claude/agents/rafael.md", "title": "r", "cli": "claude", "project_key": "RAG", "platform": "any", "relative_path": ".claude/agents/rafael.md", "content": "v2"})
    api.agregar({"artifact_key": "claude/_global/.claude/skills/s/SKILL.md", "title": "s", "cli": "claude", "project_key": None, "platform": "any", "relative_path": ".claude/skills/s/SKILL.md", "content": "global v2"})
    segundo = sync_once(_cliente(api), "claude", "RAG", repo, Registro.en(tmp_path / "estado"), "windows")
    assert sorted((r["path"], r["status"]) for r in segundo) == [
        (".claude/agents/rafael.md", "updated"),
        (".claude/skills/s/SKILL.md", "conflict"),
    ]
    assert (repo / ".claude/agents/rafael.md").read_text() == "v2"
    assert (repo / ".claude/skills/s/SKILL.md").read_text() == "mio"


def test_sync_once_rechaza_un_manifiesto_con_rutas_fuera_de_los_prefijos(api, tmp_path, repo):
    api.agregar({"artifact_key": "claude/RAG/ok", "title": "ok", "cli": "claude", "project_key": "RAG", "platform": "any", "relative_path": ".claude/agents/ok.md", "content": "ok"})
    api.agregar({"artifact_key": "claude/RAG/malo", "title": "malo", "cli": "claude", "project_key": "RAG", "platform": "any", "relative_path": ".claude/settings.json", "content": "{}"})
    with pytest.raises(ValueError):
        sync_once(_cliente(api), "claude", "RAG", repo, Registro.en(tmp_path), "linux")
    assert not (repo / ".claude/agents/ok.md").exists()
    assert not (repo / ".claude/settings.json").exists()


def test_publicar_es_idempotente(api, repo):
    (repo / ".claude/agents").mkdir(parents=True)
    (repo / ".claude/agents/rafael.md").write_text("rafael")
    (repo / ".claude/skills/revisar").mkdir(parents=True)
    (repo / ".claude/skills/revisar/SKILL.md").write_text("skill")
    (repo / ".claude/settings.json").write_text("{}")
    (repo / "CLAUDE.md").write_text("del repo")
    (repo / ".claude/agents/vacio.md").write_text("")

    primero = publicar(_cliente(api), "claude", "RAG", repo)
    assert sorted((r["path"], r["status"]) for r in primero) == [
        (".claude/agents/rafael.md", "published"),
        (".claude/agents/vacio.md", "skipped"),
        (".claude/skills/revisar/SKILL.md", "published"),
    ]
    assert api.posts == 2
    assert {a["artifact_key"] for a in api.artefactos} == {
        "claude/RAG/.claude/agents/rafael.md",
        "claude/RAG/.claude/skills/revisar/SKILL.md",
    }

    segundo = publicar(_cliente(api), "claude", "RAG", repo)
    assert api.posts == 2
    assert [r["status"] for r in segundo if r["path"] == ".claude/agents/rafael.md"] == ["unchanged"]

    (repo / ".claude/agents/rafael.md").write_text("rafael v2")
    tercero = publicar(_cliente(api), "claude", "RAG", repo)
    assert api.posts == 3
    [publicado] = [r for r in tercero if r["status"] == "published"]
    assert publicado["path"] == ".claude/agents/rafael.md" and publicado["revision"] == 2


def test_la_clave_de_artefacto_es_estable_y_valida():
    assert clave_artefacto("claude", None, ".claude/agents/a.md") == "claude/_global/.claude/agents/a.md"
    rara = clave_artefacto("claude", "RAG", ".claude/skills/con espacio/SKILL.md")
    assert rara == clave_artefacto("claude", "RAG", ".claude/skills/con espacio/SKILL.md")
    assert rara.startswith("claude/RAG/h-") and len(rara) <= 120


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks en Windows exigen privilegios")
def test_publicar_no_sigue_symlinks(repo, tmp_path):
    afuera = tmp_path / "secreto.md"
    afuera.write_text("secreto")
    (repo / ".claude/agents").mkdir(parents=True)
    (repo / ".claude/agents/link.md").symlink_to(afuera)
    encontrados, omitidos = recolectar("claude", repo)
    assert encontrados == [] and omitidos == [{"path": ".claude/agents/link.md", "motivo": "enlace simbólico"}]


def test_publicar_salta_carpetas_ocultas(repo):
    (repo / ".claude/agents/.git/hooks").mkdir(parents=True)
    (repo / ".claude/agents/.git/config").write_text("[remote]")
    (repo / ".claude/agents/.git/hooks/pre-commit.sample").write_text("#!/bin/sh")
    (repo / ".claude/agents/rafael.md").write_text("rafael")
    encontrados, _ = recolectar("claude", repo)
    assert [ruta for ruta, _ in encontrados] == [".claude/agents/rafael.md"]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="las junctions son de Windows")
def test_publicar_no_sigue_junctions(repo, tmp_path):
    import subprocess

    afuera = tmp_path / "otra_skill"
    afuera.mkdir()
    (afuera / "SKILL.md").write_text("de terceros")
    (repo / ".claude/skills").mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(repo / ".claude/skills/otra"), str(afuera)], check=True, capture_output=True)
    (repo / ".claude/agents").mkdir(parents=True)
    (repo / ".claude/agents/rafael.md").write_text("rafael")
    encontrados, _ = recolectar("claude", repo)
    assert [ruta for ruta, _ in encontrados] == [".claude/agents/rafael.md"]


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ragnar-bridge", *argv])
    with pytest.raises(SystemExit) as salida:
        cli.main()
    return salida.value.code


def test_cli_sync_once_usa_el_token_del_entorno(api, tmp_path, repo, monkeypatch, capsys):
    api.agregar({"artifact_key": "claude/RAG/a", "title": "a", "cli": "claude", "project_key": "RAG", "platform": "any", "relative_path": ".claude/agents/a.md", "content": "a"})
    monkeypatch.setenv("RAGNAR_AGENT_TOKEN", TOKEN_AGENTE)
    codigo = _main(
        monkeypatch, "sync", "--once", "--cli", "claude", "--project", "RAG", "--dir", str(repo),
        "--api", api.url, "--estado", str(tmp_path / "estado"),
    )
    assert codigo == 0
    assert "1 nuevos" in capsys.readouterr().out
    assert (repo / ".claude/agents/a.md").read_text() == "a"


def test_cli_sync_sin_once_o_sin_token_falla(api, repo, monkeypatch, capsys):
    monkeypatch.delenv("RAGNAR_AGENT_TOKEN", raising=False)
    assert _main(monkeypatch, "sync", "--cli", "claude", "--dir", str(repo), "--api", api.url) == 2
    assert _main(monkeypatch, "sync", "--once", "--cli", "claude", "--dir", str(repo), "--api", api.url) == 1
    assert "RAGNAR_AGENT_TOKEN" in capsys.readouterr().err
    assert _main(monkeypatch, "sync", "--once", "--cli", "claude", "--dir", str(repo), "--token", "x") == 2


def test_cli_publicar(api, repo, monkeypatch, capsys):
    (repo / ".agent/rules").mkdir(parents=True)
    (repo / ".agent/rules/guia.md").write_text("guia")
    monkeypatch.setenv("RAGNAR_AGENT_TOKEN", TOKEN_AGENTE)
    assert _main(monkeypatch, "publicar", "--cli", "agy", "--api", api.url, str(repo)) == 0
    assert "1 publicados" in capsys.readouterr().out
    assert api.artefactos[0]["artifact_key"] == "agy/_global/.agent/rules/guia.md"
    assert api.artefactos[0]["project_key"] is None
