import argparse
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .agents import crear_adaptadores
from .bridge import SALIDA_AUTH, ErrorFatal, ejecutar, probar_conexion
from .config import AGENTES, Config, ConfigError, cargar, guardar, ruta_por_defecto

_INSTALAR = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "codex": "npm install -g @openai/codex",
    "hermes": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
}
# Como iniciar sesion en cada CLI (lo que doctor le dice al usuario que corra).
_LOGIN = {"claude": "claude", "agy": "agy", "codex": "codex login", "hermes": "hermes"}


def _init(args) -> int:
    cfg = Config(url=args.url, token=args.token, workdir=args.workdir)
    if args.model:
        if args.agent == "codex":
            cfg.codex_model = args.model
        else:
            cfg.agy_model = args.model

    # Se guarda la ruta ABSOLUTA de cada CLI encontrado: el servicio de systemd
    # no siempre ve el mismo PATH que tu terminal.
    if args.agent:
        binario = args.cli or shutil.which(args.agent)
        if not binario:
            print(
                f"No encuentro `{args.agent}` en el PATH. Instalalo ({_INSTALAR[args.agent]}) y "
                f"logueate una vez (corre `{args.agent}`), o pasame la ruta con --cli.",
                file=sys.stderr,
            )
            return 2
        cfg.agents = [args.agent]
        setattr(cfg, f"{args.agent}_cmd", [binario])
        encontrados = [args.agent]
    else:
        encontrados = []
        for nombre in AGENTES:
            binario = shutil.which(nombre)
            if binario:
                setattr(cfg, f"{nombre}_cmd", [binario])
                encontrados.append(nombre)
        if not encontrados:
            print(
                "No encuentro ni `claude`, ni `agy`, ni `codex`, ni `hermes` en el PATH. Instalá al menos "
                "uno y logueate una vez:\n  Claude Code:  " + _INSTALAR["claude"] + "\n  Antigravity:  "
                + _INSTALAR["agy"] + "\n  Codex:        " + _INSTALAR["codex"] + "\n  Hermes:       "
                + _INSTALAR["hermes"],
                file=sys.stderr,
            )
            return 2

    ruta = guardar(cfg, Path(args.config) if args.config else None)
    print(f"Config escrita en {ruta} (permisos 600). Agentes detectados: {', '.join(encontrados)}.")
    return 0


def _doctor(args) -> int:
    """Verifica cada eslabon SIN correr ningun turno (no gasta cuota)."""
    ok = True

    def paso(bien: bool, texto: str) -> None:
        nonlocal ok
        ok = ok and bien
        print(f"  [{'ok' if bien else 'FALLA'}] {texto}")

    ruta = Path(args.config) if args.config else ruta_por_defecto()
    print(f"ragnar-bridge {__version__}")
    try:
        cfg = cargar(ruta)
    except ConfigError as e:
        paso(False, str(e))
        return 1
    paso(True, f"config {ruta}")

    # Con al menos UN agente instalado el bridge sirve: la app deja elegir entre
    # los que funcionan. Falta de sesion es un aviso, no un error: se corrige
    # logueandose y el bridge lo detecta solo (re-sondea cada pocos minutos).
    adaptadores = crear_adaptadores(cfg, ruta.parent)
    usables = 0
    for nombre, adaptador in adaptadores.items():
        if not adaptador.encontrado():
            print(f"  [--] {nombre}: no esta instalado ({_INSTALAR[nombre]})")
            continue
        sesion = adaptador.sesion_iniciada()
        estado = {True: "sesion iniciada", False: "SIN sesion: corre `%s` y logueate" % _LOGIN[nombre], None: "sesion sin comprobar"}[sesion]
        print(f"  [{'ok' if sesion is not False else '!!'}] {nombre}: {adaptador.version()} -- {estado}")
        usables += 1
    paso(usables > 0, "hay al menos un agente instalado" if usables else "no hay ningun agente instalado (claude, agy, codex o hermes)")
    paso(os.path.isdir(cfg.workdir_abs), f"carpeta de trabajo {cfg.workdir_abs}")

    try:
        nombre = asyncio.run(probar_conexion(cfg))
        paso(True, f"Ragnar acepto el token (servidor {nombre!r})")
    except ErrorFatal as e:
        paso(False, f"Ragnar rechazo la conexion: {e}")
    except Exception as e:  # red, DNS, TLS...
        paso(False, f"no pude conectar con {cfg.url}: {type(e).__name__}: {e}")

    print("Todo en orden." if ok else "Hay pasos con FALLA: corregilos y volve a correr `ragnar-bridge doctor`.")
    return 0 if ok else 1


def _run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = cargar(Path(args.config) if args.config else None)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        asyncio.run(ejecutar(cfg, (Path(args.config).parent if args.config else None)))
    except ErrorFatal as e:
        print(f"Ragnar rechazo la conexion: {e}", file=sys.stderr)
        return SALIDA_AUTH
    except KeyboardInterrupt:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="ragnar-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help=f"ruta del config (default {ruta_por_defecto()})")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="crea el archivo de configuracion")
    p_init.add_argument("--url", required=True, help="wss://.../api/v1/bridge/ws (la muestra la app)")
    p_init.add_argument("--token", required=True, help="ragbrg_... (la app lo muestra una sola vez)")
    p_init.add_argument(
        "--agent",
        choices=AGENTES,
        help="manejar SOLO este CLI (por defecto detecta claude y agy, los que esten instalados)",
    )
    p_init.add_argument("--cli", help="ruta del CLI si no esta en el PATH (con --agent)")
    p_init.add_argument("--model", help="(agy, codex) modelo; ver `agy models`. Sin --agent aplica a agy")
    p_init.add_argument("--workdir", default="~", help="donde arranca cada turno (default ~)")
    p_init.set_defaults(fn=_init)

    p_doc = sub.add_parser("doctor", help="verifica config, CLIs y conexion con Ragnar (sin gastar cuota)")
    p_doc.set_defaults(fn=_doctor)

    p_run = sub.add_parser("run", help="conecta con Ragnar (es lo que corre el servicio)")
    p_run.set_defaults(fn=_run)

    args = parser.parse_args()
    if not args.cmd:
        args.fn = _run
    sys.exit(args.fn(args))
