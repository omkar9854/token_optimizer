"""Code compression: structural skeletons instead of raw truncation.

For Python we use the real AST: keep imports, class/function signatures,
docstring first-lines, and module-level assignments; elide bodies with a
line-count note. The model keeps the *map* of the file — usually what it
actually needs — and can retrieve the full source via the store marker.

Query-aware body retention: when the caller passes the user's query, the
few function bodies most relevant to it are kept verbatim while the rest
are elided — the model sees the whole map plus the exact code it is being
asked about. (Blanket skeletonization forces a retrieval round-trip for
the one function the user actually cares about.)

For other languages we fall back to a brace/keyword skeleton: keep lines
that open declarations, drop deep indentation blocks with elision notes.
"""

from __future__ import annotations

import ast
import re
from typing import List, Optional

from .relevance import Bm25, _stem, tokenize

MAX_VERBATIM_BODIES = 3
DUPLICATE_JACCARD = 0.8

_PY_HINTS = re.compile(r"^\s*(def |class |import |from |@|if __name__)", re.MULTILINE)
_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:public|private|protected|static|async|final|abstract)?\s*"
    r"(?:function|class|interface|struct|enum|impl|fn|func|def|type|const|var|let|"
    r"void|int|string|bool|public|private)\b"
)


def looks_like_code(text: str) -> bool:
    lines = text.splitlines()
    if len(lines) < 8:
        return False
    if len(_PY_HINTS.findall(text)) >= 3:
        return True
    decl_hits = sum(1 for ln in lines[:80] if _DECL_RE.match(ln))
    brace_lines = sum(1 for ln in lines[:80] if ln.rstrip().endswith(("{", "}", ");")))
    return decl_hits + brace_lines >= len(lines[:80]) * 0.25


def compress_code(text: str, query: str = "") -> Optional[str]:
    result = _python_skeleton(text, query)
    if result is not None:
        return result
    return _generic_skeleton(text)


def _relevant_function_ids(tree: ast.AST, lines: List[str], query: str) -> set:
    """ids of function nodes whose bodies should be kept verbatim."""
    if not query:
        return set()
    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not funcs:
        return set()
    sources = [
        "\n".join(lines[f.lineno - 1 : getattr(f, "end_lineno", f.lineno)])
        for f in funcs
    ]
    bm25 = Bm25(sources)
    scored = sorted(
        ((bm25.score(i, query), i) for i in range(len(funcs))), reverse=True
    )
    # Relative gate: an absolute score threshold fails when the query term
    # appears in every function (IDF -> 0). What matters is which bodies
    # score *best*, as long as there is any signal at all. Diversity gate:
    # a body near-identical to one already kept adds no information
    # (common in codebases with many boilerplate-similar functions).
    best = scored[0][0]
    if best <= 1e-9:
        return set()
    keep: set = set()
    kept_tokens: List[set] = []
    for score, i in scored:
        if len(keep) >= MAX_VERBATIM_BODIES or score < best * 0.5:
            break
        toks = {_stem(t) for t in tokenize(sources[i])}
        if any(_jaccard(toks, prev) > DUPLICATE_JACCARD for prev in kept_tokens):
            continue
        keep.add(id(funcs[i]))
        kept_tokens.append(toks)
    return keep


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _python_skeleton(text: str, query: str = "") -> Optional[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None

    lines = text.splitlines()
    out: List[str] = []
    verbatim_ids = _relevant_function_ids(tree, lines, query)

    def emit_docstring(node: ast.AST, indent: str) -> None:
        doc = ast.get_docstring(node, clean=True)
        if doc:
            first = doc.splitlines()[0]
            out.append(f'{indent}"""{first}"""')

    def signature_lines(node: ast.AST) -> List[str]:
        # decorators + the def/class line(s) up to the colon
        start = min(
            [d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno]
        )
        body_start = node.body[0].lineno if node.body else node.lineno + 1
        sig = []
        for ln in lines[start - 1 : body_start - 1]:
            sig.append(ln)
            if ln.rstrip().endswith(":"):
                break
        return sig

    def body_size(node: ast.AST) -> int:
        return (getattr(node, "end_lineno", None) or node.lineno) - node.lineno + 1

    def visit(node: ast.AST, depth: int) -> None:
        indent = "    " * depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)) and depth == 0:
                out.append(lines[child.lineno - 1])
            elif isinstance(child, ast.ClassDef):
                out.extend(signature_lines(child))
                emit_docstring(child, indent + "    ")
                visit(child, depth + 1)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if id(child) in verbatim_ids:
                    start = min(
                        [d.lineno for d in child.decorator_list] + [child.lineno]
                    )
                    end = getattr(child, "end_lineno", child.lineno)
                    out.extend(lines[start - 1 : end])
                else:
                    out.extend(signature_lines(child))
                    emit_docstring(child, indent + "    ")
                    out.append(f"{indent}    ... # {body_size(child)} lines")
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and depth == 0:
                src_line = lines[child.lineno - 1]
                out.append(src_line if len(src_line) < 120 else src_line[:117] + "...")

    visit(tree, 0)
    if not out:
        return None
    skeleton = "\n".join(out)
    if len(skeleton) >= len(text) * 0.85:
        return None  # not worth it
    total = len(lines)
    return f"# skeleton of {total}-line python file (bodies elided)\n{skeleton}"


def _generic_skeleton(text: str) -> Optional[str]:
    lines = text.splitlines()
    out: List[str] = []
    elided = 0

    def flush() -> None:
        nonlocal elided
        if elided:
            out.append(f"    ... // {elided} lines elided")
            elided = 0

    for ln in lines:
        stripped = ln.strip()
        indent = len(ln) - len(ln.lstrip())
        is_structural = (
            _DECL_RE.match(ln)
            or stripped.startswith(("//", "/*", "*", "#", "import ", "package ", "using "))
            or (indent == 0 and stripped in ("}", "};"))
        )
        if is_structural:
            flush()
            out.append(ln)
        elif indent <= 2 and stripped.endswith("{"):
            flush()
            out.append(ln)
        else:
            elided += 1
    flush()
    if not out or len("\n".join(out)) >= len(text) * 0.85:
        return None
    return f"// skeleton of {len(lines)}-line file (bodies elided)\n" + "\n".join(out)
