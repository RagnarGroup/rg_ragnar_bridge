"""Adjuntos de imagen: guardado y rutas en el mensaje, acceso de Claude a la carpeta,
rechazo de adjuntos invalidos o pesados y limpieza de conversaciones viejas."""

import base64
import os
import time

import pytest

from ragnar_bridge import adjuntos
from ragnar_bridge.agents.claude import construir_comando
from ragnar_bridge.config import Config
from ragnar_bridge.protocolo import RunInvalido, validar_run

from helpers import run

SESION = "11111111-1111-1111-1111-111111111111"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def _adjunto(contenido, mime="image/png"):
    return {"name": "captura", "mime": mime, "data": base64.b64encode(contenido).decode()}


def _turno(*adjuntos_, **extra):
    return validar_run(run(SESION, "mira esto", attachments=list(adjuntos_), **extra))


def test_un_run_sin_adjuntos_queda_igual(tmp_path):
    turno = validar_run(run(SESION, "hola"))
    assert turno.adjuntos == ()
    assert adjuntos.guardar(tmp_path, turno) is turno
    assert not adjuntos.carpeta(tmp_path).exists()


def test_guardar_escribe_las_imagenes_y_suma_las_rutas_al_mensaje(tmp_path):
    turno = adjuntos.guardar(
        tmp_path, _turno(_adjunto(PNG), _adjunto(JPG, "image/jpeg"), _adjunto(WEBP, "image/webp"), fallback="contexto")
    )

    carpeta = adjuntos.carpeta(tmp_path) / SESION
    archivos = sorted(carpeta.iterdir())
    assert [a.suffix for a in archivos] == [".png", ".jpg", ".webp"]
    assert archivos[0].read_bytes() == PNG
    assert archivos[0].stat().st_mode & 0o777 == 0o600
    for texto in (turno.prompt, turno.prompt_fallback):
        assert "3 imagenes" in texto
        assert all(str(a) in texto for a in archivos)
    assert turno.prompt.startswith("mira esto") and turno.prompt_fallback.startswith("contexto")
    assert turno.dirs_extra == (str(carpeta),)


def test_claude_recibe_acceso_a_la_carpeta_de_adjuntos(tmp_path):
    cfg = Config(url="ws://x", token="t", config_dir=str(tmp_path / "claude"), add_dirs=["/srv/repo"])
    turno = adjuntos.guardar(tmp_path, _turno(_adjunto(PNG)))

    argv = construir_comando(cfg, turno, None).argv

    desde = argv.index("--add-dir")
    assert argv[desde + 1 : desde + 3] == ["/srv/repo", str(adjuntos.carpeta(tmp_path) / SESION)]
    assert "una imagen" in argv[-1]


@pytest.mark.parametrize(
    "crudo",
    [
        [_adjunto(PNG)] * 5,
        [_adjunto(PNG, "image/gif")],
        [_adjunto(JPG, "image/png")],
        [_adjunto(b"#!/bin/sh\nrm -rf ~", "image/png")],
        [{"mime": "image/png", "data": "no es base64 !!"}],
        [{"mime": "image/png"}],
        ["basura"],
        "basura",
    ],
)
def test_adjuntos_invalidos_rechazan_el_run(crudo):
    with pytest.raises(RunInvalido):
        validar_run(run(SESION, "x", attachments=crudo))


def test_adjuntos_demasiado_pesados_rechazan_el_run():
    enorme = PNG + b"\x00" * (4 * 1024 * 1024)
    with pytest.raises(RunInvalido, match="pesadas"):
        _turno(_adjunto(enorme))
    casi = PNG + b"\x00" * (3 * 1024 * 1024)
    with pytest.raises(RunInvalido, match="pesadas"):
        _turno(_adjunto(casi), _adjunto(casi), _adjunto(casi))


def test_se_borran_los_adjuntos_de_conversaciones_viejas(tmp_path):
    vieja = adjuntos.carpeta(tmp_path) / "22222222-2222-2222-2222-222222222222"
    vieja.mkdir(parents=True)
    (vieja / "a.png").write_bytes(PNG)
    hace_un_mes = time.time() - 30 * 86400
    os.utime(vieja, (hace_un_mes, hace_un_mes))
    reciente = adjuntos.carpeta(tmp_path) / "33333333-3333-3333-3333-333333333333"
    reciente.mkdir()

    adjuntos.guardar(tmp_path, _turno(_adjunto(PNG)))

    assert not vieja.exists() and reciente.exists()
