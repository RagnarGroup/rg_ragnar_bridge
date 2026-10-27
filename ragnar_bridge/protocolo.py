"""Forma de un `run` de Ragnar (validacion de session_id, prompt, adjuntos, images_out y
carpeta de trabajo) y reglas de permisos que valen para cualquier agente."""

import base64
import binascii
import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .carpetas import ErrorCarpeta, normalizar as normalizar_carpeta

MAX_ADJUNTOS = 4
MAX_BYTES_ADJUNTO = 4 * 1024 * 1024
MAX_BYTES_ADJUNTOS = 8 * 1024 * 1024
FIRMAS_DE_IMAGEN = {
    "image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
}

_PATRON_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_PATRONES_PROHIBIDOS = (":(){ :|:& };:", "mkfs", "> /dev/sd")
_PATRON_RM_RAIZ = re.compile(r"rm\s+-[a-zA-Z-]*\s+/(?:\s|[\"')*]|$)")


class RunInvalido(Exception):
    pass


@dataclass(frozen=True)
class Turno:
    session_id: str
    prompt: str
    prompt_fallback: str
    system_append: str
    conceder: Optional[str]
    tickets_token: Optional[str] = None
    token_de_turno: bool = False
    adjuntos: Tuple[Tuple[str, bytes], ...] = ()
    dirs_extra: Tuple[str, ...] = ()
    imagenes_salida: bool = False
    dir_salida: Optional[str] = None
    cwd: Optional[str] = None


def _validar_cwd(crudo) -> Optional[str]:
    if crudo is None or crudo == "":
        return None
    try:
        carpeta = normalizar_carpeta(crudo)
    except ErrorCarpeta as e:
        raise RunInvalido(f"carpeta de trabajo invalida: {e}")
    if not os.path.isdir(carpeta):
        raise RunInvalido(f"la carpeta de trabajo no existe en el servidor: {carpeta}")
    return carpeta


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
        _validar_adjuntos(run.get("attachments")),
        imagenes_salida=run.get("images_out") is True,
        cwd=_validar_cwd(run.get("cwd")),
    )


def _validar_adjuntos(crudo) -> Tuple[Tuple[str, bytes], ...]:
    if crudo is None:
        return ()
    if not isinstance(crudo, list) or len(crudo) > MAX_ADJUNTOS:
        raise RunInvalido(f"como mucho {MAX_ADJUNTOS} imagenes por mensaje")
    adjuntos = []
    total = 0
    for item in crudo:
        mime = item.get("mime") if isinstance(item, dict) else None
        datos = item.get("data") if isinstance(item, dict) else None
        if mime not in FIRMAS_DE_IMAGEN or not isinstance(datos, str):
            raise RunInvalido("adjunto invalido: solo imagenes PNG, JPEG o WebP")
        try:
            contenido = base64.b64decode(datos, validate=True)
        except (binascii.Error, ValueError):
            raise RunInvalido("adjunto invalido: no es base64")
        extension, es_del_tipo = FIRMAS_DE_IMAGEN[mime]
        if not es_del_tipo(contenido):
            raise RunInvalido("adjunto invalido: el contenido no es la imagen que dice ser")
        total += len(contenido)
        if len(contenido) > MAX_BYTES_ADJUNTO or total > MAX_BYTES_ADJUNTOS:
            raise RunInvalido("las imagenes adjuntas son demasiado pesadas")
        adjuntos.append((extension, contenido))
    return tuple(adjuntos)


def permiso_es_catastrofico(tool: str) -> bool:
    chico = tool.strip().lower()
    if chico in ("bash(*)", "bash( * )", "*"):
        return True
    if _PATRON_RM_RAIZ.search(chico):
        return True
    return any(p in chico for p in _PATRONES_PROHIBIDOS)


_HERRAMIENTAS_SHELL = ("bash", "powershell")
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
_PATRON_COMANDO = re.compile(r"^(?:/|~/|\./)?[A-Za-z0-9_.][A-Za-z0-9_.+/\-]*$")


def regla_concedible(tool: str) -> bool:
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
    comando = re.split(r"[\s:]", patron, maxsplit=1)[0]
    if not comando or not _PATRON_COMANDO.match(comando):
        return False
    base = comando.rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    sin_version = re.sub(r"[\d.]+$", "", base) or base
    con_comodin = "*" in patron or "?" in patron
    if (base in _INTERPRETES or sin_version in _INTERPRETES) and con_comodin:
        return False
    return True
