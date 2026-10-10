"""Hermes Agent (`hermes`, github.com/NousResearch/hermes-agent).

Diferencias con Claude Code que el adaptador absorbe (verificado en vivo contra
hermes-agent 0.21.5, RAG-187; ver tests/test_hermes.py para las formas reales
de los eventos):

* Un turno es `hermes chat --query=<prompt> --oneshot --format stream-json`
  (o con `--resume <session_id>` para seguir una conversacion) y emite JSONL
  propio (`system`/`tool_use`/`tool_result`/`text`/`result`): `TraductorHermes`
  lo convierte al formato de Claude que Ragnar espera.
* `--query=<prompt>` (no `-q <prompt>`, ni via stdin): el bridge no le escribe
  nada al stdin del proceso (queda en DEVNULL), asi que `--query-file -` no
  sirve aca aunque el propio `hermes` lo recomiende para texto arbitrario. La
  forma pegada (`=`) evita que un prompt que empiece con "-" se lea como flag,
  igual que `-p=<prompt>` en agy.
* No hay `--session-id`: Hermes asigna el `session_id` y lo informa en el
  evento `system` (subtype `init`) y de nuevo en `result`. Se guarda la
  correspondencia session_id(Ragnar) -> session_id(Hermes) en
  `hermes-estado.json`; los turnos siguientes usan `--resume`.
* No hay `--append-system-prompt`: las instrucciones de Ragnar se anteponen al
  prompt del primer turno de cada conversacion (despues viven en la sesion).
* `tool_use`/`tool_result` no traen un id que los correlacione (a diferencia
  de Claude/Codex): se generan ids propios por turno, emparejados en el orden
  en que llegan (Hermes corre las herramientas de a una, sin interlevar).
* Sin `--yolo`: probado en vivo que un turno con una herramienta de shell
  corre igual en modo `--oneshot` sin pedir aprobacion (a diferencia de agy,
  que en headless deniega solo). Si en el futuro aparece una herramienta que
  si la pide, ahi se decide como Ragnar la concede -- no se fuerza el bypass
  de entrada como con Codex/agy.
* Tickets de Ragnar (MCP): fuera de alcance de este adaptador (ver RAG-187).
  Hermes corre sin las tools de tickets hasta que se decida como pasarle un
  token por turno.
"""

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
        """A diferencia de `codex login status`/`agy models`, Hermes no tiene
        un comando de solo-estado que distinga "listo para conversar" de
        "hay algo que mejorar". Probado en vivo (RAG-187): `hermes doctor`
        devuelve exit 1 por cosas irrelevantes para un turno (una dependencia
        opcional del navegador con una vulnerabilidad npm, proveedores que
        esta instancia ni usa) en una instalacion que SI podia conversar
        normalmente -- `False` ahi seria un aviso equivocado. Mejor no decir
        nada que decir algo mal."""
        return None

    def cuota(self, version: str) -> Optional[dict]:
        """Hermes no expone un comando de solo lectura para leer limites de
        cuota (a diferencia de `codex app-server`/`agy -p=/usage`), y el
        proveedor detras de una instancia puede ni ser uno con cuota por
        ventana (proxy/custom). Sin dato de cortesia por ahora."""
        return None

    # ---- estado propio (session_id de Ragnar -> session_id de Hermes)

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

    # ---- comando

    def preparar(self, turno: Turno, username: str) -> Comando:
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
    """Convierte el JSONL de `hermes chat --format stream-json` al stream-json
    de Claude:

      system (subtype init)     -> (guarda session_id)
      tool_use                  -> assistant / tool_use
      tool_result                -> user / tool_result
      text                        -> stream_event / content_block_delta / text_delta
      result                       -> result (con usage)
    """

    def __init__(self, adaptador: HermesAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.session_id: Optional[str] = None
        # tool_use/tool_result de Hermes no traen un id compartido: se generan
        # acá y se emparejan en orden de llegada (Hermes corre las
        # herramientas de a una, nunca en paralelo dentro de un turno).
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
