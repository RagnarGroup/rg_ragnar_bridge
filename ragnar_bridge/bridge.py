"""Conexion con Ragnar y ejecucion de turnos. Protocolo: ver el docstring de
services/bridge_hub.py en ragnar_group_back (es la fuente de verdad)."""

import asyncio
import hashlib
import json
import logging
import os
import random
import shutil
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Optional

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import PROTOCOLO, __version__
from .agents import crear_adaptadores, sondear
from .archivos import ArchivosEditables, ErrorArchivo
from .config import Config, ruta_por_defecto
from .protocolo import RunInvalido, validar_run

log = logging.getLogger("ragnar_bridge")

# Salida distinta a 1 para que systemd (RestartPreventExitStatus=78) no
# reintente para siempre con un token que Ragnar ya rechazo.
SALIDA_AUTH = 78

# El limite de frame de Ragnar (uvicorn) es 16 MiB; un `event` con un
# tool_result grande viaja como UN frame.
MAX_FRAME = 32 * 1024 * 1024


# Donde puede escribir el bootstrap dentro de un proyecto, por CLI.
PREFIJOS_BOOTSTRAP = {
    "claude": (".claude/agents/", ".claude/skills/"),
    "agy": (".agent/rules/", ".agent/workflows/"),
    "codex": (".codex/agents/", ".agents/skills/"),
}


class ErrorFatal(Exception):
    """Ragnar rechazo la conexion de forma que reintentar no arregla nada
    (token revocado/invalido, protocolo incompatible)."""


class Bridge:
    def __init__(self, cfg: Config, estado_dir: Optional[Path] = None):
        self.cfg = cfg
        self.estado_dir = estado_dir or ruta_por_defecto().parent
        self.adaptadores = crear_adaptadores(cfg, self.estado_dir)
        self.archivos = ArchivosEditables(cfg, self.estado_dir)
        # Lo que el ultimo sondeo encontro: [{name, cli_version, login}].
        self.agentes: list = []
        self._ws = None
        self._tareas_aux: set = set()
        self._envio = asyncio.Lock()
        self._procesos: Dict[str, asyncio.subprocess.Process] = {}
        self._tareas: Dict[str, asyncio.Task] = {}
        self.username = "bridge"

    # ---- envio

    async def enviar(self, mensaje: dict) -> None:
        # Varias tareas (una por turno) escriben al mismo socket.
        async with self._envio:
            await self._ws.send(json.dumps(mensaje, ensure_ascii=False))

    # ---- una conexion completa (auth -> welcome -> bucle de frames)

    async def sesion(self, ws) -> None:
        self._ws = ws
        # Se lanzan los CLIs (--version, auth status): en un hilo, para no
        # frenar el loop (que tambien atiende los turnos en curso).
        self.agentes = await asyncio.to_thread(sondear, self.adaptadores)
        primero = self.agentes[0] if self.agentes else {}
        await ws.send(
            json.dumps(
                {
                    "type": "auth",
                    "token": self.cfg.token,
                    "protocol": PROTOCOLO,
                    "version": __version__,
                    "agents": self.agentes,
                    # Formato de la 0.2 (un solo agente): un Ragnar viejo solo
                    # lee estos dos.
                    "agent": primero.get("name") or "",
                    "cli_version": primero.get("cli_version") or "",
                }
            )
        )
        bienvenida = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if bienvenida.get("type") == "error":
            if bienvenida.get("code") in ("auth", "protocol"):
                raise ErrorFatal(bienvenida.get("message") or bienvenida.get("code"))
            raise WebSocketException(bienvenida.get("message") or "error de Ragnar")
        if bienvenida.get("type") != "welcome":
            raise WebSocketException(f"Respuesta inesperada de Ragnar: {bienvenida.get('type')!r}")
        self.username = str(bienvenida.get("username") or "bridge")
        log.info(
            "Conectado a Ragnar como %s (servidor %r). Agentes: %s.",
            self.username,
            bienvenida.get("nombre"),
            ", ".join(a["name"] for a in self.agentes) or "ninguno instalado",
        )

        # La cuota no viaja en el `auth` (lanzar un proceso por CLI lo demoraria):
        # se manda enseguida en un frame `agents` aparte.
        tarea_cuota = asyncio.create_task(self._sondear_y_avisar())
        self._tareas_aux.add(tarea_cuota)
        tarea_cuota.add_done_callback(self._tareas_aux.discard)
        vigia = asyncio.create_task(self._vigilar_agentes())
        try:
            async for crudo in ws:
                try:
                    mensaje = json.loads(crudo)
                except json.JSONDecodeError:
                    continue
                if isinstance(mensaje, dict):
                    await self._atender(mensaje)
        finally:
            vigia.cancel()
            # Sin Ragnar del otro lado nadie ve lo que el agente hace en tu
            # servidor: se cortan los turnos en curso (Ragnar los da por
            # fallidos al ver el socket caer).
            await self._cortar_todo()
            self._ws = None

    async def _sondear_y_avisar(self) -> None:
        try:
            self.agentes = await asyncio.to_thread(sondear, self.adaptadores, True)
            await self.enviar({"type": "agents", "agents": self.agentes})
        except Exception:
            log.exception("No se pudo responder al pedido de re-sondeo.")

    async def _vigilar_agentes(self) -> None:
        """Vuelve a sondear cada `reprobar_cada` segundos: si instalaste agy o
        iniciaste sesion despues de arrancar el bridge, Ragnar se entera sin
        que reinicies nada."""
        while True:
            await asyncio.sleep(self.cfg.reprobar_cada)
            try:
                nuevos = await asyncio.to_thread(sondear, self.adaptadores, True)
                if nuevos != self.agentes:
                    self.agentes = nuevos
                    await self.enviar({"type": "agents", "agents": nuevos})
                    log.info("Agentes actualizados: %s", nuevos)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("No se pudieron re-sondear los agentes.")

    async def _atender(self, mensaje: dict) -> None:
        tipo = mensaje.get("type")
        task_id = str(mensaje.get("task_id") or "")
        if tipo == "run":
            if len(self._tareas) >= self.cfg.max_turnos:
                await self.enviar({"type": "error", "task_id": task_id, "message": "El servidor ya tiene el maximo de turnos en curso."})
                return
            if task_id in self._tareas:
                await self.enviar({"type": "error", "task_id": task_id, "message": "Esa conversacion ya tiene un turno en curso."})
                return
            # Ragnar dice con cual agente corre esta conversacion (una sesion
            # de claude no se retoma en agy); sin dato (un Ragnar viejo), el
            # primero que este instalado.
            nombre = mensaje.get("agent") or (self.agentes[0]["name"] if self.agentes else None)
            adaptador = self.adaptadores.get(nombre) if nombre else None
            if adaptador is None or nombre not in {a["name"] for a in self.agentes}:
                disponibles = ", ".join(a["name"] for a in self.agentes) or "ninguno"
                await self.enviar(
                    {
                        "type": "error",
                        "task_id": task_id,
                        "message": (
                            f"Este servidor no tiene {nombre or 'ningun agente'} instalado "
                            f"(disponibles: {disponibles})."
                        ),
                    }
                )
                return
            tarea = asyncio.create_task(self._turno(task_id, mensaje, adaptador))
            self._tareas[task_id] = tarea
            tarea.add_done_callback(lambda _t, tid=task_id: self._tareas.pop(tid, None))
        elif tipo == "probe":
            # La app pide "volver a comprobar" (te acabas de loguear): se
            # sondea YA y se responde con `agents`, sin esperar el ciclo. En una
            # tarea aparte: sondear lanza los CLIs y puede tardar unos segundos,
            # y este metodo corre dentro del bucle que lee el socket.
            self._tareas_aux.add(asyncio.create_task(self._sondear_y_avisar()))
        elif tipo == "bootstrap":
            tarea = asyncio.create_task(self._bootstrap(mensaje))
            self._tareas_aux.add(tarea)
            tarea.add_done_callback(self._tareas_aux.discard)
        elif tipo == "files":
            tarea = asyncio.create_task(self._archivos(mensaje))
            self._tareas_aux.add(tarea)
            tarea.add_done_callback(self._tareas_aux.discard)
        elif tipo == "cancel":
            await self._matar(task_id)
        elif tipo == "error":
            log.warning("Ragnar: %s", mensaje.get("message"))

    async def _archivos(self, mensaje: dict) -> None:
        request_id = str(mensaje.get("request_id") or "")
        try:
            resultado = await asyncio.to_thread(self.archivos.atender, mensaje)
            if mensaje.get("op") in ("add", "remove", "write"):
                log.info("Archivos editables: %s %s", mensaje.get("op"), mensaje.get("path"))
        except ErrorArchivo as error:
            resultado = {"ok": False, "error": str(error), "code": error.codigo}
        except Exception as error:
            resultado = {"ok": False, "error": str(error)[:300], "code": "invalid"}
        try:
            await self.enviar({"type": "files_result", "request_id": request_id, "result": resultado})
        except Exception:
            log.exception("No se pudo enviar el resultado del pedido de archivos.")

    async def _bootstrap(self, mensaje: dict) -> None:
        request_id = str(mensaje.get("request_id") or "")
        try:
            resultado = await asyncio.to_thread(self._aplicar_bootstrap, mensaje)
        except Exception as error:
            resultado = {"ok": False, "error": str(error)[:300], "files": []}
        try:
            await self.enviar({
                "type": "bootstrap_result",
                "request_id": request_id,
                "result": resultado,
            })
        except Exception:
            log.exception("No se pudo enviar el resultado del bootstrap.")

    def _aplicar_bootstrap(self, mensaje: dict) -> dict:
        cli = mensaje.get("cli")
        project_key = mensaje.get("project_key")
        mode = mensaje.get("mode")
        overwrite = mensaje.get("overwrite") is True
        if cli not in PREFIJOS_BOOTSTRAP or mode not in ("preview", "apply"):
            raise ValueError("CLI o modo de bootstrap inválido.")
        if not isinstance(project_key, str) or project_key not in self.cfg.project_paths:
            raise ValueError("Este proyecto no tiene una ruta configurada en el bridge.")
        archivos = mensaje.get("files")
        if not isinstance(archivos, list) or not archivos or len(archivos) > 200:
            raise ValueError("El manifiesto debe tener entre 1 y 200 archivos.")
        if sum(len(str(a.get("content", "")).encode("utf-8")) for a in archivos if isinstance(a, dict)) > 4 * 1024 * 1024:
            raise ValueError("El manifiesto supera el límite de 4 MiB.")

        raiz = Path(self.cfg.project_paths[project_key]).expanduser().resolve(strict=True)
        if not raiz.is_dir():
            raise ValueError("La ruta configurada para el proyecto no es un directorio.")
        prefijos = PREFIJOS_BOOTSTRAP[cli]
        preparados = []
        vistos = set()
        for archivo in archivos:
            if not isinstance(archivo, dict):
                raise ValueError("El manifiesto contiene un elemento inválido.")
            ruta = archivo.get("path")
            contenido = archivo.get("content")
            sha = archivo.get("sha256")
            if not isinstance(ruta, str) or not ruta.startswith(prefijos):
                raise ValueError("La ruta no está permitida para ese CLI.")
            if "\\" in ruta or "\x00" in ruta or PurePosixPath(ruta).is_absolute() or any(
                parte in ("", ".", "..") for parte in ruta.split("/")
            ):
                raise ValueError("La ruta debe ser relativa y no puede salir del proyecto.")
            if ruta in vistos:
                raise ValueError("El manifiesto contiene rutas duplicadas.")
            vistos.add(ruta)
            if not isinstance(contenido, str) or len(contenido.encode("utf-8")) > 262144:
                raise ValueError("El contenido de un archivo es inválido o supera 256 KiB.")
            calculado = hashlib.sha256(contenido.encode("utf-8")).hexdigest()
            if not isinstance(sha, str) or sha != calculado:
                raise ValueError(f"La huella de {ruta} no coincide.")
            destino = raiz.joinpath(*PurePosixPath(ruta).parts)
            if not destino.resolve(strict=False).is_relative_to(raiz):
                raise ValueError("Una ruta apunta fuera del proyecto.")
            actual = raiz
            for parte in PurePosixPath(ruta).parts:
                actual = actual / parte
                if actual.is_symlink():
                    raise ValueError("No se permiten enlaces simbólicos en rutas de destino.")
            if destino.exists() and not destino.is_file():
                raise ValueError("La ruta de destino existe y no es un archivo regular.")
            preparados.append((ruta, destino, contenido))

        resultados = [
            {"path": ruta, "status": "conflict" if destino.exists() else "create"}
            for ruta, destino, _ in preparados
        ]
        conflictos = [r for r in resultados if r["status"] == "conflict"]
        if mode == "preview":
            return {"ok": True, "mode": mode, "files": resultados}
        if conflictos and not overwrite:
            return {
                "ok": False,
                "error": "Hay archivos existentes. Revisá la vista previa y confirmá sobrescritura.",
                "files": resultados,
            }

        carpeta_respaldo = raiz / ".ragnar-bootstrap-backups"
        if carpeta_respaldo.is_symlink():
            raise ValueError("La carpeta de respaldos no puede ser un enlace simbólico.")
        respaldo = carpeta_respaldo / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        estados = {ruta: destino.exists() for ruta, destino, _ in preparados}
        for ruta, destino, contenido in preparados:
            destino.parent.mkdir(parents=True, exist_ok=True)
            if destino.exists():
                respaldo_destino = respaldo.joinpath(*PurePosixPath(ruta).parts)
                respaldo_destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destino, respaldo_destino)
            fd, temporal = tempfile.mkstemp(prefix=".ragnar-bootstrap-", dir=destino.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as archivo:
                    archivo.write(contenido)
                os.chmod(temporal, 0o600)
                os.replace(temporal, destino)
            finally:
                if os.path.exists(temporal):
                    os.unlink(temporal)
        return {
            "ok": True,
            "mode": mode,
            "files": [
                {"path": ruta, "status": "updated" if estados[ruta] else "written"}
                for ruta, destino, _ in preparados
            ],
        }

    # ---- un turno

    async def _turno(self, task_id: str, run: dict, adaptador) -> None:
        try:
            turno = validar_run(run)
        except RunInvalido as e:
            await self.enviar({"type": "error", "task_id": task_id, "message": f"run invalido: {e}"})
            return

        try:
            comando = adaptador.preparar(turno, self.username)
        except OSError as e:
            await self.enviar(
                {"type": "error", "task_id": task_id, "message": f"No se pudo preparar el turno: {e}"}
            )
            return
        traductor = adaptador.traductor(turno)

        kwargs = dict(
            cwd=self.cfg.workdir_abs,
            env=comando.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Una linea de stream-json puede pasar del limite por defecto
            # (64 KB) -- un tool_result con un archivo o un log grande
            # viaja como UNA linea; readline() relanza LimitOverrunError y
            # el turno entero moria.
            limit=64 * 1024 * 1024,
        )
        if os.name == "posix":
            # Algunos CLIs (conocido en codex-cli: openai/codex#15379,
            # #47735) dejan hijos que ignoran que les maten solo el PID: se
            # lanza en su propia sesion para poder matar el grupo entero si
            # hay que cortar el turno (ver _matar_proceso).
            kwargs["start_new_session"] = True
        try:
            proceso = await asyncio.create_subprocess_exec(*comando.argv, **kwargs)
        except OSError as e:
            await self.enviar(
                {
                    "type": "error",
                    "task_id": task_id,
                    "message": f"No se pudo lanzar el CLI ({' '.join(adaptador.cmd)}) en {self.cfg.workdir_abs}: {e}",
                }
            )
            return

        self._procesos[task_id] = proceso
        stderr_buf = bytearray()

        async def leer_stderr():
            # Concurrente con stdout: si nadie drena el pipe y el CLI escribe
            # mas que su buffer, se bloquea. Solo se conserva la cola.
            while True:
                trozo = await proceso.stderr.read(4096)
                if not trozo:
                    return
                stderr_buf.extend(trozo)
                if len(stderr_buf) > self.cfg.tope_stderr:
                    del stderr_buf[: len(stderr_buf) - self.cfg.tope_stderr]

        lector_err = asyncio.create_task(leer_stderr())
        try:
            while True:
                linea = await proceso.stdout.readline()
                if not linea:
                    break
                crudo = linea.decode("utf-8", errors="replace").strip()
                if not crudo:
                    continue
                if len(crudo) > self.cfg.tope_evento:
                    # Ragnar ignora los tipos que no conoce: se avisa que hubo
                    # algo en vez de tumbar el socket con un frame gigante.
                    await self.enviar({"type": "event", "task_id": task_id, "raw": {"type": "oversize", "bytes": len(crudo)}})
                    continue
                try:
                    evento = json.loads(crudo)
                except json.JSONDecodeError:
                    continue  # linea suelta que no es del protocolo
                if isinstance(evento, dict):
                    # El traductor convierte lo que emite el CLI al formato de
                    # Claude que Ragnar espera (para Claude no toca nada).
                    for raw in traductor.evento(evento):
                        await self.enviar({"type": "event", "task_id": task_id, "raw": raw})
            codigo = await proceso.wait()
            await lector_err
            stderr = bytes(stderr_buf).decode("utf-8", errors="replace")
            for raw in traductor.fin(codigo, stderr):
                await self.enviar({"type": "event", "task_id": task_id, "raw": raw})
            await self.enviar(
                {"type": "done", "task_id": task_id, "exit_code": codigo, "stderr": stderr}
            )
        except asyncio.CancelledError:
            await self._matar_proceso(proceso)
            raise
        except Exception:
            log.exception("El turno %s se rompio.", task_id)
            await self._matar_proceso(proceso)
            try:
                await self.enviar({"type": "error", "task_id": task_id, "message": "El bridge fallo a mitad del turno."})
            except Exception:
                pass
        finally:
            lector_err.cancel()
            self._procesos.pop(task_id, None)

    # ---- corte de procesos

    @staticmethod
    async def _matar_proceso(proceso: asyncio.subprocess.Process) -> None:
        # En POSIX se manda al grupo entero (ver el `start_new_session` al
        # lanzarlo en `_turno`), no solo al PID: un hijo que el CLI haya
        # dejado suelto tambien tiene que morir, o queda huerfano.
        def _cortar(fuerte: bool) -> None:
            # `signal.SIGKILL` no existe en Windows: no se puede referenciar
            # fuera de la rama POSIX aunque sea solo para comparar.
            if os.name == "posix":
                os.killpg(proceso.pid, signal.SIGKILL if fuerte else signal.SIGTERM)
            elif fuerte:
                proceso.kill()
            else:
                proceso.terminate()

        if proceso.returncode is not None:
            return
        try:
            _cortar(False)
            await asyncio.wait_for(proceso.wait(), 5)
        except asyncio.TimeoutError:
            try:
                _cortar(True)
            except ProcessLookupError:
                pass
            await proceso.wait()
        except ProcessLookupError:
            pass

    async def _matar(self, task_id: str) -> None:
        # El bucle de _turno ve el EOF de stdout y manda el `done` con el
        # codigo de salida real; aca solo se corta el proceso.
        proceso = self._procesos.get(task_id)
        if proceso is not None:
            await self._matar_proceso(proceso)

    async def _cortar_todo(self) -> None:
        for tarea in list(self._tareas.values()):
            tarea.cancel()
        if self._tareas:
            await asyncio.gather(*self._tareas.values(), return_exceptions=True)


async def ejecutar(cfg: Config, estado_dir: Optional[Path] = None) -> None:
    """Se conecta a Ragnar y reconecta con backoff exponencial hasta que lo
    frenen. Levanta ErrorFatal si Ragnar rechaza la credencial."""
    bridge = Bridge(cfg, estado_dir)
    espera = 1.0
    while True:
        try:
            async with connect(
                cfg.url,
                max_size=MAX_FRAME,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
            ) as ws:
                espera = 1.0
                await bridge.sesion(ws)
            log.info("Ragnar cerro la conexion.")
        except ErrorFatal:
            raise
        except (OSError, WebSocketException, asyncio.TimeoutError) as e:
            log.warning("Sin conexion con Ragnar (%s: %s).", type(e).__name__, e)
        # Jitter: si Ragnar se reinicia, todos los bridges reconectan a la vez.
        await asyncio.sleep(espera + random.uniform(0, espera / 2))
        espera = min(espera * 2, 60.0)


async def probar_conexion(cfg: Config) -> str:
    """Abre el socket, se autentica y se desconecta, sin correr ningun turno
    (no gasta cuota). Devuelve el nombre del servidor segun Ragnar; levanta
    ErrorFatal si Ragnar rechaza la credencial, o el error de red que sea."""
    agentes = await asyncio.to_thread(
        sondear, crear_adaptadores(cfg, ruta_por_defecto().parent)
    )
    primero = agentes[0] if agentes else {}
    async with connect(cfg.url, max_size=MAX_FRAME, open_timeout=15) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "auth",
                    "token": cfg.token,
                    "protocol": PROTOCOLO,
                    "version": __version__,
                    "agents": agentes,
                    "agent": primero.get("name") or "",
                    "cli_version": primero.get("cli_version") or "",
                }
            )
        )
        respuesta = json.loads(await asyncio.wait_for(ws.recv(), 15))
    if respuesta.get("type") == "error":
        raise ErrorFatal(respuesta.get("message") or respuesta.get("code"))
    return str(respuesta.get("nombre") or "")
