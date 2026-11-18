"""Isolated adapter for the frozen monolithic IR generator.

The experiment and ``test/tempo_spec_ir_rtl`` carry different versions of the
``IRgeneration`` package.  Importing both in one interpreter would depend on
``sys.modules`` order, so the frontend always runs in a fresh subprocess.  The
API key is passed only through the environment and is never written to the
command line or metadata.

Two explicit frontend variants share that executable:

* ``original``  -- the frozen prompt exactly as shipped in ``test_root``;
* ``ir-grammar-v1`` -- the frozen prompt plus the versioned IR grammar
  contract this experiment's checker/compiler lowers (functions, generate
  loops, register arrays, memory depth).

The variant is resolved from ``phase_a.initial_ir.grammar_contract`` (``none``
selects the original), is recorded in ``run_config.json`` by ``run_case`` and is
hashed into :func:`frontend_fingerprint`, so a cached IR is only ever reused by
the exact variant that produced and checked it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_TEST_ROOT = Path(
    "/home/2531930/nas/2531930-2/test/tempo_spec_ir_rtl")
_FINGERPRINT_FILES = (
    "IRgeneration/ir_generate/gen_petrinet_ir.py",
    "IRgeneration/llm_stream.py",
)
_PROXY_KEYS = {"all_proxy", "http_proxy", "https_proxy", "no_proxy"}

# Versioned generation contracts.  A contract only documents constructs the
# deterministic checker/compiler already lowers; it never edits the checkout.
GRAMMAR_CONTRACT_VERSION = "ir-grammar-v1"
DEFAULT_GRAMMAR_CONTRACT = GRAMMAR_CONTRACT_VERSION
_GRAMMAR_V1 = """# Additional IR grammar: abstraction and hard compiler rules

This appendix is appended to the monolithic frontend system prompt.  It
documents IR constructs the deterministic checker/compiler of this experiment
actually lowers.  It changes no behaviour of an IR that does not use them.

---

# 12. Abstraction: functions, generate loops, register arrays

The compiler lowers these three constructs before RTL generation, so use them
instead of hand-unrolling repetitive structure.  They are optional; flat IR is
still valid.

## 12.1 Pure functions

```yaml
functions:
- name: inc
  params: [{name: a, width: 8}]
  returns: {width: 8}
  body:
  - assign: {target: t, expr: "a + 1"}
  - return: t
```

A function must be pure: read only its parameters and local variables, never a
state or port, no recursion.  ``body`` items are ``assign``, ``if`` (with
``then``/``else`` lists) and exactly one ``return``.  Calls are inlined into a
single Verilog expression, so the body must stay combinational.

## 12.2 Generate loops

```yaml
generate:
- name: lanes
  for: {var: i, start: 0, stop: LANES}
  emit:
  - transition:
      name: "lane_{i}_update"
      clock_domain: main
      guard: "en"
      action: "lane[{i}] = din"
  - derived_signal: {name: "lane_{i}_empty", width: 1, expression: "lane[{i}] == 0"}
```

``start``/``stop``/``step`` are compile-time integer expressions and may use
module parameters.  ``for`` may also be a list of loop mappings for a Cartesian
nested expansion.  Braced compile-time expressions such as ``{i}``,
``{i + 1}``, and ``{LANES}`` are replaced in every emitted string.  This can be
used to express parameterized reductions and priority scans as explicit chains
of generated intermediate signals; do not place procedural ``for`` text inside
an expression.  Every emitted name must be unique after substitution.

```yaml
generate:
- name: any_active_seed
  for: {var: unused, start: 0, stop: 1}
  emit:
  - derived_signal: {name: any_active_0, width: 1, expression: "1'b0"}
- name: any_active_chain
  for: {var: i, start: 0, stop: LANES}
  emit:
  - derived_signal:
      {name: "any_active_{i + 1}", width: 1,
       expression: "any_active_{i} | active[{i}]"}
```

## 12.3 Register arrays and memories

```yaml
states:
- name: lane
  type: register
  width: 8
  clock_domain: main
  reset_value: 8'd0
  shape: [4]
```

``shape: [N]`` expands to ``lane_0 .. lane_{N-1}``.  Indices must be constants
(use ``generate`` to iterate).  ``type: memory``/``ram`` keep ``shape`` as their
depth and accept variable indices.

Every ``type: memory`` or ``type: ram`` state MUST carry its depth, either as

```yaml
- name: table
  type: memory
  width: 32
  clock_domain: main
  depth: 16
  reset_value: 0
```

or with ``shape: [16]``.  A memory without depth/shape is rejected by the
validator; never emit one.

# 13. Hard rules for the compiler front-end

- ``clock_domains[].clock`` and ``clock_domains[].reset`` MUST be names of input
  ports in the specification.  Never invent ``reset`` when the port is ``rst_n``.
- Every ``derived_signals[].expression`` is a single right-hand side.  Never emit
  ``name = ...`` or any other assignment inside it.
- A state that is indexed with a runtime value (not a constant) must be declared
  with ``type: memory`` (or ``ram``); register arrays only accept constant indices.
- Every expression may reference only declared ports, states, derived signals
  and module parameters.  Never invent a helper signal name that is not
  declared in the same IR.
- A bit/part-select may only apply to a declared signal identifier, never to a
  parenthesised expression or a concatenation.  To take the high half of a
  product, declare an intermediate derived signal or use a right shift
  (`(a * b) >> DATAW`); `(a * b)[15:8]` is not legal Verilog after inlining.

## RAM and shift pipelines (the compiler lowers these fields directly)

- A memory preloaded at time zero declares `initial_value: <expr>` on the
  state, optionally `initial_enable: <expr>`; the compiler emits an `initial`
  block and honours the enable. Do not rely on a parameter name for this.
- A memory with byte enables declares `write_mask: <port>`; the compiler
  emits one masked slice write per mask bit. Write `ram[addr] = data`, not a
  hand-merged word.
- Shift pipelines must be parameter-safe at depth 1:
    `valid_pipe = (valid_pipe << 1) | sample_valid`
    `data_pipe  = (data_pipe << DATAW) | sample_data`
  never `pipe[DEPTH-2:0]`, which is `[-1:0]` when DEPTH is 1.
- Reading a word out of a wide pipeline is
  `pipe[DATAW*(DEPTH-1) +: DATAW]`."""

GRAMMAR_CONTRACTS = {GRAMMAR_CONTRACT_VERSION: _GRAMMAR_V1}


class TestFrontendError(RuntimeError):
    """Raised when the isolated test generator cannot produce its artifact."""


def resolve_generation_contract(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the requested frontend grammar variant.

    Returns ``{"version", "enabled", "text", "sha256"}``.  ``none``/``off``/
    ``original`` select the untouched frontend; an unknown name is an error so
    a typo can never silently fall back to a different experiment arm.
    """
    requested = str((settings or {}).get("grammar_contract",
                                         DEFAULT_GRAMMAR_CONTRACT)).strip()
    if requested.lower() in {"", "none", "off", "original", "false"}:
        return {"version": "none", "enabled": False, "text": "", "sha256": ""}
    if requested not in GRAMMAR_CONTRACTS:
        raise ValueError(
            f"unknown IR grammar contract {requested!r}; choose one of "
            f"{sorted(GRAMMAR_CONTRACTS)} or 'none'")
    text = GRAMMAR_CONTRACTS[requested]
    return {"version": requested, "enabled": True, "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def public_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    """The contract fields that belong in run_config / metadata."""
    contract = contract or resolve_generation_contract()
    return {"version": contract["version"], "enabled": bool(contract["enabled"]),
            "sha256": contract["sha256"]}


def frontend_fingerprint(test_root: Path | str, *,
                         contract: dict[str, Any] | None = None) -> str:
    """Hash the frontend files plus the selected generation contract."""
    root = Path(test_root).resolve()
    contract = contract or resolve_generation_contract()
    digest = hashlib.sha256()
    digest.update(b"test-monolithic-ir-v2\0")
    for relative in _FINGERPRINT_FILES:
        source = root / relative
        if not source.is_file():
            raise FileNotFoundError(f"test frontend file not found: {source}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    digest.update(b"generation-contract\0")
    digest.update(str(contract.get("version", "none")).encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(contract.get("sha256", "")).encode("utf-8"))
    digest.update(b"\0")
    return f"test-monolithic-ir-v2:{digest.hexdigest()}"


def generate_with_test_frontend(
        spec_path: Path | str, output_path: Path | str,
        env: dict[str, str], *, test_root: Path | str = DEFAULT_TEST_ROOT,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout_s: float = 1200.0,
        contract: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    """Generate one complete IR with the test source in an isolated process."""
    root = Path(test_root).resolve()
    spec = Path(spec_path).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    contract = contract or resolve_generation_contract()
    fingerprint = frontend_fingerprint(root, contract=contract)
    metadata_path = output.with_name("test_frontend_response.json")
    log_path = output.with_name("test_frontend.log")
    contract_path = output.with_name("generation_contract.txt")
    if contract["enabled"]:
        contract_path.write_text(contract["text"].strip() + "\n", encoding="utf-8")

    command = [
        sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
        "--test-root", str(root), "--spec", str(spec),
        "--output", str(output), "--metadata", str(metadata_path),
        "--contract-version", str(contract["version"]),
        "--temperature", str(float(temperature)),
        # No output cap; the old explicit value was 32000.
        "--max-tokens", "none" if max_tokens is None else str(int(max_tokens)),
    ]
    if contract["enabled"]:
        command.extend(["--contract", str(contract_path)])
    worker_env = os.environ.copy()
    for key in list(worker_env):
        if key.lower() in _PROXY_KEYS:
            worker_env.pop(key, None)
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        value = env.get(key)
        if not value:
            raise TestFrontendError(f"missing generator configuration: {key}")
        worker_env[key] = value
    worker_env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        completed = subprocess.run(
            command, cwd=str(root), env=worker_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=float(timeout_s), check=False)
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        log_path.write_text(text, encoding="utf-8")
        raise TestFrontendError(
            f"test frontend timed out after {timeout_s:g}s; see {log_path}") from exc

    log_path.write_text(completed.stdout or "", encoding="utf-8")
    if completed.returncode != 0:
        raise TestFrontendError(
            f"test frontend exited with {completed.returncode}; see {log_path}")
    if not output.is_file():
        raise TestFrontendError(f"test frontend did not create IR: {output}")
    if not metadata_path.is_file():
        raise TestFrontendError(
            f"test frontend did not create metadata: {metadata_path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update({
        "frontend": "test_monolithic",
        "frontend_variant": contract["version"],
        "generation_contract": public_contract(contract),
        "frontend_root": str(root),
        "frontend_fingerprint": fingerprint,
        "frontend_log": str(log_path),
        "frontend_metadata": str(metadata_path),
        "frontend_contract_file": str(contract_path) if contract["enabled"] else "",
    })
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return output, metadata


def _worker(args: argparse.Namespace) -> int:
    root = Path(args.test_root).resolve()
    if not root.is_dir():
        raise TestFrontendError(f"test frontend root not found: {root}")
    sys.path.insert(0, str(root))

    from IRgeneration.ir_generate import gen_petrinet_ir

    contract = resolve_generation_contract({"grammar_contract": args.contract_version})
    if contract["enabled"]:
        if not args.contract:
            raise TestFrontendError("contract version selected without a contract file")
        text = Path(args.contract).read_text(encoding="utf-8").strip()
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != contract["sha256"]:
            raise TestFrontendError(
                f"generation contract {contract['version']} file does not match its version")
        # Explicit, recorded variant: the frozen prompt plus the versioned
        # grammar contract.  The prompt is never edited in place.
        gen_petrinet_ir.SYSTEM_PROMPT = (
            gen_petrinet_ir.SYSTEM_PROMPT.rstrip()
            + "\n\n---\n\n# Experiment generation contract: "
            + contract["version"] + "\n\n" + text + "\n")

    max_tokens = (None if str(args.max_tokens).strip().lower() in {"", "none"}
                  else int(args.max_tokens))
    ir_path, metadata = gen_petrinet_ir.generate_ir(
        Path(args.spec).resolve(), Path(args.output).resolve(),
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        model=os.environ["OPENAI_MODEL"],
        temperature=float(args.temperature), max_tokens=max_tokens,
        repair=True, allow_invalid=True)
    payload = dict(metadata or {})
    payload.update({
        "ir": str(Path(ir_path).resolve()),
        "frontend": "test_monolithic",
        "frontend_variant": contract["version"],
        "generation_contract": public_contract(contract),
        "frontend_root": str(root),
        "frontend_fingerprint": frontend_fingerprint(root, contract=contract),
    })
    Path(args.metadata).resolve().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="isolated worker for the frozen test IR frontend")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--test-root", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--contract", default="")
    parser.add_argument("--contract-version", default="none")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", default="none")  # was 32000
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("this module is an internal --worker entry point")
    return _worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
