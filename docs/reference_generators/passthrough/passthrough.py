#!/usr/bin/env python3
"""Stock generator `passthrough` — the reference for reports on GreenPlum data.

It computes nothing: it takes the files the agent put into `--input-dir` (the
COPY→csv.gz exports from `sources`), lays them out into slots according to the
manifest's `outputs.slots`, and returns the HTML template from the bundle. All
the "report logic" lives in the static HTML (as in SL: typed arrays + filters on
the client) — neither the server nor the agent touches the data.

The contract is shared by the browser and the agent (RFC E.1, slreport.engine._cli):
    python -m passthrough --input-dir IN --output-dir OUT
                          [--cache-dir C] [--schema-version V] [--params-json J]
Progress is reported as `[stage] done/total msg` lines; the LAST stdout line is
the JSON result `{"html","slots","write_back"}`.

The generator manifest (in the bundle) declares:
    "outputs": {"html": true, "slots": [{"index":0,"name":"slot_0.csv.gz"}, ...]}
    "sources": {... "queries":[{"output_name":"slot_0.csv.gz", ...}, ...]}
where the output_name of EVERY query equals the name of the matching slot.
The bundle's default HTML template is `template.html` (overridden by
`params_json.template`).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def _progress(stage: str, done: int, total: int, msg: str = "") -> None:
    print(f"[{stage}] {done}/{total} {msg}".rstrip(), flush=True)


def _load_manifest(bundle_dir: Path) -> dict:
    return json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))


def run(input_dir: Path, output_dir: Path, bundle_dir: Path,
        template_name: str = "template.html") -> dict:
    manifest = _load_manifest(bundle_dir)
    outputs = manifest.get("outputs") or {}
    slot_specs = [s for s in (outputs.get("slots") or []) if isinstance(s, dict)]
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(slot_specs) + (1 if outputs.get("html") else 0)
    step = 0
    slots: dict[str, str] = {}

    for spec in slot_specs:
        name = spec.get("name")
        index = spec.get("index")
        src = input_dir / name
        if not src.is_file():
            raise FileNotFoundError(
                f"passthrough: no input file {name!r} for slot {index} "
                f"in {input_dir} — did the source ({name}) fail?")
        shutil.copyfile(src, output_dir / name)
        slots[str(index)] = name
        step += 1
        _progress("copy", step, total, name)

    html_name = None
    if outputs.get("html"):
        template = bundle_dir / template_name
        if not template.is_file():
            raise FileNotFoundError(
                f"passthrough: HTML template {template_name!r} not found in the bundle")
        html_name = "dashboard.html"
        shutil.copyfile(template, output_dir / html_name)
        step += 1
        _progress("html", step, total, html_name)

    return {"html": html_name, "slots": slots, "write_back": {}}


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="passthrough",
                                 description="Stock GreenPlum passthrough generator.")
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--cache-dir", default=None)          # unused, but the contract is shared
    ap.add_argument("--schema-version", default="")
    ap.add_argument("--params-json", default=None)
    args = ap.parse_args(argv)

    template_name = "template.html"
    if args.params_json:
        blob = args.params_json
        if blob.startswith("@"):
            blob = Path(blob[1:]).read_text(encoding="utf-8")
        try:
            params = json.loads(blob) or {}
            template_name = params.get("template", template_name)
        except ValueError:
            pass

    # cwd = root of the unpacked bundle (the agent starts the subprocess with cwd=app_dir).
    result = run(Path(args.input_dir), Path(args.output_dir), Path.cwd(), template_name)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
