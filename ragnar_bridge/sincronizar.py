"""Escritura de agentes/skills del catalogo de Ragnar en un proyecto local, para el
frame `bootstrap` (preview/apply/auto), `sync --once` y `publicar`.

Politica `auto`: escribe lo que no existe, actualiza lo que no se toco desde la
ultima revision aplicada y nunca pisa lo editado a mano ("conflict"). El registro
de sha aplicados vive en el directorio de estado del bridge, no en el proyecto."""

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

PREFIJOS_BOOTSTRAP = {
    "claude": (".claude/agents/", ".claude/skills/"),
    "agy": (".agent/rules/", ".agent/workflows/"),
    "codex": (".codex/agents/", ".agents/skills/"),
}

MAX_ARCHIVOS = 200
MAX_BYTES = 4 * 1024 * 1024
MAX_ARCHIVO = 262144

NOMBRE_REGISTRO = "bootstrap-aplicados.json"

Preparado = Tuple[str, Path, str]


class ManifiestoInvalido(ValueError):
    pass


def sha256_texto(contenido: str) -> str:
    return hashlib.sha256(contenido.encode("utf-8")).hexdigest()


def sha256_archivo(ruta: Path) -> Optional[str]:
    try:
        return hashlib.sha256(ruta.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def validar_cli(cli) -> Tuple[str, ...]:
    if cli == "hermes":
        raise ManifiestoInvalido(
            "Hermes no tiene una carpeta estándar de agentes/skills por proyecto "
            "(sus skills son globales, en ~/.hermes/skills/): el bootstrap no la soporta."
        )
    if cli not in PREFIJOS_BOOTSTRAP:
        raise ManifiestoInvalido("CLI o modo de bootstrap inválido.")
    return PREFIJOS_BOOTSTRAP[cli]


def ruta_permitida(cli: str, ruta) -> bool:
    if not isinstance(ruta, str) or not ruta.startswith(PREFIJOS_BOOTSTRAP.get(cli, ())):
        return False
    if "\\" in ruta or "\x00" in ruta or PurePosixPath(ruta).is_absolute():
        return False
    return not any(parte in ("", ".", "..") for parte in ruta.split("/"))


def resolver_raiz(carpeta) -> Path:
    raiz = Path(carpeta).expanduser().resolve(strict=True)
    if not raiz.is_dir():
        raise ManifiestoInvalido("La ruta configurada para el proyecto no es un directorio.")
    return raiz


def preparar(cli, raiz: Path, archivos) -> List[Preparado]:
    prefijos = validar_cli(cli)
    if not isinstance(archivos, list) or not archivos or len(archivos) > MAX_ARCHIVOS:
        raise ManifiestoInvalido("El manifiesto debe tener entre 1 y 200 archivos.")
    if sum(len(str(a.get("content", "")).encode("utf-8")) for a in archivos if isinstance(a, dict)) > MAX_BYTES:
        raise ManifiestoInvalido("El manifiesto supera el límite de 4 MiB.")
    preparados: List[Preparado] = []
    vistos = set()
    for archivo in archivos:
        if not isinstance(archivo, dict):
            raise ManifiestoInvalido("El manifiesto contiene un elemento inválido.")
        ruta = archivo.get("path")
        contenido = archivo.get("content")
        sha = archivo.get("sha256")
        if not isinstance(ruta, str) or not ruta.startswith(prefijos):
            raise ManifiestoInvalido("La ruta no está permitida para ese CLI.")
        if not ruta_permitida(cli, ruta):
            raise ManifiestoInvalido("La ruta debe ser relativa y no puede salir del proyecto.")
        if ruta in vistos:
            raise ManifiestoInvalido("El manifiesto contiene rutas duplicadas.")
        vistos.add(ruta)
        if not isinstance(contenido, str) or len(contenido.encode("utf-8")) > MAX_ARCHIVO:
            raise ManifiestoInvalido("El contenido de un archivo es inválido o supera 256 KiB.")
        if not isinstance(sha, str) or sha != sha256_texto(contenido):
            raise ManifiestoInvalido(f"La huella de {ruta} no coincide.")
        destino = raiz.joinpath(*PurePosixPath(ruta).parts)
        if not _dentro_de(destino.resolve(strict=False), raiz):
            raise ManifiestoInvalido("Una ruta apunta fuera del proyecto.")
        actual = raiz
        for parte in PurePosixPath(ruta).parts:
            actual = actual / parte
            if es_enlace(actual):
                raise ManifiestoInvalido("No se permiten enlaces simbólicos en rutas de destino.")
        if destino.exists() and not destino.is_file():
            raise ManifiestoInvalido("La ruta de destino existe y no es un archivo regular.")
        preparados.append((ruta, destino, contenido))
    return preparados


def es_enlace(ruta: Path) -> bool:
    if ruta.is_symlink():
        return True
    es_junction = getattr(os.path, "isjunction", None)
    if es_junction is not None:
        return es_junction(ruta)
    try:
        atributos = getattr(os.lstat(ruta), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(atributos & 0x400)


def _dentro_de(ruta: Path, raiz: Path) -> bool:
    try:
        ruta.relative_to(raiz)
        return True
    except ValueError:
        return False


def escribir_atomico(destino: Path, contenido: str) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, temporal = tempfile.mkstemp(prefix=".ragnar-bootstrap-", dir=destino.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as archivo:
            archivo.write(contenido)
        os.chmod(temporal, 0o600)
        os.replace(temporal, destino)
    finally:
        if os.path.exists(temporal):
            os.unlink(temporal)


class Registro:
    def __init__(self, archivo: Path):
        self.archivo = Path(archivo)
        self._datos: Dict[str, Dict[str, str]] = {}
        try:
            crudo = json.loads(self.archivo.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            crudo = {}
        if isinstance(crudo, dict):
            for raiz, rutas in crudo.items():
                if isinstance(raiz, str) and isinstance(rutas, dict):
                    self._datos[raiz] = {
                        r: s for r, s in rutas.items() if isinstance(r, str) and isinstance(s, str)
                    }

    @classmethod
    def en(cls, estado_dir) -> "Registro":
        return cls(Path(estado_dir) / NOMBRE_REGISTRO)

    def sha(self, raiz: Path, ruta: str) -> Optional[str]:
        return self._datos.get(str(raiz), {}).get(ruta)

    def marcar(self, raiz: Path, ruta: str, sha: str) -> None:
        self._datos.setdefault(str(raiz), {})[ruta] = sha

    def guardar(self) -> None:
        self.archivo.parent.mkdir(parents=True, exist_ok=True)
        fd, temporal = tempfile.mkstemp(prefix=".registro-", dir=self.archivo.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as archivo:
                archivo.write(json.dumps(self._datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            os.chmod(temporal, 0o600)
            os.replace(temporal, self.archivo)
        finally:
            if os.path.exists(temporal):
                os.unlink(temporal)


def aplicar_auto(raiz: Path, preparados: Iterable[Preparado], registro: Registro) -> List[dict]:
    resultados = []
    for ruta, destino, contenido in preparados:
        nuevo = sha256_texto(contenido)
        local = sha256_archivo(destino)
        if local is None:
            escribir_atomico(destino, contenido)
            estado = "written"
        elif local == nuevo:
            estado = "unchanged"
        elif local == registro.sha(raiz, ruta):
            escribir_atomico(destino, contenido)
            estado = "updated"
        else:
            resultados.append({"path": ruta, "status": "conflict"})
            continue
        registro.marcar(raiz, ruta, nuevo)
        resultados.append({"path": ruta, "status": estado})
    registro.guardar()
    return resultados


def marcar_aplicados(raiz: Path, preparados: Iterable[Preparado], registro: Registro) -> None:
    for ruta, _destino, contenido in preparados:
        registro.marcar(raiz, ruta, sha256_texto(contenido))
    registro.guardar()


def resumen(resultados: List[dict]) -> Dict[str, int]:
    cuenta = {"written": 0, "updated": 0, "unchanged": 0, "conflict": 0}
    for r in resultados:
        cuenta[r["status"]] = cuenta.get(r["status"], 0) + 1
    return cuenta
