"""Claude Code: armar su comando, saber si una sesion sigue viva, conceder
permisos y escribir el MCP de tickets.

Es la parte que antes vivia dentro de claude_orchestrator.py en el servidor de
Ragnar (con CLAUDE_CONFIG_DIR por usuario); ahora corre aca, sobre TU sesion.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from ..config import Config
from ..protocolo import Turno, regla_concedible
from ..tickets import token_de_tickets, url_mcp_tickets  # noqa: F401  (se re-exportan)
from .base import Adaptador, Comando

_PATRON_NO_ALFANUMERICO = re.compile(r"[^a-zA-Z0-9]")

log = logging.getLogger("ragnar_bridge")


def hay_sesion(cfg: Config, session_id: str) -> bool:
    carpeta = _PATRON_NO_ALFANUMERICO.sub("-", cfg.workdir_abs)
    return (Path(cfg.config_dir_abs) / "projects" / carpeta / f"{session_id}.jsonl").is_file()


def limpiar_lock_sesion(cfg: Config, session_id: str) -> None:
    shutil.rmtree(Path(cfg.config_dir_abs) / "session-env" / session_id, ignore_errors=True)


def ruta_permisos(estado_dir: Path, session_id: str) -> Path:
    return estado_dir / f"claude-permisos-{session_id}.json"


def conceder_permiso(estado_dir: Path, session_id: str, tool: str) -> bool:
    regla = tool.strip()
    if not regla_concedible(regla):
        return False
    ruta = ruta_permisos(estado_dir, session_id)
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(datos, dict):
        return False
    permisos = datos.setdefault("permissions", {}).setdefault("allow", [])
    if not isinstance(permisos, list):
        return False
    if regla in permisos:
        return True
    permisos.append(regla)
    tmp = ruta.with_name(ruta.name + f".tmp-{os.getpid()}")
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, ruta)
    except OSError:
        return False
    return True


def escribir_mcp_config(
    cfg: Config, estado_dir: Path, username: str, token: Optional[str], session_id: str
) -> Optional[str]:
    if not token:
        return None
    url = url_mcp_tickets(cfg.url)
    if not url:
        return None
    estado_dir.mkdir(parents=True, exist_ok=True)
    ruta = estado_dir / f"mcp-config-{session_id}.json"
    contenido = {
        "mcpServers": {
            "tickets": {
                "type": "http",
                "url": url,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "X-Agent-Name": username,
                },
            }
        }
    }
    tmp = ruta.with_name(ruta.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(contenido, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ruta)
    return str(ruta)


class ClaudeAdaptador(Adaptador):
    nombre = "claude"

    @property
    def cmd(self) -> List[str]:
        return self.cfg.claude_cmd

    def sesion_iniciada(self) -> Optional[bool]:
        try:
            salida = subprocess.run(
                [*self.cmd, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=20,
                env=_entorno(self.cfg),
            )
            return bool(json.loads(salida.stdout).get("loggedIn"))
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            return None

    def preparar(self, turno: Turno, username: str) -> Comando:
        cfg = self.cfg
        if turno.conceder and not conceder_permiso(self.estado_dir, turno.session_id, turno.conceder):
            log.warning(
                "No se concedio el permiso %r (regla demasiado amplia o archivo de permisos ilegible).",
                turno.conceder,
            )
        limpiar_lock_sesion(cfg, turno.session_id)
        mcp = escribir_mcp_config(
            cfg, self.estado_dir, username, token_de_tickets(cfg, turno), turno.session_id
        )
        permisos = ruta_permisos(self.estado_dir, turno.session_id)
        return construir_comando(cfg, turno, mcp, str(permisos) if permisos.is_file() else None)


def construir_comando(
    cfg: Config, turno: Turno, mcp_config: Optional[str], settings: Optional[str] = None
) -> Comando:
    reanudar = hay_sesion(cfg, turno.session_id)
    texto = turno.prompt if reanudar else turno.prompt_fallback
    if texto.startswith("-"):
        texto = " " + texto

    cmd = [
        *cfg.claude_cmd,
        "--print",
        "--output-format=stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if turno.system_append:
        cmd += ["--append-system-prompt", turno.system_append]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
    if settings:
        cmd += ["--settings", settings]
    dirs = [os.path.abspath(os.path.expanduser(d)) for d in cfg.add_dirs] + list(turno.dirs_extra)
    if dirs:
        cmd += ["--add-dir", *dirs]
    cmd += ["--resume" if reanudar else "--session-id", turno.session_id, texto]

    return Comando(cmd, _entorno(cfg))


def _entorno(cfg: Config) -> dict:
    env = os.environ.copy()
    por_defecto = os.path.abspath(os.path.expanduser("~/.claude"))
    if cfg.config_dir_abs != por_defecto:
        env["CLAUDE_CONFIG_DIR"] = cfg.config_dir_abs
    return env
