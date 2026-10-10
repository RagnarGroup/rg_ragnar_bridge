"""Piezas compartidas por las suites: un Ragnar de mentira y el `run`."""

import asyncio
import json
from pathlib import Path

from ragnar_bridge import PROTOCOLO

FAKE = str(Path(__file__).parent / "fake_claude.py")
FAKE_AGY = str(Path(__file__).parent / "fake_agy.py")
FAKE_CODEX = str(Path(__file__).parent / "fake_codex.py")
FAKE_HERMES = str(Path(__file__).parent / "fake_hermes.py")
TOKEN = "ragbrg_test"


class FakeRagnar:
    """Servidor de Ragnar minimo: valida el `auth`, contesta `welcome` y deja
    al test mandar frames y leer lo que el bridge emite."""

    def __init__(self, token_valido=TOKEN, protocolo=PROTOCOLO):
        self.token_valido = token_valido
        self.protocolo = protocolo
        self.recibidos: asyncio.Queue = asyncio.Queue()
        self.auth_frame = None
        self._ws = None
        self.conectado = asyncio.Event()
        self.cerrado = asyncio.Event()

    async def _handler(self, ws):
        self._ws = ws
        frame = json.loads(await ws.recv())
        self.auth_frame = frame
        if frame.get("token") != self.token_valido:
            await ws.send(json.dumps({"type": "error", "code": "auth", "message": "no"}))
            await ws.close(code=1008)
            return
        if frame.get("protocol") != self.protocolo:
            await ws.send(json.dumps({"type": "error", "code": "protocol", "message": "viejo"}))
            await ws.close(code=1008)
            return
        await ws.send(json.dumps({"type": "welcome", "bridge_id": 1, "nombre": "t", "username": "gus", "protocol": PROTOCOLO}))
        self.conectado.set()
        try:
            async for crudo in ws:
                await self.recibidos.put(json.loads(crudo))
        finally:
            self.cerrado.set()

    async def enviar(self, msg: dict):
        await self._ws.send(json.dumps(msg))

    async def hasta_done(self, task_id: str, timeout=15):
        eventos = []
        while True:
            msg = await asyncio.wait_for(self.recibidos.get(), timeout)
            if msg.get("task_id") != task_id:
                continue
            if msg["type"] == "event":
                eventos.append(msg["raw"])
            else:
                return eventos, msg



def run(task_id, prompt="hola", fallback=None, **extra):
    return {
        "type": "run",
        "task_id": task_id,
        "session_id": task_id,
        "prompt": prompt,
        "prompt_fallback": fallback or prompt,
        "system_append": "PROTOCOLO",
        **extra,
    }
