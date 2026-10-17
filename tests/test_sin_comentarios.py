"""Regla del repo: sin comentarios en el código, solo un encabezado por archivo.

Falla si un `.py` versionado tiene comentarios fuera del encabezado (salvo
directivas como `noqa` o `type: ignore`) o docstrings de función o clase. Las
tools MCP (decoradas con `herramienta`) conservan su docstring: es la
descripción que leen los agentes.
"""

import ast
import io
import re
import subprocess
import tokenize
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DIRECTIVA = re.compile(r"noqa|type:\s*ignore|pragma|fmt:\s*(on|off|skip)|isort:|^#!|coding[:=]")
DECORADORES_CON_DOCSTRING = {"herramienta"}


def _archivos():
    salida = subprocess.run(["git", "ls-files", "*.py"], cwd=RAIZ, capture_output=True, text=True)
    if salida.returncode == 0 and salida.stdout.strip():
        return [RAIZ / f for f in salida.stdout.split()]
    return [p for p in RAIZ.rglob("*.py") if ".venv" not in p.parts]


def _nombre_decorador(d):
    d = d.func if isinstance(d, ast.Call) else d
    return d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")


def _problemas(ruta):
    src = ruta.read_text(encoding="utf-8")
    arbol = ast.parse(src)
    fin_encabezado = 0
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING):
            fin_encabezado = tok.start[0] - 1
            break
    if arbol.body and isinstance(arbol.body[0], ast.Expr) and isinstance(getattr(arbol.body[0], "value", None), ast.Constant):
        fin_encabezado = arbol.body[0].end_lineno
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and tok.start[0] > fin_encabezado and not DIRECTIVA.search(tok.string):
            yield f"{ruta.relative_to(RAIZ)}:{tok.start[0]} comentario"
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ast.get_docstring(nodo) is not None:
            if not {_nombre_decorador(d) for d in nodo.decorator_list} & DECORADORES_CON_DOCSTRING:
                yield f"{ruta.relative_to(RAIZ)}:{nodo.lineno} docstring de {nodo.name}"


def test_sin_comentarios_fuera_del_encabezado():
    problemas = [p for ruta in _archivos() for p in _problemas(ruta)]
    assert not problemas, "Comentarios fuera del encabezado (regla del repo):\n" + "\n".join(problemas[:50])
