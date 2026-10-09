"""Configuracion del bridge: un JSON chico, por defecto en
~/.config/ragnar-bridge/config.json (o donde diga RAGNAR_BRIDGE_CONFIG)."""

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
    # wss://panel.ragnargroup.app/api/v1/bridge/ws -- la muestra la app al
    # crear el servidor, junto con el token.
    url: str
    token: str
    # Que CLIs maneja este bridge. Por defecto (ninguno de los dos campos)
    # detecta solo los que esten instalados en el servidor -- claude, agy, codex y/o hermes --
    # y Ragnar deja elegir entre los que funcionan. `agents` fuerza un
    # subconjunto (["claude"]); `agent` es la forma vieja (un solo CLI), se
    # sigue leyendo para no romper un config de la 0.2.
    agents: Optional[List[str]] = None
    agent: Optional[str] = None
    # Comando del CLI, como lista para poder apuntar a un wrapper
    # (["node", ".../cli.js"], ["/opt/claude/bin/claude"]). Por defecto `claude`
    # del PATH.
    claude_cmd: List[str] = field(default_factory=lambda: ["claude"])
    agy_cmd: List[str] = field(default_factory=lambda: ["agy"])
    codex_cmd: List[str] = field(default_factory=lambda: ["codex"])
    hermes_cmd: List[str] = field(default_factory=lambda: ["hermes"])
    # Modelo de agy (`agy models` los lista). Vacio = el default del CLI.
    agy_model: Optional[str] = None
    # Tope de un turno de agy (--print-timeout, formato Go: 30m, 2h).
    agy_timeout: str = "30m"
    # Carpeta de estado de agy (conversaciones); la de siempre.
    agy_dir: str = "~/.gemini/antigravity-cli"
    # Que hacer cuando agy necesita permiso para una herramienta (en headless
    # se deniega solo, y lo unico que lo destraba es aprobar TODO):
    #   preguntar (default) -- se lo pide a Ragnar; si aprobas en la app, esa
    #                          conversacion corre con las herramientas aprobadas.
    #   denegar             -- nunca se concede: el agente solo lee/conversa.
    #   auto                -- siempre aprobado (--dangerously-skip-permissions).
    #                          Solo en un servidor desechable.
    agy_permisos: str = "preguntar"
    # Codex: modelo y esfuerzo de razonamiento -- SIEMPRE se fijan en cada turno
    # (ignoran los de tu config.toml de Codex; "" = no fijar y usar los suyos),
    # carpeta de Codex (con
    # tu sesion ya iniciada; el bridge nunca escribe en su config.toml) y sandbox
    # de las herramientas. En `exec` nadie aprueba comandos, asi que lo que el
    # sandbox no permite falla: `read-only` (default) solo lee y conversa,
    # `workspace-write` edita dentro de workdir (y de `add_dirs`),
    # `danger-full-access` no restringe nada -- solo en un servidor desechable.
    codex_model: Optional[str] = "gpt-5.6-terra"
    codex_reasoning: Optional[str] = "medium"
    codex_home: str = "~/.codex"
    codex_sandbox: str = "read-only"
    # Donde arranca cada turno. Ragnar no manda ruta (una ruta de SU servidor
    # no significa nada aca): el directorio lo elegis vos.
    workdir: str = "~"
    # Rutas locales de proyectos para instalación de agentes/skills. Se
    # configuran en el servidor del usuario; Ragnar solo transmite project_key.
    project_paths: Dict[str, str] = field(default_factory=dict)
    # Directorios extra a los que el CLI puede llegar fuera de `workdir` (flag
    # --add-dir). Vacio = solo workdir.
    add_dirs: List[str] = field(default_factory=list)
    # Carpeta de config/credenciales del CLI. Por defecto la de siempre
    # (~/.claude): el bridge usa TU sesion ya iniciada, no pide login.
    config_dir: str = "~/.claude"
    # Token propio (ragagt_...) para que el CLI use el MCP de tickets de
    # Ragnar. Opcional: sin el, el agente trabaja igual, solo sin esas tools.
    tickets_token: Optional[str] = None
    # Cada cuantos segundos se vuelve a comprobar que CLIs estan instalados y
    # con sesion iniciada (por si te logueaste en agy despues de arrancar el
    # bridge). Si cambia algo, se le avisa a Ragnar sin reconectar.
    reprobar_cada: int = 120
    # Turnos simultaneos como maximo (conversaciones distintas a la vez).
    max_turnos: int = 4
    # Tope de un turno en segundos, para CUALQUIER agente: pasado esto el
    # bridge corta el CLI (y sus hijos) y le avisa a Ragnar. Generoso a
    # proposito: es para un CLI colgado, no para apurar un turno largo.
    timeout_turno: float = 2 * 60 * 60
    # Cuantos bytes del stderr del CLI se conservan para reportar un fallo.
    tope_stderr: int = 16384
    # Un evento de stream-json mas grande que esto (un Read de un archivo
    # enorme) se descarta en vez de romper el socket; Ragnar lo ignora.
    tope_evento: int = 8 * 1024 * 1024

    def agentes_pedidos(self) -> List[str]:
        """Los agentes que este bridge intenta manejar (aunque despues alguno no
        este instalado): `agents`, o el `agent` viejo, o todos."""
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
    # Un `null` (lo escribe `init` de la 0.5.0) es "sin dato", no "no fijar": vale
    # el default. Para no fijarlo, "".
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
    """Escribe la config con permisos 600: adentro va el token."""
    ruta = ruta or ruta_por_defecto()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    datos = {k: getattr(cfg, k) for k in Config.__dataclass_fields__}
    tmp = ruta.with_suffix(".tmp")
    # El archivo NACE con 600 (RAG-208): escribirlo y despues hacer chmod
    # dejaba una ventana en que, con umask 022, cualquiera podia leer el
    # token. Un .tmp que haya quedado de antes se borra: os.open no cambia
    # los permisos de un archivo que ya existe.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as archivo:
        archivo.write(json.dumps(datos, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, ruta)
    return ruta
