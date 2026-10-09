"""Doble del CLI de Claude para las pruebas: emite stream-json y sale.

Emite primero un evento con su propio argv (para que el test compruebe que
flags recibio) y despues un `result`. Si el prompt contiene SLEEP se queda
colgado, para probar cancelacion y corte por desconexion. Con FAIL sale con
codigo 3 y algo en stderr.

Con HIJO deja un proceso hijo vivo (en su mismo grupo, como un servidor MCP o
un shell en background) y escribe su PID en $FAKE_CLAUDE_PID_FILE; con
HIJO_STDOUT ese hijo ademas hereda stdout/stderr y los mantiene abiertos;
HIJO_TERCO es lo mismo pero el hijo ignora SIGTERM. Con GRANDE emite una linea
de ~300 KB antes del `result`.
"""

import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]

# Sondeos del bridge (no son turnos).
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
        # Un nieto que ignora SIGTERM: solo SIGKILL lo saca.
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
        time.sleep(1)  # que alcance a ignorar SIGTERM antes de que el lider salga
if "GRANDE" in prompt:
    # Una linea de stream-json enorme (un Read de un archivo gigante).
    print(json.dumps({"type": "user", "relleno": "x" * 300_000}), flush=True)
if "SLEEP" in prompt:
    time.sleep(60)
if "FAIL" in prompt:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)
print(json.dumps({"type": "result", "is_error": False, "usage": {"input_tokens": 1}}), flush=True)
