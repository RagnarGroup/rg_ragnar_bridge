"""La cuota de los agentes solo se consulta con el probe de Ragnar (el /usage de la
app): al conectar y en los sondeos de rutina se conserva la ultima conocida."""

import json

from ragnar_bridge.bridge import Bridge
from ragnar_bridge.config import Config


class _AgenteFalso:
    def __init__(self, cuota):
        self.consultas = 0
        self._cuota = cuota

    def encontrado(self):
        return True

    def version(self):
        return "1.0"

    def sesion_iniciada(self):
        return True

    def cuota(self, version):
        self.consultas += 1
        return self._cuota


class _WsFalso:
    def __init__(self):
        self.enviados = []

    async def send(self, texto):
        self.enviados.append(json.loads(texto))


def _bridge(tmp_path, cuota):
    bridge = Bridge(Config(url="ws://x", token="t", workdir=str(tmp_path)), tmp_path)
    agente = _AgenteFalso(cuota)
    bridge.adaptadores = {"claude": agente}
    bridge._ws = _WsFalso()
    return bridge, agente


async def test_el_sondeo_de_rutina_no_consulta_la_cuota(tmp_path):
    bridge, agente = _bridge(tmp_path, {"plan": "team", "bloques": []})
    await bridge._sondear_y_avisar(con_cuota=False)
    assert agente.consultas == 0
    assert bridge._ws.enviados[-1]["agents"][0].get("quota") is None


async def test_el_probe_consulta_la_cuota_y_la_rutina_la_conserva(tmp_path):
    cuota = {"plan": "team", "bloques": [{"grupo": None, "ventana": "5h", "usado": 3, "resetea": None}]}
    bridge, agente = _bridge(tmp_path, cuota)
    await bridge._sondear_y_avisar()
    assert agente.consultas == 1
    assert bridge._ws.enviados[-1]["agents"][0]["quota"] == cuota

    await bridge._sondear_y_avisar(con_cuota=False)
    assert agente.consultas == 1
    assert bridge._ws.enviados[-1]["agents"][0]["quota"] == cuota
