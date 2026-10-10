"""Conexion con Ragnar y ejecucion de turnos. Protocolo: ver el docstring de
services/bridge_hub.py en ragnar_group_back (es la fuente de verdad)."""

import asyncio
import json
import logging
import os
import random
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Dict, Optional, Union

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import PROTOCOLO, __version__
from .agents import crear_adaptadores, sondear
from .config import Config, ruta_por_defecto
from .protocolo import RunInvalido, validar_run
from .sincronizar import (  # noqa: F401  (PREFIJOS_BOOTSTRAP se reexporta)
    PREFIJOS_BOOTSTRAP,
    ManifiestoInvalido,
    Registro,
    aplicar_auto,
    marcar_aplicados,
    preparar,
    resolver_raiz,
)

log = logging.getLogger("ragnar_bridge")

# Salida distinta a 1 para que systemd (RestartPreventExitStatus=78) no
# reintente para siempre con un token que Ragnar ya rechazo.
SALIDA_AUTH = 78

# El limite de frame de Ragnar (uvicorn) es 16 MiB; un `event` con un
# tool_result grande viaja como UN frame.
MAX_FRAME = 32 * 1024 * 1024

# Cuanto se sigue leyendo stdout/stderr despues de que el CLI salio. Lo que el
# CLI escribio antes de salir ya esta en el pipe y se lee enseguida; si pasado
# esto no hay EOF es que un nieto heredo el pipe y lo mantiene abierto.
ESPERA_DRENAJE = 5.0

# Lo que se tolera por encima de `tope_evento` en el buffer de una linea de
# stdout antes de descartarla sin terminar de leerla.
MARGEN_LINEA = 64 * 1024
TROZO_LECTURA = 64 * 1024


def plataforma_local() -> str:
    """Lo que el bridge declara en `platform` [v2]: elige las variantes de un
    artefacto del catalogo (any + la suya)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


async def _salida_del_lider(proceso: asyncio.subprocess.Process) -> int:
    """Codigo de salida del proceso que lanzamos, apenas EL termina.

    No es `proceso.wait()`: en asyncio (verificado en 3.12/3.13) ese espera
    ademas a que se cierren sus pipes, y un nieto que heredo stdout los mantiene abiertos
    (justo el caso que hay que poder cortar, RAG-202). `returncode` se llena
    cuando el lider sale, sin importar los pipes."""
    while proceso.returncode is None:
        await asyncio.sleep(0.05)
    return proceso.returncode


async def _lineas(stream: asyncio.StreamReader, tope: int) -> AsyncIterator[Union[bytes, int]]:
    """Las lineas de `stream`, sin el salto. Una linea de mas de `tope` bytes
    no se retiene entera: se descarta a medida que llega y en su lugar sale su
    largo (un int). Asi la memoria de un turno queda acotada en ~`tope` +
    un trozo, en vez de lo que el CLI decida emitir en una sola linea."""
    buf = bytearray()
    descartados = 0  # > 0: estamos dentro de una linea gigante
    while True:
        trozo = await stream.read(TROZO_LECTURA)
        if not trozo:
            if descartados:
                yield descartados + len(buf)
            elif buf:
                yield bytes(buf)
            return
        buf.extend(trozo)
        while True:
            fin = buf.find(b"\n")
            if fin < 0:
                break
            linea = bytes(buf[:fin])
            del buf[: fin + 1]
            if descartados:
                yield descartados + len(linea)
                descartados = 0
            elif len(linea) > tope:
                yield len(linea)
            else:
                yield linea
        if len(buf) > tope:
            descartados += len(buf)
            buf.clear()


async def _esperar_reconexion(segundos: float) -> None:
    # Aparte para que los tests puedan ver el backoff sin esperarlo.
    await asyncio.sleep(segundos)


def _leer_bienvenida(crudo) -> dict:
    """Primer frame de Ragnar. Algo que no sea un objeto JSON (un proxy que
    contesta HTML, un servidor a medio desplegar) es un fallo de conexion mas:
    se reintenta con backoff, no tumba a `ejecutar` (RAG-208)."""
    try:
        mensaje = json.loads(crudo)
    except (ValueError, TypeError):
        raise WebSocketException("Ragnar contesto algo que no es JSON al autenticar.")
    if not isinstance(mensaje, dict):
        raise WebSocketException("Ragnar contesto algo inesperado al autenticar.")
    return mensaje


MODOS_BOOTSTRAP = ("preview", "apply", "auto")


def validar_cli_y_modo(cli, mode) -> None:
    from .sincronizar import validar_cli

    validar_cli(cli)
    if mode not in MODOS_BOOTSTRAP:
        raise ManifiestoInvalido("CLI o modo de bootstrap inválido.")


class ErrorFatal(Exception):
    """Ragnar rechazo la conexion de forma que reintentar no arregla nada
    (token revocado/invalido, protocolo incompatible)."""


class Bridge:
    def __init__(self, cfg: Config, estado_dir: Optional[Path] = None):
        self.cfg = cfg
        self.estado_dir = estado_dir or ruta_por_defecto().parent
        self.adaptadores = crear_adaptadores(cfg, self.estado_dir)
        # Lo que el ultimo sondeo encontro: [{name, cli_version, login}].
        self.agentes: list = []
        self._ws = None
        self._tareas_aux: set = set()
        self._envio = asyncio.Lock()
        self._procesos: Dict[str, asyncio.subprocess.Process] = {}
        self._tareas: Dict[str, asyncio.Task] = {}
        # Turnos con un `cancel` que llego antes de que su CLI arrancara.
        self._cancelados: set = set()
        self.username = "bridge"
        # True en cuanto Ragnar contesta un `welcome` valido en la conexion
        # actual: recien ahi `ejecutar` reinicia el backoff.
        self.bienvenido = False

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
                    # [v2] Solo las CLAVES de project_paths (las rutas no salen
                    # de este servidor): a que proyectos le llega el bootstrap
                    # automatico. Y la plataforma, para elegir variantes.
                    "projects": sorted(self.cfg.project_paths),
                    "platform": plataforma_local(),
                    # Formato de la 0.2 (un solo agente): un Ragnar viejo solo
                    # lee estos dos.
                    "agent": primero.get("name") or "",
                    "cli_version": primero.get("cli_version") or "",
                }
            )
        )
        bienvenida = _leer_bienvenida(await asyncio.wait_for(ws.recv(), 15))
        if bienvenida.get("type") == "error":
            if bienvenida.get("code") in ("auth", "protocol"):
                raise ErrorFatal(bienvenida.get("message") or bienvenida.get("code"))
            raise WebSocketException(bienvenida.get("message") or "error de Ragnar")
        if bienvenida.get("type") != "welcome":
            raise WebSocketException(f"Respuesta inesperada de Ragnar: {bienvenida.get('type')!r}")
        self.username = str(bienvenida.get("username") or "bridge")
        self.bienvenido = True
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
        elif tipo == "cancel":
            await self._matar(task_id)
        elif tipo == "error":
            log.warning("Ragnar: %s", mensaje.get("message"))

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
        """Frame `bootstrap` (ver bridge_hub.py). preview/apply los pide un
        humano desde la app; `auto` [v2] lo manda Ragnar solo al publicarse
        una revision (RAG-217) y nunca pisa un archivo editado a mano."""
        cli = mensaje.get("cli")
        project_key = mensaje.get("project_key")
        mode = mensaje.get("mode")
        overwrite = mensaje.get("overwrite") is True
        validar_cli_y_modo(cli, mode)
        if not isinstance(project_key, str) or project_key not in self.cfg.project_paths:
            raise ManifiestoInvalido("Este proyecto no tiene una ruta configurada en el bridge.")
        archivos = mensaje.get("files")
        raiz = resolver_raiz(self.cfg.project_paths[project_key])
        preparados = preparar(cli, raiz, archivos)
        registro = Registro.en(self.estado_dir)

        if mode == "auto":
            resultados = aplicar_auto(raiz, preparados, registro)
            conflictos = [r["path"] for r in resultados if r["status"] == "conflict"]
            if conflictos:
                log.warning(
                    "Bootstrap automatico de %s: no se pisaron %d archivos editados a mano: %s",
                    project_key,
                    len(conflictos),
                    ", ".join(conflictos),
                )
            return {"ok": True, "mode": mode, "files": resultados}

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
            raise ManifiestoInvalido("La carpeta de respaldos no puede ser un enlace simbólico.")
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
        # Lo que el humano confirmo pasa a ser la ultima revision aplicada: el
        # proximo `auto` lo puede actualizar sin tomarlo por editado a mano.
        marcar_aplicados(raiz, preparados, registro)
        return {
            "ok": True,
            "mode": mode,
            "files": [
                {"path": ruta, "status": "updated" if estados[ruta] else "written"}
                for ruta, destino, _ in preparados
            ],
        }

    # ---- un turno

    async def _avisar_error(self, task_id: str, mensaje: str) -> None:
        # Best-effort: si el socket ya se cayo, Ragnar da el turno por perdido
        # solo; lo que no puede pasar es que esto tumbe la tarea.
        try:
            await self.enviar({"type": "error", "task_id": task_id, "message": mensaje})
        except Exception:
            log.debug("No se pudo avisar el error del turno %s.", task_id, exc_info=True)

    async def _turno(self, task_id: str, run: dict, adaptador) -> None:
        """Un turno de punta a punta. Nada de lo que falle aca puede matar la
        tarea en silencio: Ragnar se quedaria esperando un `done`/`error` que
        nunca llega y la tarea, "ejecutando" para siempre (RAG-201). El mensaje
        lleva solo el TIPO de la excepcion: el texto puede traer rutas, un
        token o el prompt."""
        try:
            await self._correr_turno(task_id, run, adaptador)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("El turno %s se rompio.", task_id)
            await self._avisar_error(task_id, f"El bridge fallo en el turno ({type(e).__name__}).")
        finally:
            self._cancelados.discard(task_id)

    async def _correr_turno(self, task_id: str, run: dict, adaptador) -> None:
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
            # viaja como UNA linea. El tope es `tope_evento` + un margen: una
            # linea mas larga se descartaria igual, y con 64 MiB por turno el
            # bridge podia retener cientos de MiB antes de descartarla
            # (RAG-208). Ver `_lineas`.
            limit=self.cfg.tope_evento + MARGEN_LINEA,
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
        try:
            if task_id in self._cancelados:
                # El `cancel` llego mientras el turno se preparaba o el CLI
                # arrancaba, antes de que hubiera proceso que cortar (RAG-202):
                # se aplica ya. El `done` lo manda _seguir_proceso, igual que
                # en un cancel normal.
                log.info("El turno %s ya estaba cancelado: se corta al arrancar.", task_id)
                await self._matar_proceso(proceso)
            # Tope por turno (RAG-201): un CLI colgado ocupaba su lugar en
            # `max_turnos` para siempre (solo agy tiene un tope propio).
            await asyncio.wait_for(
                self._seguir_proceso(task_id, proceso, traductor), self.cfg.timeout_turno
            )
        except asyncio.TimeoutError:
            log.warning("El turno %s supero el tope de %s s: se corta.", task_id, self.cfg.timeout_turno)
            await self._matar_proceso(proceso)
            await self._avisar_error(
                task_id,
                f"El turno supero el tope de {self.cfg.timeout_turno} s del bridge (timeout_turno) y se corto.",
            )
        except asyncio.CancelledError:
            await self._matar_proceso(proceso)
            raise
        except Exception as e:
            log.exception("El turno %s se rompio.", task_id)
            await self._matar_proceso(proceso)
            await self._avisar_error(task_id, f"El bridge fallo a mitad del turno ({type(e).__name__}).")
        finally:
            self._procesos.pop(task_id, None)

    async def _seguir_proceso(self, task_id: str, proceso, traductor) -> None:
        """Reenvia lo que emite el CLI hasta que termina y manda el `done`.

        El fin del turno lo marca la salida del LIDER, no el EOF de stdout: un
        nieto (un servidor MCP, un shell en background) que heredo el pipe lo
        mantiene abierto y `readline` no veria EOF nunca (RAG-202). Al salir
        el lider se corta su grupo y se le da a la salida un tope corto para
        terminar de drenarse."""
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

        async def leer_stdout():
            async for linea in _lineas(proceso.stdout, self.cfg.tope_evento):
                if isinstance(linea, int):
                    # Ragnar ignora los tipos que no conoce: se avisa que hubo
                    # algo en vez de tumbar el socket con un frame gigante.
                    await self.enviar({"type": "event", "task_id": task_id, "raw": {"type": "oversize", "bytes": linea}})
                    continue
                crudo = linea.decode("utf-8", errors="replace").strip()
                if not crudo:
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

        lector_err = asyncio.create_task(leer_stderr())
        lector_out = asyncio.create_task(leer_stdout())
        espera = asyncio.ensure_future(_salida_del_lider(proceso))
        try:
            hechos, _ = await asyncio.wait({lector_out, espera}, return_when=asyncio.FIRST_COMPLETED)
            if lector_out in hechos:
                lector_out.result()  # si fallo (el socket, un traductor), que se vea
            codigo = await espera
            # El lider termino: lo que quede de su grupo (nietos) ya no tiene
            # turno al que servir (RAG-202). Best-effort: el grupo puede estar vacio.
            self._senal_grupo(proceso, "TERM")
            await self._drenar(task_id, lector_out, "stdout")
            await self._drenar(task_id, lector_err, "stderr")
            await self._rematar_grupo(proceso)
            stderr = bytes(stderr_buf).decode("utf-8", errors="replace")
            for raw in traductor.fin(codigo, stderr):
                await self.enviar({"type": "event", "task_id": task_id, "raw": raw})
            await self.enviar(
                {"type": "done", "task_id": task_id, "exit_code": codigo, "stderr": stderr}
            )
        finally:
            for tarea in (lector_out, lector_err, espera):
                tarea.cancel()

    async def _drenar(self, task_id: str, lector: asyncio.Task, nombre: str) -> None:
        """Espera a que `lector` termine de leer lo que el CLI dejo en el pipe,
        con un tope corto: si un nieto lo mantiene abierto, se deja de leer en
        vez de colgar el turno."""
        if not lector.done():
            try:
                await asyncio.wait_for(asyncio.shield(lector), ESPERA_DRENAJE)
            except asyncio.TimeoutError:
                log.warning(
                    "Turno %s: el CLI salio pero algo de su grupo mantiene %s abierto; se deja de leer.",
                    task_id,
                    nombre,
                )
                lector.cancel()
                return
        lector.result()

    # ---- corte de procesos

    @staticmethod
    def _senal_grupo(proceso: asyncio.subprocess.Process, cual: str) -> bool:
        """Manda SIGTERM/SIGKILL al grupo del proceso (POSIX; lo lanzamos como
        lider de su propia sesion, ver `start_new_session`). Devuelve False si
        ya no queda nadie en el grupo. En Windows no hace nada (no hay grupos)."""
        if os.name != "posix":
            return False
        # `signal.SIGKILL` no existe en Windows: se resuelve solo en POSIX.
        senal = signal.SIGKILL if cual == "KILL" else signal.SIGTERM
        try:
            os.killpg(proceso.pid, senal)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    @staticmethod
    def _grupo_vivo(proceso: asyncio.subprocess.Process) -> bool:
        if os.name != "posix":
            return False
        try:
            os.killpg(proceso.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    async def _rematar_grupo(self, proceso: asyncio.subprocess.Process, gracia: float = 3.0) -> None:
        """Tras el SIGTERM al grupo: si a los `gracia` segundos sigue alguien
        vivo (un nieto que ignora SIGTERM), SIGKILL."""
        if os.name != "posix":
            return
        limite = asyncio.get_running_loop().time() + gracia
        while self._grupo_vivo(proceso):
            if asyncio.get_running_loop().time() >= limite:
                self._senal_grupo(proceso, "KILL")
                return
            await asyncio.sleep(0.05)

    async def _matar_proceso(self, proceso: asyncio.subprocess.Process) -> None:
        # En POSIX se manda al grupo entero (ver el `start_new_session` al
        # lanzarlo en `_turno`), no solo al PID: un hijo que el CLI haya
        # dejado suelto tambien tiene que morir, o queda huerfano. Y NO se sale
        # temprano si el lider ya termino (RAG-202): sus nietos pueden seguir
        # vivos en el grupo.
        if os.name == "posix":
            if not self._senal_grupo(proceso, "TERM"):
                return
            try:
                await asyncio.wait_for(_salida_del_lider(proceso), 5)
            except asyncio.TimeoutError:
                self._senal_grupo(proceso, "KILL")
                await _salida_del_lider(proceso)
            await self._rematar_grupo(proceso, gracia=2.0)
            return

        if proceso.returncode is not None:
            return
        # Windows: sin grupos de procesos. `taskkill /T` corta el arbol de
        # hijos que el CLI tenga vivos (mientras el lider exista); si no se
        # puede, al menos el PID.
        try:
            corte = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proceso.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(corte.wait(), 10)
        except (OSError, asyncio.TimeoutError):
            pass
        if proceso.returncode is None:
            try:
                proceso.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(_salida_del_lider(proceso), 10)
        except asyncio.TimeoutError:
            log.warning("El proceso %s no termino tras cortarlo.", proceso.pid)

    async def _matar(self, task_id: str) -> None:
        # El bucle de _turno ve la salida del CLI y manda el `done` con el
        # codigo de salida real; aca solo se corta el proceso.
        proceso = self._procesos.get(task_id)
        if proceso is not None:
            await self._matar_proceso(proceso)
        elif task_id in self._tareas:
            # El turno existe pero su CLI todavia no arranco: se recuerda y
            # _correr_turno lo corta apenas lo registre (RAG-202).
            self._cancelados.add(task_id)

    async def _cortar_todo(self) -> None:
        for tarea in list(self._tareas.values()):
            tarea.cancel()
        if self._tareas:
            await asyncio.gather(*self._tareas.values(), return_exceptions=True)


async def ejecutar(cfg: Config, estado_dir: Optional[Path] = None, senales: bool = False) -> None:
    """Se conecta a Ragnar y reconecta con backoff exponencial hasta que lo
    frenen. Levanta ErrorFatal si Ragnar rechaza la credencial.

    Con `senales` (lo usa `ragnar-bridge run`), SIGTERM/SIGINT cancelan esta
    tarea: al cancelarse se corta cada turno en curso con su grupo de
    procesos entero. Sin esto, corriendo fuera de systemd un `kill` al bridge
    dejaba vivos a los CLIs, que se lanzan en su propia sesion (RAG-202)."""
    instaladas = []
    if senales and os.name == "posix":
        loop = asyncio.get_running_loop()
        tarea = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, tarea.cancel)
                instaladas.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
    try:
        await _reconectar(cfg, estado_dir)
    finally:
        for sig in instaladas:
            asyncio.get_running_loop().remove_signal_handler(sig)


async def _reconectar(cfg: Config, estado_dir: Optional[Path]) -> None:
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
                bridge.bienvenido = False
                try:
                    await bridge.sesion(ws)
                finally:
                    # El backoff se reinicia recien con un `welcome` valido: si
                    # Ragnar (o un proxy) acepta el socket y lo cierra enseguida,
                    # reconectar cada ~1 s para siempre no ayuda a nadie (RAG-208).
                    if bridge.bienvenido:
                        espera = 1.0
            log.info("Ragnar cerro la conexion.")
        except ErrorFatal:
            raise
        except (OSError, WebSocketException, asyncio.TimeoutError, ValueError) as e:
            log.warning("Sin conexion con Ragnar (%s: %s).", type(e).__name__, e)
        # Jitter: si Ragnar se reinicia, todos los bridges reconectan a la vez.
        await _esperar_reconexion(espera + random.uniform(0, espera / 2))
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
        respuesta = _leer_bienvenida(await asyncio.wait_for(ws.recv(), 15))
    if respuesta.get("type") == "error":
        raise ErrorFatal(respuesta.get("message") or respuesta.get("code"))
    return str(respuesta.get("nombre") or "")
