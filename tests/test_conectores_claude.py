"""Espera de los conectores de claude.ai: en -p llegan en segundo plano, asi que cada
turno de Claude arranca con un hook SessionStart que espera `claude_espera_conectores`
segundos, junto con los permisos concedidos en la conversacion."""

import json

import pytest

from ragnar_bridge.agents.claude import conceder_permiso, escribir_ajustes, ruta_permisos
from ragnar_bridge.config import ConfigError, cargar

SESION = "11111111-1111-1111-1111-111111111111"


def _hook(ruta):
    datos = json.loads(open(ruta, encoding="utf-8").read())
    return datos["hooks"]["SessionStart"][0]["hooks"][0]["command"], datos


def test_sin_espera_queda_como_antes(tmp_path):
    assert escribir_ajustes(tmp_path, SESION, 0) is None
    conceder_permiso(tmp_path, SESION, "Bash(docker ps:*)")
    assert escribir_ajustes(tmp_path, SESION, 0) == str(ruta_permisos(tmp_path, SESION))


def test_la_espera_va_como_hook_con_los_permisos_de_la_conversacion(tmp_path):
    comando, datos = _hook(escribir_ajustes(tmp_path, SESION, 6))
    assert comando == "sleep 6" and "permissions" not in datos

    conceder_permiso(tmp_path, SESION, "Bash(docker ps:*)")
    comando, datos = _hook(escribir_ajustes(tmp_path, SESION, 2.5))
    assert comando == "sleep 2.5"
    assert datos["permissions"]["allow"] == ["Bash(docker ps:*)"]
    assert "hooks" not in json.loads(ruta_permisos(tmp_path, SESION).read_text())


@pytest.mark.parametrize("valor", [-1, 61, "6", True])
def test_la_espera_se_valida(tmp_path, valor):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"url": "ws://x", "token": "t", "claude_espera_conectores": valor}))
    with pytest.raises(ConfigError):
        cargar(ruta)
    ruta.write_text(json.dumps({"url": "ws://x", "token": "t"}))
    assert cargar(ruta).claude_espera_conectores == 6.0
