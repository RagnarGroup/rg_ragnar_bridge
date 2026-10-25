"""Login de los CLIs desde la app: el link y el codigo de dispositivo llegan en
`login_state`, el codigo pegado va al CLI, el que queda abierto se cierra al ver la
sesion, un codigo malo termina en error y cancelar corta el proceso."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from ragnar_bridge import login as modulo_login
from ragnar_bridge.agents import sondear
from ragnar_bridge.agents.base import Adaptador, RecetaLogin, patron
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
    cfg = Config(url="ws://x", token="t", agents=["claude", "codex", "agy", "hermes"])
    from ragnar_bridge.agents import crear_adaptadores

    adaptadores = crear_adaptadores(cfg, tmp_path)
    for nombre, adaptador in adaptadores.items():
        adaptador.encontrado = lambda: True
        adaptador.version = lambda: "1.0"
        adaptador.sesion_iniciada = lambda: False
    conectables = {a["name"]: a["conectable"] for a in sondear(adaptadores)}
    posix_ = os.name == "posix"
    assert conectables == {"claude": posix_, "codex": posix_, "agy": posix_, "hermes": False}
