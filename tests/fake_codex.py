"""Doble de `codex` para las pruebas. Emite el JSONL de `codex exec --json`
(codex-cli 0.157.0: thread.started / turn.started / item.* / turn.completed) y
guarda argv y entorno relevante en $CODEX_HOME/argvs.jsonl para que el test
compruebe que flags y variables recibio.

Guiones segun el prompt (ultimo argumento):
  TOOL     -> ejecuta un comando (item.started + item.completed)
  FILE     -> cambia un archivo
  MCP      -> llama a una tool MCP
  FAILED   -> turn.failed y salida con codigo 1
  CRASH    -> sale con codigo 3 sin emitir turn.completed
  SLEEP    -> se cuelga
"""

import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]
if argv == ["--version"]:
    print("codex-cli 0.157.0")
    sys.exit(0)
if argv == ["login", "status"]:
    print("Logged in using ChatGPT" if os.environ.get("FAKE_CODEX_LOGIN", "1") == "1" else "Not logged in")
    sys.exit(0 if os.environ.get("FAKE_CODEX_LOGIN", "1") == "1" else 1)

if argv == ["app-server"]:
    # JSON-RPC por stdio: forma real de codex-cli 0.157.0.
    if os.environ.get("FAKE_CODEX_SPAWN_ORPHAN"):
        # Simula el bug conocido de codex-cli (openai/codex#15379, #47735):
        # `app-server` deja un descendiente que sobrevive a que lo maten a
        # el solo. Escribe su PID para que el test lo pueda revisar.
        import subprocess as sp

        huerfano = sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        with open(os.environ["FAKE_CODEX_SPAWN_ORPHAN"], "w", encoding="utf-8") as f:
            f.write(str(huerfano.pid))
    for linea in sys.stdin:
        msg = json.loads(linea)
        if msg.get("method") == "initialize":
            print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake", "codexHome": "x"}}), flush=True)
        elif msg.get("method") == "account/rateLimits/read":
            limite = {"limitId": "codex", "limitName": None, "planType": "team",
                      "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1790501311},
                      "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1791054073}}
            extra = {"limitId": "codex_spark", "limitName": "Spark", "planType": "team",
                     "primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": 1790501311}, "secondary": None}
            print(json.dumps({"id": msg["id"], "result": {"rateLimits": limite, "rateLimitsByLimitId": {"codex": limite, "codex_spark": extra}}}), flush=True)
            break
    sys.exit(0)

assert argv[0] == "exec", argv
base = os.environ["CODEX_HOME"]
prompt = argv[-1]
resume = argv[1] == "resume"
thread = argv[argv.index("--") + 1] if resume else str(uuid.uuid4())

with open(os.path.join(base, "argvs.jsonl"), "a", encoding="utf-8") as f:
    f.write(
        json.dumps(
            {
                "argv": argv,
                "cwd": os.getcwd(),
                "env": {k: v for k, v in os.environ.items() if k.startswith("RAGNAR_") or k == "CODEX_HOME"},
            }
        )
        + "\n"
    )

carpeta = os.path.join(base, "sessions", "2026", "09", "26")
os.makedirs(carpeta, exist_ok=True)
open(os.path.join(carpeta, f"rollout-2026-09-26T00-00-00-{thread}.jsonl"), "a").close()


def out(evento):
    print(json.dumps(evento), flush=True)


def mensaje(id_, texto):
    out({"type": "item.completed", "item": {"id": id_, "type": "agent_message", "text": texto}})


USO = {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 5}

out({"type": "thread.started", "thread_id": thread})
out({"type": "turn.started"})

if "SLEEP" in prompt:
    time.sleep(60)
if "CRASH" in prompt:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)
if "FAILED" in prompt:
    out({"type": "error", "message": "Reconnecting... 1/5"})
    out({"type": "turn.failed", "error": {"message": "cuota agotada"}})
    sys.exit(1)

if "TOOL" in prompt:
    mensaje("item_0", "Voy a correrlo.")
    cmd = 'bash -lc "echo hola-bridge"'
    out({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": cmd, "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    out({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": cmd, "aggregated_output": "hola-bridge\n", "exit_code": 0, "status": "completed"}})
    out({"type": "item.started", "item": {"id": "item_2", "type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    out({"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": 1, "status": "failed"}})
    mensaje("item_3", "Listo.")
elif "FILE" in prompt:
    out({"type": "item.completed", "item": {"id": "item_0", "type": "file_change", "changes": [{"path": "a.txt", "kind": "update"}], "status": "completed"}})
    mensaje("item_1", "Editado.")
elif "MCP" in prompt:
    out({"type": "item.completed", "item": {"id": "item_0", "type": "mcp_tool_call", "server": "ragnar-tickets", "tool": "listar", "arguments": {"q": 1}, "result": {"content": [{"type": "text", "text": "RAG-1"}]}, "error": None, "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "pensando"}})
    mensaje("item_2", "Hay RAG-1.")
else:
    out({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "pienso"}})
    mensaje("item_1", "Hola")
    mensaje("item_2", "mundo")

out({"type": "turn.completed", "usage": USO})
