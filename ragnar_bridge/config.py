"""Configuracion del bridge: un JSON en ~/.config/ragnar-bridge/config.json (o en
RAGNAR_BRIDGE_CONFIG), con sus valores por defecto y su validacion."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


AGENTES = ("claude", "agy", "codex", "hermes")
AGY_PERMISOS = ("preguntar", "denegar", "auto")
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_RAZONAMIENTOS = ("low", "medium", "high", "xhigh", "max", "ultra")


class ConfigError(Exception):
    pass


def ruta_por_defecto() -> Path:
    env = os.environ.get("RAGNAR_BRIDGE_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "ragnar-bridge" / "config.json"


@dataclass
class Config:
    url: str
    token: str
    agents: Optional[List[str]] = None
    agent: Optional[str] = None
    claude_cmd: List[str] = field(default_factory=lambda: ["claude"])
    agy_cmd: List[str] = field(default_factory=lambda: ["agy"])
    codex_cmd: List[str] = field(default_factory=lambda: ["codex"])
    hermes_cmd: List[str] = field(default_factory=lambda: ["hermes"])
    agy_model: Optional[str] = None
    agy_timeout: str = "30m"
    agy_dir: str = "~/.gemini/antigravity-cli"
    agy_permisos: str = "preguntar"
    codex_model: Optional[str] = "gpt-5.6-terra"
    codex_reasoning: Optional[str] = "medium"
    codex_home: str = "~/.codex"
    codex_sandbox: str = "read-only"
    workdir: str = "~"
    project_paths: Dict[str, str] = field(default_factory=dict)
    add_dirs: List[str] = field(default_factory=list)
    archivos_editables: bool = True
    config_dir: str = "~/.claude"
    tickets_token: Optional[str] = None
    claude_espera_conectores: float = 6.0
    reprobar_cada: int = 120
    max_turnos: int = 4
    timeout_turno: float = 2 * 60 * 60
    tope_stderr: int = 16384
    tope_evento: int = 8 * 1024 * 1024

    def agentes_pedidos(self) -> List[str]:
        if self.agents is not None:
            return list(self.agents)
        if self.agent:
            return [self.agent]
        return list(AGENTES)

    @property
    def workdir_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.workdir))

    @property
    def codex_home_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.codex_home))

    @property
    def config_dir_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.config_dir))


def cargar(ruta: Optional[Path] = None) -> Config:
    ruta = ruta or ruta_por_defecto()
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"No existe {ruta}. Creala con `ragnar-bridge init --url ...` (te pide el token)."
        )
    except json.JSONDecodeError as e:
        raise ConfigError(f"{ruta} no es JSON valido: {e}")

    for clave in ("url", "token"):
        if not isinstance(crudo.get(clave), str) or not crudo[clave]:
            raise ConfigError(f"Falta '{clave}' en {ruta}.")
    if not crudo["url"].startswith(("ws://", "wss://")):
        raise ConfigError("'url' tiene que empezar con wss:// (o ws:// solo para pruebas locales).")

    campos = {k: v for k, v in crudo.items() if k in Config.__dataclass_fields__}
    for clave in ("codex_model", "codex_reasoning"):
        if campos.get(clave) is None:
            campos.pop(clave, None)
    for clave in ("claude_cmd", "agy_cmd", "codex_cmd", "hermes_cmd"):
        if isinstance(campos.get(clave), str):
            campos[clave] = [campos[clave]]
    cfg = Config(**campos)
    if not isinstance(cfg.project_paths, dict) or any(
        not isinstance(k, str)
        or not k
        or len(k) > 6
        or not isinstance(v, str)
        or not v
        for k, v in cfg.project_paths.items()
    ):
        raise ConfigError("'project_paths' debe mapear claves de proyecto a directorios.")
    if (
        isinstance(cfg.claude_espera_conectores, bool)
        or not isinstance(cfg.claude_espera_conectores, (int, float))
        or not 0 <= cfg.claude_espera_conectores <= 60
    ):
        raise ConfigError("'claude_espera_conectores' tiene que ser un numero de segundos entre 0 y 60.")
    if not isinstance(cfg.archivos_editables, bool):
        raise ConfigError("'archivos_editables' tiene que ser true o false.")
    for nombre in cfg.agentes_pedidos():
        if nombre not in AGENTES:
            raise ConfigError(
                f"Agente desconocido {nombre!r}: 'agents' tiene que ser una lista con {' y/o '.join(AGENTES)}."
            )
    if isinstance(cfg.agents, list) and not cfg.agents:
        raise ConfigError("'agents' no puede estar vacio (omitilo para detectar los instalados).")
    if cfg.agy_permisos not in AGY_PERMISOS:
        raise ConfigError(
            f"'agy_permisos' tiene que ser uno de {', '.join(AGY_PERMISOS)} (es {cfg.agy_permisos!r})."
        )
    if cfg.codex_sandbox not in CODEX_SANDBOXES:
        raise ConfigError(
            f"'codex_sandbox' tiene que ser uno de {', '.join(CODEX_SANDBOXES)} (es {cfg.codex_sandbox!r})."
        )
    if (
        isinstance(cfg.timeout_turno, bool)
        or not isinstance(cfg.timeout_turno, (int, float))
        or cfg.timeout_turno <= 0
    ):
        raise ConfigError(f"'timeout_turno' tiene que ser un numero de segundos mayor a 0 (es {cfg.timeout_turno!r}).")
    if cfg.codex_reasoning and cfg.codex_reasoning not in CODEX_RAZONAMIENTOS:
        raise ConfigError(
            f"'codex_reasoning' tiene que ser uno de {', '.join(CODEX_RAZONAMIENTOS)} o vacio (es {cfg.codex_reasoning!r})."
        )
    return cfg


def guardar(cfg: Config, ruta: Optional[Path] = None) -> Path:
    ruta = ruta or ruta_por_defecto()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = {k: getattr(cfg, k) for k in Config.__dataclass_fields__}
    tmp = ruta.with_suffix(".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as archivo:
        archivo.write(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, ruta)
    return ruta
