"""Iniciar sesion en un CLI desde la app: Ragnar manda `login {agent, op}` y el bridge
corre el login del CLI (`Adaptador.receta_login`) en una pseudo-terminal ancha. Ninguno
necesita navegador en el servidor: dan un link y, segun el CLI, un codigo de dispositivo
o piden pegar el codigo que muestra la pagina (op `code`). Cada cambio se avisa con
`login_state {agent, estado, url, codigo, pide_codigo, mensaje}` y al terminar se
vuelven a sondear los agentes. Un login por CLI a la vez; solo POSIX."""

import asyncio
import logging
import os
import re
import signal
import struct
from typing import Awaitable, Callable, Dict, Optional

from .agents.base import Adaptador, RecetaLogin

log = logging.getLogger("ragnar_bridge")

TOPE_SEGUNDOS = 15 * 60
CADA_CUANTO_REVISA = 3.0
TOPE_TEXTO = 64 * 1024
MAX_CODIGO = 512
COLUMNAS = 4000
_PATRON_ENLACE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)")
_PATRON_ANSI = re.compile(r"\x1b\[[0-9;?>$=]*[A-Za-z~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?|\r")

Avisar = Callable[[dict], Awaitable[None]]


def limpiar(texto: str) -> str:
    return _PATRON_ANSI.sub("", texto)


class SesionLogin:
    def __init__(self, nombre: str, adaptador: Adaptador, receta: RecetaLogin, avisar: Avisar):
        self.nombre = nombre
        self.adaptador = adaptador
        self.receta = receta
        self.avisar = avisar
        self.estado: dict = {"agent": nombre, "estado": "iniciando", "pide_codigo": receta.pide_codigo}
        self._maestro: Optional[int] = None
        self._proceso: Optional[asyncio.subprocess.Process] = None
        self._codigo_enviado = False
        self._texto = ""

    async def _cambiar(self, **cambios) -> None:
        nuevo = {**self.estado, **cambios}
        if nuevo != self.estado:
            self.estado = nuevo
            await self.avisar({"type": "login_state", **nuevo})

    def mandar_codigo(self, codigo: str) -> bool:
        if self._maestro is None or not self.receta.pide_codigo:
            return False
        os.write(self._maestro, codigo.encode("utf-8") + b"\r")
        self._codigo_enviado = True
        return True

    async def correr(self) -> None:
        try:
            await asyncio.wait_for(self._correr(), TOPE_SEGUNDOS)
        except asyncio.TimeoutError:
            await self._cambiar(estado="error", mensaje="El login vencio: volve a intentarlo.")
        except asyncio.CancelledError:
            await self._cambiar(estado="cancelado")
            raise
        except OSError as e:
            await self._cambiar(estado="error", mensaje=f"No se pudo lanzar el login: {e}")
        finally:
            self._cerrar()

    async def _correr(self) -> None:
        import fcntl
        import termios

        maestro, esclavo = os.openpty()
        self._maestro = maestro
        fcntl.ioctl(esclavo, termios.TIOCSWINSZ, struct.pack("HHHH", 60, COLUMNAS, 0, 0))
        try:
            self._proceso = await asyncio.create_subprocess_exec(
                *self.receta.argv,
                stdin=esclavo,
                stdout=esclavo,
                stderr=esclavo,
                env={**self.receta.env, "TERM": "xterm-256color", "COLUMNS": str(COLUMNAS)},
                start_new_session=True,
            )
        finally:
            os.close(esclavo)
        await self._cambiar(estado="iniciando")
        lector = asyncio.create_task(self._leer())
        espera = asyncio.create_task(self._proceso.wait())
        revisor = asyncio.create_task(self._revisar_sesion()) if not self.receta.sale_solo else None
        tareas = {lector, espera} | ({revisor} if revisor else set())
        try:
            hechas, _ = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
            if espera in hechas and lector not in hechas:
                await asyncio.wait({lector}, timeout=2)
        finally:
            for tarea in tareas:
                if not tarea.done():
                    tarea.cancel()
        if revisor in hechas and revisor.result():
            await self._cambiar(estado="listo", mensaje=None)
            return
        codigo = await self._proceso.wait() if espera not in hechas else espera.result()
        conectado = await asyncio.to_thread(self.adaptador.sesion_iniciada)
        if codigo == 0 and conectado is not False:
            await self._cambiar(estado="listo", mensaje=None)
        else:
            await self._cambiar(estado="error", mensaje=self._resumen() or f"El login termino con codigo {codigo}.")

    async def _leer(self) -> None:
        cola: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        fd = self._maestro

        def leer():
            try:
                cola.put_nowait(os.read(fd, 65536))
            except OSError:
                loop.remove_reader(fd)
                cola.put_nowait(b"")

        loop.add_reader(fd, leer)
        crudo = b""
        pendientes = list(self.receta.respuestas)
        try:
            while True:
                trozo = await cola.get()
                if not trozo:
                    return
                crudo = (crudo + trozo)[-TOPE_TEXTO:]
                sin_limpiar = crudo.decode("utf-8", errors="replace")
                texto = limpiar(sin_limpiar)
                self._texto = texto
                for disparador, teclas in list(pendientes):
                    if disparador in texto:
                        os.write(fd, teclas.encode("utf-8"))
                        pendientes.remove((disparador, teclas))
                cambios = {}
                url = self._url(sin_limpiar, texto)
                if url and len(url) > len(self.estado.get("url") or ""):
                    cambios["url"] = url
                if self.receta.patron_codigo and "codigo" not in self.estado:
                    codigo = self.receta.patron_codigo.search(texto)
                    if codigo and codigo.end() < len(texto):
                        cambios["codigo"] = codigo.group(0)
                if "url" in cambios or "url" in self.estado:
                    cambios["estado"] = "esperando"
                if cambios:
                    await self._cambiar(**cambios)
        finally:
            loop.remove_reader(fd)

    def _url(self, sin_limpiar: str, texto: str) -> Optional[str]:
        candidatas = [e for e in _PATRON_ENLACE.findall(sin_limpiar) if self.receta.patron_url.fullmatch(e)]
        candidatas += [m.group(0) for m in self.receta.patron_url.finditer(texto) if m.end() < len(texto)]
        return max(candidatas, key=len, default=None)

    async def _revisar_sesion(self) -> bool:
        while True:
            await asyncio.sleep(CADA_CUANTO_REVISA)
            if self._codigo_enviado and await asyncio.to_thread(self.adaptador.sesion_iniciada):
                return True

    def _resumen(self) -> str:
        lineas = [l.strip() for l in self._texto.splitlines() if l.strip()]
        return " ".join(lineas[-3:])[:300]

    def _cerrar(self) -> None:
        if self._proceso is not None and self._proceso.returncode is None:
            try:
                os.killpg(self._proceso.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self._maestro is not None:
            try:
                os.close(self._maestro)
            except OSError:
                pass
            self._maestro = None


class Logins:
    def __init__(self, adaptadores: Dict[str, Adaptador], avisar: Avisar, al_terminar: Callable[[], Awaitable[None]]):
        self.adaptadores = adaptadores
        self.avisar = avisar
        self.al_terminar = al_terminar
        self._sesiones: Dict[str, SesionLogin] = {}
        self._tareas: Dict[str, asyncio.Task] = {}

    async def atender(self, mensaje: dict) -> None:
        nombre = mensaje.get("agent")
        op = mensaje.get("op")
        adaptador = self.adaptadores.get(nombre) if isinstance(nombre, str) else None
        receta = adaptador.receta_login() if adaptador is not None and os.name == "posix" else None
        if receta is None:
            await self.avisar({"type": "login_state", "agent": nombre, "estado": "error", "mensaje": "Este CLI no se puede conectar desde la app."})
            return
        if op == "start":
            await self.cancelar(nombre)
            sesion = SesionLogin(nombre, adaptador, receta, self.avisar)
            self._sesiones[nombre] = sesion
            tarea = asyncio.create_task(self._correr(nombre, sesion))
            self._tareas[nombre] = tarea
        elif op == "code":
            codigo = mensaje.get("code")
            sesion = self._sesiones.get(nombre)
            valido = isinstance(codigo, str) and 0 < len(codigo.strip()) <= MAX_CODIGO and codigo.strip().isprintable()
            if sesion is None or not valido or not sesion.mandar_codigo(codigo.strip()):
                await self.avisar({"type": "login_state", "agent": nombre, "estado": "error", "mensaje": "No hay un login esperando un codigo."})
        elif op == "cancel":
            await self.cancelar(nombre)

    async def _correr(self, nombre: str, sesion: SesionLogin) -> None:
        try:
            await sesion.correr()
        except asyncio.CancelledError:
            return
        finally:
            if self._sesiones.get(nombre) is sesion:
                self._sesiones.pop(nombre, None)
                self._tareas.pop(nombre, None)
        log.info("Login de %s: %s.", nombre, sesion.estado.get("estado"))
        try:
            await self.al_terminar()
        except Exception:
            log.exception("No se pudieron re-sondear los agentes tras el login.")

    async def cancelar(self, nombre: str) -> None:
        tarea = self._tareas.pop(nombre, None)
        self._sesiones.pop(nombre, None)
        if tarea is not None and not tarea.done():
            tarea.cancel()
            await asyncio.gather(tarea, return_exceptions=True)

    async def cancelar_todos(self) -> None:
        for nombre in list(self._tareas):
            await self.cancelar(nombre)
