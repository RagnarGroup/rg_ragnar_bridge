"""Token y URL con que el agente de un turno habla con los tickets de Ragnar."""

from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .protocolo import Turno


def url_mcp_tickets(ws_url: str) -> Optional[str]:
    partes = urlsplit(ws_url)
    sufijo = "/bridge/ws"
    if not partes.path.endswith(sufijo):
        return None
    esquema = "https" if partes.scheme == "wss" else "http"
    ruta = partes.path[: -len(sufijo)] + "/mcp/"
    return urlunsplit((esquema, partes.netloc, ruta, "", ""))


def token_de_tickets(cfg: Config, turno: Turno) -> Optional[str]:
    if turno.token_de_turno:
        return turno.tickets_token
    return cfg.tickets_token
