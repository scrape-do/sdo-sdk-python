# SPDX-License-Identifier: Apache-2.0
"""Deterministic public-API surface extractor for the ``sdo`` package.

Walks every name in ``sdo.__all__`` and prints one line per public symbol or
member, so a diff against the previous release's golden classifies a change:

* a line that disappears or changes is a break (MAJOR);
* an added line is additive (MINOR), unless it ends in `` [required]`` and its
  owner already existed: a new required parameter breaks existing callers.

Grammar (one entry per line, ``<kind> <owner> [<rest>]``, single spaces)::

    export <name>                         a name in sdo.__all__
    data <name>: <type>                   module-level data (type only, never the value)
    class <Class>
    base <Class> <Base>                   every base in the MRO (object excluded)
    ctor <Class>.__init__
    method <Class>.<name>() -> <ret>      parameters are listed on param lines
    func <name>() -> <ret>
    prop <Class>.<name>: <type>
    attr <Class>.<name>: <type>           dataclass fields and declared attributes
    param <Owner.callable> [#<pos> ]<name>[: <type>][ = <default>][ [required]]

Positional parameters carry their position (``#0`` is the first after
``self``), so a reorder is a change. Keyword-only parameters carry none: adding
one anywhere in the signature (e.g. a ``ScrapeConfig`` field, all of which
after ``url`` are keyword-only) adds one line and removes none, so it
classifies as additive. Lines are fully sorted; nothing depends on the version
string, paths, hash order or the clock.

Stdlib only. Run::

    python tools/api_surface.py > api-surface.txt
"""

from __future__ import annotations

import inspect
from typing import Any

import sdo

_PKG = "sdo"
_P = inspect.Parameter

#: Dunder members that ARE public contract.
_CONTRACT_DUNDERS = frozenset({"__init__", "__enter__", "__exit__", "__aenter__", "__aexit__"})
#: Always-present bases that carry no information.
_NOISE_BASES = (object, BaseException)


def _ann(annotation: Any) -> str:
    """Annotations arrive as strings (``from __future__ import annotations``);
    render anything else through ``inspect`` for stability."""
    if annotation is inspect.Parameter.empty or annotation is inspect.Signature.empty:
        return ""
    if isinstance(annotation, str):
        return annotation.strip()
    return inspect.formatannotation(annotation)


def _is_ours(obj: Any) -> bool:
    mod = getattr(obj, "__module__", "") or ""
    return mod == _PKG or mod.startswith(_PKG + ".")


def _params(owner: str, func: Any, *, skip_first: bool) -> list[str]:
    """One ``param`` line per parameter of ``func``."""
    params = list(inspect.signature(func).parameters.values())
    if skip_first and params:
        params = params[1:]  # self / cls
    lines = []
    position = 0
    for p in params:
        name = p.name
        pos = ""
        if p.kind in (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD):
            pos = f"#{position} "
            position += 1
        elif p.kind is _P.VAR_POSITIONAL:
            name = "*" + name
        elif p.kind is _P.VAR_KEYWORD:
            name = "**" + name
        piece = pos + name
        ann = _ann(p.annotation)
        if ann:
            piece += f": {ann}"
        if p.default is not _P.empty:
            piece += f" = {p.default!r}"
        elif p.kind not in (_P.VAR_POSITIONAL, _P.VAR_KEYWORD):
            piece += " [required]"
        lines.append(f"param {owner} {piece}")
    return lines


def _returns(func: Any) -> str:
    ret = _ann(inspect.signature(func).return_annotation)
    return f" -> {ret}" if ret else ""


def _defining_class(cls: type, name: str) -> type | None:
    for base in cls.__mro__:
        if name in vars(base):
            return base
    return None


def _describe_class(cls: type) -> list[str]:
    name = cls.__name__
    lines = [f"class {name}"]
    lines += [f"base {name} {b.__name__}" for b in cls.__mro__[1:] if b not in _NOISE_BASES]

    for member in dir(cls):
        if member.startswith("_") and member not in _CONTRACT_DUNDERS:
            continue
        defining = _defining_class(cls, member)
        if defining is None or not _is_ours(defining):
            continue  # inherited from object / Exception
        raw = vars(defining)[member]
        qual = f"{name}.{member}"
        if isinstance(raw, property):
            ret = _ann(inspect.signature(raw.fget).return_annotation) if raw.fget else ""
            lines.append(f"prop {qual}" + (f": {ret}" if ret else ""))
        elif isinstance(raw, (staticmethod, classmethod)):
            kind = type(raw).__name__
            lines.append(f"{kind} {qual}(){_returns(raw.__func__)}")
            lines += _params(qual, raw.__func__, skip_first=kind == "classmethod")
        elif inspect.isfunction(raw):
            if member == "__init__":
                lines.append(f"ctor {qual}")
            else:
                lines.append(f"method {qual}(){_returns(raw)}")
            lines += _params(qual, raw, skip_first=True)

    # Declared attributes: dataclass fields and class-level annotations of our
    # classes (own and inherited from our bases).
    seen: set[str] = set()
    for klass in cls.__mro__:
        if not _is_ours(klass):
            continue
        for attr, ann in vars(klass).get("__annotations__", {}).items():
            if attr.startswith("_") or attr in seen:
                continue
            seen.add(attr)
            lines.append(f"attr {name}.{attr}: {_ann(ann)}")
    return lines


def build_surface() -> str:
    lines: list[str] = []
    for export in sdo.__all__:
        lines.append(f"export {export}")
        obj = getattr(sdo, export)
        if inspect.isclass(obj):
            lines += _describe_class(obj)
        elif inspect.isfunction(obj):
            lines.append(f"func {export}(){_returns(obj)}")
            lines += _params(export, obj, skip_first=False)
        else:
            lines.append(f"data {export}: {type(obj).__name__}")

    header = [
        "# sdo public API surface — one owner-qualified entry per line (see tools/api_surface.py).",
        "# Generated; do not edit. Regenerate: python tools/api_surface.py > api-surface.txt",
    ]
    return "\n".join(header + sorted(set(lines))) + "\n"


def main() -> None:
    import sys

    sys.stdout.write(build_surface())


if __name__ == "__main__":
    main()
