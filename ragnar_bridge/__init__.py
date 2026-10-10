"""ragnar-bridge: corre el CLI de Claude Code, de Antigravity (agy), de Codex o
de Hermes Agent en TU servidor y le reenvia a Ragnar lo que emite, por una
conexion WebSocket saliente. Ver README.md."""

__version__ = "0.7.0"

# Version del protocolo de bridge_hub.py (ragnar_group_back). Si Ragnar habla
# otra, rechaza la conexion con un mensaje pidiendo actualizar el bridge.
PROTOCOLO = 1
