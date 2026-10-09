"""Forma de un `run` de Ragnar y reglas que valen para CUALQUIER agente."""

import re
from dataclasses import dataclass
from typing import Optional

# El session_id llega de Ragnar y termina dentro de rutas y de un archivo de
# estado: se exige forma de UUID para que un valor raro no pueda salirse de
# las carpetas del CLI con "../".
_PATRON_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Mismos patrones que permiso_es_catastrofico de claude_orchestrator.py: aunque
# Ragnar ya los filtra, el bridge es quien escribe en TU configuracion -- no
# confia en que el filtro de afuera siga ahi.
_PATRONES_PROHIBIDOS = (":(){ :|:& };:", "mkfs", "> /dev/sd")
_PATRON_RM_RAIZ = re.compile(r"rm\s+-[a-zA-Z-]*\s+/(?:\s|[\"')*]|$)")


class RunInvalido(Exception):
    """El `run` de Ragnar no tiene la forma esperada."""


@dataclass(frozen=True)
class Turno:
    """Un `run` ya validado."""

    session_id: str
    prompt: str
    # Prompt que ya trae el hilo anterior rehecho: se usa cuando la sesion del
    # CLI no existe (primer turno, o se perdio).
    prompt_fallback: str
    # Instrucciones del protocolo pregunta/permiso de Ragnar.
    system_append: str
    # Permiso que el humano ya aprobo en la app (o None).
    conceder: Optional[str]
    # Token efimero del usuario que escribio en el chat, para el MCP de tickets
    # de ESTE turno (o None: Ragnar no pudo emitirlo). `token_de_turno` dice si
    # el backend manda este campo: uno nuevo lo manda siempre y, si vino vacio,
    # el turno se queda SIN tickets; uno viejo no lo manda y se usa el
    # `tickets_token` de la config, como antes.
    tickets_token: Optional[str] = None
    token_de_turno: bool = False


def validar_run(run: dict) -> Turno:
    session_id = run.get("session_id")
    if not isinstance(session_id, str) or not _PATRON_UUID.match(session_id):
        raise RunInvalido("session_id invalido")
    prompt = run.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RunInvalido("prompt vacio")
    fallback = run.get("prompt_fallback")
    if not isinstance(fallback, str) or not fallback.strip():
        fallback = prompt
    system_append = run.get("system_append")
    if not isinstance(system_append, str):
        system_append = ""
    conceder = run.get("conceder")
    if conceder is not None and not isinstance(conceder, str):
        raise RunInvalido("conceder invalido")
    tickets_token = run.get("tickets_token")
    if tickets_token is not None and (not isinstance(tickets_token, str) or len(tickets_token) > 256):
        raise RunInvalido("tickets_token invalido")
    return Turno(
        session_id,
        prompt,
        fallback,
        system_append,
        conceder or None,
        tickets_token or None,
        "tickets_token" in run,
    )


def permiso_es_catastrofico(tool: str) -> bool:
    chico = tool.strip().lower()
    if chico in ("bash(*)", "bash( * )", "*"):
        return True
    if _PATRON_RM_RAIZ.search(chico):
        return True
    return any(p in chico for p in _PATRONES_PROHIBIDOS)


# ---- segunda linea de defensa para `conceder` (RAG-200)
#
# El texto de la regla lo elige el MODELO en su [[PERMISO]] (se puede inyectar
# desde un ticket o una pagina); el humano solo toca "Autorizar". La lista
# negra de Ragnar no alcanza: `Bash` a secas concede TODO Bash en Claude Code,
# igual que `Bash(:*)`, `Bash(**)` o `Bash(python:*)` (un interprete con
# comodin corre cualquier cosa). Aca se exige un prefijo de comando concreto.

# Herramientas que ejecutan comandos: sin parentesis, o con un patron que no
# fija el comando, conceden todo.
_HERRAMIENTAS_SHELL = ("bash", "powershell")
# Interpretes y envoltorios: con un comodin detras ejecutan lo que sea.
_INTERPRETES = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
        "python", "python2", "python3", "py", "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "lua", "pwsh", "powershell", "cmd",
        "env", "sudo", "doas", "su", "xargs", "eval", "exec", "nohup", "timeout",
        "nice", "setsid", "busybox", "npx", "bunx", "uvx", "pipx", "osascript",
    }
)
_PATRON_REGLA = re.compile(r"^([A-Za-z0-9_\-]+)(?:\((.*)\))?$", re.DOTALL)
_PATRON_COMANDO = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+/\-]*$")


def regla_concedible(tool: str) -> bool:
    """¿Se puede conceder esta regla de permiso sin entregar el shell entero?

    Rechaza (ademas de lo catastrofico):
    * nombres con comodines (`*`, `mcp__*`) o con forma rara;
    * `Bash`/`PowerShell` sin un patron que empiece por un comando concreto:
      `Bash`, `Bash()`, `Bash(*)`, `Bash(:*)`, `Bash(**)`, `Bash(* )`;
    * interpretes y envoltorios con comodin: `Bash(sh:*)`, `Bash(python *)`,
      `Bash(sudo:*)`, `Bash(env *)`.
    Acepta reglas como `WebFetch`, `Bash(docker ps:*)` o `Bash(npm run test:*)`.
    """
    if not isinstance(tool, str):
        return False
    regla = tool.strip()
    if not regla or len(regla) > 500 or "\n" in regla or "\x00" in regla:
        return False
    if permiso_es_catastrofico(regla):
        return False
    match = _PATRON_REGLA.match(regla)
    if not match:
        return False
    nombre, patron = match.group(1), match.group(2)
    if nombre.lower() not in _HERRAMIENTAS_SHELL:
        return True
    if patron is None:
        return False
    patron = patron.strip()
    # El comando es lo que va antes del primer espacio o del `:` de la
    # sintaxis de prefijo (`npm:*`).
    comando = re.split(r"[\s:]", patron, maxsplit=1)[0]
    if not comando or not _PATRON_COMANDO.match(comando):
        return False
    base = comando.rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    # python3.12, node20 -> python, node.
    sin_version = re.sub(r"[\d.]+$", "", base) or base
    con_comodin = "*" in patron or "?" in patron
    if (base in _INTERPRETES or sin_version in _INTERPRETES) and con_comodin:
        return False
    return True
