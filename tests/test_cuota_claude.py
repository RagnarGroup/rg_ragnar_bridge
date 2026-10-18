"""Cuota de Claude Code: se lee del endpoint de uso de Anthropic con el token
OAuth del CLI; si vencio lo renueva el propio CLI, y no se consulta mas de una
vez cada 5 min ni durante un 429."""

import io
import json
import time
import urllib.error

import pytest

from ragnar_bridge.agents import claude as modulo
from ragnar_bridge.agents.claude import ClaudeAdaptador
from ragnar_bridge.config import Config


@pytest.fixture(autouse=True)
def _cuota_limpia():
    modulo._estado_cuota.clear()
    yield
    modulo._estado_cuota.clear()


def _uso(utilizacion=10.0):
    return io.BytesIO(json.dumps({"five_hour": {"utilization": utilizacion, "resets_at": None}}).encode())


def _adaptador(tmp_path, credenciales=None):
    carpeta = tmp_path / "claude"
    carpeta.mkdir()
    if credenciales is not None:
        (carpeta / ".credentials.json").write_text(json.dumps(credenciales), encoding="utf-8")
    return ClaudeAdaptador(Config(url="ws://x", token="t", config_dir=str(carpeta)), tmp_path)


def test_cuota_de_claude_se_normaliza_a_bloques(tmp_path, monkeypatch):
    pedidos = []

    def urlopen(pedido, timeout):
        pedidos.append(pedido)
        cuerpo = {
            "five_hour": {"utilization": 12.4, "resets_at": "2026-10-18T11:59:59.638831+00:00"},
            "seven_day": {"utilization": 49.0, "resets_at": "2026-10-19T00:59:59.638856+00:00"},
            "seven_day_opus": None,
            "seven_day_sonnet": {"utilization": 3.0, "resets_at": None},
        }
        return io.BytesIO(json.dumps(cuerpo).encode())

    monkeypatch.setattr(modulo.urllib.request, "urlopen", urlopen)
    adaptador = _adaptador(tmp_path, {"claudeAiOauth": {"accessToken": "tok", "subscriptionType": "max"}})
    assert adaptador.cuota("2.1.0") == {
        "plan": "max",
        "bloques": [
            {"grupo": None, "ventana": "5h", "usado": 12, "resetea": "2026-10-18T11:59:59Z"},
            {"grupo": None, "ventana": "semana", "usado": 49, "resetea": "2026-10-19T00:59:59Z"},
            {"grupo": "Sonnet", "ventana": "semana", "usado": 3, "resetea": None},
        ],
    }
    assert pedidos[0].get_header("Authorization") == "Bearer tok"


def test_sin_credenciales_la_cuota_es_none_y_no_pide_nada(tmp_path, monkeypatch):
    def urlopen(*a, **kw):
        raise AssertionError("no deberia pedir nada")

    monkeypatch.setattr(modulo.urllib.request, "urlopen", urlopen)
    assert _adaptador(tmp_path).cuota("2.1.0") is None


def test_si_anthropic_rechaza_el_token_la_cuota_es_none(tmp_path, monkeypatch):
    def urlopen(pedido, timeout):
        raise urllib.error.HTTPError(pedido.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(modulo.urllib.request, "urlopen", urlopen)
    assert _adaptador(tmp_path, {"claudeAiOauth": {"accessToken": "vencido"}}).cuota("2.1.0") is None


def test_no_consulta_de_nuevo_antes_de_5_minutos(tmp_path, monkeypatch):
    llamadas = []
    monkeypatch.setattr(modulo.urllib.request, "urlopen", lambda p, timeout: llamadas.append(p) or _uso())
    adaptador = _adaptador(tmp_path, {"claudeAiOauth": {"accessToken": "tok"}})
    primera = adaptador.cuota("2.1.0")
    assert adaptador.cuota("2.1.0") == primera
    assert len(llamadas) == 1


def test_un_429_devuelve_la_ultima_cuota_y_espera_retry_after(tmp_path, monkeypatch):
    respuestas = [_uso(20.0)]

    def urlopen(pedido, timeout):
        if respuestas:
            return respuestas.pop()
        raise urllib.error.HTTPError(pedido.full_url, 429, "Too Many", {"Retry-After": "1800"}, io.BytesIO(b""))

    monkeypatch.setattr(modulo.urllib.request, "urlopen", urlopen)
    adaptador = _adaptador(tmp_path, {"claudeAiOauth": {"accessToken": "tok"}})
    buena = adaptador.cuota("2.1.0")
    assert buena["bloques"][0]["usado"] == 20
    modulo._estado_cuota[adaptador.cfg.config_dir_abs]["proxima"] = 0
    assert adaptador.cuota("2.1.0") == buena
    assert modulo._estado_cuota[adaptador.cfg.config_dir_abs]["proxima"] >= time.time() + 1700


def test_un_token_vencido_lo_renueva_el_cli_antes_de_consultar(tmp_path, monkeypatch):
    vencido = int((time.time() - 3600) * 1000)
    adaptador = _adaptador(tmp_path, {"claudeAiOauth": {"accessToken": "viejo", "expiresAt": vencido}})
    archivo = tmp_path / "claude" / ".credentials.json"
    comandos = []

    def run(argv, **kw):
        comandos.append(argv)
        nuevo = int((time.time() + 3600) * 1000)
        archivo.write_text(json.dumps({"claudeAiOauth": {"accessToken": "nuevo", "expiresAt": nuevo}}), encoding="utf-8")

    pedidos = []
    monkeypatch.setattr(modulo.subprocess, "run", run)
    monkeypatch.setattr(modulo.urllib.request, "urlopen", lambda p, timeout: pedidos.append(p) or _uso())
    assert adaptador.cuota("2.1.0") is not None
    assert comandos and "-p" in comandos[0] and "haiku" in comandos[0]
    assert pedidos[0].get_header("Authorization") == "Bearer nuevo"

