"""Doble de `hermes` para las pruebas. Emite el JSONL real de
`hermes chat --format stream-json` (hermes-agent 0.21.5: system/init, tool_use,
tool_result, text, result -- capturado en vivo, RAG-187) y guarda argv en
$RAGNAR_BRIDGE_ESTADO_DIR/../argvs_hermes.jsonl (via variable de entorno que
el test setea) para que el test compruebe que flags recibio.

Guiones segun el prompt (`--query=<prompt>`):
  TOOL     -> corre una herramienta (tool_use + tool_result) antes del texto
  FAILED   -> result con exit_code != 0
  CRASH    -> sale con codigo 3 sin emitir result
  SLEEP    -> se cuelga
"""

import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
if argv == ["--version"]:
    print("Hermes Agent v0.21.5+2453.gd0288be (2026.9.24)")
    sys.exit(0)

assert argv[0] == "chat", argv

query = next((a[len("--query="):] for a in argv if a.startswith("--query=")), "")
resume_idx = argv.index("--resume") if "--resume" in argv else None
session_id = argv[resume_idx + 1] if resume_idx is not None else f"20260928_{uuid.uuid4().hex[:12]}"

log_path = os.environ.get("FAKE_HERMES_ARGV_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "env": {k: v for k, v in os.environ.items() if k.startswith("RAGNAR_")}}) + "\n")


def out(evento):
    print(json.dumps(evento), flush=True)


out({"type": "system", "subtype": "init", "model": "antigravity/gemini-3.7-flash-medium", "session_id": session_id, "timestamp": 1790621267676})

if "SLEEP" in query:
    time.sleep(60)
if "CRASH" in query:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)

if "TOOL" in query:
    # Forma real capturada en vivo (RAG-187): un solo `text` al final, con el
    # "\n\n" que el propio Hermes antepone despues de una herramienta.
    out({"type": "tool_use", "name": "terminal", "input": {"command": "echo $((40 + 2))"}, "timestamp": 1790621279239})
    out({"type": "tool_result", "name": "terminal", "output": "42", "duration_ms": 838, "is_error": False, "timestamp": 1790621280082})
    out({"type": "text", "text": "\n\n42", "timestamp": 1790621282933})
elif "FAILED" in query:
    out({"type": "result", "session_id": session_id, "exit_code": 1, "text": "Hermes se quedo sin cuota.", "tokens": {"input": 100, "output": 5, "total": 105, "cache_read": 0, "cache_write": 0}, "duration_ms": 500, "timestamp": 1790621283094})
    sys.exit(0)
else:
    out({"type": "text", "text": "Hola mundo", "timestamp": 1790621282933})

out({"type": "result", "session_id": session_id, "exit_code": 0, "text": "42", "tokens": {"input": 20784, "output": 136, "total": 37148, "cache_read": 16228, "cache_write": 0}, "duration_ms": 15417, "timestamp": 1790621283094})
