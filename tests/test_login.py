"""Login de los CLIs desde la app: el link y el codigo de dispositivo llegan en
`login_state`, el codigo pegado va al CLI, el que queda abierto se cierra al ver la
sesion, un codigo malo termina en error y cancelar corta el proceso. El logout cierra
la sesion y avisa `desconectado`, o `error` si falla, vence o el CLI no lo admite."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from ragnar_bridge import login as modulo_login
from ragnar_bridge.agents import sondear
from ragnar_bridge.agents.base import Adaptador, Comando, RecetaLogin, patron
from ragnar_bridge.config import Config

FAKE = str(Path(__file__).parent / "fake_login.py")
posix = pytest.mark.skipif(os.name != "posix", reason="pseudo-terminales: POSIX-only")

RECETAS = {
    "codigo": dict(patron_url=patron(r"https://\S*/oauth/authorize\?\S+"), pide_codigo=True),
    "dispositivo": dict(
        patron_url=patron(r"https://auth\.openai\.com/\S+"),
        patron_codigo=patron(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b"),
        pide_codigo=False,
    ),
    "menu": dict(
        patron_url=patron(r"https://accounts\.google\.com/o/oauth2/auth\?\S+"),
        pide_codigo=True,
        respuestas=(("Select login method", "\r"),),
        sale_solo=False,
    ),
}


class _Falso(Adaptador):
    def __init__(self, modo, sesion: Path):
        self.modo = modo
        self.sesion = sesion

    @property
    def cmd(self):
        return [sys.executable]

    def sesion_iniciada(self):
        return self.sesion.exists()

    def receta_login(self):
        return RecetaLogin(argv=[sys.executable, FAKE, self.modo, str(self.sesion)], env=dict(os.environ), **RECETAS[self.modo])


async def _logins(tmp_path, modo):
    avisos, sondeos = [], []

    async def avisar(m):
        avisos.append(m)

    async def al_terminar():
        sondeos.append(True)

    adaptador = _Falso(modo, tmp_path / "sesion")
    return modulo_login.Logins({"cli": adaptador}, avisar, al_terminar), avisos, sondeos


async def _hasta(avisos, estado, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if any(a.get("estado") == estado for a in avisos):
            return next(a for a in avisos if a.get("estado") == estado)
        await asyncio.sleep(0.05)
    raise AssertionError(f"nunca llego {estado}: {avisos}")


@posix
async def test_el_link_llega_y_el_codigo_pegado_inicia_sesion(tmp_path):
    logins, avisos, sondeos = await _logins(tmp_path, "codigo")
    await logins.atender({"agent": "cli", "op": "start"})

    esperando = await _hasta(avisos, "esperando")
    assert esperando["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    assert esperando["pide_codigo"] is True
    await logins.atender({"agent": "cli", "op": "code", "code": "BIEN"})

    await _hasta(avisos, "listo")
    for _ in range(40):
        if sondeos:
            break
        await asyncio.sleep(0.05)
    assert sondeos == [True]


@posix
async def test_un_codigo_malo_termina_en_error(tmp_path):
    logins, avisos, _ = await _logins(tmp_path, "codigo")
    await logins.atender({"agent": "cli", "op": "start"})
    await _hasta(avisos, "esperando")
    await logins.atender({"agent": "cli", "op": "code", "code": "MAL"})

    error = await _hasta(avisos, "error")
    assert "Invalid code" in error["mensaje"]


@posix
async def test_el_codigo_de_dispositivo_llega_y_el_cli_termina_solo(tmp_path):
    logins, avisos, _ = await _logins(tmp_path, "dispositivo")
    await logins.atender({"agent": "cli", "op": "start"})

    listo = await _hasta(avisos, "listo")
    assert listo["url"] == "https://auth.openai.com/codex/device"
    assert listo["codigo"] == "HFYR-H2CPF"
    assert listo["pide_codigo"] is False


@posix
async def test_el_cli_que_queda_abierto_se_cierra_al_ver_la_sesion(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo_login, "CADA_CUANTO_REVISA", 0.1)
    logins, avisos, _ = await _logins(tmp_path, "menu")
    await logins.atender({"agent": "cli", "op": "start"})

    esperando = await _hasta(avisos, "esperando")
    assert esperando["url"].startswith("https://accounts.google.com/o/oauth2/auth?")
    await logins.atender({"agent": "cli", "op": "code", "code": "BIEN"})
    await _hasta(avisos, "listo")


@posix
async def test_cancelar_corta_el_login(tmp_path):
    logins, avisos, sondeos = await _logins(tmp_path, "codigo")
    await logins.atender({"agent": "cli", "op": "start"})
    await _hasta(avisos, "esperando")

    await logins.atender({"agent": "cli", "op": "cancel"})
    await _hasta(avisos, "cancelado")
    assert sondeos == []


async def test_un_cli_sin_login_o_un_codigo_sin_login_avisan_error(tmp_path):
    logins, avisos, _ = await _logins(tmp_path, "codigo")
    await logins.atender({"agent": "otro", "op": "start"})
    await logins.atender({"agent": "cli", "op": "code", "code": "BIEN"})
    assert [a["estado"] for a in avisos] == ["error", "error"]


def test_el_sondeo_dice_que_cli_se_conecta_desde_la_app(tmp_path):
    cfg = Config(url="ws://x", token="t", agents=["claude", "codex", "agy", "hermes", "cursor"])
    from ragnar_bridge.agents import crear_adaptadores

    adaptadores = crear_adaptadores(cfg, tmp_path)
    for nombre, adaptador in adaptadores.items():
        adaptador.encontrado = lambda: True
        adaptador.version = lambda: "1.0"
        adaptador.sesion_iniciada = lambda: False
    conectables = {a["name"]: a["conectable"] for a in sondear(adaptadores)}
    posix_ = os.name == "posix"
    assert conectables == {"claude": posix_, "codex": posix_, "agy": posix_, "hermes": False, "cursor": posix_}


class _ConLogout(_Falso):
    def __init__(self, modo, sesion: Path, modo_logout):
        super().__init__(modo, sesion)
        self.modo_logout = modo_logout

    def receta_logout(self):
        if self.modo_logout is None:
            return None
        return Comando([sys.executable, FAKE, self.modo_logout, str(self.sesion)], dict(os.environ))


async def _logouts(tmp_path, modo_logout):
    avisos, sondeos = [], []

    async def avisar(m):
        avisos.append(m)

    async def al_terminar():
        sondeos.append(True)

    sesion = tmp_path / "sesion"
    sesion.write_text("ok")
    adaptador = _ConLogout("codigo", sesion, modo_logout)
    return modulo_login.Logins({"cli": adaptador}, avisar, al_terminar), avisos, sondeos, sesion


async def test_logout_cierra_la_sesion_y_avisa_desconectado(tmp_path):
    logins, avisos, sondeos, sesion = await _logouts(tmp_path, "logout")
    await logins.atender({"agent": "cli", "op": "logout"})

    assert not sesion.exists()
    assert [a["estado"] for a in avisos] == ["desconectando", "desconectado"]
    assert all(a["type"] == "login_state" and a["agent"] == "cli" for a in avisos)
    assert sondeos == [True]


async def test_un_logout_que_falla_avisa_error_con_el_motivo(tmp_path):
    logins, avisos, sondeos, sesion = await _logouts(tmp_path, "logout-falla")
    await logins.atender({"agent": "cli", "op": "logout"})

    assert sesion.exists()
    assert avisos[-1]["estado"] == "error"
    assert "could not reach the keyring" in avisos[-1]["mensaje"]
    assert sondeos == [True]


async def test_un_logout_colgado_vence(tmp_path, monkeypatch):
    monkeypatch.setattr(modulo_login, "TOPE_LOGOUT", 0.5)
    logins, avisos, _, _ = await _logouts(tmp_path, "logout-cuelga")
    await logins.atender({"agent": "cli", "op": "logout"})

    assert avisos[-1] == {"type": "login_state", "agent": "cli", "estado": "error", "mensaje": "El logout no termino a tiempo."}


async def test_un_cli_sin_logout_avisa_que_no_se_puede_desconectar(tmp_path):
    logins, avisos, sondeos, _ = await _logouts(tmp_path, None)
    await logins.atender({"agent": "cli", "op": "logout"})
    await logins.atender({"agent": "otro", "op": "logout"})

    assert [a["mensaje"] for a in avisos] == ["Este CLI no se puede desconectar desde la app."] * 2
    assert sondeos == []


@posix
async def test_logout_corta_el_login_en_curso(tmp_path):
    logins, avisos, _, sesion = await _logouts(tmp_path, "logout")
    sesion.unlink()
    await logins.atender({"agent": "cli", "op": "start"})
    await _hasta(avisos, "esperando")

    await logins.atender({"agent": "cli", "op": "logout"})
    assert [a["estado"] for a in avisos][-3:] == ["cancelado", "desconectando", "desconectado"]


def test_las_recetas_de_logout_de_cada_cli(tmp_path):
    cfg = Config(url="ws://x", token="t", agents=["claude", "codex", "agy", "hermes", "cursor"])
    from ragnar_bridge.agents import crear_adaptadores

    adaptadores = crear_adaptadores(cfg, tmp_path)
    argvs = {n: (a.receta_logout().argv if a.receta_logout() else None) for n, a in adaptadores.items()}
    assert argvs == {
        "claude": [*cfg.claude_cmd, "auth", "logout"],
        "codex": [*cfg.codex_cmd, "logout"],
        "cursor": [*cfg.cursor_cmd, "logout"],
        "agy": None,
        "hermes": None,
    }
    for adaptador in adaptadores.values():
        adaptador.encontrado = lambda: True
        adaptador.version = lambda: "1.0"
        adaptador.sesion_iniciada = lambda: True
    desconectables = {a["name"]: a["desconectable"] for a in sondear(adaptadores)}
    assert desconectables == {"claude": True, "codex": True, "agy": False, "hermes": False, "cursor": True}
