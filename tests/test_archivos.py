"""Archivos editables desde Ragnar: lista permitida, rutas prohibidas o invalidas,
escritura con respaldo y control de cambios, y el frame `files` del bridge."""

import json

import pytest

from ragnar_bridge.archivos import MAX_BYTES, ArchivosEditables, ErrorArchivo
from ragnar_bridge.bridge import Bridge
from ragnar_bridge.config import Config


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    estado = home / ".config" / "ragnar-bridge"
    estado.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RAGNAR_BRIDGE_CONFIG", str(estado / "config.json"))
    (estado / "config.json").write_text('{"token": "ragbrg_secreto"}')
    cfg = Config(url="ws://localhost", token="t", config_dir=str(home / ".claude"))
    return ArchivosEditables(cfg, estado), home


def _settings(home):
    return home / ".claude" / "settings.json"


def test_la_lista_trae_settings_y_claude_md_aunque_no_existan(entorno):
    archivos, home = entorno
    _settings(home).write_text("{}")
    assert archivos.listar()["files"] == [
        {"path": str(_settings(home)), "fixed": True, "exists": True},
        {"path": str(home / ".claude" / "CLAUDE.md"), "fixed": True, "exists": False},
    ]


def test_agregar_y_quitar_una_ruta(entorno):
    archivos, home = entorno
    notas = home / "notas.md"
    notas.write_text("hola")

    rutas = [f["path"] for f in archivos.agregar("~/notas.md")["files"]]
    assert rutas[-1] == str(notas)
    assert len(archivos.agregar(str(notas))["files"]) == 3
    assert archivos.leer(str(notas))["content"] == "hola"

    assert len(archivos.quitar(str(notas))["files"]) == 2
    with pytest.raises(ErrorArchivo) as error:
        archivos.leer(str(notas))
    assert error.value.codigo == "not_found"


def test_las_rutas_fijas_no_se_quitan(entorno):
    archivos, home = entorno
    with pytest.raises(ErrorArchivo) as error:
        archivos.quitar(str(_settings(home)))
    assert error.value.codigo == "forbidden"


@pytest.mark.parametrize(
    "ruta",
    ["~/.config/ragnar-bridge/config.json", "~/.ssh/id_ed25519", "~/.claude/.credentials.json", "/proc/self/environ"],
)
def test_no_se_agregan_archivos_con_credenciales(entorno, ruta):
    archivos, home = entorno
    (home / ".ssh").mkdir()
    with pytest.raises(ErrorArchivo) as error:
        archivos.agregar(ruta)
    assert error.value.codigo == "forbidden"


def test_un_enlace_a_un_archivo_prohibido_tampoco_se_agrega(entorno):
    archivos, home = entorno
    (home / "inocente.json").symlink_to(home / ".config" / "ragnar-bridge" / "config.json")
    with pytest.raises(ErrorArchivo) as error:
        archivos.agregar("~/inocente.json")
    assert error.value.codigo == "forbidden"


@pytest.mark.parametrize("ruta", ["relativa.txt", "", None, "~/no-existe/archivo.txt", "~"])
def test_rutas_invalidas_se_rechazan(entorno, ruta):
    archivos, _ = entorno
    with pytest.raises(ErrorArchivo):
        archivos.agregar(ruta)


def test_no_se_lee_ni_escribe_fuera_de_la_lista(entorno):
    archivos, home = entorno
    (home / "otro.txt").write_text("x")
    for operacion in (lambda: archivos.leer("~/otro.txt"), lambda: archivos.escribir("~/otro.txt", "y", None)):
        with pytest.raises(ErrorArchivo) as error:
            operacion()
        assert error.value.codigo == "not_found"
    assert (home / "otro.txt").read_text() == "x"


def test_escribir_guarda_respalda_y_conserva_los_permisos(entorno):
    archivos, home = entorno
    settings = _settings(home)
    settings.write_text('{"permissions": {"allow": []}}')
    settings.chmod(0o640)
    leido = archivos.leer(str(settings))

    nuevo = '{"permissions": {"allow": ["Bash(node *)"]}}'
    guardado = archivos.escribir(str(settings), nuevo, leido["sha256"])

    assert settings.read_text() == nuevo
    assert settings.stat().st_mode & 0o777 == 0o640
    assert guardado["sha256"] == archivos.leer(str(settings))["sha256"]
    respaldos = list((home / ".config" / "ragnar-bridge" / "respaldos-archivos").rglob("settings.json"))
    assert len(respaldos) == 1 and json.loads(respaldos[0].read_text()) == {"permissions": {"allow": []}}


def test_crear_un_archivo_que_no_existia(entorno):
    archivos, home = entorno
    destino = home / ".claude" / "CLAUDE.md"
    assert archivos.leer(str(destino)) == {
        "ok": True, "path": str(destino), "exists": False, "content": "", "sha256": None,
    }
    archivos.escribir(str(destino), "# Reglas\n", None)
    assert destino.read_text() == "# Reglas\n"


def test_si_el_archivo_cambio_en_el_servidor_no_se_pisa(entorno):
    archivos, home = entorno
    settings = _settings(home)
    settings.write_text("{}")
    leido = archivos.leer(str(settings))
    settings.write_text('{"permissions": {"allow": ["Read"]}}')

    with pytest.raises(ErrorArchivo) as error:
        archivos.escribir(str(settings), '{"x": 1}', leido["sha256"])
    assert error.value.codigo == "conflict"
    assert "Read" in settings.read_text()
    with pytest.raises(ErrorArchivo):
        archivos.escribir(str(settings), "{}", None)


def test_un_json_roto_no_se_guarda(entorno):
    archivos, home = entorno
    settings = _settings(home)
    settings.write_text("{}")
    with pytest.raises(ErrorArchivo, match="JSON"):
        archivos.escribir(str(settings), '{"a": ', archivos.leer(str(settings))["sha256"])
    assert settings.read_text() == "{}"


def test_settings_no_acepta_un_permiso_catastrofico_nuevo(entorno):
    archivos, home = entorno
    settings = _settings(home)
    settings.write_text('{"permissions": {"allow": ["Bash(*)"]}}')
    sha = archivos.leer(str(settings))["sha256"]

    with pytest.raises(ErrorArchivo) as error:
        archivos.escribir(str(settings), '{"permissions": {"allow": ["Bash(*)", "*"]}}', sha)
    assert error.value.codigo == "forbidden"
    archivos.escribir(str(settings), '{"permissions": {"allow": ["Bash(*)", "Bash(node *)"]}}', sha)


def test_limite_de_tamano_y_solo_texto(entorno):
    archivos, home = entorno
    grande = home / "grande.txt"
    grande.write_bytes(b"a" * (MAX_BYTES + 1))
    binario = home / "binario.bin"
    binario.write_bytes(b"\xff\xfe\x00")
    archivos.agregar(str(grande))
    archivos.agregar(str(binario))

    with pytest.raises(ErrorArchivo, match="limite"):
        archivos.leer(str(grande))
    with pytest.raises(ErrorArchivo, match="UTF-8"):
        archivos.leer(str(binario))
    with pytest.raises(ErrorArchivo, match="limite"):
        archivos.escribir(str(binario), "a" * (MAX_BYTES + 1), None)


def test_atender_despacha_por_operacion_y_respeta_el_apagado(entorno):
    archivos, home = entorno
    assert [f["fixed"] for f in archivos.atender({"op": "list"})["files"]] == [True, True]
    with pytest.raises(ErrorArchivo):
        archivos.atender({"op": "borrar-todo"})

    archivos.cfg.archivos_editables = False
    with pytest.raises(ErrorArchivo) as error:
        archivos.atender({"op": "list"})
    assert error.value.codigo == "forbidden"


async def test_el_bridge_responde_el_frame_files_con_su_request_id(entorno):
    archivos, home = entorno
    bridge = Bridge(archivos.cfg, archivos.estado_dir)
    enviados = []

    async def enviar(mensaje):
        enviados.append(mensaje)

    bridge.enviar = enviar
    await bridge._archivos({"type": "files", "request_id": "r1", "op": "list"})
    await bridge._archivos({"type": "files", "request_id": "r2", "op": "read", "path": "/etc/passwd"})

    assert enviados[0]["type"] == "files_result" and enviados[0]["request_id"] == "r1"
    assert enviados[0]["result"]["ok"] is True
    assert enviados[1]["result"] == {
        "ok": False,
        "error": "Esa ruta no esta en la lista de archivos editables.",
        "code": "not_found",
    }
