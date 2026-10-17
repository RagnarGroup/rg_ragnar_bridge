# AGENTS.md — ragnar-bridge

Guía para agentes que trabajan en este repo. El uso y la instalación están en `README.md`.

## Comentarios en el código

- **Sin comentarios dentro del código**: ni comentarios sueltos o al final de una línea, ni docstrings/doc comments de funciones, clases o miembros. Los nombres explican el qué; el porqué y la historia van al commit, al ticket o a la documentación viva, no al archivo.
- **Un solo encabezado por archivo, opcional**, arriba de todo (antes de los imports): qué hace o contiene el archivo, en presente, máximo 8 líneas, sin historia ni ids de tickets. Si un cambio altera lo que el archivo hace, el encabezado se actualiza en el mismo cambio.
- **Se permiten** las directivas de herramientas (`noqa`, `type: ignore`, `// ignore:`, `eslint-disable`, `@ts-…`).
- Lo hace cumplir `tests/test_sin_comentarios.py`. Si falla, se borra el comentario; la guardia no se relaja.
