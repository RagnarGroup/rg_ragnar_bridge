"""Explorador de carpetas para elegir donde trabaja una conversacion: lista solo las
subcarpetas visibles de una ruta (sin archivos), marca los repos git con su rama y,
sin ruta, devuelve las raices del sistema (unidades en Windows, / en Linux y macOS)."""

import os
import stat
import string
from pathlib import Path
from typing import List, Optional

MAX_ENTRADAS = 500
MAX_LARGO_RUTA = 1024
_ATRIBUTOS_OCULTOS = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0)
_OMITIDAS = {"node_modules", "__pycache__", "$recycle.bin", "system volume information"}


class ErrorCarpeta(Exception):
    def __init__(self, mensaje: str, codigo: str = "invalid"):
        super().__init__(mensaje)
        self.codigo = codigo


def rama_git(carpeta: str) -> Optional[str]:
    git = Path(carpeta) / ".git"
    try:
        if git.is_file():
            destino = git.read_text(encoding="utf-8", errors="replace").strip()
            if not destino.startswith("gitdir:"):
                return None
            git = (Path(carpeta) / destino[len("gitdir:"):].strip()).resolve()
        cabeza = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if cabeza.startswith("ref: refs/heads/"):
        return cabeza[len("ref: refs/heads/"):]
    return cabeza[:7] or None


def _entrada(nombre: str, ruta: str) -> dict:
    rama = rama_git(ruta)
    return {"name": nombre, "path": ruta, "git": rama is not None, "branch": rama}


def _raices() -> List[dict]:
    if os.name == "nt":
        return [
            _entrada(f"{letra}:\\", f"{letra}:\\")
            for letra in string.ascii_uppercase
            if os.path.isdir(f"{letra}:\\")
        ]
    return [_entrada("/", "/")]


def _oculta(entrada: os.DirEntry) -> bool:
    if entrada.name.startswith(".") or entrada.name.lower() in _OMITIDAS:
        return True
    if os.name == "nt":
        try:
            return bool(entrada.stat(follow_symlinks=False).st_file_attributes & _ATRIBUTOS_OCULTOS)
        except (OSError, AttributeError):
            return True
    return False


def normalizar(ruta) -> str:
    if not isinstance(ruta, str) or not ruta.strip() or len(ruta) > MAX_LARGO_RUTA or "\x00" in ruta:
        raise ErrorCarpeta("La ruta es invalida.")
    expandida = os.path.expanduser(ruta.strip())
    if not os.path.isabs(expandida):
        raise ErrorCarpeta("La ruta tiene que ser absoluta (o empezar con ~).")
    return os.path.realpath(expandida)


def listar(ruta, inicio: str) -> dict:
    base = {"home": str(Path.home()), "default": inicio}
    if ruta is None or (isinstance(ruta, str) and not ruta.strip()):
        return {"ok": True, **base, "path": "", "parent": None, "git": False, "branch": None,
                "entries": _raices(), "truncated": False}

    carpeta = normalizar(ruta)
    if not os.path.isdir(carpeta):
        raise ErrorCarpeta("Esa carpeta no existe en el servidor.", "not_found")
    try:
        with os.scandir(carpeta) as iterador:
            candidatas = [e for e in iterador if not _oculta(e) and e.is_dir()]
    except PermissionError:
        raise ErrorCarpeta("Sin permiso para leer esa carpeta.", "forbidden")
    except OSError as e:
        raise ErrorCarpeta(f"No se pudo leer la carpeta: {e.strerror or e}", "unavailable")

    candidatas.sort(key=lambda e: e.name.lower())
    entradas = [_entrada(e.name, os.path.join(carpeta, e.name)) for e in candidatas[:MAX_ENTRADAS]]
    padre = os.path.dirname(carpeta)
    rama = rama_git(carpeta)
    return {
        "ok": True,
        **base,
        "path": carpeta,
        "parent": "" if padre == carpeta else padre,
        "git": rama is not None,
        "branch": rama,
        "entries": entradas,
        "truncated": len(candidatas) > MAX_ENTRADAS,
    }


def atender(mensaje: dict, inicio: str) -> dict:
    return listar(mensaje.get("path"), inicio)
