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
from ..protocolo import Turno, permiso_es_catastrofico
from ..tickets import token_de_tickets, url_mcp_tickets  # noqa: F401  (se re-exportan)
from .base import Adaptador, Comando

# El CLI real sanea CUALQUIER caracter no alfanumerico del cwd a "-" para
# nombrar la carpeta de proyecto bajo <config_dir>/projects/ (verificado
# contra carpetas reales: un repo con "_" cae en una carpeta con "-").
_PATRON_NO_ALFANUMERICO = re.compile(r"[^a-zA-Z0-9]")

log = logging.getLogger("ragnar_bridge")


def hay_sesion(cfg: Config, session_id: str) -> bool:
    """¿Sigue el CLI guardando esta conversacion? Cada sesion es un .jsonl bajo
    <config_dir>/projects/<cwd saneado>/. `--resume` de una sesion inexistente
    responde «No conversation found» y sale con codigo 0 (la tarea quedaba
    «completada» con ese texto), asi que se comprueba ANTES."""
    carpeta = _PATRON_NO_ALFANUMERICO.sub("-", cfg.workdir_abs)
    return (Path(cfg.config_dir_abs) / "projects" / carpeta / f"{session_id}.jsonl").is_file()


def limpiar_lock_sesion(cfg: Config, session_id: str) -> None:
    """En modo --print el CLI nunca limpia <config_dir>/session-env/<id>/ (eso
    pasa en una salida interactiva): si queda de una invocacion anterior, la
    siguiente --session-id/--resume del MISMO id responde «Session ID ... is
    already in use» sin que haya proceso vivo. Se llama antes de cada
    invocacion propia."""
    shutil.rmtree(Path(cfg.config_dir_abs) / "session-env" / session_id, ignore_errors=True)


def conceder_permiso(cfg: Config, tool: str) -> bool:
    """Suma `tool` a permissions.allow de TU settings.json (el humano ya lo
    aprobo en la app de Ragnar). Escritura atomica: un JSON a medio escribir
    tumbaria tambien tus sesiones interactivas. Devuelve False si no se
    escribio (patron bloqueado o settings ilegible)."""
    if permiso_es_catastrofico(tool):
        return False
    ruta = Path(cfg.config_dir_abs) / "settings.json"
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(datos, dict):
        return False
    permisos = datos.setdefault("permissions", {}).setdefault("allow", [])
    if tool in permisos:
        return True
    permisos.append(tool)
    tmp = ruta.with_name(ruta.name + f".tmp-{os.getpid()}")
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError:
        return False
    return True


def escribir_mcp_config(
    cfg: Config, estado_dir: Path, username: str, token: Optional[str], session_id: str
) -> Optional[str]:
    """MCP de tickets de Ragnar autenticado con `token`, o None si no hay. Se
    regenera en cada turno: barato, y evita servir un token que ya cambiaste o
    revocaste. Un archivo por conversacion: el bridge corre turnos de varias
    personas a la vez y con uno solo se pisarian entre si."""
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
        """`claude auth status` imprime JSON con `loggedIn`; no gasta cuota."""
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
        if turno.conceder and not conceder_permiso(cfg, turno.conceder):
            # No se corta el turno: sin el permiso Claude vuelve a pedirlo.
            log.warning(
                "No se pudo conceder el permiso %r (patron bloqueado o settings.json ilegible).",
                turno.conceder,
            )
        limpiar_lock_sesion(cfg, turno.session_id)
        mcp = escribir_mcp_config(
            cfg, self.estado_dir, username, token_de_tickets(cfg, turno), turno.session_id
        )
        return construir_comando(cfg, turno, mcp)


def construir_comando(cfg: Config, turno: Turno, mcp_config: Optional[str]) -> Comando:
    """Comando y entorno de UN turno. Si la sesion sigue viva se retoma con
    `prompt`; si no, se abre con `prompt_fallback`, que ya trae el hilo
    anterior rehecho por prompt (ver prompt_con_contexto en Ragnar)."""
    reanudar = hay_sesion(cfg, turno.session_id)
    texto = turno.prompt if reanudar else turno.prompt_fallback
    # Un prompt que empieza con "-" el CLI lo leeria como un flag.
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
    dirs = [os.path.abspath(os.path.expanduser(d)) for d in cfg.add_dirs] + list(turno.dirs_extra)
    if dirs:
        cmd += ["--add-dir", *dirs]
    cmd += ["--resume" if reanudar else "--session-id", turno.session_id, texto]

    return Comando(cmd, _entorno(cfg))


def _entorno(cfg: Config) -> dict:
    env = os.environ.copy()
    # Solo se fija CLAUDE_CONFIG_DIR si la moviste de sitio: con la variable
    # puesta el CLI guarda su estado (.claude.json) DENTRO de esa carpeta, no
    # junto a ella en ~/, y eso rompe tu sesion ya iniciada.
    por_defecto = os.path.abspath(os.path.expanduser("~/.claude"))
    if cfg.config_dir_abs != por_defecto:
        env["CLAUDE_CONFIG_DIR"] = cfg.config_dir_abs
    return env
