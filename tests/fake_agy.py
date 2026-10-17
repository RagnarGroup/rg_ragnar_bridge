"""Doble de `agy` para las pruebas: emite init/step_update/result y guarda su argv
en $AGY_FAKE_DIR/argvs.jsonl.

Guiones segun el prompt (`-p=<prompt>`):
  TOOL          -> pide un comando; lo deniega salvo con --dangerously-skip-permissions.
  STATUS_ERROR  -> result con status distinto de SUCCESS.
  FAIL          -> sale con codigo 3 sin emitir result.
  SLEEP         -> se cuelga."""

import json
import os
import sys
import time
import uuid

argv = sys.argv[1:]

if argv == ["--version"]:
    print(os.environ.get("FAKE_AGY_VERSION", "fake-agy 1.0"))
    sys.exit(0)
if argv == ["models"]:
    print("fake-model\tFake")
    sys.exit(0 if os.environ.get("FAKE_AGY_LOGIN", "1") == "1" else 1)

prompt = next(a for a in argv if a.startswith("-p="))[3:]
base = os.environ["AGY_FAKE_DIR"]

if prompt == "/usage":
    with open(os.path.join(base, "usage-llamado"), "a"):
        pass
    print(json.dumps({"conversation_id": "", "status": "SUCCESS", "response": "", "num_turns": 0, "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "window": "weekly", "remaining_fraction": 0.75, "reset_time": "2026-10-04T02:10:32Z"},
            {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 1, "reset_time": "2026-09-27T09:31:35Z"}]},
        {"name": "Claude and GPT models", "buckets": [
            {"id": "3p-5h", "name": "Five Hour Limit Remaining", "window": "5h", "remaining_fraction": 0.5, "reset_time": "2026-09-27T09:31:35Z"}]}]}}}))
    sys.exit(0)

with open(os.path.join(base, "argvs.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")

conv = argv[argv.index("--conversation") + 1] if "--conversation" in argv else str(uuid.uuid4())
os.makedirs(os.path.join(base, "conversations"), exist_ok=True)
open(os.path.join(base, "conversations", f"{conv}.db"), "a").close()


def out(evento):
    print(json.dumps(evento), flush=True)


def paso(idx, estado, tipo, **extra):
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": idx, "state": estado, "step_type": tipo, **extra}})


def resultado(status="SUCCESS", response="", **extra):
    out({"event": "result", "result": {"conversation_id": conv, "status": status, "response": response, "duration_seconds": 1.0, "num_turns": 1, "usage": {"input_tokens": 9999, "output_tokens": 9999}, **extra}})


out({"event": "init", "conversation_id": conv, "init": {"model": "fake", "cwd": os.getcwd(), "tools": [], "permission_mode": "request-review"}})
paso(0, "DONE", "user_input")

if "SLEEP" in prompt:
    time.sleep(60)
if "FAIL" in prompt:
    print("boom", file=sys.stderr, flush=True)
    sys.exit(3)

if "TOOL" in prompt:
    paso(1, "DONE", "agent_response", usage={"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 0, "cache_read_tokens": 0})
    params = {"CommandLine": "echo hola-bridge"}
    paso(2, "ACTIVE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": params})
    if "--dangerously-skip-permissions" in argv:
        paso(2, "DONE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": params, "output": "hola-bridge\r\n"})
        paso(3, "ACTIVE", "agent_response", text_delta="Listo: hola-bridge")
        paso(3, "DONE", "agent_response", text_delta="\n", usage={"input_tokens": 120, "output_tokens": 8, "thinking_tokens": 0, "cache_read_tokens": 0})
        resultado(response="Listo: hola-bridge\n")
    else:
        msg = 'permission check failed for command "echo hola-bridge": user denied permission to run command'
        paso(2, "ERROR", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": params, "error": {"type": "TOOL_ERROR", "message": msg}})
        print('jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for', file=sys.stderr, flush=True)
        resultado(denied_actions=[{"action": "command", "display_name": "RunCommand"}])
    sys.exit(0)

if "STATUS_ERROR" in prompt:
    resultado(status="ERROR", response="algo fallo")
    sys.exit(0)

paso(1, "ACTIVE", "agent_response", text_delta="Hola ")
paso(1, "ACTIVE", "agent_response", text_delta="mundo")
paso(1, "DONE", "agent_response", text_delta="\n", usage={"input_tokens": 100, "output_tokens": 5, "thinking_tokens": 0, "cache_read_tokens": 2})
resultado(response="Hola mundo\n")
