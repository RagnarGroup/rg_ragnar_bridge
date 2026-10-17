import argparse
import asyncio
import getpass
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .agents import crear_adaptadores
from .api import VARIABLE_API, VARIABLE_TOKEN_AGENTE, ClienteRagnar, ErrorApi, token_del_entorno, url_api
from .bridge import SALIDA_AUTH, ErrorFatal, ejecutar, plataforma_local, probar_conexion
from .catalogo import publicar, sync_once
from .config import AGENTES, Config, ConfigError, cargar, guardar, ruta_por_defecto
from .sincronizar import PREFIJOS_BOOTSTRAP, Registro, resumen

_INSTALAR = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "codex": "npm install -g @openai/codex",
    "hermes": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
}
_LOGIN = {"claude": "claude", "agy": "agy", "codex": "codex login", "hermes": "hermes"}


VARIABLE_TOKEN = "RAGNAR_BRIDGE_TOKEN"


def _leer_token(args) -> str:
    if args.token:
        print(
            f"AVISO: --token esta deprecado (queda visible en `ps` y en el historial). "
            f"Pasalo por la variable {VARIABLE_TOKEN} o por stdin.",
            file=sys.stderr,
        )
        return args.token.strip()
    token = os.environ.get(VARIABLE_TOKEN, "").strip()
    if token:
        return token
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass("Token del servidor (ragbrg_..., no se muestra): ").strip()


def _init(args) -> int:
    token = _leer_token(args)
    if not token:
        print(
            f"Falta el token: pasalo por la variable {VARIABLE_TOKEN} o por stdin "
            "(la app lo muestra una sola vez al crear el servidor).",
            file=sys.stderr,
        )
        return 2
    cfg = Config(url=args.url, token=token, workdir=args.workdir)
    if args.model:
        if args.agent == "codex":
            cfg.codex_model = args.model
        else:
            cfg.agy_model = args.model

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
    except Exception as e:
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
        asyncio.run(ejecutar(cfg, (Path(args.config).parent if args.config else None), senales=True))
    except ErrorFatal as e:
        print(f"Ragnar rechazo la conexion: {e}", file=sys.stderr)
        return SALIDA_AUTH
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


def _sync(args) -> int:
    if not args.once:
        print(
            "Por ahora `sync` corre una sola vez: pasá --once. En un servidor, el "
            "bridge (`ragnar-bridge run`) ya recibe los cambios solo.",
            file=sys.stderr,
        )
        return 2
    estado = Path(args.estado).expanduser() if args.estado else ruta_por_defecto().parent
    try:
        cliente = ClienteRagnar(url_api(args.api), token_del_entorno(), "ragnar-bridge-sync")
        resultados = sync_once(
            cliente,
            args.cli,
            args.project,
            args.dir,
            Registro.en(estado),
            args.platform or plataforma_local(),
        )
    except (ErrorApi, ValueError, OSError) as e:
        print(f"ragnar-bridge sync: {e}", file=sys.stderr)
        return 1
    for r in resultados:
        if r["status"] in ("written", "updated"):
            print(f"  {'nuevo' if r['status'] == 'written' else 'actualizado'}: {r['path']}")
        elif r["status"] == "conflict":
            print(f"  CONFLICTO (editado a mano, no se pisó): {r['path']}")
    cuenta = resumen(resultados)
    print(
        f"ragnar-bridge sync {args.cli}{' ' + args.project if args.project else ''}: "
        f"{cuenta['written']} nuevos, {cuenta['updated']} actualizados, "
        f"{cuenta['unchanged']} sin cambios, {cuenta['conflict']} conflictos."
    )
    return 0


def _publicar(args) -> int:
    try:
        cliente = ClienteRagnar(url_api(args.api), token_del_entorno(), "ragnar-bridge-publicar")
        resultados = publicar(cliente, args.cli, args.project, args.carpeta)
    except (ErrorApi, ValueError, OSError) as e:
        print(f"ragnar-bridge publicar: {e}", file=sys.stderr)
        return 1
    publicados = 0
    for r in resultados:
        if r["status"] == "published":
            publicados += 1
            print(f"  publicado: {r['path']} (revisión {r.get('revision')})")
        elif r["status"] == "skipped":
            print(f"  omitido: {r['path']} ({r['motivo']})")
    sin_cambios = sum(1 for r in resultados if r["status"] == "unchanged")
    print(f"ragnar-bridge publicar: {publicados} publicados, {sin_cambios} sin cambios.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="ragnar-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help=f"ruta del config (default {ruta_por_defecto()})")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="crea el archivo de configuracion")
    p_init.add_argument("--url", required=True, help="wss://.../api/v1/bridge/ws (la muestra la app)")
    p_init.add_argument(
        "--token",
        help=(
            f"DEPRECADO (queda visible en `ps`): el token va por la variable {VARIABLE_TOKEN} "
            "o por stdin; sin ninguno de los dos se pide sin eco"
        ),
    )
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

    clis_bootstrap = sorted(PREFIJOS_BOOTSTRAP)
    ayuda_api = (
        f"URL de la API de Ragnar (default ${VARIABLE_API} o https://panel.ragnargroup.app/api/v1). "
        f"El token personal de agente se lee de ${VARIABLE_TOKEN_AGENTE}, nunca de argv"
    )
    p_sync = sub.add_parser(
        "sync",
        help="baja del catálogo de Ragnar los agentes/skills de un proyecto y los escribe sin pisar lo editado a mano",
    )
    p_sync.add_argument("--once", action="store_true", help="una sola pasada (obligatorio por ahora: no hay modo daemon)")
    p_sync.add_argument("--cli", required=True, choices=clis_bootstrap)
    p_sync.add_argument("--project", help="clave del proyecto (ej. RAG); sin ella, solo lo global")
    p_sync.add_argument("--dir", required=True, help="raíz del proyecto donde escribir (ej. el clon del repo)")
    p_sync.add_argument("--api", help=ayuda_api)
    p_sync.add_argument("--platform", choices=("linux", "windows", "macos"), help="default: la de esta máquina")
    p_sync.add_argument(
        "--estado",
        help="carpeta del registro de sha aplicados (default: la del config del bridge)",
    )
    p_sync.set_defaults(fn=_sync)

    p_pub = sub.add_parser(
        "publicar",
        help="sube al catálogo (solo administrador) los archivos de una carpeta bajo los prefijos permitidos",
    )
    p_pub.add_argument("--cli", required=True, choices=clis_bootstrap)
    p_pub.add_argument("--project", help="clave del proyecto; sin ella se publica como global")
    p_pub.add_argument("--api", help=ayuda_api)
    p_pub.add_argument("carpeta", help="raíz que contiene .claude/agents/, .claude/skills/, ...")
    p_pub.set_defaults(fn=_publicar)

    args = parser.parse_args()
    if not args.cmd:
        args.fn = _run
    sys.exit(args.fn(args))
