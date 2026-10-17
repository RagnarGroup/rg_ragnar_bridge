"""mcp_proxy: el puente stdio -> HTTP que le da a agy las tools de tickets con el
token de CADA turno (agy no puede llevarlo en su archivo global de MCP)."""

import io
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ragnar_bridge.agents.agy import AgyAdaptador
from ragnar_bridge.config import Config
from ragnar_bridge.mcp_proxy import Puente, bucle
from ragnar_bridge.protocolo import validar_run


class _Ragnar(BaseHTTPRequestHandler):
    visto: list = []
    modo = "json"

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/v1/mcp":
            self.send_response(307)
            self.send_header("Location", "/api/v1/mcp/")
            self.end_headers()
            return
        type(self).visto.append({"auth": self.headers.get("Authorization"), "agente": self.headers.get("X-Agent-Name"), "msg": cuerpo})
        if "id" not in cuerpo:
            self.send_response(202)
            self.end_headers()
            return
        respuesta = json.dumps({"jsonrpc": "2.0", "id": cuerpo["id"], "result": {"eco": cuerpo["method"], "protocolVersion": "2025-06-18"}})
        if self.path.endswith("/error/"):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"no")
            return
        self.send_response(200)
        if type(self).modo == "sse":
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {respuesta}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(respuesta.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def ragnar_http():
    _Ragnar.visto = []
    _Ragnar.modo = "json"
    srv = HTTPServer(("127.0.0.1", 0), _Ragnar)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _correr(puente: Puente, *mensajes: dict) -> list:
    salida = io.StringIO()
    bucle(puente, io.StringIO("".join(json.dumps(m) + "\n" for m in mensajes)), salida)
    return [json.loads(l) for l in salida.getvalue().splitlines()]


def test_reenvia_con_el_token_y_sigue_la_redireccion(ragnar_http):
    puente = Puente(f"{ragnar_http}/api/v1/mcp", "ragagt_turno", "agy")
    r = _correr(puente, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r[0]["result"]["eco"] == "initialize"
    assert _Ragnar.visto[0]["auth"] == "Bearer ragagt_turno"
    assert _Ragnar.visto[0]["agente"] == "agy"


def test_las_notificaciones_no_generan_respuesta(ragnar_http):
    puente = Puente(f"{ragnar_http}/api/v1/mcp", "ragagt_turno", "agy")
    assert _correr(puente, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_entiende_una_respuesta_sse(ragnar_http):
    _Ragnar.modo = "sse"
    puente = Puente(f"{ragnar_http}/api/v1/mcp", "ragagt_turno", "agy")
    r = _correr(puente, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert r[0]["id"] == 7 and r[0]["result"]["eco"] == "tools/list"


def test_un_error_http_llega_como_error_jsonrpc(ragnar_http):
    puente = Puente(f"{ragnar_http}/api/v1/mcp/error/", "ragagt_turno", "agy")
    r = _correr(puente, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert r[0]["id"] == 3 and "401" in r[0]["error"]["message"]


def test_no_sigue_una_redireccion_a_otro_servidor():
    class _Otro(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(307)
            self.send_header("Location", "http://otro.invalid/robar")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _Otro)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        puente = Puente(f"http://127.0.0.1:{srv.server_address[1]}/mcp", "ragagt_turno", "agy")
        r = _correr(puente, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "307" in r[0]["error"]["message"]
    finally:
        srv.shutdown()


def test_sin_token_es_un_servidor_mcp_valido_y_vacio():
    puente = Puente(None, None, "agy")
    r = _correr(
        puente,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert r[0]["result"]["protocolVersion"] == "2025-11-25"
    assert r[1] == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}


def _agy(tmp_path) -> AgyAdaptador:
    cfg = Config(url="wss://panel.ejemplo.app/api/v1/bridge/ws", token="ragbrg_x", agents=["agy"], agy_dir=str(tmp_path))
    adaptador = AgyAdaptador(cfg, tmp_path)
    adaptador._mcp_listo = True
    return adaptador


def _turno(**extra):
    return validar_run({"session_id": str(uuid.uuid4()), "prompt": "hola", **extra})


def test_el_entorno_de_cada_turno_lleva_su_propio_token(tmp_path):
    agy = _agy(tmp_path)
    ana = agy.entorno(_turno(tickets_token="ragagt_ana"))
    beto = agy.entorno(_turno(tickets_token="ragagt_beto"))
    assert ana["RAGNAR_TICKETS_TOKEN"] == "ragagt_ana"
    assert beto["RAGNAR_TICKETS_TOKEN"] == "ragagt_beto"
    assert ana["RAGNAR_TICKETS_URL"] == "https://panel.ejemplo.app/api/v1/mcp/"


def test_sin_token_de_turno_el_entorno_no_hereda_uno_viejo(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGNAR_TICKETS_TOKEN", "ragagt_de_otra_cosa")
    env = _agy(tmp_path).entorno(_turno(tickets_token=None))
    assert "RAGNAR_TICKETS_TOKEN" not in env


def test_si_agy_no_acepta_el_mcp_el_turno_sigue_sin_tickets(tmp_path):
    agy = _agy(tmp_path)
    agy._mcp_listo = False
    agy._mcp_intentos = 3
    env = agy.entorno(_turno(tickets_token="ragagt_ana"))
    assert "RAGNAR_TICKETS_TOKEN" not in env


def test_una_respuesta_mas_grande_que_el_tope_es_error(ragnar_http, monkeypatch):
    import ragnar_bridge.mcp_proxy as modulo

    monkeypatch.setattr(modulo, "TOPE_RESPUESTA", 10)
    puente = Puente(f"{ragnar_http}/api/v1/mcp", "ragagt_turno", "agy")
    r = _correr(puente, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
    assert r[0]["id"] == 4 and "supera" in r[0]["error"]["message"]
