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
    """Un adaptador por cada agente que este bridge intenta manejar (esten o no
    instalados: eso lo dice `sondear`)."""
    return {nombre: _crear(nombre, cfg, estado_dir) for nombre in cfg.agentes_pedidos()}


def crear_adaptador(cfg: Config, estado_dir: Path) -> Adaptador:
    """Compatibilidad: el primer agente pedido."""
    return next(iter(crear_adaptadores(cfg, estado_dir).values()))


def sondear(adaptadores: Dict[str, Adaptador], con_cuota: bool = False) -> List[dict]:
    """Los agentes que de verdad estan instalados, con su version y si tienen
    sesion iniciada (True/False/None = no se sabe). Con `con_cuota` cada uno
    suma su `quota` (o None) -- cuesta lanzar otro proceso por CLI, asi que el
    primer sondeo, del que depende el `auth`, no la pide. Bloquea (lanza los
    CLIs): desde codigo async, con asyncio.to_thread."""
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
            # Sin sesion no hay cuota que leer.
            info["quota"] = adaptador.cuota(info["cli_version"]) if info["login"] is not False else None
        encontrados.append(info)
    return encontrados
