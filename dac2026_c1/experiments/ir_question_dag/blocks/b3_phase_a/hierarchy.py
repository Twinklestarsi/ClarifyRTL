"""Split a reference RTL module into always blocks (hierarchical generation).

Each block becomes the unit for one IR section: its sensitivity list, the
signals it assigns, and the signals it reads are extracted so the generated
section can be constrained to that namespace.
"""
from __future__ import annotations
import re
from pathlib import Path

_MODULE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\s*(#\s*\((.*?)\))?\s*\((.*?)\);", re.S)
_ALWAYS = re.compile(r"\balways(?:_ff|_comb|_latch)?\s*(?:@\s*(?:\(([^)]*)\)|\*))?")
_ASSIGN = re.compile(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:<=|=(?!=))")
_READ = re.compile(r"[A-Za-z_][\w$]*")


def _blocks(text: str):
    """Return (header, blocks) where each block has range/sensitivity/writes/reads."""
    matches = list(_ALWAYS.finditer(text))
    blocks = []
    for index, match in enumerate(matches):
        start = match.start()
        # Block body ends at the matching "end" of its always statement: walk forward.
        depth = 0
        end = len(text)
        for token in re.finditer(r"\b(begin|end)\b", text[match.end():]):
            if token.group(1) == "begin":
                depth += 1
            else:
                depth -= 1
                if depth <= 0:
                    end = match.end() + token.end()
                    break
        body = text[match.end():end]
        writes = sorted(set(_ASSIGN.findall(body)))
        reads = sorted({name for name in _READ.findall(body)
                        if name not in writes and name not in {"if", "else", "case", "endcase",
                                                               "begin", "end", "always", "assign",
                                                               "posedge", "negedge", "or"}})
        blocks.append(dict(index=index, start=start, end=end,
                           sensitivity=(match.group(1) or "*").strip(),
                           writes=writes, reads=reads,
                           lines=text[:start].count("\n") + 1))
    return blocks


def split_reference(path: Path | str) -> dict:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    module = _MODULE.search(text)
    header = {}
    if module:
        header = {"name": module.group(1), "parameters": module.group(3) or "",
                  "ports": module.group(4) or ""}
    blocks = _blocks(text)
    combined = set()
    for block in blocks:
        combined |= set(block["writes"])
    return {"module": header, "blocks": len(blocks), "written_signals": sorted(combined),
            "via": [{"index": b["index"], "line": b["lines"], "sensitivity": b["sensitivity"],
                     "writes": b["writes"][:8]} for b in blocks]}
