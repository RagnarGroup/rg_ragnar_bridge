"""Adaptador de Antigravity CLI (`agy`).

* `TraductorAgy` convierte su stream-json (`init`/`step_update`/`result`) al de Claude.
* agy asigna el id de conversacion: se guarda session_id -> conversation_id en
  `agy-estado.json` y los turnos siguientes usan `--conversation`.
* Sin `--append-system-prompt`: las instrucciones van antepuestas al primer prompt.
* Su config MCP es global y no expande variables: `ragnar-tickets` se registra una
  vez como servidor stdio (`mcp_proxy`) y el token de cada turno viaja en el entorno
  del agy, que el modelo puede leer desde un shell (es el token efimero del turno).
* En headless una herramienta que pide permiso se deniega sola y `permissions.allow`
  se ignora: el turno se relanza con `--dangerously-skip-permissions` solo si el
  humano aprueba en Ragnar (marcador [[PERMISO]]), segun `Config.agy_permisos`."""

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..protocolo import Turno
from ..tickets import token_de_tickets, url_mcp_tickets
from .base import Adaptador, Comando, RecetaLogin, Traductor, bloque_de_cuota, patron

log = logging.getLogger("ragnar_bridge")

PERMISO_AGY = "agy:permisos"

_SEPARADOR = "\n\n---\n\n"

NOMBRE_MCP = "ragnar-tickets"
_INTENTOS_REGISTRO = 3

_AGY_USAGE_DESDE = (1, 1, 11)


def conversacion_existe(cfg: Config, conversation_id: str) -> bool:
    ruta = Path(os.path.expanduser(cfg.agy_dir)) / "conversations" / f"{conversation_id}.db"
    return ruta.is_file()


class AgyAdaptador(Adaptador):
    nombre = "agy"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.agy_cmd

    def receta_login(self) -> RecetaLogin:
        return RecetaLogin(
            argv=list(self.cmd),
            env=os.environ.copy(),
            patron_url=patron(r"https://accounts\.google\.com/o/oauth2/auth\?\S+"),
            pide_codigo=True,
            respuestas=(("Select login method", "\r"),),
            sale_solo=False,
        )

    def sesion_iniciada(self) -> Optional[bool]:
        try:
            salida = subprocess.run(
                [*self.cmd, "models"], capture_output=True, text=True, timeout=45
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return salida.returncode == 0

    def cuota(self, version: str) -> Optional[dict]:
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
        if not match or tuple(map(int, match.groups())) < _AGY_USAGE_DESDE:
            return None
        try:
            salida = subprocess.run(
                [*self.cmd, "-p=/usage", "--output-format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            comando = json.loads(salida.stdout).get("command") or {}
            if comando.get("name") != "usage":
                return None
            bloques = []
            for grupo in (comando.get("data") or {}).get("groups") or []:
                for cubo in grupo.get("buckets") or []:
                    restante = float(cubo["remaining_fraction"])
                    reset = cubo.get("reset_time")
                    bloques.append(
                        bloque_de_cuota(
                            str(grupo.get("name") or "")[:60] or None,
                            "semana" if cubo.get("window") == "weekly" else str(cubo.get("window") or "")[:12],
                            (1 - restante) * 100,
                            datetime.fromisoformat(reset.replace("Z", "+00:00")) if reset else None,
                        )
                    )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
            log.debug("No se pudo leer la cuota de agy.", exc_info=True)
            return None
        return {"plan": None, "bloques": bloques} if bloques else None


    def asegurar_mcp(self) -> bool:
        if getattr(self, "_mcp_listo", False):
            return True
        intentos = getattr(self, "_mcp_intentos", 0)
        if intentos >= _INTENTOS_REGISTRO:
            return False
        self._mcp_intentos = intentos + 1
        try:
            r = subprocess.run(
                [*self.cmd, "mcp", "add", NOMBRE_MCP, sys.executable, "--", "-m", "ragnar_bridge.mcp_proxy"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("No se pudo registrar el MCP de tickets en agy: %s", e)
            return False
        if r.returncode != 0:
            log.warning("agy no acepto el MCP de tickets: %s", (r.stderr or r.stdout).strip()[:200])
            return False
        self._mcp_listo = True
        return True

    def entorno(self, turno: Turno) -> Dict[str, str]:
        env = os.environ.copy()
        for k in ("RAGNAR_TICKETS_URL", "RAGNAR_TICKETS_TOKEN", "RAGNAR_TICKETS_AGENT"):
            env.pop(k, None)
        token = token_de_tickets(self.cfg, turno)
        url = url_mcp_tickets(self.cfg.url)
        if token and url and self.asegurar_mcp():
            env["RAGNAR_TICKETS_URL"] = url
            env["RAGNAR_TICKETS_TOKEN"] = token
            env["RAGNAR_TICKETS_AGENT"] = "agy"
        return env


    @property
    def _ruta_estado(self) -> Path:
        return self.estado_dir / "agy-estado.json"

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
        cfg = self.cfg
        sesion = self._leer().get(turno.session_id, {})
        conversacion = sesion.get("conversation_id")
        reanudar = bool(conversacion) and conversacion_existe(cfg, conversacion)

        permisos = bool(sesion.get("permisos"))
        if turno.conceder and turno.conceder != PERMISO_AGY:
            log.warning("Ragnar pidio conceder %r a agy; solo vale %r: se ignora.", turno.conceder, PERMISO_AGY)
        elif turno.conceder and cfg.agy_permisos == "preguntar" and not permisos:
            permisos = True
            self.actualizar_sesion(turno.session_id, permisos=True)
        elif turno.conceder and cfg.agy_permisos != "preguntar":
            log.warning("Ragnar pidio conceder un permiso pero agy_permisos=%r: se ignora.", cfg.agy_permisos)

        auto = cfg.agy_permisos == "auto" or (permisos and cfg.agy_permisos == "preguntar")

        if reanudar:
            texto = turno.prompt
        elif turno.system_append:
            texto = turno.system_append + _SEPARADOR + turno.prompt_fallback
        else:
            texto = turno.prompt_fallback
        argv = [
            *cfg.agy_cmd,
            f"-p={texto}",
            "--output-format",
            "stream-json",
            "--print-timeout",
            cfg.agy_timeout,
        ]
        if cfg.agy_model:
            argv += ["--model", cfg.agy_model]
        for d in [os.path.abspath(os.path.expanduser(d)) for d in cfg.add_dirs] + list(turno.dirs_extra):
            argv += ["--add-dir", d]
        if reanudar:
            argv += ["--conversation", conversacion]
        if auto:
            argv.append("--dangerously-skip-permissions")
        return Comando(argv, self.entorno(turno))

    def traductor(self, turno: Turno) -> "TraductorAgy":
        return TraductorAgy(self, turno)


class TraductorAgy(Traductor):
    def __init__(self, adaptador: AgyAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.conversation_id: Optional[str] = None
        self._entrada = 0
        self._salida = 0
        self._cache = 0
        self._denegadas: List[dict] = []
        self._herramientas: Dict[int, dict] = {}
        self._resultado_emitido = False

    @staticmethod
    def _texto(texto: str) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": texto}},
        }

    def evento(self, linea: dict) -> List[dict]:
        tipo = linea.get("event")
        if tipo == "init":
            self.conversation_id = linea.get("conversation_id") or self.conversation_id
            return []
        if tipo == "step_update":
            return self._paso(linea.get("step_update") or {})
        if tipo == "result":
            return self._resultado(linea.get("result") or {})
        return []

    def _paso(self, paso: dict) -> List[dict]:
        self.conversation_id = paso.get("conversation_id") or self.conversation_id
        tipo, estado = paso.get("step_type"), paso.get("state")

        if tipo == "agent_response":
            uso = paso.get("usage") or {}
            if estado == "DONE" and uso:
                self._entrada += int(uso.get("input_tokens") or 0)
                self._salida += int(uso.get("output_tokens") or 0)
                self._cache += int(uso.get("cache_read_tokens") or 0)
            texto = paso.get("text_delta") or ""
            return [self._texto(texto)] if texto else []

        if tipo == "tool":
            idx = paso.get("step_index")
            info = paso.get("tool_info") or {}
            id_herramienta = f"{paso.get('conversation_id') or self.conversation_id}:{idx}"
            if estado == "ACTIVE":
                self._herramientas[idx] = {
                    "name": paso.get("tool_name") or info.get("name"),
                    "input": info.get("parameters") or {},
                }
                return [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": id_herramienta,
                                    "name": self._herramientas[idx]["name"],
                                    "input": self._herramientas[idx]["input"],
                                }
                            ]
                        },
                    }
                ]
            if estado in ("DONE", "ERROR"):
                error = info.get("error") or {}
                contenido = error.get("message") if estado == "ERROR" else info.get("output")
                if estado == "ERROR" and "permission" in str(contenido or "").lower():
                    self._denegadas.append(self._herramientas.get(idx, {}))
                return [
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": id_herramienta,
                                    "content": str(contenido or ""),
                                    "is_error": estado == "ERROR",
                                }
                            ]
                        },
                    }
                ]
        return []

    def _resultado(self, resultado: dict) -> List[dict]:
        salida: List[dict] = []
        denegadas = resultado.get("denied_actions") or []
        exito = resultado.get("status") == "SUCCESS"

        if denegadas:
            salida.append(self._texto(self._aviso_permiso(denegadas)))

        evento = {
            "type": "result",
            "is_error": not exito,
            "usage": {
                "input_tokens": self._entrada,
                "output_tokens": self._salida,
                "cache_read_input_tokens": self._cache,
                "cache_creation_input_tokens": 0,
            },
        }
        if not exito:
            evento["result"] = str(resultado.get("response") or resultado.get("status") or "agy termino con error.")
        salida.append(evento)
        self._resultado_emitido = True
        return salida

    def _aviso_permiso(self, denegadas: List[dict]) -> str:
        acciones = ", ".join(sorted({str(d.get("display_name") or d.get("action")) for d in denegadas}))
        detalle = "; ".join(
            f"{h.get('name')}: {json.dumps(h.get('input'), ensure_ascii=False)[:200]}"
            for h in self._denegadas
            if h
        )
        if self._ad.cfg.agy_permisos != "preguntar":
            return (
                f"\n\nAntigravity necesita permiso para: {acciones}, pero este servidor "
                f"esta configurado con agy_permisos={self._ad.cfg.agy_permisos!r} y no se "
                "lo puede conceder desde la app."
            )
        motivo = f"Antigravity necesita permiso para: {acciones}"
        if detalle:
            motivo += f" ({detalle})"
        motivo += ". Al autorizar, esta conversacion corre con las herramientas aprobadas."
        marcador = {"herramienta": PERMISO_AGY, "motivo": motivo[:500]}
        return f"\n\n[[PERMISO]]{json.dumps(marcador, ensure_ascii=False)}[[/PERMISO]]"

    def fin(self, codigo: Optional[int], stderr: str) -> List[dict]:
        if self.conversation_id:
            self._ad.actualizar_sesion(self._turno.session_id, conversation_id=self.conversation_id)
        return []
