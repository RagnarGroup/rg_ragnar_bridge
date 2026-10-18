"""`ragnar-bridge sync --once` y `ragnar-bridge publicar` contra el catalogo de
bootstrap de Ragnar.

* sync: baja el manifiesto vigente y lo escribe con la politica `auto`, que
  nunca pisa lo editado a mano.
* publicar: sube como artefacto cada archivo bajo los prefijos permitidos del
  CLI cuyo sha cambio respecto de la revision vigente (solo administrador)."""

import hashlib
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .api import ClienteRagnar
from .sincronizar import (
    MAX_ARCHIVO,
    PREFIJOS_BOOTSTRAP,
    Registro,
    aplicar_auto,
    es_enlace,
    preparar,
    resolver_raiz,
    ruta_permitida,
    sha256_texto,
    validar_cli,
)

_PATRON_CLAVE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
MAX_CLAVE = 120
SIN_PROYECTO = "_global"


def sync_once(
    cliente: ClienteRagnar,
    cli: str,
    project_key: Optional[str],
    carpeta,
    registro: Registro,
    plataforma: str,
) -> List[dict]:
    validar_cli(cli)
    raiz = resolver_raiz(carpeta)
    artefactos = cliente.get(
        "/bootstrap/manifest",
        {"cli": cli, "project_key": project_key, "platform": plataforma},
    )
    if not isinstance(artefactos, list):
        raise ValueError("Ragnar devolvio un manifiesto con forma inesperada.")
    if not artefactos:
        return []
    archivos = [
        {
            "path": a.get("relative_path"),
            "content": a.get("content"),
            "sha256": a.get("sha256"),
        }
        for a in artefactos
        if isinstance(a, dict)
    ]
    preparados = preparar(cli, raiz, archivos)
    return aplicar_auto(raiz, preparados, registro)


def clave_artefacto(cli: str, project_key: Optional[str], ruta: str) -> str:
    clave = f"{cli}/{project_key or SIN_PROYECTO}/{ruta}"
    if len(clave) <= MAX_CLAVE and _PATRON_CLAVE.match(clave):
        return clave
    huella = hashlib.sha256(ruta.encode("utf-8")).hexdigest()[:32]
    return f"{cli}/{project_key or SIN_PROYECTO}/h-{huella}"


def recolectar(cli: str, carpeta) -> Tuple[List[Tuple[str, str]], List[dict]]:
    validar_cli(cli)
    raiz = resolver_raiz(carpeta)
    encontrados: List[Tuple[str, str]] = []
    omitidos: List[dict] = []
    for prefijo in PREFIJOS_BOOTSTRAP[cli]:
        base = raiz.joinpath(*prefijo.rstrip("/").split("/"))
        if not base.is_dir() or _tiene_symlink(raiz, base):
            continue
        for directorio, subdirs, nombres in os.walk(base):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and not es_enlace(Path(directorio, d)))
            for nombre in sorted(nombres):
                archivo = Path(directorio, nombre)
                ruta = archivo.relative_to(raiz).as_posix()
                if es_enlace(archivo):
                    omitidos.append({"path": ruta, "motivo": "enlace simbólico"})
                    continue
                if not ruta_permitida(cli, ruta):
                    omitidos.append({"path": ruta, "motivo": "ruta no permitida"})
                    continue
                crudo = archivo.read_bytes()
                if not crudo:
                    omitidos.append({"path": ruta, "motivo": "vacío"})
                    continue
                if len(crudo) > MAX_ARCHIVO:
                    omitidos.append({"path": ruta, "motivo": "supera 256 KiB"})
                    continue
                try:
                    contenido = crudo.decode("utf-8")
                except UnicodeDecodeError:
                    omitidos.append({"path": ruta, "motivo": "no es texto UTF-8"})
                    continue
                encontrados.append((ruta, contenido))
    return encontrados, omitidos


def _tiene_symlink(raiz: Path, ruta: Path) -> bool:
    actual = raiz
    for parte in ruta.relative_to(raiz).parts:
        actual = actual / parte
        if es_enlace(actual):
            return True
    return False


def publicar(cliente: ClienteRagnar, cli: str, project_key: Optional[str], carpeta) -> List[dict]:
    encontrados, omitidos = recolectar(cli, carpeta)
    vigentes = cliente.get(
        "/bootstrap/catalog", {"cli": cli, "project_key": project_key, "platform": "any"}
    )
    sha_vigente = {
        a.get("artifact_key"): a.get("sha256")
        for a in (vigentes if isinstance(vigentes, list) else [])
        if isinstance(a, dict) and a.get("platform") == "any" and a.get("project_key") == project_key
    }
    resultados: List[dict] = [{"path": o["path"], "status": "skipped", "motivo": o["motivo"]} for o in omitidos]
    for ruta, contenido in encontrados:
        clave = clave_artefacto(cli, project_key, ruta)
        if sha_vigente.get(clave) == sha256_texto(contenido):
            resultados.append({"path": ruta, "status": "unchanged", "artifact_key": clave})
            continue
        creado = cliente.post(
            "/bootstrap/artifacts",
            {
                "artifact_key": clave,
                "title": ruta[:200],
                "cli": cli,
                "project_key": project_key,
                "platform": "any",
                "relative_path": ruta,
                "content": contenido,
            },
        )
        resultados.append(
            {
                "path": ruta,
                "status": "published",
                "artifact_key": clave,
                "revision": creado.get("revision") if isinstance(creado, dict) else None,
            }
        )
    return resultados
