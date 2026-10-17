"""Imagenes que la persona adjunta a un mensaje del chat de Ragnar.

Los CLIs reciben el mensaje como texto, asi que la imagen no viaja dentro: se
guarda en <estado>/adjuntos/<conversacion>/ y al mensaje se le suma la ruta,
que el agente abre con su herramienta de lectura de archivos. Sirve igual
para cualquier CLI que lea un archivo del disco.
"""

import dataclasses
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .protocolo import Turno

DIAS_QUE_SE_CONSERVAN = 14


def carpeta(estado_dir: Path) -> Path:
    return Path(estado_dir) / "adjuntos"


def guardar(estado_dir: Path, turno: Turno) -> Turno:
    if not turno.adjuntos:
        return turno
    raiz = carpeta(estado_dir)
    _borrar_viejos(raiz)
    destino = raiz / turno.session_id
    destino.mkdir(parents=True, exist_ok=True)
    os.chmod(raiz, 0o700)
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    rutas = []
    for i, (extension, contenido) in enumerate(turno.adjuntos, 1):
        ruta = destino / f"{sello}-{i}.{extension}"
        ruta.write_bytes(contenido)
        os.chmod(ruta, 0o600)
        rutas.append(str(ruta))

    aviso = (
        "\n\n[El usuario adjunto "
        + ("una imagen" if len(rutas) == 1 else f"{len(rutas)} imagenes")
        + " a este mensaje. Estan guardadas en este servidor; abrilas con tu "
        "herramienta de lectura de archivos antes de responder:\n"
        + "\n".join(f"- {r}" for r in rutas)
        + "]"
    )
    return dataclasses.replace(
        turno,
        prompt=turno.prompt + aviso,
        prompt_fallback=turno.prompt_fallback + aviso,
        dirs_extra=turno.dirs_extra + (str(destino),),
    )


def _borrar_viejos(raiz: Path) -> None:
    limite = time.time() - DIAS_QUE_SE_CONSERVAN * 86400
    try:
        conversaciones = [p for p in raiz.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return
    for conversacion in conversaciones:
        try:
            if conversacion.stat().st_mtime < limite:
                shutil.rmtree(conversacion, ignore_errors=True)
        except OSError:
            continue
