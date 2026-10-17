"""Antigravity CLI (`agy`, github.com/google-antigravity/antigravity-cli).

Diferencias con Claude Code que el adaptador absorbe (verificado contra agy
1.1.27, ver tests/test_agy.py para las formas reales de los eventos):

* Su stream-json es otro (`init`/`step_update`/`result`): `TraductorAgy` lo
  convierte al formato de Claude que Ragnar espera.
* No hay `--session-id`: agy asigna el id de conversacion y lo informa en
  `init`. Se guarda la correspondencia session_id(Ragnar) -> conversation_id en
  `agy-estado.json` y los turnos siguientes usan `--conversation`.
* No hay `--append-system-prompt`: las instrucciones de Ragnar se anteponen al
  prompt del primer turno de cada conversacion (despues viven en el hilo).
* Tickets de Ragnar (MCP): agy tiene UN archivo global de servidores MCP y sus
  cabeceras no expanden variables de entorno, asi que un token por turno no
  cabe ahi. Se registra una sola vez `ragnar-tickets` como servidor stdio
  (`mcp_proxy`) y el token de cada turno viaja en el ENTORNO del agy de ese
  turno, que el servidor hereda. Ver `mcp_proxy.py`.
  Limite conocido (RAG-208): a diferencia de Codex
  (`shell_environment_policy.exclude`), agy no tiene forma de quitar variables
  del entorno que heredan las herramientas de shell del modelo, y el servidor
  stdio solo recibe el token heredandolo de ese mismo entorno (`agy mcp add
  --env` es estatico y global, no sirve para un token por turno). Asi que el
  modelo PUEDE leer `RAGNAR_TICKETS_TOKEN` desde un shell. Se acepta porque es
  el token efimero y estricto de ESE turno, el mismo que el modelo ya usa via
  MCP. Pasarlo por un archivo que el proxy borre al leerlo dependeria de que
  agy lance el servidor una sola vez y antes de cualquier herramienta, cosa no
  verificada: si fallara, el turno se quedaria sin tickets.
* En modo headless una herramienta que pide permiso se DENIEGA sola (y
  `permissions.allow` de su settings.json se ignora en headless, issue #548 de
  antigravity-cli). La salida es `--dangerously-skip-permissions`, que aprueba
  TODO. Por eso el bridge no lo pasa de entrada: deja que agy deniegue, le
  cuenta a Ragnar que hace falta un permiso (marcador [[PERMISO]], el mismo
  flujo de la tarjeta "Autorizar" que Claude) y, solo si el humano aprueba,
  relanza esa conversacion con el flag. Ver `Config.agy_permisos`.
"""

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
from .base import Adaptador, Comando, Traductor, bloque_de_cuota

log = logging.getLogger("ragnar_bridge")

# Lo que Ragnar recibe como `herramienta` del permiso pedido. Es una etiqueta,
# no un patron: agy no concede por herramienta en headless, concede la
# conversacion entera.
PERMISO_AGY = "agy:permisos"

_SEPARADOR = "\n\n---\n\n"

# Nombre con el que se registra el puente en el archivo global de MCP de agy: uno
# propio, para no pisar un servidor "tickets" que ya tengas por tu cuenta.
NOMBRE_MCP = "ragnar-tickets"
_INTENTOS_REGISTRO = 3

# Primera version de agy que contesta `-p "/usage"` sin arrancar un turno.
_AGY_USAGE_DESDE = (1, 1, 11)


def conversacion_existe(cfg: Config, conversation_id: str) -> bool:
    """agy guarda cada conversacion como <agy_dir>/conversations/<id>.db."""
    ruta = Path(os.path.expanduser(cfg.agy_dir)) / "conversations" / f"{conversation_id}.db"
    return ruta.is_file()


class AgyAdaptador(Adaptador):
    nombre = "agy"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.agy_cmd

    def sesion_iniciada(self) -> Optional[bool]:
        """agy no tiene un comando de estado: `agy models` pide la lista a la
        cuenta (no gasta cuota) y sale con codigo distinto de 0 si no hay
        sesion. Un timeout o un fallo al lanzarlo no prueba nada (sin red)."""
        try:
            salida = subprocess.run(
                [*self.cmd, "models"], capture_output=True, text=True, timeout=45
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return salida.returncode == 0

    def cuota(self, version: str) -> Optional[dict]:
        """`agy -p "/usage" --output-format json`: desde agy 1.1.11 los comandos
        de solo lectura se contestan en modo print SIN arrancar un turno ni
        gastar cuota. Con un agy mas viejo `/usage` iria al modelo como un
        prompt cualquiera, asi que ahi ni se intenta."""
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

    # ---- MCP de tickets

    def asegurar_mcp(self) -> bool:
        """Registra `ragnar-tickets` en agy (una vez por proceso del bridge;
        `mcp add` reemplaza si ya existe, asi que tambien sirve tras mover el
        bridge de venv). Si agy no lo acepta el turno sigue, sin tickets."""
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

    # ---- estado propio (session_id de Ragnar -> conversacion de agy)

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

    # ---- comando

    def preparar(self, turno: Turno, username: str) -> Comando:
        cfg = self.cfg
        sesion = self._leer().get(turno.session_id, {})
        conversacion = sesion.get("conversation_id")
        reanudar = bool(conversacion) and conversacion_existe(cfg, conversacion)

        permisos = bool(sesion.get("permisos"))
        if turno.conceder and turno.conceder != PERMISO_AGY:
            # Solo la etiqueta exacta que pidio ESTE adaptador destraba agy
            # (RAG-200): cualquier otro texto (una regla de Claude, algo
            # inyectado en el [[PERMISO]]) no activa el modo sin permisos.
            log.warning("Ragnar pidio conceder %r a agy; solo vale %r: se ignora.", turno.conceder, PERMISO_AGY)
        elif turno.conceder and cfg.agy_permisos == "preguntar" and not permisos:
            # El humano aprobo en la app: desde ahora esta conversacion corre
            # con las herramientas aprobadas.
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
        # El prompt va PEGADO al flag (`-p=<prompt>`, un solo argumento): con
        # `-p <prompt>` agy toma como prompt lo que venga despues, y si es un
        # flag ("--output-format") falla; y un prompt que empieza con "-" (o
        # tiene saltos de linea) tampoco se confunde con un flag. Es la forma
        # que el propio agy recomienda en su mensaje de error.
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
        for d in cfg.add_dirs:
            argv += ["--add-dir", os.path.abspath(os.path.expanduser(d))]
        if reanudar:
            argv += ["--conversation", conversacion]
        if auto:
            argv.append("--dangerously-skip-permissions")
        return Comando(argv, self.entorno(turno))

    def traductor(self, turno: Turno) -> "TraductorAgy":
        return TraductorAgy(self, turno)


class TraductorAgy(Traductor):
    """Convierte los eventos de agy al stream-json de Claude que Ragnar parsea:

      init                       -> (guarda conversation_id)
      step agent_response        -> stream_event / content_block_delta / text_delta
      step tool ACTIVE           -> assistant / tool_use
      step tool DONE|ERROR       -> user / tool_result
      result                     -> [marcador de permiso] + result
    """

    def __init__(self, adaptador: AgyAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.conversation_id: Optional[str] = None
        # Uso sumado de los pasos de ESTE proceso. `result.usage` de agy es
        # acumulado de toda la conversacion, y Ragnar suma el uso de cada
        # turno: usarlo tal cual contaria dos veces lo de los turnos previos.
        self._entrada = 0
        self._salida = 0
        self._cache = 0
        # Herramientas que agy nego por falta de permiso: nombre -> parametros.
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
        """Texto FINAL del turno cuando agy nego una herramienta. Con
        agy_permisos=preguntar lleva el marcador [[PERMISO]] (tiene que ser lo
        ultimo del turno para que Ragnar lo tome); con otro modo solo avisa."""
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
