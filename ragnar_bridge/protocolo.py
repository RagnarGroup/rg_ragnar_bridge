"""Forma de un `run` de Ragnar y reglas que valen para CUALQUIER agente."""

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Optional, Tuple

MAX_ADJUNTOS = 4
MAX_BYTES_ADJUNTO = 4 * 1024 * 1024
MAX_BYTES_ADJUNTOS = 8 * 1024 * 1024
# mime -> (extension, como reconocer que los bytes son de ese formato).
FIRMAS_DE_IMAGEN = {
    "image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
}

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
    # Imagenes que la persona adjunto a este mensaje: (extension, bytes). El
    # bridge las guarda en disco y le pasa las rutas al agente (ver adjuntos.py).
    adjuntos: Tuple[Tuple[str, bytes], ...] = ()
    # Carpetas que el CLI tiene que poder leer ademas de `workdir` en este
    # turno (hoy, la de los adjuntos ya guardados).
    dirs_extra: Tuple[str, ...] = ()


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
        # El tipo lo declara quien manda; lo que se guarda en disco con esa
        # extension tiene que serlo de verdad.
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
