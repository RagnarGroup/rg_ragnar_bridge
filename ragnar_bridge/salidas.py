"""Imagenes que el agente genera para el usuario durante un turno.

Si Ragnar las acepta (`images_out` en el `run`), cada turno tiene su carpeta en
<estado>/salidas/<conversacion>/<turno>/: se le avisa al CLI en el mensaje y al
terminar el turno se mandan a Ragnar las imagenes validas que dejo ahi."""

import base64
import dataclasses
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .protocolo import FIRMAS_DE_IMAGEN, Turno

DIAS_QUE_SE_CONSERVAN = 14
MAX_IMAGENES = 8
MAX_BYTES_IMAGEN = 8 * 1024 * 1024
_MIME_POR_EXTENSION = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def carpeta(estado_dir: Path) -> Path:
    return Path(estado_dir) / "salidas"


def preparar(estado_dir: Path, turno: Turno) -> Turno:
    if not turno.imagenes_salida:
        return turno
    raiz = carpeta(estado_dir)
    _borrar_viejos(raiz)
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destino = raiz / turno.session_id / sello
    destino.mkdir(parents=True, exist_ok=True)
    os.chmod(raiz, 0o700)
    aviso = (
        "\n\n[Si en este turno generas imagenes para el usuario (por ejemplo con el "
        "image-generator de agy), guardalas como PNG, JPG o WebP en esta carpeta: "
        f"{destino} . Al terminar el turno se le muestran en el chat (hasta {MAX_IMAGENES}, "
        f"de {MAX_BYTES_IMAGEN // (1024 * 1024)} MB cada una). No dejes ahi nada que no sea "
        "para mostrarle.]"
    )
    return dataclasses.replace(
        turno,
        prompt=turno.prompt + aviso,
        prompt_fallback=turno.prompt_fallback + aviso,
        dirs_extra=turno.dirs_extra + (str(destino),),
        dir_salida=str(destino),
    )


def recolectar(dir_salida: str) -> List[dict]:
    try:
        candidatos = sorted(
            (p for p in Path(dir_salida).iterdir() if p.is_file() and not p.is_symlink()),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
    except OSError:
        return []
    imagenes = []
    for ruta in candidatos:
        if len(imagenes) >= MAX_IMAGENES:
            break
        mime = _MIME_POR_EXTENSION.get(ruta.suffix.lower())
        try:
            if mime is None or ruta.stat().st_size > MAX_BYTES_IMAGEN:
                continue
            contenido = ruta.read_bytes()
        except OSError:
            continue
        mime = next((m for m, (_, es) in FIRMAS_DE_IMAGEN.items() if es(contenido)), None)
        if mime is None:
            continue
        imagenes.append({"mime": mime, "data": base64.b64encode(contenido).decode(), "name": ruta.name})
    return imagenes


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
