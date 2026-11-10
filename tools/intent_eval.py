"""Measure the intent route end to end — the exact server chain (frozen
semantics -> match_forms, else build_intent_prompt -> Ollama -> parse_intent),
no UI — and, optionally, run what it produced over a REAL folder through the
browser engine's own code (static/py/_explore.py under CPython, same as
tools/real_smoke.py).

    # model path, semantics from the database, inside the container
    python tools/intent_eval.py --generator slreport --questions tools/intent_eval_slreport.json

    # the same chain against real DeepSeek, from a laptop with egress
    #   (the key comes from the environment; never pass it where it lands in a log)
    OP_LLM_API_KEY=... python tools/intent_eval.py --generator slreport \
        --questions tools/intent_eval_slreport.json --provider deepseek \
        --semantics ~/Downloads/slreport-folder-bundle-2.2.15.zip

    # no LLM anywhere: only the deterministic form matcher, on real data
    python tools/intent_eval.py --generator slreport --questions tools/intent_eval_slreport.json \
        --semantics ~/Downloads/slreport-folder-bundle-2.2.15.zip \
        --no-llm --data-dir ~/data/SL --sheet Заказы --table sl_zakazy

Prints one line per question (source form|model, chosen vs expected KPI, dims,
filters, latency, and rows/error when a folder is given) plus an accuracy
summary. `--no-llm` scores the form matcher alone: a question whose `expect`
list is empty is CORRECT exactly when nothing matched confidently.
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
sys.path.insert(0, REPO)


def _load_semantics(args) -> dict | None:
    """Frozen semantics from the DB (what the server would use) or, with
    --semantics, straight from a bundle zip / semantics.json — the latter is
    what makes this runnable on a laptop with no OnePage database."""
    if args.semantics:
        from app.services import semantics as S
        if args.semantics.endswith(".zip"):
            from app.services import genstorage
            raw = genstorage.read_semantics_from_zip(open(args.semantics, "rb").read())
        else:
            raw = open(args.semantics, "rb").read()
        return S.parse_bytes(raw, args.generator)
    from app.database import SessionLocal
    from app.models import Generator, GeneratorVersion
    db = SessionLocal()
    row = (db.query(GeneratorVersion.semantics_json, GeneratorVersion.version_number)
           .join(Generator, Generator.id == GeneratorVersion.generator_id)
           .filter(Generator.name == args.generator, GeneratorVersion.is_current == True)  # noqa: E712
           .first())
    db.close()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def _index_real_folder(args, sem):
    """Index `--data-dir` through the worker's own module and bind `sem` to
    it, so every intent below can be turned into SQL and actually run.
    Returns (module, cleanup_dir) or (None, None) when there is no folder."""
    sys.path.insert(0, os.path.join(REPO, "static", "py"))
    import _explore
    from app.services import semantics as S
    files = sorted(p for p in globmod.glob(os.path.join(args.data_dir, args.glob))
                   if not os.path.basename(p).startswith("~$"))
    if not files:
        print(f"no files match {args.glob} in {args.data_dir}")
        return None, None
    tmp = tempfile.mkdtemp(prefix="op-inteval-")
    _explore.CACHE_DIR = os.path.join(tmp, "cache")
    catalog = {"tables": [{"table_id": "t_real", "name": args.table, "display_name": args.table,
                           "columns": [], "partitions": []}]}
    _explore.set_catalog(catalog)
    t0 = time.time()
    for p in files:
        name = os.path.basename(p)
        dst = os.path.join(tmp, name)
        shutil.copy(p, dst)
        st = os.stat(p)
        key = _explore.cache_key(name, st.st_size, int(st.st_mtime * 1000), args.sheet or "", 0)
        _explore.index_file(dst, args.sheet or "", 0, args.table, name.rsplit(".", 1)[0], name, key)
    types = _explore._conn().execute(f"DESCRIBE main.{_explore._qi(args.table)}").fetchall()
    catalog["tables"][0]["columns"] = [{"name": r[0]} for r in types]
    catalog["tables"][0]["partitions"] = [
        {"file": os.path.basename(p), "sheet": args.sheet or "", "key": os.path.basename(p).rsplit(".", 1)[0]}
        for p in files]
    bindings = S.bind(sem, catalog["tables"])
    _explore.set_catalog({**catalog, "semantics": [
        {"generator": args.generator, "generator_id": 1, "semantics": sem, "bindings": bindings}]})
    st = _explore.semantics_status()["generators"][0]
    ready = [k["key"] for k in st["kpis"] if k["ready"]]
    print(f"indexed {len(files)} files from {args.data_dir} in {int((time.time() - t0) * 1000)} ms · "
          f"ready KPIs: {len(ready)}/{len(st['kpis'])}\n")
    return _explore, tmp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generator", required=True)
    ap.add_argument("--questions", required=True, help="JSON: [{question, expect: [kpi keys]}]")
    ap.add_argument("--semantics", default=None, help="bundle zip or semantics.json (default: the DB's current version)")
    ap.add_argument("--model", default=None, help="override OP_LLM_MODEL for this run")
    ap.add_argument("--provider", default=None, choices=["ollama", "deepseek"],
                    help="override OP_LLM_PROVIDER for this run")
    ap.add_argument("--api-key", default=None,
                    help="bearer key for an external provider; defaults to $OP_LLM_API_KEY. "
                         "Pass a key on the command line only on a machine where the shell "
                         "history is yours — the environment variable is the safer route.")
    ap.add_argument("--ready", default=None, help="comma list of ready KPI keys (default: all)")
    ap.add_argument("--no-llm", action="store_true", help="exercise ONLY match_forms — never calls Ollama")
    ap.add_argument("--data-dir", default=None, help="real folder to index and run every intent over")
    ap.add_argument("--glob", default="*.xlsx")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--table", default="real_table")
    args = ap.parse_args()

    if args.model:
        os.environ["OP_LLM_MODEL"] = args.model
    if args.provider:
        os.environ["OP_LLM_PROVIDER"] = args.provider
    from app.config import settings
    if args.model:
        settings.llm_model = args.model
    if args.provider:
        settings.llm_provider = args.provider
    # The key never reaches stdout, an argv echo or the settings dump below:
    # it is read once, put on the settings object, and used only as an
    # Authorization header inside nl2sql.
    key = args.api_key or os.environ.get("OP_LLM_API_KEY") or ""
    if key:
        settings.llm_api_key = key
    from app.services import nl2sql

    sem = _load_semantics(args)
    if sem is None:
        print(f"generator {args.generator!r}: no current version with semantics")
        return 2
    ready = args.ready.split(",") if args.ready else None
    cases = json.load(open(args.questions, encoding="utf-8"))
    label = ("FORMS ONLY (no LLM)" if args.no_llm
             else f"{nl2sql.provider()} · model {nl2sql.model_for()}"
                  + ("" if nl2sql.provider() == "ollama" or key else "  [no API key — expect 503]"))
    print(f"generator {args.generator} · {label} · "
          f"{len(cases)} questions · {len(sem.get('kpis') or {})} KPIs in menu\n")

    engine, tmp = (None, None)
    if args.data_dir:
        engine, tmp = _index_real_folder(args, sem)

    hits = forms = form_wrong = 0
    declined_ok = declined_missed = 0
    sql_errors = 0
    try:
        for case in cases:
            q, expect = case["question"], case.get("expect") or []
            shortlist = nl2sql.rank_kpis(sem, q, ready)
            t0 = time.time()
            source, chosen, intent, detail = "none", None, None, ""
            hit = nl2sql.match_forms(sem, q, ready)
            if hit:
                source = "form"
                intent = nl2sql.form_intent(sem, hit["kpi"], q)
                chosen = intent["kpi"]
                detail = f"score={hit['score']} vs {hit['runner_up']} form={hit['form']!r}"
                forms += 1
            elif args.no_llm:
                detail = "no confident form match"
            else:
                source = "model"
                try:
                    fmt = nl2sql.build_intent_schema(sem, shortlist) if settings.llm_structured else None
                    raw = nl2sql.call_llm(nl2sql.build_intent_prompt(sem, shortlist, q), fmt)
                    intent = nl2sql.ground_intent(q, nl2sql.parse_intent(raw, sem))
                    chosen = intent["kpi"]
                except nl2sql.Nl2SqlError as e:
                    detail = f"ERROR {e.status_code}: {e.message[:80]}"
            ms = int((time.time() - t0) * 1000)
            if intent is not None:
                detail += (f" dims={intent['dims']} filters="
                           f"{[(f['dim'], f['op'], f.get('value', f.get('values'))) for f in intent['filters']]}"
                           f" limit={intent['limit']}")
            ok = (chosen in expect) if expect else (chosen is None)
            hits += int(ok)
            if source == "form" and not ok:
                form_wrong += 1
            if source == "none":
                declined_ok += int(not expect)
                declined_missed += int(bool(expect))
            run = ""
            if engine is not None and intent is not None:
                out = engine.intent_sql(dict(intent, generator_id=1))
                if out.get("error"):
                    run, sql_errors = f"  RUN intent error: {out['error'][:120]}", sql_errors + 1
                else:
                    res = engine.run_sql(out["sql"], limit=5)
                    if res.get("error"):
                        run, sql_errors = f"  RUN SQL ERROR: {str(res['error'])[:120]}", sql_errors + 1
                    else:
                        run = f"  RUN rows={len(res['rows'])} total={res.get('total')} sample={res['rows'][:1]}"
            print(f"{'OK  ' if ok else 'MISS'} {source:5s} {ms:6d} ms  {q}\n"
                  f"      chosen={chosen}  expected={expect or '(none)'}\n      {detail}"
                  + (f"\n    {run}" if run else ""))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    n = max(1, len(cases))
    print(f"\naccuracy: {hits}/{len(cases)} = {hits / n:.0%}")
    print(f"  form matched   : {forms}/{len(cases)}  ({forms - form_wrong} correct, {form_wrong} wrong"
          f" → precision {(forms - form_wrong) / max(1, forms):.0%})")
    print(f"  no form match  : {declined_ok + declined_missed}"
          f"  ({declined_ok} correctly declined — not a metric question,"
          f" {declined_missed} left to the model)")
    if engine is not None:
        print(f"  SQL over the real folder: {sql_errors} failures")
    return 0 if hits == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
