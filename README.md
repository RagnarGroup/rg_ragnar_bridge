# ragnar-bridge

Corre **Claude Code**, **Antigravity (`agy`)** o **Codex** en **tu propio servidor** y le
reenvía a Ragnar lo que emite, por una conexión WebSocket **saliente**. Tus
credenciales del CLI no salen de tu máquina y no hace falta abrir ningún puerto.

```
tu VPS                                          Ragnar
┌────────────────────────────┐  wss (saliente)  ┌───────────────────┐
│ claude / agy / codex (tu   │ ───────────────► │ /api/v1/bridge/ws │
│ sesión)                    │                  │                   │
│    ▲                       │ ◄─────────────── │   chat de la app  │
│    └─ ragnar-bridge        │  run / event     └───────────────────┘
└────────────────────────────┘
```

Un solo bridge por servidor maneja **todos** los CLIs que tengas: detecta cuáles
están instalados y con sesión iniciada, y en la app elegís con cuál arranca cada
conversación.

## Qué necesitás

- Un servidor Linux con `python3` (≥ 3.9, con el módulo `venv`) y salida a
  internet por HTTPS. Mejor con un **usuario sin privilegios** (no root): el
  agente corre con los permisos de quien arranca el bridge.
- **Una** de estas cuentas ya funcionando en ese servidor:
  - Claude Code con una cuenta de Claude,
  - Antigravity CLI con una cuenta de Google, o
  - Codex CLI con una cuenta de ChatGPT (o una API key de OpenAI).

## Paso 1 — Qué CLI vas a usar

Con **uno** alcanza; con varios podés elegir en el chat con cuál arranca cada
conversación (una conversación sigue con el CLI con el que empezó).

| | Claude Code | Antigravity (`agy`) | Codex | Hermes |
|---|---|---|---|---|
| Comando | `claude` | `agy` | `codex` | `hermes` |
| Sesión de la conversación | `--session-id` / `--resume` (la maneja el CLI) | el bridge recuerda qué conversación de agy es cuál (`agy-estado.json`) | el bridge recuerda qué hilo de Codex es cuál (`codex-estado.json`) y lo retoma con `codex exec resume` | el bridge recuerda qué sesión de Hermes es cuál (`hermes-estado.json`) y la retoma con `hermes chat --resume` |
| Permisos de herramientas | Ragnar te muestra la tarjeta **Autorizar**; al aprobar, el bridge suma **esa** herramienta a los permisos de **esa conversación** (`--settings` propio, nunca tu `~/.claude/settings.json`) | agy **deniega** solo lo que pide permiso (en modo headless no puede preguntar). Ragnar te muestra la misma tarjeta; al aprobar, **esa conversación** corre con las herramientas aprobadas | En `exec` Codex **no puede preguntar**: lo que el sandbox no permite falla. Sin tarjeta **Autorizar**; el alcance lo fija `codex_sandbox` (default `read-only`: lee y conversa) | Probado en vivo corriendo una herramienta de shell en `--oneshot` sin pedir aprobación (a diferencia de agy). Sin tarjeta **Autorizar** por ahora |
| Herramientas de tickets de Ragnar (MCP) | sí (token efímero de quien escribe) | sí, por un puente propio (`ragnar-tickets`, se registra solo) | sí, por el mismo puente, declarado en cada turno (no se escribe nada en tu `config.toml`) | todavía no (RAG-187) |
| Cuota real de la suscripción | sí, desde 0.11.0 (endpoint de uso de Anthropic con el token del CLI) | sí, desde agy 1.1.11 (`agy -p "/usage"`) | sí (`codex app-server`, límites de 5 h y semanal) | todavía no |
| Probado con | Claude Code 2.1.170 | agy 1.1.27, 1.2.2 y 1.2.7 | codex-cli 0.157.0 | hermes-agent 0.21.5 |

Si no elegís ninguno en la conversación nueva, Ragnar usa el primero que
funcione (Claude Code si está listo; si no, Antigravity; si no, Codex).

## Paso 2 — Prepará el o los CLIs en tu VPS

Instalá **al menos uno** (o varios) y comprobá que responde **antes** de instalar el
bridge. Si instalás el otro después, el bridge lo detecta solo en unos minutos.

### A) Claude Code

```sh
curl -fsSL https://claude.ai/install.sh | bash     # o: npm install -g @anthropic-ai/claude-code
claude                                              # primer arranque: te da un link, lo abrís en tu
                                                    # navegador y pegás el código de vuelta
claude --version
claude -p "respondé solo OK"                        # debe contestar OK
```

### B) Antigravity CLI

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy                                                 # primer arranque: login por dispositivo, abrís el
                                                    # link en tu navegador y pegás el código de vuelta
agy --version
agy models                                          # lista los modelos disponibles
agy "-p=respondé solo OK"                           # debe contestar OK
```

### C) Codex CLI

```sh
npm install -g @openai/codex                        # requiere Node 16+
codex login                                         # login con tu cuenta de ChatGPT (por dispositivo en un VPS:
                                                    # `codex login --device-auth`)
codex --version
codex login status                                  # debe decir «Logged in using …»
codex exec "respondé solo OK"                       # debe contestar OK
```

Si después de instalar el comando no se encuentra, abrí una terminal nueva (el
PATH se recarga al iniciar sesión).

## Paso 3 — Registrá el servidor en la app

En la app de Ragnar: **Agents → Conectá tu servidor** → poné un nombre → te
muestra el comando de instalación con tu URL y tu token (`ragbrg_...`).
**El token se muestra una sola vez**: si lo perdés, revocás ese servidor y
generás otro.

### ¿Personal o para todo el grupo?

En **Quién lo usa** elegís:

- **Todo el grupo «X»** (recomendado si varias personas usan el mismo VPS):
  se instala **un solo bridge** y todos los miembros del grupo chatean a través
  de él. Un solo CLI, una sola versión, una sola cuenta: nadie se queda con su
  `claude`/`agy` desactualizado por su cuenta. Los miembros no instalan nada;
  al abrir Agents ya lo ven conectado.
- **Solo yo (personal)**: nadie más lo ve ni lo puede usar.

Alguien de **otro grupo** no puede usar tu bridge compartido: tiene que
instalar el suyo.

Tené en cuenta en un bridge compartido:

- Todos los miembros ejecutan en el **mismo servidor y con la misma cuenta**
  del CLI (la cuota es una sola). Los archivos y el `settings.json` son los
  mismos para todos: un permiso que uno autoriza queda autorizado para el resto.
- Cada conversación es de quien la creó (los demás no la ven), pero comparten
  el disco del VPS.
- Las herramientas de tickets usan un token que Ragnar emite **por turno** para
  quien escribió en el chat (vence solo y ve solo las empresas de esa persona,
  aunque sea administradora). No hace falta configurar nada. `tickets_token` en
  la config queda solo como respaldo para un Ragnar viejo que no manda el token
  de turno: con un Ragnar nuevo se ignora, para que nadie actúe como el dueño del
  bridge.
- En `agy` el MCP de tickets pasa por un puente stdio→HTTP (`ragnar_bridge.mcp_proxy`)
  que el bridge registra solo en `~/.gemini/config/mcp_config.json` con el nombre
  `ragnar-tickets`: ese archivo es global y `agy` no expande variables en las
  cabeceras, así que el token de cada turno viaja en el entorno del proceso de
  `agy` (nunca en disco). Como en cualquier herramienta de `agy` en modo
  headless, la primera vez te pide **Autorizar** en la app.
- Lo puede revocar quien lo creó o cualquier miembro del grupo (así no queda
  huérfano si su creador se va).
- **Un compañero usa el bridge compartido solo si lo elige** al crear la
  conversación (el dueño del VPS puede ver lo que escribe, el código y su token
  de turno). Sin elegirlo, Ragnar usa solo sus bridges propios y, si no tiene
  ninguno conectado, el turno falla con un mensaje que nombra los compartidos
  que puede elegir. Quien creó el bridge compartido lo usa como propio. Una
  conversación que ya vive en un bridge sigue en ese.

## Paso 4 — Instalá el bridge

Pegá en tu VPS el comando que te muestra la app (con tu URL); el script te
pide el token (`ragbrg_...`) sin mostrarlo:

```sh
curl -fsSL https://raw.githubusercontent.com/RagnarGroup/rg_ragnar_bridge/main/install.sh \
  | bash -s -- --url wss://panel.ragnargroup.app/api/v1/bridge/ws
```

Sin terminal interactiva, pasalo por la variable `RAGNAR_BRIDGE_TOKEN`. `--token
ragbrg_...` sigue funcionando pero está deprecado: en los argumentos queda
visible en `ps` y en el historial del shell.

No hace falta decirle el agente: detecta `claude`, `agy`, `codex` y/o `hermes`. Opciones:
`--agent claude|agy|codex|hermes` (maneja SOLO ese), `--model M` (modelo de agy o de codex con `--agent codex`),
`--workdir DIR` (dónde arranca cada turno, por defecto tu home) y `--name N`
(un segundo bridge distinto en el mismo servidor). El script:

1. crea un entorno en `~/.local/share/ragnar-bridge`;
2. escribe `~/.config/ragnar-bridge/config.json` (permisos 600);
3. corre `ragnar-bridge doctor` y **se detiene si algo falla**;
4. deja un servicio de systemd de usuario y activa *linger* para que siga
   corriendo al cerrar tu sesión SSH.

## Instalar agentes y skills de proyecto desde Ragnar

La sección **Servicios → Bootstrap de agentes** permite previsualizar e
instalar archivos publicados por un administrador en el proyecto elegido. La
escritura ocurre en este VPS; Ragnar no recibe ni almacena secretos del CLI.
El bootstrap admite `.claude/agents/` y `.claude/skills/` para Claude Code, y
`.agent/rules/` y `.agent/workflows/` para Antigravity, y `.codex/agents/` y
`.agents/skills/` para Codex. Archivos existentes se
marcan como conflictos: hay que confirmar la sobrescritura y el bridge guarda
una copia en `.ragnar-bootstrap-backups/`.

Para habilitarlo, añade `project_paths` al archivo
`~/.config/ragnar-bridge/config.json`. Las claves son las keys de proyecto de
Ragnar y los valores son rutas locales absolutas al repositorio:

```json
{
  "project_paths": {
    "RAG": "/home/usuario/proyectos/ragnar_group_back",
    "CRM": "/home/usuario/proyectos/crm"
  }
}
```

El bridge no adivina rutas ni crea mapeos automáticamente. Rechaza rutas fuera
de las carpetas permitidas, enlaces simbólicos, rutas duplicadas y contenidos
cuyo SHA-256 no coincida con el catálogo publicado.

### Automático: siempre al día (0.10.0+)

Cuando un administrador publica una revisión en el catálogo, Ragnar se la manda
sola a los bridges conectados con ese CLI, para cada proyecto que el bridge
tiene en `project_paths` (y cuyo dueño puede ver). El bridge la aplica **sin
pisar nada editado a mano**:

| Estado del archivo en el proyecto | Qué hace |
|---|---|
| no existe | lo escribe |
| ya tiene ese contenido | nada |
| no se tocó desde la última revisión aplicada | lo actualiza |
| editado a mano (o existía antes sin pasar por Ragnar) | **no lo toca**: lo reporta como conflicto y queda en la auditoría de Ragnar |

Para saber qué es «no tocado», el bridge guarda el SHA-256 de lo último que
escribió en `~/.config/ragnar-bridge/bootstrap-aplicados.json` (fuera del
proyecto). Un conflicto se resuelve a mano: o se descarta tu cambio (borrás el
archivo y llega la revisión en el próximo aviso) o se aplica desde la app con
**Instalar** confirmando la sobrescritura (queda respaldo). Al bridge solo le
llegan las **claves** de `project_paths`; las rutas no salen de tu servidor.

Esta función requiere `ragnar-bridge` **0.4.0 o posterior** (**0.5.0** para Codex, **0.10.0** para
el modo automático). Para actualizar
una instalación existente sin reemplazar su config:

```sh
~/.local/share/ragnar-bridge/venv/bin/pip install --upgrade --force-reinstall 'git+https://github.com/RagnarGroup/rg_ragnar_bridge'
systemctl --user restart ragnar-bridge.service
```

## Imágenes en el chat

Desde la app se pueden adjuntar hasta 4 imágenes (PNG, JPEG o WebP) a un
mensaje. Los CLIs reciben el mensaje como texto, así que la imagen no viaja
dentro: el bridge la guarda en `~/.config/ragnar-bridge/adjuntos/<conversación>/`
y le suma al mensaje la ruta, que el agente abre con su herramienta de lectura
de archivos. Ragnar solo la transporta, no la guarda.

- El bridge comprueba que cada archivo sea de verdad una imagen del tipo que
  dice ser, y rechaza el turno si pesa más de 4 MiB (8 MiB entre todas).
- A Claude Code y a Antigravity les da acceso a esa carpeta solo en ese turno (`--add-dir`).
- Los adjuntos de una conversación que lleva 14 días sin recibir otro se borran.

Requiere `ragnar-bridge` **0.9.0 o posterior**.

### Imágenes que genera el agente

Al revés también: si Ragnar lo acepta (el `run` trae `images_out`), cada turno
tiene una carpeta propia en `~/.config/ragnar-bridge/salidas/<conversación>/<turno>/`,
que se le avisa al agente en el mensaje y a la que tiene acceso (`--add-dir`).
Lo que deje ahí (por ejemplo, una imagen hecha con el `image-generator` de
Antigravity) se manda a Ragnar al terminar el turno, en un frame `image` antes
del `done`, y la app lo muestra en el chat.

- Solo PNG, JPEG o WebP comprobados por su contenido, hasta 8 MiB cada una y
  8 por turno; se ignoran subcarpetas, enlaces simbólicos y otros archivos.
- Las salidas de una conversación que lleva 14 días sin turnos nuevos se borran.

Requiere `ragnar-bridge` **0.13.0 o posterior** y el backend con RAG-237.

## Editar archivos de configuración desde la app

El botón de **configuración** de la pestaña Agents (al lado de *Mis servidores*)
abre en modo edición archivos de texto de este VPS. Vienen dos rutas fijas, el
`settings.json` de Claude Code (donde viven sus `permissions.allow`) y su
`CLAUDE.md` global, las dos dentro de `config_dir`; el resto de las rutas las
sumás vos desde la app y quedan en `~/.config/ragnar-bridge/archivos-editables.json`.

- Solo lo usa **quien conectó el servidor**, con su sesión de la app: ni otro
  miembro del grupo (aunque el servidor esté compartido) ni un agente.
- El bridge solo lee y escribe rutas de esa lista, archivos de texto UTF-8 de
  hasta 512 KiB. No acepta su propia carpeta de config (ahí está el token),
  `~/.ssh`, `.credentials.json` ni el `auth.json` de Codex.
- Un `.json` que no parsea no se guarda, y en el `settings.json` de Claude no se
  pueden sumar los patrones que el bridge tampoco concede (`Bash(*)`, `rm -rf /`…).
- Si el archivo cambió en el servidor desde que lo abriste (por ejemplo, el
  agente sumó un permiso), la app te avisa en vez de pisarlo.
- Antes de cada guardado queda una copia en
  `~/.config/ragnar-bridge/respaldos-archivos/` (las últimas 30).

Requiere `ragnar-bridge` **0.8.0 o posterior**. Para apagarlo en un servidor,
`"archivos_editables": false` en el config.

**Si ya tenías un bridge en ese servidor**, volver a correr el comando (con el
mismo token o con otro) **reemplaza la config y reinicia el servicio**: es la
forma de actualizarlo y de cambiar de token. El servidor viejo de la app queda
"desconectado": revocalo desde **Mis servidores**.

## Config de agentes en tu PC: `sync` y `publicar`

El catálogo de Ragnar es la única fuente de la config de los agentes. En un PC
(sin bridge corriendo) se usa la misma lógica, con dos comandos que hablan con
la API REST de Ragnar. Ninguno recibe el token por argumento: se lee de la
variable `RAGNAR_AGENT_TOKEN` (un token **personal** de agente, `ragagt_...`,
que se crea en la app en «Mi token de agente»). La URL por defecto es
`https://panel.ragnargroup.app/api/v1`; se cambia con `--api` o con
`RAGNAR_API_URL`.

### `ragnar-bridge sync --once`

Baja los agentes/skills vigentes de un proyecto y los escribe con la misma
política que el modo automático (nunca pisa lo editado a mano):

```sh
ragnar-bridge sync --once --cli claude --project RAG --dir ~/proyectos/rg_ragnar_back
```

- `--project` es opcional: sin él baja solo lo global.
- Solo escribe bajo las carpetas permitidas de ese CLI (`.claude/agents/`,
  `.claude/skills/`, ...). Si Ragnar mandara algo fuera de ahí, rechaza el
  manifiesto entero sin escribir nada.
- Salida: una línea por archivo nuevo, actualizado o en conflicto, y un
  resumen. Sale con 0 aunque haya conflictos (se avisan, no se pisan) y con 1
  si no pudo hablar con Ragnar.
- El registro de SHA aplicados va a la carpeta del config del bridge
  (`~/.config/ragnar-bridge/`, o `--estado <carpeta>`).
- No hay modo daemon: en un servidor, el bridge ya recibe los cambios solo.

### Hook `SessionStart` de Claude Code

Para que cada sesión arranque con la config del día, agregá un hook
`SessionStart` en el `settings.json` del proyecto (`.claude/settings.json`) o en
el tuyo (`~/.claude/settings.json`). Ejemplo:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "ragnar-bridge sync --once --cli claude --project RAG --dir \"$CLAUDE_PROJECT_DIR\"",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

- `RAGNAR_AGENT_TOKEN` tiene que estar en el entorno con el que arranca Claude
  Code (en Windows: `setx RAGNAR_AGENT_TOKEN "ragagt_..."` y abrir una terminal
  nueva). No lo pongas en el `settings.json`.
- Lo que el comando imprime le llega a Claude como contexto de la sesión: si hay
  un conflicto, el agente lo ve.
- Si Ragnar no responde, la sesión arranca igual (con la config que ya tenías).

### `ragnar-bridge publicar`

Sube al catálogo (solo un **administrador**: el token del entorno tiene que ser
de uno) cada archivo de una carpeta que caiga bajo las carpetas permitidas del
CLI. La clave de cada artefacto se deriva de CLI + proyecto + ruta
(`claude/RAG/.claude/agents/rafael.md`; `_global` sin proyecto), así que
volver a publicar la misma ruta crea una **revisión nueva** del mismo
artefacto. Es idempotente: un archivo cuyo SHA-256 es igual al vigente no se
vuelve a subir.

```sh
ragnar-bridge publicar --cli claude --project RAG ~/proyectos/rg_ragnar_back
ragnar-bridge publicar --cli claude ~/agentes-globales   # sin --project: global
```

Se omiten (y se avisan) los archivos vacíos, los que no son texto UTF-8, los de
más de 256 KiB y los enlaces simbólicos. Cada publicación dispara el aviso
automático a los bridges conectados.

## Paso 5 — Verificá

```sh
~/.local/share/ragnar-bridge/venv/bin/ragnar-bridge doctor
systemctl --user status ragnar-bridge
journalctl --user -u ragnar-bridge -f
```

`doctor` no corre ningún turno (no gasta cuota); comprueba, en orden: el config,
cada CLI (instalado, versión y si tiene la sesión iniciada), la carpeta de trabajo
y que Ragnar acepte tu token. Que a un CLI le falte la sesión es un aviso, no un
error: te logueás y el bridge lo detecta solo. En la app, **Mis servidores** debe
mostrar el tuyo con un punto verde y los CLIs que encontró.

## Paso 6 — Probalo

En el chat de Agents escribí `respondé solo OK`. Después pedile algo que
necesite una herramienta, por ejemplo `listá los archivos de la carpeta actual`:
vas a ver la tarjeta **Autorizar** (paso siguiente).

### Cómo funcionan los permisos

- **Claude Code**: la tarjeta dice qué herramienta pide (ej. `Bash(docker *)`).
  Al aprobar, el bridge la agrega a un settings **propio de esa conversación**
  (`claude-permisos-<sesión>.json`, junto al config del bridge) que se pasa con
  `--settings` en cada turno de ella, y retoma el turno. Tu
  `~/.claude/settings.json` no se toca (hasta la 0.7.0 sí: la regla quedaba para
  siempre y también valía en tus sesiones interactivas, RAG-200). Además rechaza
  localmente toda regla sin un comando concreto: `Bash` a secas, `Bash(*)`,
  `Bash(:*)`, `Bash(**)`, intérpretes o envoltorios con comodín
  (`Bash(sh:*)`, `Bash(python *)`, `Bash(sudo:*)`) y `rm -rf /`.
- **Antigravity**: agy en modo headless **no puede preguntar** y `permissions.allow`
  de su `settings.json` se ignora ahí (issue #548 de antigravity-cli). La única
  forma de destrabarlo es aprobar *todas* las herramientas, así que el bridge
  no lo hace de entrada: deja que agy deniegue, te muestra la tarjeta
  `agy:permisos` con lo que intentó hacer, y **solo si aprobás** esa
  conversación pasa a correr con las herramientas aprobadas (el permiso es de
  esa conversación, no de las demás). Se controla con `agy_permisos`:

| `agy_permisos` | Qué pasa |
|---|---|
| `preguntar` (default) | Ragnar te pregunta; si aprobás, esa conversación queda aprobada |
| `denegar` | Nunca se concede: el agente solo lee y conversa |
| `auto` | Todo aprobado siempre (`--dangerously-skip-permissions`). **Solo en un servidor desechable** |
- **Codex**: en `codex exec` no hay nadie que apruebe comandos, así que Codex no
  pregunta: lo que el *sandbox* no permite falla, y por eso no hay tarjeta
  **Autorizar**. El alcance lo fija `codex_sandbox` en el config del bridge:
  `read-only` (default: lee y conversa), `workspace-write` (edita dentro de
  `workdir` y de `add_dirs`) o `danger-full-access`. Cada turno va con
  `approval_policy="never"`: lo que el sandbox no deja hacer falla y el modelo ve
  el error, nada se queda esperando una aprobación. Solo con `danger-full-access`
  el bridge pasa `--dangerously-bypass-approvals-and-sandbox` (sin sandbox: el
  modelo corre cualquier comando como el usuario del bridge). Hasta la 0.7.0 ese
  flag iba **siempre** y `codex_sandbox` no tenía efecto (RAG-196): si venías
  usando Codex para editar, ahora necesitás `workspace-write`. En Linux el
  sandbox de Codex usa Landlock/seccomp; en un contenedor sin esas capacidades
  los comandos del modelo fallan con `read-only`/`workspace-write`. El bridge nunca escribe en tu
  `~/.codex/config.toml`: sandbox, modelo y las tools de tickets van como
  overrides de cada turno, y el token de tickets viaja en el entorno del proceso,
  nunca en el comando.

## Configuración

`~/.config/ragnar-bridge/config.json` (o donde diga `RAGNAR_BRIDGE_CONFIG`):

| campo | default | para qué |
|---|---|---|
| `url`, `token` | — | los que muestra la app |
| `agents` | detecta los instalados | fuerza un subconjunto: `["claude"]`, `["agy"]`, `["codex"]`, `["hermes"]` o varios |
| `agent` | — | (0.2, un solo agente) se sigue leyendo; usá `agents` |
| `reprobar_cada` | `120` | cada cuántos segundos re-comprueba qué CLIs están instalados y con sesión |
| `claude_cmd` / `agy_cmd` / `codex_cmd` / `hermes_cmd` | `["claude"]` / `["agy"]` / `["codex"]` / `["hermes"]` | comando del CLI (lista, por si es un wrapper) |
| `workdir` | `~` | dónde arranca cada turno |
| `add_dirs` | `[]` | directorios extra accesibles (`--add-dir`) |
| `config_dir` | `~/.claude` | (claude) carpeta de config del CLI |
| `archivos_editables` | `true` | deja editar desde la app el `settings.json` y el `CLAUDE.md` de Claude y las rutas que sumes; `false` lo apaga |
| `tickets_token` | — | (claude) tu `ragagt_...`, para las tools de tickets de Ragnar |
| `agy_model` | — | (agy) modelo; vacío = el default de agy. Ver `agy models` |
| `agy_timeout` | `30m` | (agy) tope de un turno (`--print-timeout`) |
| `agy_permisos` | `preguntar` | (agy) ver arriba |
| `agy_dir` | `~/.gemini/antigravity-cli` | (agy) dónde agy guarda sus conversaciones |
| `codex_model` | `gpt-5.6-terra` | (codex) modelo. Se fija en **cada** turno, aunque tu `config.toml` de Codex diga otro; `""` = no fijarlo y usar el de Codex |
| `codex_reasoning` | `medium` | (codex) esfuerzo de razonamiento (`low`, `medium`, `high`, `xhigh`, `max`, `ultra`); `""` = no fijarlo |
| `codex_sandbox` | `read-only` | (codex) `read-only` (lee y conversa), `workspace-write` (edita dentro de `workdir` y de `add_dirs`) o `danger-full-access` (sin sandbox ni aprobaciones, `--dangerously-bypass-approvals-and-sandbox`: **solo en un servidor desechable**). Siempre con `approval_policy="never"` |
| `codex_home` | `~/.codex` | (codex) carpeta de Codex con tu sesión iniciada; solo se exporta `CODEX_HOME` si la cambiás |
| `max_turnos` | `4` | conversaciones simultáneas |
| `timeout_turno` | `7200` | tope de un turno en segundos, para cualquier agente: pasado esto el bridge corta el CLI (y sus hijos) y Ragnar ve el turno como fallido |

Después de editarlo: `systemctl --user restart ragnar-bridge`.

## Paso 7 — Cambiar entre CLIs

No hay nada que configurar: si el servidor tiene varios con sesión iniciada, el
chat de la app muestra un selector (**Claude Code | Antigravity | Codex**) al empezar una
conversación. Si uno deja de funcionar (te deslogueaste, lo desinstalaste), el
bridge lo avisa en un par de minutos y la app lo marca «sin sesión»; el otro sigue andando.
Después de loguearte, tocá **Volver a comprobar** en la app y se actualiza al instante.

Una conversación que ya empezó sigue con su CLI (su sesión vive ahí). Si ese CLI
no está disponible, la app te lo dice y podés empezar una conversación nueva con
el otro.

Solo si querés **dos bridges separados** en el mismo servidor (por ejemplo con
dos cuentas o dos carpetas de trabajo): creá otro servidor en la app y corré el
instalador con `--name otro` y el token del segundo.

## Qué hace y qué no hace

- Cada turno lanza el CLI en modo no interactivo con **tu** sesión y reenvía sus
  eventos. Para Antigravity y Codex el bridge traduce sus eventos al formato que Ragnar
  entiende (texto, herramientas y resultados, uso de tokens).
- Si se corta la conexión con Ragnar, el bridge **mata** los turnos en curso:
  nadie estaría viendo lo que el agente hace en tu servidor. Reconecta solo, con
  backoff.
- Ragnar ve el contenido de tus conversaciones (prompts y resultados de
  herramientas, estos últimos recortados). Lo que **no** ve es tu sesión de
  Claude, de Google o de OpenAI.
- Un token filtrado equivale a ejecutar comandos en tu servidor con los permisos
  del usuario del bridge: revocalo desde **Mis servidores** (corta el socket en
  el acto) y usá un usuario sin privilegios.

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| La app dice *«Tu servidor no está conectado»* | el servicio está caído o no sale a internet | `systemctl --user status ragnar-bridge`; `ragnar-bridge doctor` |
| `doctor`: *«Credencial inválida o revocada»* | token mal copiado o servidor revocado | generá otro servidor en la app y reinstalá |
| `doctor`: *«Protocolo … no soportado»* | Ragnar es más nuevo que tu bridge | actualizá (más abajo) |
| `doctor`: *«no encuentro `claude`/`agy`»* | el servicio no ve tu PATH | reinstalá desde una terminal donde el comando funcione, o poné la ruta absoluta en `claude_cmd`/`agy_cmd`/`codex_cmd`/`hermes_cmd` |
| El servicio se apaga al cerrar SSH | falta *linger* | como root: `loginctl enable-linger <usuario>` |
| Instalé con otro token y la app sigue *«Esperando que tu servidor se conecte»* | con el instalador de la 0.2 el servicio ya corría y no se reiniciaba: seguía con la config vieja | volvé a correr el comando de la app (la 0.3 reinicia el servicio), o `systemctl --user restart ragnar-bridge` |
| La app no ofrece Antigravity (o Claude) | ese CLI no está instalado, o le falta la sesión | `ragnar-bridge doctor` dice cuál; se corrige logueándose y el bridge lo detecta solo |
| El servicio no reintenta y sale con 78 | Ragnar rechazó el token | es a propósito: generá un token nuevo |
| (agy) el agente dice que no pudo ejecutar un comando | agy denegó la herramienta | aprobá la tarjeta en la app, o `agy_permisos` |
| (agy) un turno largo se corta a los 30 min | `agy_timeout` | subilo en el config (`"2h"`) |
| (codex) el agente dice que no pudo escribir o ejecutar algo | el sandbox es `read-only` | subí `codex_sandbox` a `workspace-write` en el config y reiniciá |
| (codex) `doctor` dice *«SIN sesión»* | Codex no tiene login en el usuario del bridge | `codex login` (o `codex login --device-auth`) con ese usuario; el bridge lo detecta solo |
| El agente dice que no tiene las tools de tickets | bridge anterior a la 0.3.3 (usaba una URL del MCP que Ragnar rechaza) | actualizá (arriba) y abrí una conversación nueva |
| (claude) *«No conversation found»* / *«Session ID already in use»* | el bridge ya lo evita (comprueba la sesión y limpia el lock) | si aparece igual, abrí un issue con el log |

## Actualizar

Cuando Ragnar o el chat te pida «actualizar el bridge» (o `doctor` diga que el protocolo no es soportado), en el VPS donde corre:

```sh
# 1. Ver qué versión tenés
~/.local/share/ragnar-bridge/venv/bin/ragnar-bridge --version

# 2. Actualizar (--force-reinstall para que traiga el código nuevo aunque la versión no haya cambiado)
~/.local/share/ragnar-bridge/venv/bin/pip install --upgrade --force-reinstall --no-deps git+https://github.com/RagnarGroup/rg_ragnar_bridge

# 3. Reiniciar el servicio y comprobar
systemctl --user restart ragnar-bridge
~/.local/share/ragnar-bridge/venv/bin/ragnar-bridge doctor
```

Tu configuración (`~/.config/ragnar-bridge/config.json`, con el token del servidor) no se toca. Reiniciar corta una conversación que esté corriendo en ese momento: hacelo cuando nadie esté usando el chat.

Si preferís, volver a correr el comando de instalación que muestra la app (Paso 4) también actualiza y reinicia.

Versiones que importan: **0.3.2** manda a cada turno el token de tickets de quien escribe; **0.3.3** arregla la URL del MCP de tickets (sin ella el agente decía que no tenía las tools de tickets); **0.5.0** suma Codex CLI; **0.5.1** fija su modelo (`gpt-5.6-terra`) y razonamiento (`medium`) en cada turno; **0.6.0** informa a Ragnar la cuota de Antigravity y de Codex (la app la muestra en *Mis servidores* y en `/usage`); **0.7.0** suma Hermes Agent; **0.8.0** deja editar archivos de configuración desde la app; **0.9.0** recibe imágenes adjuntas en el chat; **0.10.0** suma el protocolo 2, la sincronización automática de agentes (`sync --once`, `publicar`) y los arreglos de la auditoría (permisos por conversación, procesos huérfanos, tope por turno). **0.11.0** informa la cuota real de Claude Code; **0.11.1** renueva su token vencido a través del CLI y respeta los 429; **0.12.0** consulta la cuota solo cuando la app la pide (`/usage`, frame `probe`): los sondeos de rutina (al conectar y cada 120 s) reportan versión y sesión y conservan la última cuota conocida. **0.12.1** sella cada cuota consultada con `quota_at` para que la app sepa cuándo llegó la nueva. **0.13.0** devuelve al chat las imágenes que genera el agente (carpeta de salidas por turno, frame `image`) y le da a Antigravity acceso a la carpeta de adjuntos.

## Desinstalar

```sh
systemctl --user disable --now ragnar-bridge
rm -rf ~/.local/share/ragnar-bridge ~/.config/ragnar-bridge ~/.config/systemd/user/ragnar-bridge*.service
```

Y revocá el servidor desde **Mis servidores** en la app.

## macOS / Windows

El instalador es solo Linux + systemd. En otros sistemas se instala a mano (el
bridge en sí corre en Windows, probado en Windows 11):

```sh
python -m venv venv && venv/bin/pip install git+https://github.com/RagnarGroup/rg_ragnar_bridge   # Windows: venv\Scripts\pip
ragnar-bridge init --agent claude --url wss://...    # o --agent agy; pide el token (o RAGNAR_BRIDGE_TOKEN, o stdin)
ragnar-bridge doctor
ragnar-bridge run            # dejalo corriendo (launchd en macOS, Tareas programadas o NSSM en Windows)
```

## Desarrollo

```sh
pip install -e '.[dev]'
pytest
```

Las pruebas usan un Ragnar de mentira (`tests/helpers.py`) y dobles del CLI
(`tests/fake_claude.py`, `tests/fake_agy.py`, `tests/fake_codex.py`, `tests/fake_hermes.py`);
el de agy emite los eventos reales capturados de agy 1.1.27, el de Codex el JSONL de
`codex exec --json` (codex-cli 0.157.0), y el de Hermes el JSONL real de
`hermes chat --format stream-json` (hermes-agent 0.21.5).

El protocolo está documentado en `services/bridge_hub.py` de `ragnar_group_back`
(fuente de verdad); `PROTOCOLO` en `ragnar_bridge/__init__.py` tiene que
estar entre los `PROTOCOLOS_ACEPTADOS` del hub (hoy 1 y 2; este bridge habla 2,
así que necesita un backend que ya acepte la 2). Para sumar otro agente: una clase en `ragnar_bridge/agents/` que
arme el comando y traduzca sus eventos al formato de Claude Code (ver
`agents/agy.py`), y registrarla en `agents/__init__.py`.

## Pendiente

- Tools de tickets de Ragnar (MCP) para Antigravity.
- Tarjeta **Autorizar** para Codex (hoy el alcance se fija con `codex_sandbox`).
- Config global por máquina (`~/.claude/CLAUDE.md`, `~/.gemini/GEMINI.md`): necesita un prefijo de
  alcance usuario con allowlist explícita en el catálogo; hoy `sync` y el modo automático solo
  escriben dentro de un proyecto.
- Los conflictos de `sync` en el PC se avisan en la salida pero no se reportan a Ragnar (los del
  bridge sí quedan en la auditoría).
- Bootstrap de agentes/skills para Hermes: sus skills son globales (`~/.hermes/skills/`) y no hay
  una carpeta estándar por proyecto; el bridge responde que no lo soporta en vez de inventar una.
