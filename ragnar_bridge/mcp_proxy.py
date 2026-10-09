"""Puente MCP stdio -> HTTP para las tools de tickets de Ragnar en `agy`.

Claude Code acepta un archivo de MCP por invocacion (`--mcp-config`), asi que
cada turno lleva su propio token. `agy` no: sus servidores MCP viven en UN
archivo global (`~/.gemini/config/mcp_config.json`) y sus cabeceras HTTP no
expanden variables de entorno (probado: manda el literal `${VAR}`). Con un
token fijo ahi, todo el grupo actuaria como una sola persona.

Salida: se registra UNA vez este proceso como servidor stdio, y el bridge le
pasa el token de cada turno por el ENTORNO del propio `agy` (un servidor stdio
hereda el entorno de quien lo lanza; tambien probado). Asi el token nunca se
escribe en disco y dos personas hablando a la vez no se pisan.

Solo libreria estandar: corre con el mismo Python que el bridge. Variables:

  RAGNAR_TICKETS_URL     https://<panel>/api/v1/mcp
  RAGNAR_TICKETS_TOKEN   token de turno del usuario que escribio
  RAGNAR_TICKETS_AGENT   nombre que queda en el historial (opcional)

Sin URL o sin token no hay tools: el servidor arranca igual (agy espera el
saludo MCP y, si nunca llega, se queda colgado) pero anuncia una lista vacia.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Iterator, List, Optional
from urllib.parse import urljoin, urlsplit

VERSION_PROTOCOLO = "2025-06-18"
TIMEOUT = 120
# Tope de una respuesta de Ragnar: mas que esto no es una respuesta de tickets
# sana, y leerla entera sin limite podia comerse la RAM (RAG-208).
TOPE_RESPUESTA = 16 * 1024 * 1024


def _error(id_, mensaje: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32000, "message": mensaje}}


class Puente:
    def __init__(self, url: Optional[str], token: Optional[str], agente: str):
        self.url = url
        self.token = token
        self.agente = agente
        self.sesion: Optional[str] = None
        self.protocolo: Optional[str] = None

    # ---- sin token: un servidor MCP valido y vacio

    def _vacio(self, msg: dict) -> Optional[dict]:
        id_ = msg.get("id")
        if id_ is None:
            return None  # notificacion
        metodo = msg.get("method")
        if metodo == "initialize":
            pedido = (msg.get("params") or {}).get("protocolVersion") or VERSION_PROTOCOLO
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {
                    "protocolVersion": pedido,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ragnar-tickets", "version": "sin-token"},
                },
            }
        if metodo == "tools/list":
            return {"jsonrpc": "2.0", "id": id_, "result": {"tools": []}}
        if metodo == "ping":
            return {"jsonrpc": "2.0", "id": id_, "result": {}}
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "Sin acceso a tickets en este turno."}}

    # ---- con token: se reenvia tal cual a Ragnar

    def _cabeceras(self) -> dict:
        cab = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "X-Agent-Name": self.agente,
        }
        if self.sesion:
            cab["Mcp-Session-Id"] = self.sesion
        if self.protocolo:
            cab["MCP-Protocol-Version"] = self.protocolo
        return cab

    @staticmethod
    def _eventos_sse(cuerpo: str) -> Iterator[str]:
        datos: List[str] = []
        for linea in cuerpo.splitlines():
            if not linea:
                if datos:
                    yield "\n".join(datos)
                    datos = []
            elif linea.startswith("data:"):
                datos.append(linea[5:].lstrip(" "))
        if datos:
            yield "\n".join(datos)

    def _post(self, msg: dict):
        """POST con hasta 3 redirecciones 307/308 del MISMO servidor (Starlette
        redirige /api/v1/mcp a /api/v1/mcp/): urllib no las sigue en un POST, y
        una hacia OTRO host no se sigue jamas, para no mandarle el token."""
        cuerpo = json.dumps(msg).encode("utf-8")
        for _ in range(4):
            peticion = urllib.request.Request(self.url, data=cuerpo, headers=self._cabeceras(), method="POST")
            try:
                return urllib.request.urlopen(peticion, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                destino = e.headers.get("Location") if e.code in (307, 308) else None
                if not destino:
                    raise
                nuevo = urljoin(self.url, destino)
                if urlsplit(nuevo)[:2] != urlsplit(self.url)[:2]:
                    raise
                self.url = nuevo
        raise urllib.error.URLError("demasiadas redirecciones")

    def _reenviar(self, msg: dict) -> List[dict]:
        try:
            with self._post(msg) as resp:
                if resp.headers.get("Mcp-Session-Id"):
                    self.sesion = resp.headers["Mcp-Session-Id"]
                crudo = resp.read(TOPE_RESPUESTA + 1)
                tipo = (resp.headers.get("Content-Type") or "").lower()
            if len(crudo) > TOPE_RESPUESTA:
                if msg.get("id") is None:
                    return []
                return [_error(msg["id"], f"La respuesta de Ragnar supera {TOPE_RESPUESTA // (1024 * 1024)} MiB.")]
            cuerpo = crudo.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if msg.get("id") is None:
                return []
            detalle = e.read(200).decode("utf-8", errors="replace")
            return [_error(msg["id"], f"Ragnar respondio {e.code}: {detalle}")]
        except (urllib.error.URLError, OSError) as e:
            if msg.get("id") is None:
                return []
            return [_error(msg["id"], f"No se pudo hablar con Ragnar: {e}")]

        if msg.get("method") == "initialize":
            try:
                self.protocolo = json.loads(self._primer_json(cuerpo, tipo))["result"]["protocolVersion"]
            except (ValueError, KeyError, TypeError):
                pass

        if not cuerpo.strip():
            return []
        trozos = self._eventos_sse(cuerpo) if "text/event-stream" in tipo else [cuerpo]
        salida: List[dict] = []
        for trozo in trozos:
            try:
                dato = json.loads(trozo)
            except ValueError:
                continue
            salida.extend(dato if isinstance(dato, list) else [dato])
        return salida

    def _primer_json(self, cuerpo: str, tipo: str) -> str:
        if "text/event-stream" in tipo:
            for trozo in self._eventos_sse(cuerpo):
                return trozo
        return cuerpo

    def responder(self, msg: dict) -> List[dict]:
        if not self.url or not self.token:
            r = self._vacio(msg)
            return [r] if r else []
        return self._reenviar(msg)


def bucle(puente: Puente, entrada, salida) -> None:
    for linea in entrada:
        linea = linea.strip()
        if not linea:
            continue
        try:
            msg = json.loads(linea)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        for respuesta in puente.responder(msg):
            salida.write(json.dumps(respuesta, ensure_ascii=False) + "\n")
            salida.flush()


def main() -> None:
    puente = Puente(
        os.environ.get("RAGNAR_TICKETS_URL") or None,
        os.environ.get("RAGNAR_TICKETS_TOKEN") or None,
        os.environ.get("RAGNAR_TICKETS_AGENT") or "agy",
    )
    # El protocolo es UTF-8 con independencia de la consola (en Windows seria cp1252).
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    bucle(puente, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
