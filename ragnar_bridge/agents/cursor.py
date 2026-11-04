"""Adaptador de Cursor CLI (`agent`).

* Un turno es `agent --print --force --trust` con `stream-json` y salida parcial.
  `TraductorCursor` convierte ese NDJSON al de Claude Code.
* Cursor asigna el `session_id`: se guarda en `cursor-estado.json` y el turno
  siguiente lo retoma con `--resume`.
* Sin system prompt propio: las instrucciones van antepuestas al primer prompt.
* `--force` y `--sandbox disabled` evitan que el modo headless se quede pidiendo
  aprobacion. No hay tarjeta Autorizar.
* El login de la app es `agent login` con `NO_OPEN_BROWSER=1`: imprime el link
  y el proceso termina solo cuando el navegador completa la sesion.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..protocolo import _PATRON_UUID, Turno
from .base import Adaptador, Comando, RecetaLogin, Traductor, patron

log = logging.getLogger("ragnar_bridge")

_SEPARADOR = "\n\n---\n\n"
_TOPE_SALIDA = 8000
_TOPE_ENTRADA = 200
_CLAVES_SESION = ("loggedIn", "authenticated", "isAuthenticated", "logged_in")
_NO_SESION = ("not logged", "not authenticated", "no autentic")


def sesion_desde_status(stdout: str, stderr: str, codigo: int) -> Optional[bool]:
    texto = (stdout or "").strip()
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError:
        datos = None
    if isinstance(datos, dict):
        for clave in _CLAVES_SESION:
            if isinstance(datos.get(clave), bool):
                return datos[clave]
        usuario = datos.get("user") or datos.get("account") or datos.get("email")
        if isinstance(usuario, str) and usuario.strip():
            return True
        if isinstance(usuario, dict) and (usuario.get("email") or usuario.get("id")):
            return True
    bajo = f"{stdout}\n{stderr}".lower()
    if any(marca in bajo for marca in _NO_SESION):
        return False
    if "logged in" in bajo or "authenticated" in bajo:
        return True
    if codigo != 0:
        return False
    return None


class CursorAdaptador(Adaptador):
    nombre = "cursor"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.cursor_cmd

    def _entorno(self) -> Dict[str, str]:
        return os.environ.copy()

    def receta_login(self) -> RecetaLogin:
        env = self._entorno()
        env["NO_OPEN_BROWSER"] = "1"
        return RecetaLogin(
            argv=[*self.cmd, "login"],
            env=env,
            patron_url=patron(r"https://(?:[\w.-]*cursor[\w.-]*|authenticator\.cursor\.sh)/\S+"),
            pide_codigo=False,
        )

    def sesion_iniciada(self) -> Optional[bool]:
        try:
            salida = subprocess.run(
                [*self.cmd, "status", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=20,
                env=self._entorno(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return sesion_desde_status(salida.stdout, salida.stderr, salida.returncode)

    def cuota(self, version: str) -> Optional[dict]:
        return None

    @property
    def _ruta_estado(self) -> Path:
        return self.estado_dir / "cursor-estado.json"

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
                "Ragnar pidio conceder %r a Cursor, que corre con --force y no tiene "
                "permisos que conceder: no cambia nada en este turno.",
                turno.conceder,
            )
        guardada = self._leer().get(turno.session_id, {}).get("cursor_session_id")
        reanudar = isinstance(guardada, str) and bool(_PATRON_UUID.match(guardada))
        if reanudar:
            texto = turno.prompt
        elif turno.system_append:
            texto = turno.system_append + _SEPARADOR + turno.prompt_fallback
        else:
            texto = turno.prompt_fallback
        carpeta = turno.cwd or self.cfg.workdir_abs
        argv = [
            *self.cfg.cursor_cmd,
            "--print",
            "--force",
            "--trust",
            "--sandbox",
            "disabled",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--workspace",
            carpeta,
        ]
        if reanudar:
            argv += ["--resume", guardada]
        argv += ["--", texto]
        return Comando(argv, self._entorno())

    def traductor(self, turno: Turno) -> "TraductorCursor":
        return TraductorCursor(self, turno)


class TraductorCursor(Traductor):
    def __init__(self, adaptador: CursorAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.session_id: Optional[str] = None
        self._hubo_texto = False

    @staticmethod
    def _texto(texto: str) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": texto}},
        }

    def evento(self, linea: dict) -> List[dict]:
        tipo = linea.get("type")
        self.session_id = linea.get("session_id") or self.session_id
        if tipo == "assistant":
            return self._assistant(linea)
        if tipo == "tool_call":
            return self._herramienta(linea)
        if tipo == "result":
            return self._resultado(linea)
        return []

    def _assistant(self, linea: dict) -> List[dict]:
        parcial = "timestamp_ms" in linea and "model_call_id" not in linea
        completo = "timestamp_ms" not in linea and "model_call_id" not in linea and not self._hubo_texto
        if not parcial and not completo:
            return []
        salida = []
        for bloque in (linea.get("message") or {}).get("content") or []:
            if not isinstance(bloque, dict) or bloque.get("type") not in (None, "text"):
                continue
            texto = bloque.get("text") or ""
            if texto:
                salida.append(self._texto(texto))
        if salida and parcial:
            self._hubo_texto = True
        return salida

    def _herramienta(self, linea: dict) -> List[dict]:
        nombre, entrada, contenido, error = _describir(linea.get("tool_call") or {})
        id_llamada = str(linea.get("call_id") or f"cursor:{self._turno.session_id}")
        if linea.get("subtype") == "started":
            return [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "id": id_llamada, "name": nombre, "input": entrada}]
                    },
                }
            ]
        if linea.get("subtype") == "completed":
            return [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": id_llamada,
                                "content": contenido,
                                "is_error": error,
                            }
                        ]
                    },
                }
            ]
        return []

    def _resultado(self, linea: dict) -> List[dict]:
        es_error = linea.get("is_error") is True or linea.get("subtype") not in (None, "success")
        evento = {
            "type": "result",
            "is_error": es_error,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        if es_error:
            evento["result"] = str(linea.get("result") or "Cursor termino con error.")
        return [evento]

    def fin(self, codigo: Optional[int], stderr: str) -> List[dict]:
        if self.session_id and _PATRON_UUID.match(self.session_id):
            self._ad.actualizar_sesion(self._turno.session_id, cursor_session_id=self.session_id)
        return []


def _describir(tool_call: dict) -> Tuple[str, dict, str, bool]:
    if isinstance(tool_call.get("function"), dict):
        funcion = tool_call["function"]
        argumentos = funcion.get("arguments")
        if isinstance(argumentos, str):
            try:
                argumentos = json.loads(argumentos)
            except json.JSONDecodeError:
                argumentos = {"arguments": argumentos}
        if not isinstance(argumentos, dict):
            argumentos = {}
        return str(funcion.get("name") or "tool"), _entrada(argumentos), "", False
    for clave, valor in tool_call.items():
        if not isinstance(valor, dict):
            continue
        nombre = _nombre(clave)
        entrada = valor.get("args") if isinstance(valor.get("args"), dict) else {}
        contenido, error = _resultado_herramienta(valor.get("result"))
        return nombre, _entrada(entrada), contenido, error
    return "tool", {}, "", False


def _nombre(clave: str) -> str:
    base = clave[: -len("ToolCall")] if clave.endswith("ToolCall") else clave
    if base.lower() in ("shell", "bash"):
        return "Bash"
    separado = re.sub(r"([A-Z])", r" \1", base).strip()
    return separado.title().replace(" ", "") or "tool"


def _entrada(args: dict) -> dict:
    visible = {}
    for clave, valor in args.items():
        if clave in ("fileText", "contents", "content") and isinstance(valor, str) and len(valor) > _TOPE_ENTRADA:
            visible[clave] = f"<{len(valor)} caracteres>"
        else:
            visible[clave] = valor
    return visible


def _resultado_herramienta(resultado) -> Tuple[str, bool]:
    if not isinstance(resultado, dict):
        return "", False
    if "error" in resultado or "failure" in resultado:
        error = resultado.get("error") or resultado.get("failure")
        if isinstance(error, dict):
            error = error.get("message") or error
        return str(error)[:_TOPE_SALIDA], True
    exito = resultado.get("success")
    if isinstance(exito, dict):
        for clave in ("content", "output", "stdout"):
            if exito.get(clave):
                return str(exito[clave])[:_TOPE_SALIDA], False
        return json.dumps({k: v for k, v in exito.items() if k != "content"}, ensure_ascii=False)[:_TOPE_SALIDA], False
    return "", False
