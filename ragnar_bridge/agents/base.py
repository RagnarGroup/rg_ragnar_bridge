"""Interfaz de un agente (CLI) que el bridge sabe manejar.

Ragnar espera SIEMPRE el mismo formato de eventos: las lineas de stream-json de
Claude Code (`stream_event`/`assistant`/`user`/`result`). Un agente cuyo CLI
emite otra cosa (Antigravity) trae un traductor que las convierte en el borde,
aca, para que el orquestador de Ragnar no tenga que saber que agentes existen.
"""

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..protocolo import Turno


@dataclass
class Comando:
    argv: List[str]
    env: Dict[str, str]


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

    def preparar(self, turno: Turno, username: str) -> Comando:
        raise NotImplementedError

    def traductor(self, turno: Turno) -> Traductor:
        return Traductor()
