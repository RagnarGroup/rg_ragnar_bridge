from pathlib import Path
from typing import Dict, List

from ..config import Config
from .base import Adaptador


def _crear(nombre: str, cfg: Config, estado_dir: Path) -> Adaptador:
    if nombre == "claude":
        from .claude import ClaudeAdaptador

        return ClaudeAdaptador(cfg, estado_dir)
    if nombre == "agy":
        from .agy import AgyAdaptador

        return AgyAdaptador(cfg, estado_dir)
    if nombre == "codex":
        from .codex import CodexAdaptador

        return CodexAdaptador(cfg, estado_dir)
    if nombre == "hermes":
        from .hermes import HermesAdaptador

        return HermesAdaptador(cfg, estado_dir)
    raise ValueError(f"Agente desconocido: {nombre!r}")


def crear_adaptadores(cfg: Config, estado_dir: Path) -> Dict[str, Adaptador]:
    return {nombre: _crear(nombre, cfg, estado_dir) for nombre in cfg.agentes_pedidos()}


def crear_adaptador(cfg: Config, estado_dir: Path) -> Adaptador:
    return next(iter(crear_adaptadores(cfg, estado_dir).values()))


def sondear(adaptadores: Dict[str, Adaptador], con_cuota: bool = False) -> List[dict]:
    encontrados = []
    for nombre, adaptador in adaptadores.items():
        if not adaptador.encontrado():
            continue
        info = {
            "name": nombre,
            "cli_version": adaptador.version(),
            "login": adaptador.sesion_iniciada(),
        }
        if con_cuota:
            info["quota"] = adaptador.cuota(info["cli_version"]) if info["login"] is not False else None
        encontrados.append(info)
    return encontrados
