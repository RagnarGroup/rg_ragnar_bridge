"""Interfaz de un agente (CLI) del bridge: `Adaptador` arma el comando de cada turno
y como iniciar sesion desde la app (`RecetaLogin`), y `Traductor` convierte su salida
al stream-json de Claude Code (`stream_event`/`assistant`/`user`/`result`), el unico
formato que Ragnar entiende."""

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Tuple

from ..config import Config
from ..protocolo import Turno


@dataclass
class Comando:
    argv: List[str]
    env: Dict[str, str]


@dataclass(frozen=True)
class RecetaLogin:
    argv: List[str]
    env: Dict[str, str]
    patron_url: Pattern
    pide_codigo: bool
    patron_codigo: Optional[Pattern] = None
    respuestas: Tuple[Tuple[str, str], ...] = ()
    sale_solo: bool = True


def patron(texto: str) -> Pattern:
    return re.compile(texto)


def ventana(minutos: int) -> str:
    if minutos == 10080:
        return "semana"
    if minutos % 60 == 0:
        return f"{minutos // 60}h"
    return f"{minutos}min"


def bloque_de_cuota(grupo: Optional[str], ventana_: str, usado: float, resetea: Optional[datetime]) -> dict:
    return {
        "grupo": grupo,
        "ventana": ventana_,
        "usado": max(0, min(100, round(usado))),
        "resetea": resetea.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if resetea else None,
    }


class Traductor:
    def evento(self, linea: dict) -> List[dict]:
        return [linea]

    def fin(self, codigo: Optional[int], stderr: str) -> List[dict]:
        return []


class Adaptador:
    nombre = ""

    def __init__(self, cfg: Config, estado_dir: Path):
        self.cfg = cfg
        self.estado_dir = estado_dir

    @property
    def cmd(self) -> List[str]:
        raise NotImplementedError

    def version(self) -> str:
        try:
            salida = subprocess.run(
                [*self.cmd, "--version"], capture_output=True, text=True, timeout=20
            )
            return (salida.stdout or salida.stderr).strip()[:80] or "desconocida"
        except (OSError, subprocess.SubprocessError):
            return "no encontrado"

    def encontrado(self) -> bool:
        return shutil.which(self.cmd[0]) is not None

    def sesion_iniciada(self) -> Optional[bool]:
        return None

    def cuota(self, version: str) -> Optional[dict]:
        return None

    def receta_login(self) -> Optional[RecetaLogin]:
        return None

    def preparar(self, turno: Turno, username: str) -> Comando:
        raise NotImplementedError

    def traductor(self, turno: Turno) -> Traductor:
        return Traductor()
