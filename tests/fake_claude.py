"""Doble del CLI de Claude para las pruebas: emite su argv y un `result` en stream-json.

Guiones segun el prompt: SLEEP se cuelga; FAIL sale con codigo 3 y stderr;
HIJO deja un hijo vivo en su grupo (PID en $FAKE_CLAUDE_PID_FILE), HIJO_STDOUT
ademas le hereda stdout/stderr y HIJO_TERCO ignora SIGTERM; GRANDE emite una
linea de ~300 KB antes del `result`; IMAGEN deja en la carpeta de salidas un PNG, un
falso PNG y un .txt."""

import json
import os
import re
import subprocess
import sys
import time

argv = sys.argv[1:]

if argv == ["--version"]:
    print("fake-claude 1.0")
    sys.exit(0)
if argv == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.environ.get("FAKE_CLAUDE_LOGIN", "1") == "1"}))
    sys.exit(0)

prompt = argv[-1]

print(json.dumps({"type": "system", "subtype": "init", "argv": argv}), flush=True)
if "HIJO" in prompt:
    hereda = "HIJO_STDOUT" in prompt or "HIJO_TERCO" in prompt
    codigo = "import time; time.sleep(30)"
    if "HIJO_TERCO" in prompt:
        codigo = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    hijo = subprocess.Popen(
        [sys.executable, "-c", codigo],
        stdout=None if hereda else subprocess.DEVNULL,
        stderr=None if hereda else subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    with open(os.environ["FAKE_CLAUDE_PID_FILE"], "w", encoding="utf-8") as f:
        f.write(str(hijo.pid))
    if "HIJO_TERCO" in prompt:
        time.sleep(1)
if "GRANDE" in prompt:
    print(json.dumps({"type": "user", "relleno": "x" * 300_000}), flush=True)
if "IMAGEN" in prompt:
    salida = re.search(r"en esta carpeta: (\S+) \.", prompt).group(1)
    with open(os.path.join(salida, "b-logo.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x01" * 32)
    with open(os.path.join(salida, "a-trucho.png"), "wb") as f:
        f.write(b"no soy una imagen")
    with open(os.path.join(salida, "notas.txt"), "w", encoding="utf-8") as f:
        f.write("hola")
if "SLEEP" in prompt:
    time.sleep(60)
if "FAIL" in prompt:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)
print(json.dumps({"type": "result", "is_error": False, "usage": {"input_tokens": 1}}), flush=True)
