"""Doble de Cursor CLI (`agent`) para las pruebas.

Guiones segun el prompt (el argumento despues de `--`):
  TOOL -> tool_call de lectura y un delta de texto;
  FAILED -> result con is_error;
  CRASH -> codigo 3 sin result;
  SLEEP -> se cuelga.
`status --format json` responde loggedIn, salvo FAKE_CURSOR_LOGGED_OUT=1.
"""

import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
if argv == ["--version"]:
    print("2026.10.22-27dcf5f")
    sys.exit(0)

if argv[:2] == ["status", "--format"] and argv[2:] == ["json"]:
    logged = os.environ.get("FAKE_CURSOR_LOGGED_OUT") != "1"
    print(json.dumps({"loggedIn": logged}))
    sys.exit(0 if logged else 1)

if argv == ["login"]:
    print("Open this URL in your browser:")
    print("https://cursor.com/loginDeepControl?challenge=abc&uuid=def&mode=login")
    sys.exit(0)

log_path = os.environ.get("FAKE_CURSOR_ARGV_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as archivo:
        archivo.write(json.dumps({"argv": argv}) + "\n")

prompt = argv[argv.index("--") + 1] if "--" in argv else ""
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
session_id = resume or str(uuid.uuid4())


def out(evento):
    print(json.dumps(evento), flush=True)


out({
    "type": "system",
    "subtype": "init",
    "apiKeySource": "login",
    "cwd": "/tmp",
    "session_id": session_id,
    "model": "Composer",
    "permissionMode": "default",
})

if "SLEEP" in prompt:
    time.sleep(60)
if "CRASH" in prompt:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)

if "TOOL" in prompt:
    out({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Leo el archivo. "}]},
        "session_id": session_id,
        "timestamp_ms": 10,
    })
    out({
        "type": "tool_call",
        "subtype": "started",
        "call_id": "call-1",
        "tool_call": {"readToolCall": {"args": {"path": "README.md"}}},
        "session_id": session_id,
    })
    out({
        "type": "tool_call",
        "subtype": "completed",
        "call_id": "call-1",
        "tool_call": {
            "readToolCall": {
                "args": {"path": "README.md"},
                "result": {"success": {"content": "hola archivo", "totalLines": 1}},
            }
        },
        "session_id": session_id,
    })
    out({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Listo."}]},
        "session_id": session_id,
        "timestamp_ms": 20,
        "model_call_id": "dup",
    })
    out({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Leo el archivo. Listo."}]},
        "session_id": session_id,
    })
elif "FAILED" in prompt:
    out({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "Cursor se quedo sin cuota.",
        "session_id": session_id,
    })
    sys.exit(0)
else:
    out({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hola mundo"}]},
        "session_id": session_id,
        "timestamp_ms": 10,
    })

out({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Hola mundo",
    "session_id": session_id,
    "duration_ms": 12,
})
