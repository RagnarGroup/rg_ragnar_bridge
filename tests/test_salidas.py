"""Imagenes que genera el agente: carpeta por turno avisada en el mensaje solo si Ragnar
las acepta, acceso de los CLIs, recoleccion validada y frames `image` antes del `done`."""

import asyncio
import base64
import os
import sys
import time
import uuid

import pytest

from ragnar_bridge import salidas
from ragnar_bridge.agents.agy import AgyAdaptador
from ragnar_bridge.agents.claude import construir_comando
from ragnar_bridge.bridge import ejecutar
from ragnar_bridge.config import Config
from ragnar_bridge.protocolo import validar_run

from helpers import FAKE, TOKEN, run

SESION = "22222222-2222-2222-2222-222222222222"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x02" * 32


def _turno(**extra):
    return validar_run(run(SESION, "hace un logo", fallback="contexto", **extra))


def test_sin_images_out_no_se_crea_nada(tmp_path):
    for extra in ({}, {"images_out": "si"}, {"images_out": 1}):
        turno = _turno(**extra)
        assert salidas.preparar(tmp_path, turno) is turno
    assert not salidas.carpeta(tmp_path).exists()


def test_con_images_out_hay_carpeta_por_turno_avisada_en_el_mensaje(tmp_path):
    turno = salidas.preparar(tmp_path, _turno(images_out=True))
    otro = salidas.preparar(tmp_path, _turno(images_out=True))

    assert turno.dir_salida and os.path.isdir(turno.dir_salida)
    assert os.path.dirname(turno.dir_salida) == str(salidas.carpeta(tmp_path) / SESION)
    assert otro.dir_salida != turno.dir_salida
    for texto in (turno.prompt, turno.prompt_fallback):
        assert f"en esta carpeta: {turno.dir_salida} ." in texto
    assert turno.prompt.startswith("hace un logo") and turno.prompt_fallback.startswith("contexto")
    assert turno.dirs_extra == (turno.dir_salida,)
    assert salidas.carpeta(tmp_path).stat().st_mode & 0o777 == 0o700


def test_claude_y_agy_reciben_acceso_a_la_carpeta(tmp_path):
    cfg = Config(url="ws://x", token="t", config_dir=str(tmp_path / "claude"), add_dirs=["/srv/repo"])
    turno = salidas.preparar(tmp_path, _turno(images_out=True))

    argv = construir_comando(cfg, turno, None).argv
    assert argv[argv.index("--add-dir") + 1 : argv.index("--add-dir") + 3] == ["/srv/repo", turno.dir_salida]

    argv_agy = AgyAdaptador(cfg, tmp_path).preparar(turno, "gus").argv
    dirs = [argv_agy[i + 1] for i, a in enumerate(argv_agy) if a == "--add-dir"]
    assert dirs == ["/srv/repo", turno.dir_salida]


def test_recolectar_valida_firma_tamano_tope_y_symlinks(tmp_path):
    (tmp_path / "1.png").write_bytes(PNG)
    os.utime(tmp_path / "1.png", (time.time() - 10, time.time() - 10))
    (tmp_path / "2.jpeg").write_bytes(JPG)
    (tmp_path / "3.png").write_bytes(JPG)
    (tmp_path / "falso.png").write_bytes(b"texto")
    (tmp_path / "grande.png").write_bytes(PNG + b"\x00" * salidas.MAX_BYTES_IMAGEN)
    (tmp_path / "notas.txt").write_bytes(PNG)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.png").write_bytes(PNG)
    afuera = tmp_path.parent / f"afuera-{uuid.uuid4().hex}.png"
    afuera.write_bytes(PNG)
    (tmp_path / "enlace.png").symlink_to(afuera)

    imagenes = salidas.recolectar(str(tmp_path))

    assert [(i["name"], i["mime"]) for i in imagenes] == [
        ("1.png", "image/png"),
        ("2.jpeg", "image/jpeg"),
        ("3.png", "image/jpeg"),
    ]
    assert base64.b64decode(imagenes[0]["data"]) == PNG


def test_recolectar_respeta_el_tope_por_turno(tmp_path):
    for i in range(salidas.MAX_IMAGENES + 3):
        (tmp_path / f"{i:02}.png").write_bytes(PNG)
    assert len(salidas.recolectar(str(tmp_path))) == salidas.MAX_IMAGENES
    assert salidas.recolectar(str(tmp_path / "no-existe")) == []


def test_las_salidas_viejas_se_borran(tmp_path):
    vieja = salidas.carpeta(tmp_path) / "33333333-3333-3333-3333-333333333333"
    vieja.mkdir(parents=True)
    hace_mucho = time.time() - (salidas.DIAS_QUE_SE_CONSERVAN + 1) * 86400
    os.utime(vieja, (hace_mucho, hace_mucho))

    salidas.preparar(tmp_path, _turno(images_out=True))

    assert not vieja.exists()


@pytest.fixture
async def conectado(tmp_path, ragnar):
    (tmp_path / "work").mkdir()
    cfg = Config(
        url=ragnar.url,
        token=TOKEN,
        agents=["claude"],
        claude_cmd=[sys.executable, FAKE],
        workdir=str(tmp_path / "work"),
        config_dir=str(tmp_path / "claude"),
    )
    tarea = asyncio.create_task(ejecutar(cfg, tmp_path))
    await asyncio.wait_for(ragnar.conectado.wait(), 10)
    yield cfg
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)


async def _frames_hasta_done(ragnar, tid):
    frames = []
    while True:
        msg = await asyncio.wait_for(ragnar.recibidos.get(), 15)
        if msg.get("task_id") != tid:
            continue
        frames.append(msg)
        if msg["type"] in ("done", "error"):
            return frames


async def test_el_turno_manda_las_imagenes_validas_antes_del_done(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, "IMAGEN por favor", images_out=True))

    frames = await _frames_hasta_done(ragnar, tid)

    tipos = [f["type"] for f in frames]
    assert tipos[-1] == "done" and frames[-1]["exit_code"] == 0
    imagenes = [f for f in frames if f["type"] == "image"]
    assert [(i["name"], i["mime"]) for i in imagenes] == [("b-logo.png", "image/png")]
    assert base64.b64decode(imagenes[0]["data"]).startswith(b"\x89PNG")
    assert tipos.index("image") > tipos.index("event")


async def test_sin_images_out_no_hay_carpeta_ni_frames(conectado, ragnar):
    tid = str(uuid.uuid4())
    await ragnar.enviar(run(tid, "hola"))

    frames = await _frames_hasta_done(ragnar, tid)

    assert "image" not in [f["type"] for f in frames]
    argv = next(f for f in frames if f["type"] == "event")["raw"]["argv"]
    assert "en esta carpeta" not in argv[-1]
