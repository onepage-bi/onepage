"""Smoke test for the generators feature (Stage 1) — full API lifecycle.

Runs against a throwaway SQLite DB + uploads dir. Exercises:
registration (409/400/413/422), create-generated draft flow, manifest/bundle
delivery, run-lock (409 live / lost by TTL), heartbeat, error flow, atomic
publish (draft lifted, version bumped), git-mode 503, permission gating.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import zipfile

TMP = tempfile.mkdtemp(prefix="op-smoke-")
os.environ.update({
    "OP_DB_PATH": f"{TMP}/onepage.db",
    "OP_UPLOADS_DIR": f"{TMP}/uploads",
    "OP_PERF_DIR": f"{TMP}/perflog",
    "OP_DEV_BYPASS_AUTH": "true",
    "OP_INITIAL_ADMIN": "admin1",
    "OP_CACHE_WARM": "0",
    "OP_GENERATORS_BUNDLE_MAX_MB": "1",  # small cap so the 413 test stays cheap
})

sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.services import genmanifest  # noqa: E402

# The version note is an English DB string (app/api/generators.py; a stored
# note is not translated, see "Not translated" in CLAUDE.md).
NOTE_SCENARIO = "scenario"
NOTE_OFFLINE = "built offline"

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def make_bundle(name="sl-test", engine="auto", deps=None, manifest_override=None, pad_bytes=0, aux_inputs=None):
    m = {
        "name": name,
        "version": "1.0",
        "entrypoint": {"module": "sltest.main"},
        "outputs": {"html": True, "slots": [{"index": 0, "name": "sl_bc_data.bin.gz", "write_back": True}]},
        "engine": engine,
    }
    if deps is not None:
        m["deps"] = deps
    if aux_inputs is not None:
        m["aux_inputs"] = aux_inputs
    if manifest_override is not None:
        m = manifest_override
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(m))
        z.writestr("sltest/__init__.py", "")
        z.writestr("sltest/main.py", "def run(file_iter, out_dir, progress):\n    pass\n")
        if pad_bytes:
            z.writestr("pad.bin", os.urandom(pad_bytes))  # urandom -> incompressible
    return buf.getvalue()


def login(c, person_id):
    r = c.post("/dev-login", data={"person_id": person_id}, follow_redirects=False)
    assert r.status_code == 302, f"dev-login {person_id}: {r.status_code}"


def post_bundle(c, url, zip_bytes):
    return c.post(url, files={"bundle": ("bundle.zip", zip_bytes, "application/zip")})


with TestClient(app) as c:
    # Second (non-admin) user for permission checks
    from app.database import SessionLocal
    from app.models import Dashboard, GenerationRun, User
    _db = SessionLocal()
    _db.add(User(person_id="anna", role="superuser"))
    _db.commit()
    _db.close()

    # --- registration: permissions -------------------------------------
    login(c, "anna")
    r = post_bundle(c, "/api/admin/generators", make_bundle())
    check("superuser cannot register (403)", r.status_code == 403, str(r.status_code))

    login(c, "admin1")
    r = post_bundle(c, "/api/admin/generators", make_bundle())
    check("register bundle -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    gen = r.json()
    gid = gen.get("id")
    check("register returns name+version", gen.get("name") == "sl-test" and gen.get("version_number") == 1, str(gen))

    r = post_bundle(c, "/api/admin/generators", make_bundle())
    check("duplicate name -> 409", r.status_code == 409, str(r.status_code))
    # The refusal must say WHERE the bundle goes: the admin UI uses these
    # fields to offer uploading it as a new version, instead of sending a
    # person to hunt for a row in the table (owner, 05.10).
    d = (r.json() or {}).get("detail")
    check("409 carries a machine-readable reason", isinstance(d, dict) and d.get("error") == "generator_exists", str(d))
    check("409 names the generator", isinstance(d, dict) and d.get("name") == "sl-test", str(d))
    check("409 names the id to upload the version to",
          isinstance(d, dict) and isinstance(d.get("generator_id"), int) and d["generator_id"] > 0, str(d))
    # The id points at the ALREADY registered generator, not at something new.
    # The version upload itself is deliberately not done here: further down
    # version numbers are checked in order, and an extra version would shift them.
    check("409 points at the existing generator",
          isinstance(d, dict) and d.get("generator_id") == gen.get("id"),
          "%s vs %s" % (d, gen.get("id")))

    # --- registration: validation errors --------------------------------
    bad = make_bundle(manifest_override={"nam": "typo"})
    r = post_bundle(c, "/api/admin/generators", bad)
    check("broken manifest -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "no manifest here")
    r = post_bundle(c, "/api/admin/generators", buf.getvalue())
    check("zip without manifest -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = post_bundle(c, "/api/admin/generators", make_bundle(name="fat", pad_bytes=2 * 1024 * 1024))
    check("oversized bundle -> 413", r.status_code == 413, str(r.status_code))

    r = post_bundle(c, "/api/admin/generators",
                    make_bundle(name="br-bad", engine="browser",
                                deps=[{"name": "definitely-not-a-real-pkg-x77", "version": "1.0.0"}]))
    check("browser engine + unavailable dep -> 422", r.status_code == 422, f"{r.status_code} {r.text[:300]}")

    # --- aux_inputs validation (direct genmanifest tests) -----------------
    # Base manifest with a write_back slot for aux_inputs to reference
    base_with_writeback = {
        "name": "test-wb",
        "version": "1.0",
        "entrypoint": {"module": "test.main"},
        "outputs": {"html": True, "slots": [{"index": 0, "name": "sl_unassigned_reasons.xlsx", "write_back": True}]},
        "engine": "auto",
    }

    # Valid aux_inputs
    valid_aux = {
        **base_with_writeback,
        "aux_inputs": [
            {"name": "sl_assigned_reasons.xlsx", "label": "Run with reasons", "download_from": "sl_unassigned_reasons.xlsx"}
        ]
    }
    check("validate() with valid aux_inputs -> []", genmanifest.validate(valid_aux) == [], str(genmanifest.validate(valid_aux)))

    # normalize() defaults aux_inputs to []
    manifest_no_aux = {
        "name": "test",
        "version": "1.0",
        "entrypoint": {"module": "test.main"},
        "outputs": {"html": True},
    }
    normalized = genmanifest.normalize(manifest_no_aux)
    check("normalize() defaults aux_inputs to []", normalized.get("aux_inputs") == [], str(normalized.get("aux_inputs")))

    # aux_inputs not a list
    bad_not_list = {**base_with_writeback, "aux_inputs": {"name": "x"}}
    check("reject: aux_inputs not a list", "aux_inputs" in str(genmanifest.validate(bad_not_list)), str(genmanifest.validate(bad_not_list)))

    # 4 entries (more than 3 allowed)
    bad_4_entries = {
        **base_with_writeback,
        "aux_inputs": [
            {"name": "a.xlsx", "label": "A"},
            {"name": "b.xlsx", "label": "B"},
            {"name": "c.xlsx", "label": "C"},
            {"name": "d.xlsx", "label": "D"},
        ]
    }
    check("reject: 4 aux_inputs entries (max 3)", "aux_inputs" in str(genmanifest.validate(bad_4_entries)), str(genmanifest.validate(bad_4_entries)))

    # Missing name
    bad_no_name = {**base_with_writeback, "aux_inputs": [{"label": "No name"}]}
    check("reject: aux_inputs entry missing name", "aux_inputs" in str(genmanifest.validate(bad_no_name)) or "name" in str(genmanifest.validate(bad_no_name)), str(genmanifest.validate(bad_no_name)))

    # Name with '..'
    bad_path_traversal = {**base_with_writeback, "aux_inputs": [{"name": "a/../b.xlsx", "label": "Bad"}]}
    check("reject: aux_inputs name with '..'", "aux_inputs" in str(genmanifest.validate(bad_path_traversal)) or "name" in str(genmanifest.validate(bad_path_traversal)), str(genmanifest.validate(bad_path_traversal)))

    # Name starting with '.'
    bad_dot_start = {**base_with_writeback, "aux_inputs": [{"name": ".hidden.xlsx", "label": "Bad"}]}
    check("reject: aux_inputs name starting with '.'", "aux_inputs" in str(genmanifest.validate(bad_dot_start)) or "name" in str(genmanifest.validate(bad_dot_start)), str(genmanifest.validate(bad_dot_start)))

    # Missing label
    bad_no_label = {**base_with_writeback, "aux_inputs": [{"name": "file.xlsx"}]}
    check("reject: aux_inputs entry missing label", "aux_inputs" in str(genmanifest.validate(bad_no_label)) or "label" in str(genmanifest.validate(bad_no_label)), str(genmanifest.validate(bad_no_label)))

    # Label of 65 chars (too long, max 64)
    bad_long_label = {**base_with_writeback, "aux_inputs": [{"name": "file.xlsx", "label": "x" * 65}]}
    check("reject: aux_inputs label of 65 chars", "aux_inputs" in str(genmanifest.validate(bad_long_label)) or "label" in str(genmanifest.validate(bad_long_label)), str(genmanifest.validate(bad_long_label)))

    # download_from referencing slot WITHOUT write_back
    bad_no_writeback = {
        "name": "test",
        "version": "1.0",
        "entrypoint": {"module": "test.main"},
        "outputs": {"html": True, "slots": [{"index": 0, "name": "readonly.xlsx"}]},
        "aux_inputs": [{"name": "file.xlsx", "label": "X", "download_from": "readonly.xlsx"}]
    }
    check("reject: download_from slot without write_back", "aux_inputs" in str(genmanifest.validate(bad_no_writeback)) or "download_from" in str(genmanifest.validate(bad_no_writeback)), str(genmanifest.validate(bad_no_writeback)))

    # download_from naming nonexistent slot
    bad_missing_slot = {
        **base_with_writeback,
        "aux_inputs": [{"name": "file.xlsx", "label": "X", "download_from": "does_not_exist.xlsx"}]
    }
    check("reject: download_from nonexistent slot", "aux_inputs" in str(genmanifest.validate(bad_missing_slot)) or "download_from" in str(genmanifest.validate(bad_missing_slot)), str(genmanifest.validate(bad_missing_slot)))

    # Duplicate names
    bad_duplicate = {
        **base_with_writeback,
        "aux_inputs": [
            {"name": "same.xlsx", "label": "First"},
            {"name": "same.xlsx", "label": "Second"},
        ]
    }
    check("reject: duplicate aux_inputs names", "aux_inputs" in str(genmanifest.validate(bad_duplicate)) or "duplicate" in str(genmanifest.validate(bad_duplicate)).lower(), str(genmanifest.validate(bad_duplicate)))

    # --- required_files validation (direct genmanifest tests) ----------------
    # Valid required_files
    valid_req_files = {
        **base_with_writeback,
        "required_files": ["SL_NWD.xls", "rates.xlsx"]
    }
    check("validate() with valid required_files -> []", genmanifest.validate(valid_req_files) == [], str(genmanifest.validate(valid_req_files)))

    # normalize() defaults required_files to []
    normalized_no_req = genmanifest.normalize(manifest_no_aux)
    check("normalize() defaults required_files to []", normalized_no_req.get("required_files") == [], str(normalized_no_req.get("required_files")))

    # required_files not a list
    bad_req_not_list = {**base_with_writeback, "required_files": "not_a_list.xlsx"}
    check("reject: required_files not a list", "required_files" in str(genmanifest.validate(bad_req_not_list)), str(genmanifest.validate(bad_req_not_list)))

    # 9 entries (more than 8 allowed)
    bad_req_9_entries = {
        **base_with_writeback,
        "required_files": ["a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx", "e.xlsx", "f.xlsx", "g.xlsx", "h.xlsx", "i.xlsx"]
    }
    check("reject: 9 required_files entries (max 8)", "required_files" in str(genmanifest.validate(bad_req_9_entries)), str(genmanifest.validate(bad_req_9_entries)))

    # Entry with '..'
    bad_req_path_traversal = {**base_with_writeback, "required_files": ["a/../b.xlsx"]}
    check("reject: required_files entry with '..'", "required_files" in str(genmanifest.validate(bad_req_path_traversal)) or "name" in str(genmanifest.validate(bad_req_path_traversal)), str(genmanifest.validate(bad_req_path_traversal)))

    # Entry starting with '.'
    bad_req_dot_start = {**base_with_writeback, "required_files": [".hidden.xlsx"]}
    check("reject: required_files entry starting with '.'", "required_files" in str(genmanifest.validate(bad_req_dot_start)) or "name" in str(genmanifest.validate(bad_req_dot_start)), str(genmanifest.validate(bad_req_dot_start)))

    # Non-string entry
    bad_req_non_string = {**base_with_writeback, "required_files": [42]}
    check("reject: required_files non-string entry", "required_files" in str(genmanifest.validate(bad_req_non_string)), str(genmanifest.validate(bad_req_non_string)))

    # Duplicate entries
    bad_req_duplicate = {**base_with_writeback, "required_files": ["same.xlsx", "same.xlsx"]}
    check("reject: duplicate required_files entries", "required_files" in str(genmanifest.validate(bad_req_duplicate)) or "duplicate" in str(genmanifest.validate(bad_req_duplicate)).lower(), str(genmanifest.validate(bad_req_duplicate)))

    # Entry equal to aux_inputs name
    bad_req_conflicts_aux = {
        **base_with_writeback,
        "aux_inputs": [{"name": "sl_assigned_reasons.xlsx", "label": "Run with reasons", "download_from": "sl_unassigned_reasons.xlsx"}],
        "required_files": ["sl_assigned_reasons.xlsx"]
    }
    check("reject: required_files entry conflicts with aux_inputs name", "required_files" in str(genmanifest.validate(bad_req_conflicts_aux)) or "aux" in str(genmanifest.validate(bad_req_conflicts_aux)).lower(), str(genmanifest.validate(bad_req_conflicts_aux)))

    # --- inputs validation (direct genmanifest tests) ----------------------
    base_inputs = {
        "name": "test-inputs",
        "version": "1.0",
        "entrypoint": {"module": "test.main"},
        "outputs": {"html": True},
    }

    # Valid full example with multiple inputs, sheets, and columns
    valid_inputs = {
        **base_inputs,
        "inputs": [
            {"match": "Stock_*.xlsx", "role": "data", "required": False,
             "sheets": [{"name": "Summary", "columns": ["Date", "Volume", "Price"]},
                        {"name": "*", "columns": []}]},
            {"match": "rates.xlsx", "role": "reference", "required": True,
             "sheets": [{"name": "Rates", "columns": ["Currency", "Value"]}]},
        ]
    }
    check("validate() with valid inputs -> []", genmanifest.validate(valid_inputs) == [], str(genmanifest.validate(valid_inputs)))

    # normalize() keeps inputs as written and defaults inputs to []
    manifest_no_inputs = dict(base_inputs)
    normalized_no_inputs = genmanifest.normalize(manifest_no_inputs)
    check("normalize() defaults inputs to []", normalized_no_inputs.get("inputs") == [], str(normalized_no_inputs.get("inputs")))
    normalized_with_inputs = genmanifest.normalize(valid_inputs)
    check("normalize() keeps inputs as written", normalized_with_inputs.get("inputs") == valid_inputs.get("inputs"), "inputs were modified")

    # inputs not a list
    bad_inputs_not_list = {**base_inputs, "inputs": {"match": "*.xlsx"}}
    check("reject: inputs not a list", "inputs must be a list" in str(genmanifest.validate(bad_inputs_not_list)), str(genmanifest.validate(bad_inputs_not_list)))

    # inputs entry not a dict
    bad_inputs_not_dict = {**base_inputs, "inputs": [["match", "*.xlsx"]]}
    check("reject: inputs[i] not a dict", "inputs[0] must be an object" in str(genmanifest.validate(bad_inputs_not_dict)), str(genmanifest.validate(bad_inputs_not_dict)))

    # subfolders (walk N levels below each chosen folder): integer 0..4
    check("subfolders: 2 accepted", not [e for e in genmanifest.validate({**base_inputs, "subfolders": 2}) if "subfolders" in e])
    check("subfolders: 5 rejected", any("subfolders" in e for e in genmanifest.validate({**base_inputs, "subfolders": 5})))
    check("subfolders: true (bool) rejected", any("subfolders" in e for e in genmanifest.validate({**base_inputs, "subfolders": True})))

    # Unknown field in inputs[i]
    bad_inputs_unknown_field = {**base_inputs, "inputs": [{"match": "*.xlsx", "unknown_field": "bad"}]}
    check("reject: inputs[i] unknown field", "unknown field" in str(genmanifest.validate(bad_inputs_unknown_field)), str(genmanifest.validate(bad_inputs_unknown_field)))

    # Bad role
    bad_inputs_bad_role = {**base_inputs, "inputs": [{"match": "*.xlsx", "role": "invalid"}]}
    check("reject: inputs[i] bad role", "role must be one of" in str(genmanifest.validate(bad_inputs_bad_role)), str(genmanifest.validate(bad_inputs_bad_role)))

    # match with "/"
    bad_inputs_match_slash = {**base_inputs, "inputs": [{"match": "folder/file*.xlsx"}]}
    check("reject: inputs[i].match with '/'", "must not contain '/' or '\\\\'" in str(genmanifest.validate(bad_inputs_match_slash)), str(genmanifest.validate(bad_inputs_match_slash)))

    # Wildcard-only match
    bad_inputs_wildcard_only = {**base_inputs, "inputs": [{"match": "***"}]}
    check("reject: wildcard-only match", "only wildcards" in str(genmanifest.validate(bad_inputs_wildcard_only)), str(genmanifest.validate(bad_inputs_wildcard_only)))

    # Duplicate match (case-insensitive)
    bad_inputs_duplicate_match = {**base_inputs, "inputs": [
        {"match": "Report.xlsx"},
        {"match": "report.XLSX"}  # same after casefold
    ]}
    check("reject: duplicate match (case-insensitive)", "duplicate" in str(genmanifest.validate(bad_inputs_duplicate_match)).lower(), str(genmanifest.validate(bad_inputs_duplicate_match)))

    # >32 entries
    bad_inputs_too_many = {**base_inputs, "inputs": [{"match": f"f{i}.xlsx"} for i in range(33)]}
    check("reject: >32 inputs entries", "at most 32 entries" in str(genmanifest.validate(bad_inputs_too_many)), str(genmanifest.validate(bad_inputs_too_many)))

    # >16 sheets
    bad_inputs_too_many_sheets = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": f"Sheet{i}"} for i in range(17)]}]
    }
    check("reject: >16 sheets", "at most 16 entries" in str(genmanifest.validate(bad_inputs_too_many_sheets)), str(genmanifest.validate(bad_inputs_too_many_sheets)))

    # >256 columns
    bad_inputs_too_many_cols = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": "S", "columns": [f"Col{i}" for i in range(257)]}]}]
    }
    check("reject: >256 columns", "at most 256 entries" in str(genmanifest.validate(bad_inputs_too_many_cols)), str(genmanifest.validate(bad_inputs_too_many_cols)))

    # Duplicate column (casefold)
    bad_inputs_duplicate_col = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": "S", "columns": ["Name", "name"]}]}]
    }
    check("reject: duplicate column (casefold)", "duplicate" in str(genmanifest.validate(bad_inputs_duplicate_col)).lower(), str(genmanifest.validate(bad_inputs_duplicate_col)))

    # required_files name also admitted by a data glob -> accepted; the exact
    # name wins (prod 2026-08-27: refusing this forced `SL_??.xlsx`, which
    # silently dropped `SL_01_02.xlsx`).
    overlap = {
        **base_inputs,
        "required_files": ["SL_NWD.xlsx"],
        "inputs": [{"match": "SL_*.xlsx", "role": "data", "required": True}]
    }
    check("accept: required_files name covered by a data glob", genmanifest.validate(overlap) == [], str(genmanifest.validate(overlap)))
    order = genmanifest.inputs_for(overlap)
    check("inputs_for(): the exact required name comes FIRST, before the glob",
          [e["match"] for e in order] == ["SL_NWD.xlsx", "SL_*.xlsx"] and order[0]["role"] == "reference"
          and order[0]["from_required_files"] is True, order)
    check("inputs_for(): a required name spelled as an explicit entry is not duplicated",
          [e["match"] for e in genmanifest.inputs_for({**overlap, "inputs": [
              {"match": "SL_*.xlsx", "role": "data"}, {"match": "sl_nwd.xlsx", "role": "reference", "required": True}]})]
          == ["sl_nwd.xlsx", "SL_*.xlsx"])

    # required_files sugar: appears in inputs_for() as {role:'reference', required:true, from_required_files:true}
    manifest_with_required_files = {
        **base_inputs,
        "required_files": ["helper.xlsx"],
        "inputs": [{"match": "data_*.xlsx", "role": "data"}]
    }
    inputs_list = genmanifest.inputs_for(manifest_with_required_files)
    check("inputs_for() includes required_files sugar",
          any(e.get("match") == "helper.xlsx" and e.get("role") == "reference" and
              e.get("required") is True and e.get("from_required_files") is True
              for e in inputs_list),
          f"sugar not found in {inputs_list}")

    # --- columns as objects with descriptions (P3.6) ----------------------
    inputs_col_objects = {
        **base_inputs,
        "inputs": [
            {"match": "Stock_*.xlsx", "role": "data",
             "sheets": [{"name": "Summary", "columns": [
                 {"name": "Date"},
                 {"name": "Volume", "description": "Units sold, not revenue."},
                 "Price",  # bare string still allowed alongside object form
             ]}]},
        ],
    }
    check("validate() accepts column object form -> []",
          genmanifest.validate(inputs_col_objects) == [], str(genmanifest.validate(inputs_col_objects)))
    check("inputs_for() normalises columns back to list[str]",
          genmanifest.inputs_for(inputs_col_objects)[0]["sheets"][0]["columns"]
          == ["Date", "Volume", "Price"],
          str(genmanifest.inputs_for(inputs_col_objects)))
    check("column_descriptions() exposes only the object-form descriptions",
          genmanifest.column_descriptions(inputs_col_objects)
          == {"Stock_*.xlsx/Summary": {"Volume": "Units sold, not revenue."}},
          str(genmanifest.column_descriptions(inputs_col_objects)))
    check("normalize() keeps the object column form as authored",
          genmanifest.normalize(inputs_col_objects)["inputs"][0]["sheets"][0]["columns"][1]
          == {"name": "Volume", "description": "Units sold, not revenue."})
    # duplicate name across the two forms (object vs bare string) still rejected
    dup_col_form = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": "S", "columns": [
            {"name": "Name"}, "name",
        ]}]}],
    }
    check("reject: duplicate column across object/string forms (casefold)",
          "duplicate" in str(genmanifest.validate(dup_col_form)).lower(),
          str(genmanifest.validate(dup_col_form)))
    # description over the 500-char cap
    long_desc_form = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": "S", "columns": [
            {"name": "A", "description": "x" * 501},
        ]}]}],
    }
    check("reject: column description > 500 chars",
          "description must be a string of at most 500" in str(genmanifest.validate(long_desc_form)),
          str(genmanifest.validate(long_desc_form)))
    # unknown key inside the column object
    bad_col_object_field = {
        **base_inputs,
        "inputs": [{"match": "*.xlsx", "sheets": [{"name": "S", "columns": [
            {"name": "A", "note": "not a real field"},
        ]}]}],
    }
    check("reject: column object unknown field",
          "unknown field" in str(genmanifest.validate(bad_col_object_field)),
          str(genmanifest.validate(bad_col_object_field)))
    # a check can still declare against a column named via the object form
    checks_against_object_col = {
        **base_inputs, "name": "test-checks-obj",
        "inputs": [{"match": "Stock_*.xlsx", "role": "data",
                    "sheets": [{"name": "Summary", "columns": [{"name": "Volume", "description": "d"}]}]}],
        "checks": [{"match": "Stock_*.xlsx", "sheet": "Summary", "column": "Volume", "type": "not_null"}],
    }
    check("validate() accepts a check against an object-form column -> []",
          genmanifest.validate(checks_against_object_col) == [],
          str(genmanifest.validate(checks_against_object_col)))

    # --- checks (P0.1: manifest contract + checks_for + endpoint) --------
    chk_inputs = [
        {"match": "Sales_*.xlsx", "role": "data",
         "sheets": [{"name": "Data", "columns": ["Branch", "Amount", "Code"]}]},
    ]
    def with_checks(*checks, inputs=chk_inputs):
        m = {**base_inputs, "name": "test-checks", "checks": list(checks)}
        if inputs is not None:
            m["inputs"] = inputs
        return m
    def err(m):
        return str(genmanifest.validate(m))
    ok_checks = with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Branch", "type": "known_values"},
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Amount", "type": "range", "min": 0, "max": 1e9},
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Amount", "type": "not_null", "label": "amount present"},
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "regex", "pattern": r"^\\d{4}$"},
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "enum", "values": ["0001", "0002"]},
        {"match": "Sales_*.xlsx", "column": "Code", "type": "unique"},
    )
    # the last check has no sheet -> "*", which overlaps the declared "Data"
    # (the check covers the declared sheet), so it is valid; a sheet glob
    # that overlaps nothing declared is not.
    check("reject: check sheet overlaps no declared sheet",
          "not declared in inputs" in err(with_checks(
              {"match": "Sales_*.xlsx", "sheet": "Other*", "column": "Code", "type": "unique"})))
    check("validate() with valid checks -> []", genmanifest.validate(ok_checks) == [], err(ok_checks))
    check("reject: checks without inputs", "checks require inputs" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"}, inputs=None)))
    check("reject: checks not a list", "checks must be a list" in err({**base_inputs, "checks": {}}))
    check("reject: >64 checks", "at most 64" in err(with_checks(
        *[{"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"}] * 65)))
    check("reject: check match not in inputs", "must equal one of inputs[].match" in err(with_checks(
        {"match": "Other_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"})))
    check("reject: check column not declared", "not declared in inputs" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Missing", "type": "not_null"})))
    # A colon in sheet/column would make observations._parse_check_id
    # mis-split "{type}:{match}:{sheet}:{column}" from the right (only
    # `match` may legitimately contain one) — reject it at validation time
    # instead of letting it corrupt the stored finding + baseline downstream.
    check("reject: check column contains ':'", "column must not contain ':'" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Total: USD", "type": "not_null"})))
    check("reject: check sheet contains ':'", "sheet must not contain ':'" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data: 2026", "column": "Code", "type": "not_null"})))
    check("reject: check unknown type", "type must be one of" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "positive"})))
    check("reject: check unknown key", "unknown field" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null", "severity": 1})))
    check("reject: range without min/max", "range needs min and/or max" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Amount", "type": "range"})))
    check("reject: range min > max", "min must be <= max" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Amount", "type": "range", "min": 5, "max": 1})))
    check("reject: range min non-numeric", "min must be a number" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Amount", "type": "range", "min": "0"})))
    check("reject: regex does not compile", "does not compile" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "regex", "pattern": "("})))
    check("reject: regex > 200 chars", "at most 200" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "regex", "pattern": "a" * 201})))
    check("reject: enum empty values", "values must be a non-empty list" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "enum", "values": []})))
    check("reject: enum > 500 values", "at most 500" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "enum", "values": ["x"] * 501})))
    check("reject: enum value > 64 chars", "at most 64 chars" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "enum", "values": ["x" * 65]})))
    check("reject: known_values with values", "values are only valid for type enum" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "known_values", "values": ["a"]})))
    check("reject: on_fail block (reserved)", 'on_fail "block" is not supported in this version' in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null", "on_fail": "block"})))
    check("reject: on_fail garbage", 'on_fail must be "warn"' in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null", "on_fail": "ignore"})))
    check("reject: label > 64", "label must be a string of at most 64" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null", "label": "x" * 65})))
    check("reject: duplicate check (casefold key)", "duplicate check" in err(with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"},
        {"match": "Sales_*.xlsx", "sheet": " data ", "column": "code", "type": "not_null"})))
    # match is exact: the engine filters checks by inputs[].match spelling
    check("reject: match differs by case (engine compares exactly)",
          "match must equal one of inputs[].match" in err(with_checks(
              {"match": "sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"})))
    check("accept: default sheet '*' against a named declared sheet",
          genmanifest.validate(with_checks({"match": "Sales_*.xlsx", "column": "Code", "type": "unique"})) == [])
    check("checks_for(): sheet stripped in id",
          genmanifest.checks_for({"checks": [{"match": "m", "column": "c", "type": "not_null", "sheet": " Data "}]})[0]["id"]
          == "not_null:m:Data:c")
    # checks_for shape + normalize
    cf = genmanifest.checks_for(ok_checks)
    check("checks_for(): ids + all keys present",
          len(cf) == 6 and cf[0]["id"] == "known_values:Sales_*.xlsx:Data:Branch"
          and cf[0]["values"] is None and cf[0]["on_fail"] == "warn" and cf[0]["label"] is None
          and cf[1]["min"] == 0 and cf[3]["pattern"] == r"^\\d{4}$" and cf[4]["values"] == ["0001", "0002"]
          and set(cf[0]) == {"id", "type", "match", "sheet", "column", "min", "max", "pattern", "values", "on_fail", "label", "table"}
          and cf[0]["table"] is None,
          str(cf))
    check("normalize() sets checks=[] when absent", genmanifest.normalize(base_inputs).get("checks") == [])
    # end-to-end: 400 on bad checks (validate() errors are 400 here, 422 is dep resolution), 201 + endpoint on good ones
    r = post_bundle(c, "/api/admin/generators", make_bundle(manifest_override=with_checks(
        {"match": "Sales_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null", "on_fail": "block"})))
    check("register: on_fail block -> 400 (validate() error)", r.status_code == 400 and "block" in r.text, f"{r.status_code} {r.text[:200]}")
    r = post_bundle(c, "/api/admin/generators", make_bundle(manifest_override=with_checks(
        {"match": "Nope_*.xlsx", "sheet": "Data", "column": "Code", "type": "not_null"})))
    check("register: foreign match -> 400 (validate() error)", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = post_bundle(c, "/api/admin/generators", make_bundle(manifest_override=ok_checks))
    check("register: valid checks -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        r = c.post("/api/dashboards/generated", json={"title": "Checks", "generator_id": r.json()["id"]})
        r = c.get(f"/api/dashboards/{r.json()['id']}/generator/manifest")
        got = r.json().get("checks") if r.status_code == 200 else None
        check("manifest endpoint returns normalized checks", got == cf, f"{r.status_code} {str(got)[:300]}")

    # --- platform reference inputs + ref_lookup (ADR-16) ------------------
    # A platform input is the server's own workbook: fixed name, fixed layout
    # (one sheet "data", key column first), every column typed. It also flips
    # the manifest into allow-list mode, so it may not stand alone.
    plat_data = {"match": "SL_*.xlsx", "role": "data",
                 "sheets": [{"name": "Orders", "columns": [{"name": "BV", "type": "text"}]}]}
    plat_input = {"match": "nwd_bv_map.xlsx", "role": "reference", "source": "platform",
                  "table": "nwd_bv_map",
                  "sheets": [{"name": "data", "columns": [
                      {"name": "BV", "type": "text"},
                      {"name": "Code", "type": "text"},
                      {"name": "Name", "type": "text"}]}]}
    def with_platform(*, data=plat_data, plat=plat_input, checks=None, **extra):
        m = {**base_inputs, "name": "test-ref", "inputs": [x for x in (data, plat) if x]}
        if checks is not None:
            m["checks"] = list(checks)
        m.update(extra)
        return m
    ok_plat = with_platform()
    check("validate() accepts a platform input -> []",
          genmanifest.validate(ok_plat) == [], err(ok_plat))
    check("inputs_for(): source/table on the platform entry, folder/None on the other",
          [(e["source"], e["table"]) for e in genmanifest.inputs_for(ok_plat)]
          == [("folder", None), ("platform", "nwd_bv_map")],
          str(genmanifest.inputs_for(ok_plat)))
    check("inputs_for(): columns stay a flat list[str]",
          genmanifest.inputs_for(ok_plat)[1]["sheets"][0]["columns"] == ["BV", "Code", "Name"],
          str(genmanifest.inputs_for(ok_plat)[1]))
    check("inputs_for(): required_files sugar carries source folder/table None",
          genmanifest.inputs_for({**base_inputs, "required_files": ["h.xlsx"],
                                  "inputs": [{"match": "d_*.xlsx"}]})[-1]["source"] == "folder")
    check("platform_inputs(): normalised entry with key_column = first column",
          genmanifest.platform_inputs(ok_plat) == [{
              "match": "nwd_bv_map.xlsx", "table": "nwd_bv_map", "key_column": "BV",
              "columns": [{"name": "BV", "type": "text"}, {"name": "Code", "type": "text"},
                          {"name": "Name", "type": "text"}]}],
          str(genmanifest.platform_inputs(ok_plat)))
    check("platform_inputs(): a folder-only manifest yields []",
          genmanifest.platform_inputs(base_inputs) == [])
    check("column_types(): keyed (match, sheet, column)",
          genmanifest.column_types(ok_plat)[("nwd_bv_map.xlsx", "data", "Code")] == "text"
          and genmanifest.column_types(ok_plat)[("SL_*.xlsx", "Orders", "BV")] == "text",
          str(genmanifest.column_types(ok_plat)))
    check("column_types(): a bare-string column contributes nothing",
          genmanifest.column_types({"inputs": [{"match": "a.xlsx", "sheets": [
              {"name": "S", "columns": ["A", {"name": "B"}]}]}]}) == {})
    check("normalize() keeps source/table as authored",
          genmanifest.normalize(ok_plat)["inputs"][1]["source"] == "platform"
          and genmanifest.normalize(ok_plat)["inputs"][1]["table"] == "nwd_bv_map")

    check("reject: platform match is not <table>.xlsx",
          "must equal 'nwd_bv_map.xlsx'" in err(with_platform(
              plat={**plat_input, "match": "bv.xlsx"})),
          err(with_platform(plat={**plat_input, "match": "bv.xlsx"})))
    check("reject: platform match is a glob, not the exact name",
          "must equal 'nwd_bv_map.xlsx'" in err(with_platform(
              plat={**plat_input, "match": "nwd_bv_map*.xlsx"})))
    check("reject: platform match differs by case",
          "must equal 'nwd_bv_map.xlsx'" in err(with_platform(
              plat={**plat_input, "match": "NWD_BV_MAP.xlsx"})))
    check("reject: platform table not a slug",
          "table must match" in err(with_platform(
              plat={**plat_input, "table": "NWD BV", "match": "NWD BV.xlsx"})))
    check("reject: platform role is not reference",
          'role must be "reference"' in err(with_platform(
              plat={**plat_input, "role": "data"})))
    check("reject: platform column without a type",
          "must be an object with a type" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data", "columns": [
                  {"name": "BV", "type": "text"}, {"name": "Code"}]}]})),
          err(with_platform(plat={**plat_input, "sheets": [{"name": "data", "columns": [
              {"name": "BV", "type": "text"}, {"name": "Code"}]}]})))
    check("reject: platform column as a bare string",
          "must be an object with a type" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data",
                                              "columns": ["BV", "Code"]}]})))
    check("reject: unknown column type",
          "type must be one of text, number, date" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data", "columns": [
                  {"name": "BV", "type": "text"}, {"name": "Code", "type": "money"}]}]})))
    check("reject: platform sheet not named data",
          'name must be "data"' in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "Sheet1", "columns": [
                  {"name": "BV", "type": "text"}, {"name": "Code", "type": "text"}]}]})))
    check("reject: platform declares two sheets",
          "exactly one sheet" in err(with_platform(
              plat={**plat_input, "sheets": plat_input["sheets"] + [
                  {"name": "extra", "columns": [{"name": "A", "type": "text"}]}]})))
    check("reject: platform declares one column (no value to map to)",
          "at least 2 columns" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data", "columns": [
                  {"name": "BV", "type": "text"}]}]})))
    check("reject: platform input also in required_files",
          "may not also be listed in required_files" in err(with_platform(
              required_files=["nwd_bv_map.xlsx"])),
          err(with_platform(required_files=["nwd_bv_map.xlsx"])))
    check("reject: table on a folder input",
          'table is only valid for source "platform"' in err(with_platform(
              data={**plat_data, "table": "nwd_bv_map"})))
    check("reject: unknown source",
          "source must be one of folder, platform" in err(with_platform(
              data={**plat_data, "source": "greenplum"})))
    # Risk 4: a platform input alone would flip the engine's allow-list on and
    # starve a legacy generator of every real data file.
    check("reject: platform input without any folder role=data input",
          "a platform input requires declared inputs with role=data" in err(
              with_platform(data=None)),
          err(with_platform(data=None)))
    check("reject: platform input when the only folder input is role=reference",
          "a platform input requires declared inputs with role=data" in err(
              with_platform(data={"match": "helper.xlsx", "role": "reference"})))

    check("reject: platform key column typed number (leading zeros would rot)",
          "key column and must have type" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data", "columns": [
                  {"name": "BV", "type": "number"}, {"name": "Code", "type": "text"}]}]})),
          err(with_platform(plat={**plat_input, "sheets": [{"name": "data", "columns": [
              {"name": "BV", "type": "number"}, {"name": "Code", "type": "text"}]}]})))
    check("reject: platform declares more than 32 columns",
          "at most 32" in err(with_platform(
              plat={**plat_input, "sheets": [{"name": "data", "columns": [
                  {"name": f"c{i}", "type": "text"} for i in range(33)]}]})))
    # the two numbers above are mirrors of app/services/reference.py
    try:
        from app.services import reference as _ref
        check("platform schema ceilings mirror app/services/reference.py",
              genmanifest._MAX_PLATFORM_COLUMNS == _ref.MAX_COLUMNS
              and genmanifest.COLUMN_TYPES == _ref.COLUMN_TYPES
              and genmanifest._NAME_RE.pattern == _ref.SLUG_RE.pattern)
    except ImportError:
        pass

    ref_chk = {"match": "SL_*.xlsx", "sheet": "Orders", "column": "BV",
               "type": "ref_lookup", "table": "nwd_bv_map"}
    ok_ref = with_platform(checks=[ref_chk])
    check("validate() accepts ref_lookup against a declared platform table -> []",
          genmanifest.validate(ok_ref) == [], err(ok_ref))
    rcf = genmanifest.checks_for(ok_ref)
    check("checks_for(): ref_lookup id has no table in it, table carried alongside",
          len(rcf) == 1 and rcf[0]["id"] == "ref_lookup:SL_*.xlsx:Orders:BV"
          and rcf[0]["table"] == "nwd_bv_map" and rcf[0]["values"] is None,
          str(rcf))
    check("reject: ref_lookup without table",
          "table must match" in err(with_platform(
              checks=[{k: v for k, v in ref_chk.items() if k != "table"}])))
    check("reject: ref_lookup naming a table no platform input declares",
          "has no platform input in this manifest" in err(with_platform(
              checks=[{**ref_chk, "table": "other_map"}])),
          err(with_platform(checks=[{**ref_chk, "table": "other_map"}])))
    check("reject: ref_lookup against an undeclared column",
          "not declared in inputs" in err(with_platform(
              checks=[{**ref_chk, "column": "Missing"}])))
    check("reject: table on a check that is not ref_lookup",
          "table is only valid for type ref_lookup" in err(with_platform(
              checks=[{"match": "SL_*.xlsx", "sheet": "Orders", "column": "BV",
                       "type": "not_null", "table": "nwd_bv_map"}])))
    check("reject: duplicate ref_lookup on the same match/sheet/column",
          "duplicate check" in err(with_platform(checks=[ref_chk, dict(ref_chk)])))
    check("accept: ref_lookup and not_null coexist on one column",
          genmanifest.validate(with_platform(checks=[
              ref_chk, {"match": "SL_*.xlsx", "sheet": "Orders", "column": "BV",
                        "type": "not_null"}])) == [])
    # end-to-end registration
    r = post_bundle(c, "/api/admin/generators", make_bundle(manifest_override=ok_ref))
    check("register: platform input + ref_lookup -> 201", r.status_code == 201,
          f"{r.status_code} {r.text[:300]}")
    r2 = post_bundle(c, "/api/admin/generators", make_bundle(manifest_override=with_platform(
        data=None, checks=[ref_chk])))
    check("register: platform input without data inputs -> 400", r2.status_code == 400
          and "role=data" in r2.text, f"{r2.status_code} {r2.text[:300]}")

    # required_files sugar is NOT duplicated when an explicit reference pattern already covers it
    manifest_with_explicit_ref = {
        **base_inputs,
        "required_files": ["helper.xlsx"],
        "inputs": [
            {"match": "data_*.xlsx", "role": "data"},
            {"match": "helper.xlsx", "role": "reference"}  # explicit entry covers the required file
        ]
    }
    inputs_list = genmanifest.inputs_for(manifest_with_explicit_ref)
    helper_count = sum(1 for e in inputs_list if e.get("match") == "helper.xlsx")
    check("inputs_for() does NOT duplicate explicit reference pattern over required_files sugar",
          helper_count == 1,  # only the explicit entry, not sugar
          f"expected 1 entry for helper.xlsx, got {helper_count}: {inputs_list}")

    # --- sources (GreenPlum agent executor) validation --------------------
    base_sources = {
        "name": "gp-test",
        "version": "1.0",
        "entrypoint": {"module": "passthrough.main"},
        "outputs": {"html": True, "slots": [{"index": 0, "name": "slot_0.csv.gz"}]},
        "engine": "agent",
    }
    valid_sources = {
        "type": "greenplum",
        "connection": "analytics_dw",
        "parallelism": 3,
        "statement_timeout_ms": 300000,
        "freeze_cycle": True,
        "queries": [
            {"sql_file": "sql/01_head.sql", "output_name": "slot_0.csv.gz", "format": "csv_gz"},
            {"sql_file": "sql/04_rejection.sql", "output_name": "slot_2.csv.gz",
             "format": "csv_gz", "limit_declared": 200000},
        ],
    }
    ok_src = {**base_sources, "sources": valid_sources}
    check("validate() with valid sources -> []", genmanifest.validate(ok_src) == [], str(genmanifest.validate(ok_src)))

    # normalize fills the executor defaults and preserves queries
    minimal_src = {**base_sources, "sources": {
        "type": "greenplum", "connection": "analytics_dw",
        "queries": [{"sql_file": "sql/a.sql", "output_name": "o.csv.gz", "format": "csv_gz"}]}}
    norm_src = genmanifest.normalize(minimal_src).get("sources", {})
    check("normalize() defaults sources.parallelism=3", norm_src.get("parallelism") == 3, str(norm_src))
    check("normalize() defaults sources.statement_timeout_ms", norm_src.get("statement_timeout_ms") == 300000, str(norm_src))
    check("normalize() defaults sources.freeze_cycle=False", norm_src.get("freeze_cycle") is False, str(norm_src))
    check("normalize() without sources leaves it absent", "sources" not in genmanifest.normalize(dict(base_sources)), "unexpected sources key")

    # cross-engine sanity: browser can't reach a DB
    browser_src = genmanifest.normalize({**base_sources, "engine": "browser", "sources": valid_sources})
    check("browser_blockers: browser + sources -> blocked",
          any("sources require the agent engine" in b for b in genmanifest.browser_blockers(browser_src)),
          str(genmanifest.browser_blockers(browser_src)))
    auto_src = genmanifest.normalize({**base_sources, "engine": "auto", "sources": valid_sources})
    check("browser_blockers: auto + sources -> no blocker (resolved at runtime)",
          genmanifest.browser_blockers(auto_src) == [], str(genmanifest.browser_blockers(auto_src)))

    # data_scale is advice, not a ceiling (2026-08-20): a browser manifest may
    # declare any scale; the engine warns the analyst instead of rejecting.
    big = genmanifest.normalize({**base_sources, "engine": "browser", "data_scale": {
        "max_files": genmanifest.REC_MAX_FILES * 20,
        "max_total_bytes": genmanifest.REC_MAX_TOTAL_BYTES * 20,
        "max_file_bytes": genmanifest.REC_MAX_FILE_BYTES * 20}})
    check("validate(): data_scale above the recommendation -> []",
          genmanifest.validate(big) == [], str(genmanifest.validate(big)))
    check("browser_blockers: data_scale above the recommendation -> no blocker",
          genmanifest.browser_blockers(big) == [], str(genmanifest.browser_blockers(big)))
    check("normalize(): omitted data_scale backfills the recommendation",
          genmanifest.normalize(dict(base_sources))["data_scale"] == {
              "max_files": genmanifest.REC_MAX_FILES,
              "max_total_bytes": genmanifest.REC_MAX_TOTAL_BYTES,
              "max_file_bytes": genmanifest.REC_MAX_FILE_BYTES},
          str(genmanifest.normalize(dict(base_sources)).get("data_scale")))

    # ── publish-time diagnostics (never reject; the author is the only fixer) ──
    pw_manifest = {"outputs": {"slots": [{"index": 0, "name": "data.bin.gz"},
                                         {"index": 1, "name": "detail.bin.gz"}]}}
    ok_pub = {0: ("data.bin.gz", 100), 1: ("detail.bin.gz", 100)}

    check("publish_warnings: everything matching -> []",
          genmanifest.publish_warnings(pw_manifest, ok_pub, preview_max=1_500_000,
                                       upload_max=50 * 1024 * 1024,
                                       render_size=200_000) == [],
          str(genmanifest.publish_warnings(pw_manifest, ok_pub, preview_max=1_500_000,
                                           upload_max=50 * 1024 * 1024, render_size=200_000)))

    heavy = genmanifest.publish_warnings(pw_manifest, ok_pub, preview_max=1_500_000,
                                         render_size=1_680_000)
    check("publish_warnings: html over the preview cutoff -> warned",
          len(heavy) == 1 and "preview" in heavy[0] and "1.7" in heavy[0], str(heavy))
    check("publish_warnings: cutoff disabled (0) -> silent",
          genmanifest.publish_warnings(pw_manifest, ok_pub, preview_max=0,
                                       render_size=99_000_000) == [], "expected no warning")

    undeclared = genmanifest.publish_warnings(pw_manifest, {**ok_pub, 7: ("stray.csv", 10)})
    check("publish_warnings: slot outside outputs.slots -> warned",
          any("slot_7" in w for w in undeclared), str(undeclared))

    renamed = genmanifest.publish_warnings(pw_manifest, {0: ("data.csv", 10),
                                                         1: ("detail.bin.gz", 10)})
    check("publish_warnings: slot filename != manifest -> warned",
          any("data.csv" in w and "data.bin.gz" in w for w in renamed), str(renamed))

    missing_first = genmanifest.publish_warnings(pw_manifest, {0: ("data.bin.gz", 10)},
                                                 first_publish=True)
    check("publish_warnings: declared slot missing on FIRST publish -> warned",
          any("slot_1" in w for w in missing_first), str(missing_first))
    check("publish_warnings: same gap on a later publish stays silent "
          "(slots are carried over from the previous version)",
          genmanifest.publish_warnings(pw_manifest, {0: ("data.bin.gz", 10)}) == [],
          str(genmanifest.publish_warnings(pw_manifest, {0: ("data.bin.gz", 10)})))

    near_cap = genmanifest.publish_warnings(pw_manifest,
                                            {0: ("data.bin.gz", 45 * 1024 * 1024),
                                             1: ("detail.bin.gz", 10)},
                                            upload_max=50 * 1024 * 1024)
    check("publish_warnings: slot past 80% of the publish cap -> warned",
          any("90%" in w for w in near_cap), str(near_cap))

    check("publish_warnings: manifest declaring no slots has no opinion",
          genmanifest.publish_warnings({}, {3: ("whatever.bin", 10)}) == [],
          str(genmanifest.publish_warnings({}, {3: ("whatever.bin", 10)})))
    check("publish_warnings: malformed slot entries do not raise",
          genmanifest.publish_warnings({"outputs": {"slots": [None, {"index": "x"}, {}]}},
                                       ok_pub) == [], "expected tolerance")

    def _rej_src(label, override, needle="sources"):
        m = {**base_sources, "sources": {**valid_sources, **override}}
        errs = str(genmanifest.validate(m))
        check(label, needle in errs, errs)

    check("reject: sources not an object",
          "sources must be an object" in str(genmanifest.validate({**base_sources, "sources": []})),
          str(genmanifest.validate({**base_sources, "sources": []})))
    _rej_src("reject: sources unknown field", {"bogus": 1}, "unknown sources field")
    _rej_src("reject: sources.type missing", {"type": None})
    _rej_src("reject: sources.type unknown", {"type": "oracle"})
    _rej_src("reject: sources.connection missing", {"connection": None})
    _rej_src("reject: sources.connection bad charset", {"connection": "Bad Name!"})
    _rej_src("reject: sources.parallelism=0", {"parallelism": 0})
    _rej_src("reject: sources.parallelism=5 (>4)", {"parallelism": 5})
    _rej_src("reject: sources.statement_timeout_ms=0", {"statement_timeout_ms": 0})
    _rej_src("reject: sources.freeze_cycle non-bool", {"freeze_cycle": "yes"})
    _rej_src("reject: sources.queries missing", {"queries": None})
    _rej_src("reject: sources.queries empty", {"queries": []})
    _rej_src("reject: sources.queries too many (>16)",
             {"queries": [{"sql_file": f"sql/q{i}.sql", "output_name": f"o{i}.csv.gz", "format": "csv_gz"} for i in range(17)]})
    _rej_src("reject: query sql_file traversal",
             {"queries": [{"sql_file": "sql/../x.sql", "output_name": "o.csv.gz", "format": "csv_gz"}]}, "sql_file")
    _rej_src("reject: query sql_file not .sql",
             {"queries": [{"sql_file": "sql/x.txt", "output_name": "o.csv.gz", "format": "csv_gz"}]}, "sql_file")
    _rej_src("reject: query sql_file backslash",
             {"queries": [{"sql_file": "sql\\x.sql", "output_name": "o.csv.gz", "format": "csv_gz"}]}, "sql_file")
    _rej_src("reject: duplicate sql_file",
             {"queries": [{"sql_file": "sql/x.sql", "output_name": "a.csv.gz", "format": "csv_gz"},
                          {"sql_file": "sql/x.sql", "output_name": "b.csv.gz", "format": "csv_gz"}]}, "duplicate")
    _rej_src("reject: query output_name traversal",
             {"queries": [{"sql_file": "sql/x.sql", "output_name": "../o.csv.gz", "format": "csv_gz"}]}, "output_name")
    _rej_src("reject: duplicate output_name",
             {"queries": [{"sql_file": "sql/a.sql", "output_name": "o.csv.gz", "format": "csv_gz"},
                          {"sql_file": "sql/b.sql", "output_name": "o.csv.gz", "format": "csv_gz"}]}, "duplicate")
    _rej_src("reject: query bad format",
             {"queries": [{"sql_file": "sql/x.sql", "output_name": "o.csv.gz", "format": "parquet"}]}, "format")
    _rej_src("reject: query unknown field",
             {"queries": [{"sql_file": "sql/x.sql", "output_name": "o.csv.gz", "format": "csv_gz", "bogus": 1}]}, "unknown field")
    _rej_src("reject: query limit_declared=0",
             {"queries": [{"sql_file": "sql/x.sql", "output_name": "o.csv.gz", "format": "csv_gz", "limit_declared": 0}]}, "limit_declared")

    # sources frozen verbatim in the immutable version (round-trip through registration)
    gp_manifest = {**base_sources, "name": "gp-delta", "sources": valid_sources}
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="gp-delta", engine="agent", manifest_override=gp_manifest))
    check("register bundle with sources (agent) -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    if r.status_code == 201:
        gp_gid = r.json().get("id")
        _db = SessionLocal()
        from app.models import GeneratorVersion
        ver = _db.query(GeneratorVersion).filter_by(generator_id=gp_gid, version_number=1).first()
        gp_mf = json.loads(ver.manifest_json) if ver else {}
        check("sources frozen in manifest_json",
              gp_mf.get("sources", {}).get("queries") == valid_sources["queries"]
              and gp_mf.get("sources", {}).get("connection") == "analytics_dw",
              f"got {gp_mf.get('sources')}")
        _db.close()

    # a browser-engine bundle carrying sources must be rejected at registration
    gp_browser = {**base_sources, "name": "gp-browser", "engine": "browser", "sources": valid_sources}
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="gp-browser", engine="browser", manifest_override=gp_browser))
    check("register browser bundle with sources -> 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")

    # --- new version, immutability --------------------------------------
    r = post_bundle(c, f"/api/admin/generators/{gid}/versions", make_bundle())
    check("new version -> v2", r.status_code == 200 and r.json().get("version_number") == 2,
          f"{r.status_code} {r.text[:200]}")
    r = post_bundle(c, f"/api/admin/generators/{gid}/versions", make_bundle(name="other-name"))
    check("bundle of another generator -> 400", r.status_code == 400, str(r.status_code))

    # --- version dir pruning: current + previous stay, older leave disk ---
    # A dedicated generator: the suite's later pins rely on sl-test keeping
    # v1 on disk at v2-current (pruning starts at the THIRD version).
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="prune-test"))
    check("prune-test registered", r.status_code == 201, str(r.status_code))
    p_gid = r.json().get("id")
    for want in (2, 3):
        r = post_bundle(c, f"/api/admin/generators/{p_gid}/versions", make_bundle(name="prune-test"))
        check(f"prune-test -> v{want}",
              r.status_code == 200 and r.json().get("version_number") == want,
              f"{r.status_code} {r.text[:200]}")
    from app.services import genstorage as _gs
    v_dirs = sorted(p.name for p in _gs._generator_dir(p_gid).iterdir()
                    if p.name.startswith("v"))
    check("pruning keeps only current + previous bundle dirs",
          v_dirs == ["v2", "v3"], v_dirs)
    _db = SessionLocal()
    from app.models import GeneratorVersion as _GV
    n_rows = _db.query(_GV).filter_by(generator_id=p_gid).count()
    _db.close()
    check("pruning never touches version ROWS (history intact)", n_rows == 3, n_rows)

    # --- aux_inputs registration -------------------------------------------
    aux_spec = [{"name": "sl_assigned_reasons.xlsx", "label": "Run with reasons", "download_from": "sl_bc_data.bin.gz"}]
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="aux-test", aux_inputs=aux_spec))
    check("register bundle with aux_inputs -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    aux_gen_id = r.json().get("id")

    # Verify aux_inputs frozen in manifest_json
    _db = SessionLocal()
    from app.models import GeneratorVersion
    ver = _db.query(GeneratorVersion).filter_by(generator_id=aux_gen_id, version_number=1).first()
    manifest_dict = json.loads(ver.manifest_json) if ver else {}
    check("aux_inputs frozen in manifest_json",
          manifest_dict.get("aux_inputs") == aux_spec,
          f"expected {aux_spec}, got {manifest_dict.get('aux_inputs')}")
    _db.close()

    # --- required_files registration -------------------------------------------
    req_files_spec = ["SL_NWD.xls", "rates.xlsx"]
    req_manifest = {
        **base_with_writeback,
        "name": "req-files-test",
        "required_files": req_files_spec
    }
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="req-files-test", manifest_override=req_manifest))
    check("register bundle with required_files -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    req_files_gen_id = r.json().get("id")

    # Verify required_files frozen in manifest_json
    _db = SessionLocal()
    from app.models import GeneratorVersion
    ver = _db.query(GeneratorVersion).filter_by(generator_id=req_files_gen_id, version_number=1).first()
    manifest_dict = json.loads(ver.manifest_json) if ver else {}
    check("required_files frozen in manifest_json",
          manifest_dict.get("required_files") == req_files_spec,
          f"expected {req_files_spec}, got {manifest_dict.get('required_files')}")
    _db.close()

    # --- git mode disabled ----------------------------------------------
    r = c.put(f"/api/admin/generators/{gid}/git",
              json={"repo": "https://example.com/x.git", "branch": "main", "token": "s3cr3t"})
    check("git register while disabled -> 503", r.status_code == 503, str(r.status_code))
    r = c.post(f"/api/admin/generators/{gid}/git/sync")
    check("git sync while disabled -> 503", r.status_code == 503, str(r.status_code))

    # --- admin + light lists ---------------------------------------------
    r = c.get("/api/admin/generators")
    body = r.text
    check("admin list ok, token never serialized", r.status_code == 200 and "s3cr3t" not in body, str(r.status_code))
    r = c.get("/api/generators")
    lst = r.json() if r.status_code == 200 else []
    check("light list has sl-test v2",
          any(g.get("name") == "sl-test" and g.get("version_number") == 2 for g in lst),
          f"{r.status_code} {r.text[:200]}")

    # --- create generated draft ------------------------------------------
    r = c.post("/api/dashboards/generated", json={"title": "SL Weekly", "generator_id": gid})
    check("create generated -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    dash_id = r.json().get("id")
    check("create returns dock deep-link", (r.json().get("url") or "").endswith("?dock=data"), r.text[:200])

    _db = SessionLocal()
    d = _db.query(Dashboard).get(dash_id)
    check("draft: is_published False, generator bound",
          d is not None and d.is_published is False and d.generator_id == gid,
          f"{d and (d.is_published, d.generator_id)}")
    _db.close()

    r = c.get(f"/render/{dash_id}/")
    check("CTA v1 renders for owner", r.status_code == 200 and b"not generated yet" in r.content.lower(), str(r.status_code))

    # --- manifest + bundle for the engine ---------------------------------
    r = c.get(f"/api/dashboards/{dash_id}/generator/manifest")
    man = r.json() if r.status_code == 200 else {}
    check("engine manifest ok", r.status_code == 200 and man.get("version_number") == 2, f"{r.status_code} {r.text[:200]}")
    r = c.get(man.get("bundle_url", f"/api/dashboards/{dash_id}/generator/bundle?v=2"))
    check("bundle downloads, immutable",
          r.status_code == 200 and "immutable" in r.headers.get("cache-control", ""),
          f"{r.status_code} {r.headers.get('cache-control')}")
    r = c.get(f"/api/dashboards/{dash_id}/generator/bundle?v=1")
    check("old version v1 still served (immutability)", r.status_code == 200, str(r.status_code))
    r = c.get(f"/api/dashboards/{dash_id}/generator/bundle?v=99")
    check("unknown version -> 404", r.status_code == 404, str(r.status_code))

    # --- run lifecycle -----------------------------------------------------
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "browser"})
    check("run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    run = r.json()
    run_id = run.get("run_id")

    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "browser"})
    lock = r.json().get("detail", {}) if r.status_code == 409 else {}
    check("second run -> 409 with lock info", r.status_code == 409 and lock.get("locked_by") == "admin1",
          f"{r.status_code} {r.text[:200]}")

    r = c.patch(f"/api/dashboards/{dash_id}/generator/runs/{run_id}")
    check("heartbeat ok", r.status_code == 200, str(r.status_code))

    # --- atomic publish -----------------------------------------------------
    html = "<html><head><title>SL</title></head><body>ready</body></html>"
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/publish",
               files={"html_file": ("dashboard.html", html.encode(), "text/html"),
                      "slot_0": ("sl_bc_data.bin.gz", b"\x1f\x8b" + os.urandom(64), "application/gzip")})
    check("publish -> ok, v2", r.status_code == 200 and r.json().get("version") == 2, f"{r.status_code} {r.text[:300]}")

    _db = SessionLocal()
    d = _db.query(Dashboard).get(dash_id)
    rrow = _db.query(GenerationRun).get(run_id)
    check("first publish lifts draft", d.is_published is True, str(d.is_published))
    check("run success recorded",
          rrow.status == "success" and rrow.output_version_number == 2 and (rrow.bytes_published or 0) > 0,
          f"{rrow.status} {rrow.output_version_number} {rrow.bytes_published}")
    _db.close()

    r = c.get(f"/render/{dash_id}/")
    check("published render serves new html", r.status_code == 200 and b"ready" in r.content, str(r.status_code))

    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{run_id}/publish",
               files={"html_file": ("dashboard.html", html.encode(), "text/html")})
    check("re-publish finished run -> 409", r.status_code == 409, str(r.status_code))

    # --- lock freed after success; TTL-lost discipline -----------------------
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "browser"})
    check("new run after success -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    stale_id = r.json().get("run_id")

    con = sqlite3.connect(f"{TMP}/onepage.db")
    con.execute("UPDATE generation_runs SET started_at='2026-07-20 00:00:00', heartbeat_at=NULL WHERE id=?", (stale_id,))
    con.commit()
    con.close()

    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "agent"})
    check("stale lock -> new run 201 (read-path TTL)", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    new_run = r.json().get("run_id")
    _db = SessionLocal()
    stale = _db.query(GenerationRun).get(stale_id)
    check("stale run declared lost", stale.status == "lost", stale.status)
    _db.close()

    # --- error flow + cancel + history ----------------------------------------
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs/{new_run}/error",
               json={"message": "boom " * 1000, "detail": "trace"})
    check("error flow ok", r.status_code == 200, str(r.status_code))
    _db = SessionLocal()
    er = _db.query(GenerationRun).get(new_run)
    check("error truncated <=2000", er.status == "error" and len(er.error_message) <= 2000,
          f"{er.status} len={len(er.error_message or '')}")
    _db.close()

    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "agent"})
    cancel_id = r.json().get("run_id")
    r = c.delete(f"/api/dashboards/{dash_id}/generator/runs/{cancel_id}")
    check("cancel ok", r.status_code == 200 and r.json().get("status") == "canceled", r.text[:200])

    r = c.get(f"/api/dashboards/{dash_id}/generator/runs")
    runs = r.json() if r.status_code == 200 else []
    check("history lists all runs", len(runs) >= 4, f"{r.status_code} n={len(runs)}")

    # --- K2: the third engine kind "local" (the agent's native folder run) ---
    # A folder run reads FILES under the R2 contract — the same as the browser,
    # only with real Python. So it is allowed wherever the browser one is
    # (manifest browser/auto) and refused exactly where there are no files: the
    # manifest declares agent or carries `sources` (GreenPlum). The refusal
    # must NAME the reason — the two problems are fixed differently.
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "nope"})
    check("unknown engine -> 400 naming all three kinds",
          r.status_code == 400 and "local" in r.text and "browser" in r.text and "agent" in r.text,
          f"{r.status_code} {r.text[:200]}")

    r = c.post("/api/dashboards/generated",
               json={"title": "Folders report", "generator_id": gid})
    loc_dash = r.json().get("id")
    check("create report for the folder run -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    folders_manifest = {
        "source": "folders", "scenario": "without-Irkutsk",
        "files": [{"name": "SL_01.xlsx", "size": 123, "mtime_ms": 1760000000000,
                   "dir": 0, "path": "D:\\Exports\\SL_01.xlsx"}],
        "dirs": ["Exports"],
        "roots": [{"dir": 0, "root_path": "D:\\Exports",
                   "root_id": "0123456789abcdef"}],
        "total_bytes": 123, "count": 1, "skipped": [],
    }
    r = c.post(f"/api/dashboards/{loc_dash}/generator/runs",
               json={"engine": "local", "input_manifest": folders_manifest})
    check("local run on an auto manifest without sources -> 201",
          r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    loc_run = r.json().get("run_id") if r.status_code == 201 else None

    _db = SessionLocal()
    lr = _db.query(GenerationRun).get(loc_run) if loc_run else None
    check("run row keeps engine 'local'", lr is not None and lr.engine == "local",
          lr.engine if lr else "no row")
    from app.models import DataSource as _DS, SourceFile as _SF
    ds_row = _db.query(_DS).filter_by(kind="folder", key_name="Exports").first()
    check("folder manifest reaches the DB: root_path + 16-hex root_id survive",
          ds_row is not None and ds_row.root_path == "D:\\Exports"
          and ds_row.root_id == "0123456789abcdef",
          f"{ds_row.root_path if ds_row else None} / {ds_row.root_id if ds_row else None}")
    sf_row = (_db.query(_SF).filter(_SF.name == "SL_01.xlsx")
              .order_by(_SF.id.desc()).first())
    check("files[].path reaches source_files",
          sf_row is not None and sf_row.path == "D:\\Exports\\SL_01.xlsx",
          sf_row.path if sf_row else "no row")
    _db.close()

    r = c.get(f"/api/dashboards/{loc_dash}/generator/runs")
    check("history reports engine 'local'",
          r.status_code == 200 and r.json() and r.json()[0].get("engine") == "local",
          r.text[:200])

    r = c.post(f"/api/dashboards/{loc_dash}/generator/runs/{loc_run}/publish",
               files={"html_file": ("dashboard.html", b"<html><body>folders</body></html>", "text/html")})
    check("local run publishes", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    _db = SessionLocal()
    from app.models import DashboardVersion as _DV
    note = (_db.query(_DV).filter_by(dashboard_id=loc_dash)
            .order_by(_DV.version_number.desc()).first().note or "")
    _db.close()
    check("version note names the scenario",
          f"{NOTE_SCENARIO} without-Irkutsk" in note and f"generated: run {loc_run}" in note, note)

    # Catch-up (mirror phase 7): the numbers were computed EARLIER and offline,
    # while the version's `started_at` is the upload time. The note must name
    # both dates, otherwise six months later the report looks computed on the
    # day it was published.
    r = c.post(f"/api/dashboards/{loc_dash}/generator/runs",
               json={"engine": "local",
                     "input_manifest": {**folders_manifest,
                                        "offline": {"created_at": "2026-10-11T19:04:00", "k": 1}}})
    off_run = r.json().get("run_id") if r.status_code == 201 else None
    check("catch-up run starts like any other local run", r.status_code == 201,
          f"{r.status_code} {r.text[:300]}")
    r = c.post(f"/api/dashboards/{loc_dash}/generator/runs/{off_run}/publish",
               files={"html_file": ("dashboard.html", b"<html><body>off</body></html>", "text/html")})
    check("catch-up run publishes", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    _db = SessionLocal()
    note = (_db.query(_DV).filter_by(dashboard_id=loc_dash)
            .order_by(_DV.version_number.desc()).first().note or "")
    _db.close()
    check("version note says the numbers were computed offline, and WHEN",
          f"{NOTE_OFFLINE} 2026-10-11" in note and f"generated: run {off_run}" in note, note)
    check("the offline mark does not eat the scenario", f"{NOTE_SCENARIO} without-Irkutsk" in note, note)

    # A mark without a date: say it was offline, but never invent "today".
    r = c.post(f"/api/dashboards/{loc_dash}/generator/runs",
               json={"engine": "local",
                     "input_manifest": {**folders_manifest, "offline": {"k": 2}}})
    bare_run = r.json().get("run_id") if r.status_code == 201 else None
    c.post(f"/api/dashboards/{loc_dash}/generator/runs/{bare_run}/publish",
           files={"html_file": ("dashboard.html", b"<html><body>off2</body></html>", "text/html")})
    _db = SessionLocal()
    note = (_db.query(_DV).filter_by(dashboard_id=loc_dash)
            .order_by(_DV.version_number.desc()).first().note or "")
    _db.close()
    check("an offline mark with no date says \"built offline\" and invents nothing",
          note.endswith(NOTE_OFFLINE), note)

    # The manifest declares agent — it has no files, the folder run is refused.
    r = c.post("/api/dashboards/generated",
               json={"title": "GP report", "generator_id": gp_gid})
    gp_dash = r.json().get("id")
    r = c.post(f"/api/dashboards/{gp_dash}/generator/runs", json={"engine": "local"})
    check("local on an agent-only manifest -> 400 naming the declared engine",
          r.status_code == 400 and "agent" in r.text, f"{r.status_code} {r.text[:200]}")

    # engine=auto + sources: the declaration allows anything, but there is
    # nothing to read except GreenPlum — the refusal must name `sources`, not
    # the engine.
    auto_src_manifest = {**base_sources, "name": "gp-auto", "engine": "auto",
                         "sources": valid_sources}
    r = post_bundle(c, "/api/admin/generators",
                    make_bundle(name="gp-auto", engine="auto", manifest_override=auto_src_manifest))
    check("register auto+sources bundle -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    auto_src_gid = r.json().get("id")
    r = c.post("/api/dashboards/generated",
               json={"title": "GP auto report", "generator_id": auto_src_gid})
    auto_src_dash = r.json().get("id")
    r = c.post(f"/api/dashboards/{auto_src_dash}/generator/runs", json={"engine": "local"})
    check("local on auto+sources -> 400 naming `sources`",
          r.status_code == 400 and "sources" in r.text, f"{r.status_code} {r.text[:200]}")

    # The manifest declares browser and has NO sources — exactly the generator
    # that can now run natively from folders.
    r = post_bundle(c, "/api/admin/generators", make_bundle(name="br-only", engine="browser"))
    br_gid = r.json().get("id")
    r = c.post("/api/dashboards/generated",
               json={"title": "Browser report", "generator_id": br_gid})
    br_dash = r.json().get("id")
    r = c.post(f"/api/dashboards/{br_dash}/generator/runs", json={"engine": "local"})
    check("local on a browser-declared manifest -> 201", r.status_code == 201,
          f"{r.status_code} {r.text[:300]}")

    # The resolved engine: "auto" alone is no answer, the presence of sources is.
    check("genmanifest.resolved_engine resolves auto by `sources`",
          genmanifest.resolved_engine({"engine": "auto"}) == "browser"
          and genmanifest.resolved_engine({"engine": "auto", "sources": {"queries": []}}) == "agent"
          and genmanifest.resolved_engine({"engine": "browser"}) == "browser"
          and genmanifest.resolved_engine({"engine": "agent"}) == "agent"
          and genmanifest.resolved_engine({}) == "browser")

    # --- aux_inputs lifecycle (run with aux_inputs enabled) -------------------
    # Create a generated dashboard from the aux-test generator
    r = c.post("/api/dashboards/generated", json={"title": "Report with aux", "generator_id": aux_gen_id})
    check("create generated with aux -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    aux_dash_id = r.json().get("id")

    # Start a run
    r = c.post(f"/api/dashboards/{aux_dash_id}/generator/runs", json={"engine": "browser"})
    check("aux run start -> 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    aux_run_id = r.json().get("run_id")

    # Upload matching aux file
    aux_bytes = b"test aux data content"
    r = c.post(f"/api/dashboards/{aux_dash_id}/generator/runs/{aux_run_id}/aux",
               files={"file": ("sl_assigned_reasons.xlsx", aux_bytes, "application/vnd.ms-excel")})
    check("upload matching aux file -> 200 with ok", r.status_code == 200 and r.json().get("ok") is True,
          f"{r.status_code} {r.text[:200]}")

    # Upload mismatched filename -> 400
    r = c.post(f"/api/dashboards/{aux_dash_id}/generator/runs/{aux_run_id}/aux",
               files={"file": ("wrong_filename.xlsx", aux_bytes, "application/vnd.ms-excel")})
    check("upload mismatched aux filename -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    # Download aux file
    r = c.get(f"/api/dashboards/{aux_dash_id}/generator/runs/{aux_run_id}/aux?name=sl_assigned_reasons.xlsx")
    check("download aux file -> 200 with attachment header",
          r.status_code == 200 and "attachment" in r.headers.get("content-disposition", "").lower(),
          f"{r.status_code} {r.headers.get('content-disposition')}")
    check("downloaded aux file content matches", r.content == aux_bytes,
          f"expected {len(aux_bytes)} bytes, got {len(r.content)}")

    # --- permission on runs: anna has no grant -> 403 ---------------------------
    login(c, "anna")
    r = c.post(f"/api/dashboards/{dash_id}/generator/runs", json={"engine": "browser"})
    check("no-grant user cannot start run (403)", r.status_code == 403, str(r.status_code))

    # =====================================================================
    # Stage 2 — UI pages (integration of all three agents together)
    # =====================================================================
    login(c, "admin1")

    r = c.get("/generators")
    page = r.text
    check("launcher page 200 + dashboard + generator + last-run status",
          r.status_code == 200 and "SL Weekly" in page and "sl-test" in page
          and ("error" in page or "canceled" in page),  # latest run for dash is canceled
          str(r.status_code))
    check("launcher has create dialog for admin", "New generated report" in page, "")

    r = c.get("/")
    check("index 200 + sidebar Generators link", r.status_code == 200 and 'href="/generators"' in r.text,
          str(r.status_code))

    r = c.get(f"/d/{dash_id}")
    page = r.text
    check("viewer dock: provenance + engine-root + Data tab",
          r.status_code == 200 and "sl-test v2" in page and "gen-engine-root" in page
          and 'data-tab="data"' in page,
          str(r.status_code))
    check("viewer has Data Quality link",
          "/data-quality?generator=" in page and "Data Quality" in page,
          str(r.status_code))

    r = c.get("/admin/generators")
    page = r.text
    check("admin generators page 200 + register UI + failed runs",
          r.status_code == 200 and "sl-test" in page and "Register generator" in page,
          str(r.status_code))
    check("admin page shows the errored run", "boom" in page, "")
    for p in ("/admin", "/admin/audit", "/admin/errors", "/admin/perf"):
        r = c.get(p)
        check(f"{p} 200 + Generators tab", r.status_code == 200 and 'href="/admin/generators"' in r.text,
              str(r.status_code))

    # non-admin: no admin page, launcher visible but without create button
    login(c, "anna")
    r = c.get("/admin/generators")
    check("superuser blocked from admin generators page", r.status_code == 403, str(r.status_code))
    r = c.get("/generators")
    check("anna sees launcher but not admin dash (no grant)",
          r.status_code == 200 and "SL Weekly" not in r.text, str(r.status_code))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
