"""CLI de mentira para los logins: `codigo` imprime un link y espera el codigo pegado
(como Claude Code), `dispositivo` imprime link y codigo de dispositivo y sale (como
Codex), `menu` espera un Enter en un menu, pide el codigo y queda abierto (como agy).
Con el codigo BIEN escribe el archivo de sesion que recibe como segundo argumento."""

import sys
import time
from pathlib import Path

modo, sesion = sys.argv[1], Path(sys.argv[2])


def pedir_codigo():
    print("Paste code here if prompted > ", end="", flush=True)
    codigo = sys.stdin.readline().strip()
    if codigo != "BIEN":
        print("Invalid code")
        sys.exit(1)
    sesion.write_text("ok")


if modo == "codigo":
    print("Opening browser to sign in...")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=abc", flush=True)
    pedir_codigo()
    print("Login successful.")
elif modo == "dispositivo":
    print("1. Open this link in your browser\n   https://auth.openai.com/codex/device")
    print("2. Enter this one-time code\n   HFYR-H2CPF", flush=True)
    time.sleep(0.5)
    sesion.write_text("ok")
elif modo == "menu":
    print("Select login method:\n > 1. Google OAuth", flush=True)
    sys.stdin.readline()
    print("Open the URL below in your browser:\n https://accounts.google.com/o/oauth2/auth?client_id=1&state=x", flush=True)
    pedir_codigo()
    time.sleep(60)
