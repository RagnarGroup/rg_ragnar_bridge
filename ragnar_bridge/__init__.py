"""ragnar-bridge: corre el CLI de Claude Code, de Antigravity (agy), de Codex o
de Hermes Agent en TU servidor y le reenvia a Ragnar lo que emite, por una
conexion WebSocket saliente. Ver README.md."""

__version__ = "0.10.0"

# Version del protocolo de bridge_hub.py (ragnar_group_back). Si Ragnar habla
# otra, rechaza la conexion con un mensaje pidiendo actualizar el bridge.
# La 2 (RAG-217: `projects`/`platform` en el auth y bootstrap `auto`) la acepta
# un Ragnar que habla 1 y 2; contra uno que solo habla 1 este bridge no
# conecta, asi que se despliega primero el backend.
PROTOCOLO = 2
