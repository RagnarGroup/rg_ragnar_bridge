"""Adaptador de Codex CLI (`codex`).

* Un turno es `codex exec --json` (o `exec resume <thread>`); `TraductorCodex`
  convierte su JSONL (`thread.started`/`item.*`/`turn.completed`) al de Claude.
* Codex asigna el `thread_id`: se guarda session_id -> thread_id en
  `codex-estado.json` y se reanuda mientras el hilo exista en disco.
* Sin `--append-system-prompt`: las instrucciones van antepuestas al primer prompt.
* Nunca escribe el `config.toml`: sandbox, modelo y MCP de tickets van por `-c`.
* El token de tickets viaja en el entorno (no en argv), llega a `mcp_proxy` por
  `env_vars` y se excluye del entorno de las herramientas de shell del modelo.
* `exec` no tiene quien apruebe (`approval_policy="never"`): el sandbox por defecto
  es `read-only` y el bypass solo va con `codex_sandbox="danger-full-access"`."""

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..protocolo import _PATRON_UUID, Turno
from ..tickets import token_de_tickets, url_mcp_tickets
from .base import Adaptador, Comando, RecetaLogin, Traductor, bloque_de_cuota, patron, ventana

log = logging.getLogger("ragnar_bridge")

_SEPARADOR = "\n\n---\n\n"

NOMBRE_MCP = "ragnar-tickets"
_VARIABLES_TICKETS = ("RAGNAR_TICKETS_URL", "RAGNAR_TICKETS_TOKEN", "RAGNAR_TICKETS_AGENT")


def _toml(valor) -> str:
    return json.dumps(valor, ensure_ascii=False)


def hilo_existe(cfg: Config, thread_id: str) -> bool:
    if not _PATRON_UUID.match(thread_id):
        return False
    sesiones = Path(cfg.codex_home_abs) / "sessions"
    return next(sesiones.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"), None) is not None


class CodexAdaptador(Adaptador):
    nombre = "codex"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.codex_cmd

    def _entorno_base(self) -> Dict[str, str]:
        env = os.environ.copy()
        for k in _VARIABLES_TICKETS:
            env.pop(k, None)
        por_defecto = os.path.abspath(os.path.expanduser("~/.codex"))
        if self.cfg.codex_home_abs != por_defecto:
            env["CODEX_HOME"] = self.cfg.codex_home_abs
        return env

    def receta_login(self) -> RecetaLogin:
        return RecetaLogin(
            argv=[*self.cmd, "login", "--device-auth"],
            env=self._entorno_base(),
            patron_url=patron(r"https://auth\.openai\.com/\S+"),
            patron_codigo=patron(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b"),
            pide_codigo=False,
        )

    def sesion_iniciada(self) -> Optional[bool]:
        try:
            salida = subprocess.run(
                [*self.cmd, "login", "status"],
                capture_output=True,
                text=True,
                timeout=20,
                env=self._entorno_base(),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return salida.returncode == 0

    def cuota(self, version: str) -> Optional[dict]:
        try:
            respuesta = self._rpc(
                [
                    {"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "ragnar-bridge", "title": "Ragnar Bridge", "version": "0"}}},
                    {"method": "initialized"},
                    {"method": "account/rateLimits/read", "id": 1},
                ],
                esperar=1,
            )
            resultado = (respuesta or {}).get("result") or {}
            limites = resultado.get("rateLimitsByLimitId") or {}
            if not limites and resultado.get("rateLimits"):
                limites = {"codex": resultado["rateLimits"]}
            bloques = []
            for id_limite, limite in limites.items():
                grupo = None if id_limite == "codex" else str(limite.get("limitName") or id_limite)[:60]
                for ventana_ in (limite.get("primary"), limite.get("secondary")):
                    if not isinstance(ventana_, dict):
                        continue
                    resetea = ventana_.get("resetsAt")
                    bloques.append(
                        bloque_de_cuota(
                            grupo,
                            ventana(int(ventana_["windowDurationMins"])),
                            float(ventana_["usedPercent"]),
                            datetime.fromtimestamp(resetea, timezone.utc) if resetea else None,
                        )
                    )
            plan = (limites.get("codex") or {}).get("planType")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
            log.debug("No se pudo leer la cuota de Codex.", exc_info=True)
            return None
        return {"plan": str(plan)[:30] if plan else None, "bloques": bloques} if bloques else None

    def _rpc(self, mensajes: List[dict], esperar: int, timeout: float = 25) -> Optional[dict]:
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=self._entorno_base(),
        )
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proceso = subprocess.Popen([*self.cmd, "app-server"], **kwargs)
        lineas: "queue.Queue[Optional[str]]" = queue.Queue()

        def leer() -> None:
            for linea in proceso.stdout:
                lineas.put(linea)
            lineas.put(None)

        threading.Thread(target=leer, daemon=True).start()
        try:
            for mensaje in mensajes:
                proceso.stdin.write(json.dumps(mensaje) + "\n")
            proceso.stdin.flush()
            limite = time.monotonic() + timeout
            while (restante := limite - time.monotonic()) > 0:
                try:
                    linea = lineas.get(timeout=restante)
                except queue.Empty:
                    break
                if linea is None:
                    break
                try:
                    respuesta = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                if isinstance(respuesta, dict) and respuesta.get("id") == esperar:
                    return respuesta
            return None
        finally:
            self._matar_grupo(proceso)

    @staticmethod
    def _matar_grupo(proceso: subprocess.Popen) -> None:
        if os.name == "posix":
            try:
                os.killpg(proceso.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                proceso.kill()
            except ProcessLookupError:
                pass
        proceso.wait()


    @property
    def _ruta_estado(self) -> Path:
        return self.estado_dir / "codex-estado.json"

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


    def _overrides(self, turno: Turno) -> List[str]:
        cfg = self.cfg
        pares = [
            ("sandbox_mode", _toml(cfg.codex_sandbox)),
            ("approval_policy", _toml("never")),
            ("shell_environment_policy.exclude", _toml(["RAGNAR_*"])),
        ]
        if cfg.codex_sandbox == "workspace-write" and cfg.add_dirs:
            raices = [os.path.abspath(os.path.expanduser(d)) for d in cfg.add_dirs]
            pares.append(("sandbox_workspace_write.writable_roots", _toml(raices)))
        if cfg.codex_model:
            pares.append(("model", _toml(cfg.codex_model)))
        if cfg.codex_reasoning:
            pares.append(("model_reasoning_effort", _toml(cfg.codex_reasoning)))
        if self._tickets(turno):
            pares += [
                (f"mcp_servers.{NOMBRE_MCP}.command", _toml(sys.executable)),
                (f"mcp_servers.{NOMBRE_MCP}.args", _toml(["-m", "ragnar_bridge.mcp_proxy"])),
                (f"mcp_servers.{NOMBRE_MCP}.env_vars", _toml(list(_VARIABLES_TICKETS))),
            ]
        argv: List[str] = []
        for clave, valor in pares:
            argv += ["-c", f"{clave}={valor}"]
        return argv

    def _tickets(self, turno: Turno) -> Optional[Dict[str, str]]:
        token = token_de_tickets(self.cfg, turno)
        url = url_mcp_tickets(self.cfg.url)
        if not token or not url:
            return None
        return {
            "RAGNAR_TICKETS_URL": url,
            "RAGNAR_TICKETS_TOKEN": token,
            "RAGNAR_TICKETS_AGENT": "codex",
        }

    def preparar(self, turno: Turno, username: str) -> Comando:
        hilo = self._leer().get(turno.session_id, {}).get("thread_id")
        reanudar = isinstance(hilo, str) and hilo_existe(self.cfg, hilo)

        if reanudar:
            texto = turno.prompt
        elif turno.system_append:
            texto = turno.system_append + _SEPARADOR + turno.prompt_fallback
        else:
            texto = turno.prompt_fallback

        argv = [*self.cfg.codex_cmd, "exec"]
        if reanudar:
            argv.append("resume")
        if self.cfg.codex_sandbox == "danger-full-access":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv += ["--json", "--skip-git-repo-check", *self._overrides(turno)]
        argv += ["--", *([hilo] if reanudar else []), texto]

        env = self._entorno_base()
        env.update(self._tickets(turno) or {})
        return Comando(argv, env)

    def traductor(self, turno: Turno) -> "TraductorCodex":
        return TraductorCodex(self, turno)


class TraductorCodex(Traductor):
    def __init__(self, adaptador: CodexAdaptador, turno: Turno):
        self._ad = adaptador
        self._turno = turno
        self.thread_id: Optional[str] = None
        self._hubo_texto = False
        self._abiertas: set = set()

    @staticmethod
    def _texto(texto: str) -> dict:
        return {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": texto}},
        }

    @staticmethod
    def _uso(uso: dict, cache: int) -> dict:
        return {
            "input_tokens": max(int(uso.get("input_tokens") or 0) - cache, 0),
            "output_tokens": int(uso.get("output_tokens") or 0),
            "cache_read_input_tokens": cache,
            "cache_creation_input_tokens": 0,
        }

    def evento(self, linea: dict) -> List[dict]:
        tipo = linea.get("type")
        if tipo == "thread.started":
            self.thread_id = linea.get("thread_id") or self.thread_id
            return []
        if tipo in ("item.started", "item.completed"):
            item = linea.get("item")
            return self._item(item, tipo == "item.completed") if isinstance(item, dict) else []
        if tipo == "turn.completed":
            return self._resultado(None, linea.get("usage") or {})
        if tipo == "turn.failed":
            mensaje = (linea.get("error") or {}).get("message")
            return self._resultado(str(mensaje or "Codex termino con error."), {})
        return []

    def _item(self, item: dict, terminado: bool) -> List[dict]:
        tipo, id_item = item.get("type"), str(item.get("id") or "")
        if tipo == "agent_message":
            texto = item.get("text") or ""
            if not terminado or not texto:
                return []
            prefijo = "\n\n" if self._hubo_texto else ""
            self._hubo_texto = True
            return [self._texto(prefijo + texto)]

        herramienta = self._herramienta(tipo, item)
        if herramienta is None:
            return []
        nombre, entrada = herramienta
        salida: List[dict] = []
        if id_item not in self._abiertas:
            self._abiertas.add(id_item)
            salida.append(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "id": id_item, "name": nombre, "input": entrada}]},
                }
            )
        if terminado:
            self._abiertas.discard(id_item)
            contenido, error = self._resultado_herramienta(tipo, item)
            salida.append(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": id_item,
                                "content": contenido,
                                "is_error": error,
                            }
                        ]
                    },
                }
            )
        return salida

    @staticmethod
    def _herramienta(tipo: Optional[str], item: dict):
        if tipo == "command_execution":
            return "Bash", {"command": item.get("command") or ""}
        if tipo == "file_change":
            return "apply_patch", {"changes": item.get("changes") or []}
        if tipo == "mcp_tool_call":
            return f"mcp__{item.get('server')}__{item.get('tool')}", item.get("arguments") or {}
        if tipo == "web_search":
            return "WebSearch", {"query": item.get("query") or ""}
        return None

    @staticmethod
    def _resultado_herramienta(tipo: Optional[str], item: dict):
        estado = item.get("status")
        fallo = estado in ("failed", "declined")
        if tipo == "command_execution":
            codigo = item.get("exit_code")
            return str(item.get("aggregated_output") or ""), fallo or (codigo not in (None, 0))
        if tipo == "file_change":
            cambios = item.get("changes") or []
            return "\n".join(f"{c.get('kind')} {c.get('path')}" for c in cambios if isinstance(c, dict)), fallo
        if tipo == "mcp_tool_call":
            error = item.get("error")
            if error:
                return str((error or {}).get("message") or error), True
            resultado = item.get("result") or {}
            partes = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in (resultado.get("content") or [])
            ]
            return "\n".join(p for p in partes if p), fallo
        return "", fallo

    def _resultado(self, error: Optional[str], uso: dict) -> List[dict]:
        cache = int(uso.get("cached_input_tokens") or 0)
        evento = {"type": "result", "is_error": error is not None, "usage": self._uso(uso, cache)}
        if error is not None:
            evento["result"] = error
        return [evento]

    def fin(self, codigo: Optional[int], stderr: str) -> List[dict]:
        if self.thread_id and _PATRON_UUID.match(self.thread_id):
            self._ad.actualizar_sesion(self._turno.session_id, thread_id=self.thread_id)
        return []
