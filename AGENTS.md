# AGENTS.md — ragnar-bridge

Guía para agentes que trabajan en este repo. El uso y la instalación están en `README.md`.

## Flujos de la app (diagramas)

Los flujos de Ragnar viven en Mermaid en la parte «6. Flujos» del libro `ragnar-group` (documentación viva), uno por sección, con la línea **Código:** de las rutas `repo:ruta` que cubren.

- **Al terminar un cambio**: `ver_flujos(repo="rg_ragnar_bridge", archivos=<git diff --name-only origin/main...HEAD>)` del MCP `tickets`. Si devuelve flujos y el cambio altera lo que dibujan (pasos, llamadas, estados, quién habla con quién), se actualizan con `guardar_flujo` en el mismo trabajo. Si no lo altera, no se toca.
- Un proceso nuevo (pantalla, endpoint, tarea de fondo) que no aparece en ningún flujo se dibuja con `guardar_flujo`, sumando sus rutas a **Código:**.
- Solo `flowchart TD|LR` con nodos y flechas (sin `subgraph` ni estilos): es lo que la app dibuja, y `guardar_flujo` rechaza lo demás.

## Comentarios en el código

- **Sin comentarios dentro del código**: ni comentarios sueltos o al final de una línea, ni docstrings/doc comments de funciones, clases o miembros. Los nombres explican el qué; el porqué y la historia van al commit, al ticket o a la documentación viva, no al archivo.
- **Un solo encabezado por archivo, opcional**, arriba de todo (antes de los imports): qué hace o contiene el archivo, en presente, máximo 8 líneas, sin historia ni ids de tickets. Si un cambio altera lo que el archivo hace, el encabezado se actualiza en el mismo cambio.
- **Se permiten** las directivas de herramientas (`noqa`, `type: ignore`, `// ignore:`, `eslint-disable`, `@ts-…`).
- Lo hace cumplir `tests/test_sin_comentarios.py`. Si falla, se borra el comentario; la guardia no se relaja.
