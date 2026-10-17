"""Adaptador de Hermes Agent (`hermes`).

* Un turno es `hermes chat --query=<prompt> --oneshot --format stream-json` (con
  `--resume` para seguir); `TraductorHermes` convierte su JSONL al de Claude.
* `--query=` va pegado y sin stdin (queda en DEVNULL): un prompt que empiece con
  "-" no se lee como flag.
* Hermes asigna el session_id: se guarda en `hermes-estado.json` y se usa en `--resume`.
* Sin `--append-system-prompt`: las instrucciones van antepuestas al primer prompt.
* `tool_use`/`tool_result` no traen id: se generan por turno, emparejados en orden.
* Corre sus herramientas sin pedir aprobacion: no hay permisos que conceder.
* Sin MCP de tickets."""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from ..protocolo import Turno
from .base import Adaptador, Comando, Traductor

log = logging.getLogger("ragnar_bridge")

_SEPARADOR = "\n\n---\n\n"


class HermesAdaptador(Adaptador):
    nombre = "hermes"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.hermes_cmd

    def sesion_iniciada(self) -> Optional[bool]:
        return None

    def cuota(self, version: str) -> Optional[dict]:
        return None


    @property
    def _ruta_estado(self) -> Path:
        return self.estado_dir / "hermes-estado.json"

    def _leer(self) -> Dict[str, dict]:
        try:
            datos = json.loads(self._ruta_estado.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        sesiones = datos.get("sesiones") if isinstance(datos, dict) else None
        return sesiones if isinstance(sesiones, dict) else {}

    def _guardar(self, sesiones: Dict[str, dict]) -> None:
        self.estado_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._ruta_estado.with_name(self._ruta_estado.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps({"sesiones": sesiones}, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._ruta_estado)

    def actualizar_sesion(self, session_id: str, **campos) -> None:
        sesiones = self._leer()
        sesiones.setdefault(session_id, {}).update(campos)
        self._guardar(sesiones)


    def preparar(self, turno: Turno, username: str) -> Comando:
        if turno.conceder:
            log.warning(
                "Ragnar pidio conceder %r a Hermes, que no tiene permisos que conceder "
                "(corre sus herramientas sin pedir aprobacion): no cambia nada en este turno.",
                turno.conceder,
            )
        sesion = self._leer().get(turno.session_id, {})
        hermes_session = sesion.get("hermes_session_id")
        reanudar = bool(hermes_session)

        if reanudar:
            texto = turno.prompt
        elif turno.system_append:
            texto = turno.system_append + _SEPARADOR + turno.prompt_fallback
        else:
            texto = turno.prompt_fallback

        argv = [
            *self.cfg.hermes_cmd,
            "chat",
            f"--query={texto}",
            "--oneshot",
            "--format",
            "stream-json",
        ]
        if reanudar:
            argv += ["--resume", hermes_session]
        return Comando(argv, os.environ.copy())

    def traductor(self, turno: Turno) -> "TraductorHermes":
        return TraductorHermes(self, turno)


class TraductorHermes(Traductor):
    def __init__(self, adaptador: HermesAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.session_id: Optional[str] = None
        self._pendientes: List[str] = []
        self._contador = 0

    @staticmethod
    def _texto(texto: str) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": texto}},
        }

    def evento(self, linea: dict) -> List[dict]:
        tipo = linea.get("type")
        if tipo == "system":
            self.session_id = linea.get("session_id") or self.session_id
            return []
        if tipo == "tool_use":
            return self._tool_use(linea)
        if tipo == "tool_result":
            return self._tool_result(linea)
        if tipo == "text":
            texto = linea.get("text") or ""
            return [self._texto(texto)] if texto else []
        if tipo == "result":
            self.session_id = linea.get("session_id") or self.session_id
            return self._resultado(linea)
        return []

    def _tool_use(self, linea: dict) -> List[dict]:
        id_herramienta = f"hermes:{self._turno.session_id}:{self._contador}"
        self._contador += 1
        self._pendientes.append(id_herramienta)
        return [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": id_herramienta,
                            "name": linea.get("name") or "tool",
                            "input": linea.get("input") or {},
                        }
                    ]
                },
            }
        ]

    def _tool_result(self, linea: dict) -> List[dict]:
        id_herramienta = self._pendientes.pop(0) if self._pendientes else f"hermes:{self._turno.session_id}:huerfano"
        contenido = linea.get("output")
        if contenido is None:
            contenido = linea.get("error") or ""
        return [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": id_herramienta,
                            "content": str(contenido),
                            "is_error": bool(linea.get("is_error")),
                        }
                    ]
                },
            }
        ]

    def _resultado(self, linea: dict) -> List[dict]:
        tokens = linea.get("tokens") or {}
        cache = int(tokens.get("cache_read") or 0)
        codigo = linea.get("exit_code")
        es_error = codigo not in (None, 0)
        evento = {
            "type": "result",
            "is_error": es_error,
            "usage": {
                "input_tokens": max(int(tokens.get("input") or 0) - cache, 0),
                "output_tokens": int(tokens.get("output") or 0),
                "cache_read_input_tokens": cache,
                "cache_creation_input_tokens": int(tokens.get("cache_write") or 0),
            },
        }
        if es_error:
            evento["result"] = str(linea.get("text") or "Hermes termino con error.")
        return [evento]

    def fin(self, codigo: Optional[int], stderr: str) -> List[dict]:
        if self.session_id:
            self._ad.actualizar_sesion(self._turno.session_id, hermes_session_id=self.session_id)
        return []
