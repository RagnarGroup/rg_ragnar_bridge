"""Explorador de carpetas (frame `dirs`) y carpeta de trabajo por conversacion (`run.cwd`):
que se lista, que se oculta, raices, errores, y que el turno corre y retoma sesion ahi."""

import os
import re

import pytest

from ragnar_bridge import carpetas
from ragnar_bridge.agents.claude import construir_comando, hay_sesion
from ragnar_bridge.bridge import Bridge
from ragnar_bridge.carpetas import ErrorCarpeta
from ragnar_bridge.config import Config
from ragnar_bridge.protocolo import RunInvalido, validar_run

from helpers import run

SESION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def arbol(tmp_path):
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alfa").mkdir()
    (tmp_path / ".oculta").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "archivo.txt").write_text("x")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n")
    return tmp_path


def test_lista_solo_subcarpetas_visibles_ordenadas(arbol):
    resultado = carpetas.listar(str(arbol), inicio="~")
    assert resultado["ok"] is True
    assert resultado["path"] == os.path.realpath(arbol)
    assert [e["name"] for e in resultado["entries"]] == ["Alfa", "beta", "repo"]
    assert resultado["parent"] == os.path.dirname(os.path.realpath(arbol))
    assert resultado["truncated"] is False


def test_marca_los_repos_git_con_su_rama(arbol):
    entradas = {e["name"]: e for e in carpetas.listar(str(arbol), inicio="~")["entries"]}
    assert entradas["repo"]["git"] is True and entradas["repo"]["branch"] == "feat/x"
    assert entradas["Alfa"]["git"] is False and entradas["Alfa"]["branch"] is None
    dentro = carpetas.listar(str(arbol / "repo"), inicio="~")
    assert dentro["git"] is True and dentro["branch"] == "feat/x"


def test_un_worktree_con_archivo_git_tambien_cuenta(tmp_path):
    real = tmp_path / "real-git"
    real.mkdir()
    (real / "HEAD").write_text("ref: refs/heads/main\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real}\n")
    assert carpetas.rama_git(str(worktree)) == "main"


def test_sin_ruta_devuelve_las_raices(arbol):
    resultado = carpetas.listar(None, inicio=str(arbol))
    assert resultado["path"] == "" and resultado["parent"] is None
    assert resultado["default"] == str(arbol)
    nombres = [e["name"] for e in resultado["entries"]]
    if os.name == "nt":
        assert os.path.splitdrive(os.getcwd())[0].upper() + "\\" in nombres
    else:
        assert nombres == ["/"]


def test_desde_una_raiz_el_padre_son_las_raices():
    raiz = os.path.abspath(os.sep)
    assert carpetas.listar(raiz, inicio="~")["parent"] == ""


@pytest.mark.parametrize("ruta,codigo", [("relativa/x", "invalid"), ("a\x00b", "invalid"), ("x" * 2000, "invalid")])
def test_rutas_invalidas(ruta, codigo):
    with pytest.raises(ErrorCarpeta) as error:
        carpetas.listar(ruta, inicio="~")
    assert error.value.codigo == codigo


def test_carpeta_inexistente(tmp_path):
    with pytest.raises(ErrorCarpeta) as error:
        carpetas.listar(str(tmp_path / "no-esta"), inicio="~")
    assert error.value.codigo == "not_found"


def test_tope_de_entradas(tmp_path, monkeypatch):
    monkeypatch.setattr(carpetas, "MAX_ENTRADAS", 3)
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
    resultado = carpetas.listar(str(tmp_path), inicio="~")
    assert len(resultado["entries"]) == 3 and resultado["truncated"] is True


async def test_el_bridge_responde_el_frame_dirs_con_su_request_id(arbol):
    bridge = Bridge(Config(url="ws://x", token="t", workdir=str(arbol)), arbol / "estado")
    enviados = []

    async def enviar(mensaje):
        enviados.append(mensaje)

    bridge.enviar = enviar
    await bridge._carpetas({"type": "dirs", "request_id": "r1", "path": str(arbol)})
    await bridge._carpetas({"type": "dirs", "request_id": "r2", "path": str(arbol / "no-esta")})

    assert enviados[0]["type"] == "dirs_result" and enviados[0]["request_id"] == "r1"
    assert enviados[0]["result"]["ok"] is True
    assert enviados[0]["result"]["default"] == os.path.abspath(arbol)
    assert enviados[1]["result"] == {"ok": False, "error": "Esa carpeta no existe en el servidor.", "code": "not_found"}


def test_run_sin_cwd_usa_la_carpeta_del_bridge():
    assert validar_run(run(SESION, "hola")).cwd is None
    assert validar_run(run(SESION, "hola", cwd="")).cwd is None


def test_run_con_cwd_valido(tmp_path):
    assert validar_run(run(SESION, "hola", cwd=str(tmp_path))).cwd == os.path.realpath(tmp_path)


@pytest.mark.parametrize("cwd", ["relativa", 123])
def test_run_con_cwd_invalido(cwd):
    with pytest.raises(RunInvalido):
        validar_run(run(SESION, "hola", cwd=cwd))


def test_run_con_cwd_que_no_existe(tmp_path):
    with pytest.raises(RunInvalido, match="no existe"):
        validar_run(run(SESION, "hola", cwd=str(tmp_path / "no-esta")))


def test_la_sesion_se_busca_en_la_carpeta_del_turno(tmp_path):
    cfg = Config(url="ws://x", token="t", config_dir=str(tmp_path / "claude"), workdir=str(tmp_path))
    proyecto = tmp_path / "proyecto"
    proyecto.mkdir()
    carpeta_sesiones = tmp_path / "claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(proyecto))
    carpeta_sesiones.mkdir(parents=True)
    (carpeta_sesiones / f"{SESION}.jsonl").write_text("{}")

    assert hay_sesion(cfg, SESION, os.path.realpath(proyecto)) is True
    assert hay_sesion(cfg, SESION) is False

    turno = validar_run(run(SESION, "seguimos", cwd=str(proyecto), fallback="hilo completo"))
    argv = construir_comando(cfg, turno, None).argv
    assert argv[-3:] == ["--resume", SESION, "seguimos"]

    sin_cwd = validar_run(run(SESION, "seguimos", fallback="hilo completo"))
    assert construir_comando(cfg, sin_cwd, None).argv[-3:] == ["--session-id", SESION, "hilo completo"]
