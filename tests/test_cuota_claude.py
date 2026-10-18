"""Cuota de Claude Code: se lee del endpoint de uso de Anthropic con el token
OAuth que el CLI dejo en su carpeta de config, sin lanzar ningun turno."""

import io
import json
import urllib.error

from ragnar_bridge.agents import claude as modulo
from ragnar_bridge.agents.claude import ClaudeAdaptador
from ragnar_bridge.config import Config


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
