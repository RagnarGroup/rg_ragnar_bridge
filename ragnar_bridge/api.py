"""Cliente minimo (urllib) de la API REST de Ragnar para `sync` y `publicar`.

Se autentica con el token personal de agente, leido siempre del entorno
(`RAGNAR_AGENT_TOKEN`) y nunca de argv, que queda visible en `ps` y el historial."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from . import __version__

API_POR_DEFECTO = "https://panel.ragnargroup.app/api/v1"
VARIABLE_TOKEN_AGENTE = "RAGNAR_AGENT_TOKEN"
VARIABLE_API = "RAGNAR_API_URL"
TIMEOUT = 30


class ErrorApi(Exception):
    def __init__(self, mensaje: str, status: Optional[int] = None):
        super().__init__(mensaje)
        self.status = status


def token_del_entorno() -> str:
    token = os.environ.get(VARIABLE_TOKEN_AGENTE, "").strip()
    if not token:
        raise ErrorApi(
            f"Falta el token personal de agente en la variable {VARIABLE_TOKEN_AGENTE} "
            "(se crea en la app de Ragnar, «Mi token de agente»)."
        )
    return token


def url_api(argumento: Optional[str]) -> str:
    url = (argumento or os.environ.get(VARIABLE_API) or API_POR_DEFECTO).rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ErrorApi("La URL de la API tiene que empezar con https:// (o http:// solo para pruebas locales).")
    return url


class ClienteRagnar:
    def __init__(self, base: str, token: str, agente: str = "ragnar-bridge"):
        self.base = base.rstrip("/")
        self._token = token
        self.agente = agente

    def _pedir(self, metodo: str, ruta: str, params: Optional[dict] = None, cuerpo=None):
        url = self.base + ruta
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        pedido = urllib.request.Request(url, data=datos, method=metodo)
        pedido.add_header("Authorization", f"Bearer {self._token}")
        pedido.add_header("X-Agent-Name", self.agente)
        pedido.add_header("Accept", "application/json")
        pedido.add_header("User-Agent", f"ragnar-bridge/{__version__}")
        if datos is not None:
            pedido.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT) as respuesta:
                crudo = respuesta.read()
        except urllib.error.HTTPError as error:
            raise ErrorApi(f"{metodo} {ruta}: {error.code} {_detalle(error)}", error.code) from None
        except (urllib.error.URLError, OSError) as error:
            raise ErrorApi(f"No pude conectar con {self.base}: {error}") from None
        try:
            return json.loads(crudo) if crudo else None
        except ValueError:
            raise ErrorApi(f"{metodo} {ruta}: la respuesta no es JSON.") from None

    def get(self, ruta: str, params: Optional[dict] = None):
        return self._pedir("GET", ruta, params)

    def post(self, ruta: str, cuerpo: dict):
        return self._pedir("POST", ruta, cuerpo=cuerpo)


def _detalle(error: urllib.error.HTTPError) -> str:
    try:
        cuerpo = json.loads(error.read() or b"{}")
    except ValueError:
        return error.reason or ""
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if isinstance(detalle, str):
        return detalle
    return json.dumps(detalle, ensure_ascii=False)[:300] if detalle else (error.reason or "")
