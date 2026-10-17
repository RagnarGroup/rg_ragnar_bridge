"""Archivos de configuracion que el dueno del servidor edita desde la app de
Ragnar (boton de configuracion de la pestana Agents): el settings.json de
Claude Code, su CLAUDE.md global y las rutas que el sume a mano.

Solo se lee y se escribe lo que esta en la lista: las dos rutas fijas mas las
que se agregaron, guardadas en <estado>/archivos-editables.json. Ragnar manda
la ruta y este modulo decide; nunca al reves.
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Config, ruta_por_defecto
from .protocolo import permiso_es_catastrofico

MAX_BYTES = 512 * 1024
MAX_RUTAS = 50
MAX_LARGO_RUTA = 1024
RESPALDOS_A_CONSERVAR = 30

_NOMBRES_PROHIBIDOS = (".credentials.json",)
_RAICES_PROHIBIDAS = ("/proc", "/sys", "/dev")


class ErrorArchivo(Exception):
    def __init__(self, mensaje: str, codigo: str = "invalid"):
        super().__init__(mensaje)
        self.codigo = codigo


def _huella(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()


class ArchivosEditables:
    def __init__(self, cfg: Config, estado_dir: Path):
        self.cfg = cfg
        self.estado_dir = Path(estado_dir)
        self._lista = self.estado_dir / "archivos-editables.json"


    def fijas(self) -> List[Path]:
        base = Path(self.cfg.config_dir_abs)
        return [self._real(base / "settings.json"), self._real(base / "CLAUDE.md")]

    def _agregadas(self) -> List[Path]:
        try:
            crudo = json.loads(self._lista.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rutas = crudo.get("rutas") if isinstance(crudo, dict) else None
        if not isinstance(rutas, list):
            return []
        return [Path(r) for r in rutas if isinstance(r, str) and r]

    def _guardar(self, rutas: List[Path]) -> None:
        self.estado_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._lista.with_name(self._lista.name + f".tmp-{os.getpid()}")
        tmp.write_text(
            json.dumps({"rutas": [str(r) for r in rutas]}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._lista)

    def _todas(self) -> List[Path]:
        fijas = self.fijas()
        return fijas + [r for r in self._agregadas() if r not in fijas]

    def listar(self) -> dict:
        fijas = self.fijas()
        return {
            "ok": True,
            "files": [
                {"path": str(r), "fixed": r in fijas, "exists": r.is_file()}
                for r in self._todas()
            ],
        }


    @staticmethod
    def _real(ruta: Path) -> Path:
        return Path(os.path.realpath(ruta))

    def _normalizar(self, ruta) -> Path:
        if not isinstance(ruta, str) or not ruta.strip() or len(ruta) > MAX_LARGO_RUTA or "\x00" in ruta:
            raise ErrorArchivo("La ruta es invalida.")
        expandida = os.path.expanduser(ruta.strip())
        if not os.path.isabs(expandida):
            raise ErrorArchivo("La ruta tiene que ser absoluta (o empezar con ~/).")
        return self._real(Path(expandida))

    def _prohibida(self, ruta: Path) -> bool:
        if ruta.name in _NOMBRES_PROHIBIDOS:
            return True
        vedadas = [
            self._real(self.estado_dir),
            self._real(ruta_por_defecto()),
            self._real(Path.home() / ".ssh"),
            self._real(Path(self.cfg.codex_home_abs) / "auth.json"),
            *[Path(r) for r in _RAICES_PROHIBIDAS],
        ]
        return any(ruta == v or v in ruta.parents for v in vedadas)

    def _de_la_lista(self, ruta) -> Path:
        real = self._normalizar(ruta)
        if real not in self._todas():
            raise ErrorArchivo("Esa ruta no esta en la lista de archivos editables.", "not_found")
        if self._prohibida(real):
            raise ErrorArchivo("Ese archivo guarda credenciales y no se edita desde la app.", "forbidden")
        return real


    def agregar(self, ruta) -> dict:
        real = self._normalizar(ruta)
        if self._prohibida(real):
            raise ErrorArchivo("Ese archivo guarda credenciales y no se edita desde la app.", "forbidden")
        if real.exists() and not real.is_file():
            raise ErrorArchivo("La ruta existe y no es un archivo.")
        if not real.exists() and not real.parent.is_dir():
            raise ErrorArchivo("La carpeta de esa ruta no existe en el servidor.", "not_found")
        agregadas = self._agregadas()
        if real not in self._todas():
            if len(agregadas) >= MAX_RUTAS:
                raise ErrorArchivo(f"Ya hay {MAX_RUTAS} rutas agregadas: quita alguna antes de sumar otra.")
            self._guardar(agregadas + [real])
        return self.listar()

    def quitar(self, ruta) -> dict:
        real = self._normalizar(ruta)
        if real in self.fijas():
            raise ErrorArchivo("Esa ruta es fija y no se puede quitar.", "forbidden")
        agregadas = self._agregadas()
        if real not in agregadas:
            raise ErrorArchivo("Esa ruta no esta en la lista de archivos editables.", "not_found")
        self._guardar([r for r in agregadas if r != real])
        return self.listar()

    def leer(self, ruta) -> dict:
        real = self._de_la_lista(ruta)
        if not real.exists():
            return {"ok": True, "path": str(real), "exists": False, "content": "", "sha256": None}
        crudo = self._bytes(real)
        try:
            contenido = crudo.decode("utf-8")
        except UnicodeDecodeError:
            raise ErrorArchivo("El archivo no es texto UTF-8.")
        return {"ok": True, "path": str(real), "exists": True, "content": contenido, "sha256": _huella(crudo)}

    @staticmethod
    def _bytes(real: Path) -> bytes:
        if not real.is_file():
            raise ErrorArchivo("La ruta existe y no es un archivo.")
        if real.stat().st_size > MAX_BYTES:
            raise ErrorArchivo(f"El archivo supera el limite de {MAX_BYTES // 1024} KiB.")
        return real.read_bytes()

    def escribir(self, ruta, contenido, sha256_base: Optional[str]) -> dict:
        real = self._de_la_lista(ruta)
        if not isinstance(contenido, str):
            raise ErrorArchivo("El contenido es invalido.")
        nuevo = contenido.encode("utf-8")
        if len(nuevo) > MAX_BYTES:
            raise ErrorArchivo(f"El contenido supera el limite de {MAX_BYTES // 1024} KiB.")

        anterior = self._bytes(real) if real.exists() else None
        if (_huella(anterior) if anterior is not None else None) != (sha256_base or None):
            raise ErrorArchivo(
                "El archivo cambio en el servidor desde que lo abriste. Volve a cargarlo.", "conflict"
            )
        if real.suffix == ".json":
            self._validar_json(real, contenido, anterior)

        if anterior is not None:
            self._respaldar(real)
        real.parent.mkdir(parents=True, exist_ok=True)
        fd, temporal = tempfile.mkstemp(prefix=".ragnar-edit-", dir=real.parent)
        try:
            with os.fdopen(fd, "wb") as archivo:
                archivo.write(nuevo)
            os.chmod(temporal, real.stat().st_mode & 0o777 if anterior is not None else 0o600)
            os.replace(temporal, real)
        finally:
            if os.path.exists(temporal):
                os.unlink(temporal)
        return {"ok": True, "path": str(real), "exists": True, "sha256": _huella(nuevo)}

    def _validar_json(self, real: Path, contenido: str, anterior: Optional[bytes]) -> None:
        try:
            datos = json.loads(contenido)
        except json.JSONDecodeError as e:
            raise ErrorArchivo(f"No es JSON valido (linea {e.lineno}, columna {e.colno}): {e.msg}.")
        if real != self.fijas()[0]:
            return
        try:
            previos = set(_permisos(json.loads(anterior.decode("utf-8")))) if anterior else set()
        except (ValueError, UnicodeDecodeError):
            previos = set()
        for permiso in _permisos(datos):
            if permiso not in previos and permiso_es_catastrofico(permiso):
                raise ErrorArchivo(
                    f"El permiso {permiso!r} esta bloqueado: es demasiado amplio para concederlo desde la app.",
                    "forbidden",
                )

    def _respaldar(self, real: Path) -> None:
        carpeta = self.estado_dir / "respaldos-archivos"
        destino = carpeta / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        destino.mkdir(parents=True, exist_ok=True)
        shutil.copy2(real, destino / real.name)
        for viejo in sorted(p for p in carpeta.iterdir() if p.is_dir())[:-RESPALDOS_A_CONSERVAR]:
            shutil.rmtree(viejo, ignore_errors=True)


    def atender(self, mensaje: dict) -> dict:
        if not self.cfg.archivos_editables:
            raise ErrorArchivo(
                "La edicion de archivos esta desactivada en la configuracion de este bridge.", "forbidden"
            )
        op = mensaje.get("op")
        ruta = mensaje.get("path")
        if op == "list":
            return self.listar()
        if op == "add":
            return self.agregar(ruta)
        if op == "remove":
            return self.quitar(ruta)
        if op == "read":
            return self.leer(ruta)
        if op == "write":
            return self.escribir(ruta, mensaje.get("content"), mensaje.get("sha256"))
        raise ErrorArchivo("Operacion de archivos desconocida.")


def _permisos(datos) -> List[str]:
    permisos = datos.get("permissions") if isinstance(datos, dict) else None
    lista = permisos.get("allow") if isinstance(permisos, dict) else None
    return [p for p in lista if isinstance(p, str)] if isinstance(lista, list) else []
