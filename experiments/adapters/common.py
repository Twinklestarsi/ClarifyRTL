"""Shared helpers for the tool adapters."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXPERIMENTS = HERE.parent
RUNNER = EXPERIMENTS / "runner_cases"
WORKSPACE = EXPERIMENTS.parents[1]
DAG_ENV = WORKSPACE / "DAG/.env"
VERILATOR = Path("/home/2531930/nas/2531930-2/eda_tools/conda/bin/verilator")
CONDA_BIN = "/home/2531930/nas/2531930-2/eda_tools/conda/bin"

_PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
               "no_proxy", "socks_proxy", "socks5_proxy"}
for _key in list(os.environ):
    if _key.lower() in _PROXY_KEYS:
        os.environ.pop(_key, None)

MODELS = {
    "deepseek": {"model": "deepseek-flash",
                 "base": "https://api.deepseek.com/v1",
                 "key_env": "DEEPSEEK_API_KEY",
                 "model_env": "DEEPSEEK_MODEL",
                 "base_env": "DEEPSEEK_BASE_URL"},
    "qwen": {"model": "qwen3.8-27b",
             "base": "https://aic.tongjieda.cn/v1",
             "key_env": "DAG_API_KEY"},
    "deepseek_aic": {"model": "deepseek-v4.1-flash-1017",
                     "base": "https://aic.tongjieda.cn/v1",
                     "key_env": "DAG_API_KEY"},
    "mimo": {"model": "MiMo-V2.6-Flash",
             "base": "https://agi.tongji.edu.cn/v1",
             "key_env": "AGI_API_KEY"},
    "glm": {"model": "GLM-5.3-Flash",
            "base": "https://agi.tongji.edu.cn/v1",
            "key_env": "AGI_API_KEY_2"},
}


def load_env() -> None:
    for path in (DAG_ENV, WORKSPACE / ".env"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def model_config(name: str) -> dict[str, str]:
    load_env()
    spec = MODELS[name]
    key = os.environ.get(spec["key_env"], "")
    if not key:
        raise RuntimeError(f"{spec['key_env']} missing")
    model = os.environ.get(spec.get("model_env", ""), spec["model"])
    base = os.environ.get(spec.get("base_env", ""), spec["base"])
    return {"name": name, "model": model, "base": base, "key": key}


def manifest() -> list[dict[str, Any]]:
    return json.loads((RUNNER / "manifest.json").read_text(encoding="utf-8"))["cases"]


def case_info(key: str) -> dict[str, Any]:
    for row in manifest():
        if row["case_id"] == key or row["short"] == key:
            return row
    raise KeyError(key)


def extract_verilog(text: str) -> str:
    text = text.strip()
    fenced = re.findall(r"```(?:verilog|systemverilog|sv)?\s*(.*?)```", text, re.S | re.I)
    if fenced:
        text = max(fenced, key=len).strip()
    start = re.search(r"(?im)^\s*(?:module|`timescale)\b", text)
    if start:
        text = text[start.start():]
    end = list(re.finditer(r"(?im)^\s*endmodule\b", text))
    if end:
        text = text[:end[-1].end()]
    return text.strip() + "\n" if text.strip() else ""


def _tb_top(tb_text: str, dut_top: str) -> str:
    blocks = list(re.finditer(r"(?m)^\s*module\s+([A-Za-z_]\w*)", tb_text))
    for i, match in enumerate(blocks):
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(tb_text)
        body = tb_text[match.start():end]
        if re.search(r"\b" + re.escape(dut_top) + r"\s+[A-Za-z_]\w*\s*\(", body):
            return match.group(1)
    return blocks[0].group(1) if blocks else "tb"


def evaluate_rtl(rtl_path: Path, case: dict[str, Any], out_dir: Path,
                 *, timeout: int = 300) -> dict[str, Any]:
    rtl_path = Path(rtl_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tb = (RUNNER / case["testbench"]).resolve()
    tb_text = tb.read_text(encoding="utf-8", errors="replace")
    tb_top = _tb_top(tb_text, case["top_module"])
    build = out_dir / "build"
    build.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = CONDA_BIN + os.pathsep + env.get("PATH", "")
    env["CCACHE_DISABLE"] = "1"
    exe = out_dir / "sim"
    sources = [str(rtl_path)]
    if re.search(r"\bRefModule\b", tb_text):
        ref = (RUNNER / case["reference"]).resolve()
        if ref != rtl_path:
            sources.append(str(ref))
    cmd = [str(VERILATOR), "--language", "1800-2017", "--Wno-fatal", "--binary",
           "--timing", "--top-module", tb_top, "--Mdir", str(build),
           "-o", str(exe), *sources, str(tb)]
    started = time.time()
    try:
        comp = subprocess.run(cmd, cwd=str(out_dir), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stage": "compile", "elapsed_s": timeout}
    (out_dir / "compile.log").write_text(comp.stdout, encoding="utf-8")
    if comp.returncode != 0:
        errors = [l.strip() for l in comp.stdout.splitlines() if "%Error" in l]
        return {"status": "compile_failed", "stage": "compile", "tb_top": tb_top,
                "errors": errors[:12], "log_tail": comp.stdout[-4000:],
                "elapsed_s": round(time.time() - started, 2)}
    try:
        run = subprocess.run([str(exe)], cwd=str(out_dir), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "stage": "run", "tb_top": tb_top,
                "elapsed_s": timeout}
    (out_dir / "simulation.log").write_text(run.stdout, encoding="utf-8")
    markers = []
    for line in run.stdout.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        mism = re.search(r"mismatches:\s*(\d+)\s+in\s+(\d+)\s+samples", low)
        if ("test failed" in low or "fatal" in low or
                re.search(r"={3,}\s*error\b", low) or
                re.match(r"^(?:error|failed|failure|mismatch)\b", low) or
                (mism and int(mism.group(1)) > 0) or
                re.search(r"\btest completed with [1-9]\d* /\s*\d+ failures?\b", low)):
            markers.append(stripped)
    errors = [l.strip() for l in run.stdout.splitlines()
              if "ERROR" in l or "MISMATCH" in l]
    passed = run.returncode == 0 and not markers
    return {"status": "passed" if passed else "failed", "stage": "run",
            "tb_top": tb_top, "returncode": run.returncode,
            "failure_markers": markers[:8], "errors": errors[:8],
            "log_tail": run.stdout[-3000:],
            "elapsed_s": round(time.time() - started, 2)}


def write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def call_llm(messages: list[dict[str, str]], model_name: str, *,
             temperature: float = 0.0, max_tokens: int = 8000) -> tuple[str, dict]:
    from openai import OpenAI
    cfg = model_config(model_name)
    import time as _time
    client = OpenAI(api_key=cfg["key"], base_url=cfg["base"], timeout=600,
                    max_retries=0)
    last = None
    for attempt in range(8):
        try:
            resp = client.chat.completions.create(
                model=cfg["model"], messages=messages, temperature=temperature,
                max_tokens=max_tokens)
            text = resp.choices[0].message.content or ""
            usage = resp.usage.model_dump() if resp.usage else {}
            return text, usage
        except Exception as exc:  # noqa: BLE001
            last = exc
            if "429" in str(exc) or "capacity" in str(exc).lower():
                _time.sleep(8 + 4 * attempt)
                continue
            raise
    raise last
