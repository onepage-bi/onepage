"""genmanifest's `rls` block (ADR-18): structural validation + cross-check
against outputs.slots. Policy semantics belong to ONE validator —
`app.services.rls.validate_policy`, imported by genmanifest; the duplicate
"fallback" copy that used to live here was deleted because it drifted (it
accepted `key` without `via`, and `column: null` alongside `via`). Checks 4c
and 4d below pin exactly those divergences, so a second copy cannot come back
unnoticed. `normalize()` passing the block through unchanged is pinned too.
"""
import copy
import os
import sys

sys.path.insert(0, os.getcwd())
from app.services import genmanifest  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(label)


def base_manifest(rls=None):
    m = {
        "name": "ligatest",
        "version": "1.0",
        "entrypoint": {"module": "ligatest.build"},
        "outputs": {
            "html": True,
            "slots": [{"index": i, "name": f"slot{i}.bin.gz", "type": "data"}
                      for i in range(8)],
        },
    }
    if rls is not None:
        m["rls"] = rls
    return m


VALID_RLS = {
    "slots": [
        {"index": 0, "column": "tmr_position_name"},
        {"index": 1, "column": "tmr_territory_name"},
        {"index": 2, "key": "customer_number",
         "via": {"slot": 1, "key": "customer_number", "column": "tmr_territory_name"}},
        {"index": 3, "column": "tmr_territory_name"},
        {"index": 4, "column": "tmr_territory_name"},
        {"index": 5, "column": "tmr_position_name"},
        {"index": 6, "column": "territory"},
    ],
    "passthrough": [7],
}


def has_error_containing(errors, needle):
    return any(needle in e for e in errors)


def has_any(errors, needles):
    """True if any error contains any of the given substrings. Used for
    checks whose wording is owned by app/services/rls.py (Russian) when that
    module is present, or by genmanifest's own fallback (English) when it
    isn't — this test must pass either way, since which one runs is an
    accident of checkout state, not something this test should pin."""
    return any(any(n in e for n in needles) for e in errors)


def main():
    # 1. Valid rls block passes cleanly.
    errors = genmanifest.validate(base_manifest(VALID_RLS))
    check("valid rls block passes", errors == [], str(errors))

    # 2. No rls block at all is still fine (rls is optional).
    errors = genmanifest.validate(base_manifest())
    check("no rls block is valid (optional field)", errors == [], str(errors))

    # 3. Unknown top-level field inside rls.
    bad = copy.deepcopy(VALID_RLS)
    bad["oops"] = True
    errors = genmanifest.validate(base_manifest(bad))
    check("unknown rls field is an error",
          has_any(errors, ["unknown field: 'oops'", "unknown fields: oops"]),
          str(errors))

    # 4. Slot missing both column and via.
    bad = copy.deepcopy(VALID_RLS)
    del bad["slots"][0]["column"]
    errors = genmanifest.validate(base_manifest(bad))
    check("slot missing column|via is an error",
          has_any(errors, ["exactly one of column|via", "exactly one of column or via"]),
          str(errors))

    # 4b. Slot with BOTH column and via.
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"][0]["via"] = {"slot": 1, "key": "customer_number", "column": "tmr_territory_name"}
    errors = genmanifest.validate(base_manifest(bad))
    check("slot with both column and via is an error",
          has_any(errors, ["not both", "exactly one of column or via"]), str(errors))

    # 4c. `key` without `via` — the strict validator refuses it (the deleted
    # fallback silently accepted it, which is how a typo shipped unfiltered).
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"][0]["key"] = "customer_number"
    errors = genmanifest.validate(base_manifest(bad))
    check("key without via is an error",
          has_error_containing(errors, "key is only allowed together with via"), str(errors))

    # 4d. `column: null` next to a via — «exactly one of column|via» is about
    # the KEY being present, not about its value (the fallback tested
    # `is not None` and let this through).
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"][2]["column"] = None
    errors = genmanifest.validate(base_manifest(bad))
    check("column: null alongside via is an error",
          has_error_containing(errors, "exactly one of column or via"), str(errors))

    # 5. via missing its own column field.
    bad = copy.deepcopy(VALID_RLS)
    del bad["slots"][2]["via"]["column"]
    errors = genmanifest.validate(base_manifest(bad))
    check("via missing .column is an error",
          has_any(errors, ["via.column must be", "via.column"]), str(errors))

    # 5b. via missing .slot.
    bad = copy.deepcopy(VALID_RLS)
    del bad["slots"][2]["via"]["slot"]
    errors = genmanifest.validate(base_manifest(bad))
    check("via missing .slot is an error",
          has_any(errors, ["via.slot must be an integer", "via.slot: integer"]), str(errors))

    # 6. via-on-via: slot 2's via points at slot 3, but make slot 3 itself a via.
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"][3] = {"index": 3, "key": "x",
                        "via": {"slot": 1, "key": "customer_number",
                                "column": "tmr_territory_name"}}
    bad["slots"][2]["via"]["slot"] = 3
    errors = genmanifest.validate(base_manifest(bad))
    check("via pointing at a via (depth 2) is an error",
          has_any(errors, ["via-on-via is not allowed", "via depth is 1"]), str(errors))

    # 6b. via.slot pointing at an index not declared in slots at all.
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"][2]["via"]["slot"] = 99
    errors = genmanifest.validate(base_manifest(bad))
    check("via.slot not declared anywhere is an error",
          has_any(errors, ["via.slot 99 is not declared in slots", "slot 99 is not declared"]),
          str(errors))

    # 7. index used in both slots and passthrough.
    bad = copy.deepcopy(VALID_RLS)
    bad["passthrough"] = [6]
    errors = genmanifest.validate(base_manifest(bad))
    check("index overlapping slots and passthrough is an error",
          has_any(errors, ["also appears in passthrough", "is also in passthrough"]),
          str(errors))

    # 8. rls index not present in outputs.slots — genmanifest's OWN cross-check,
    # wording is genmanifest's regardless of which validate_policy ran.
    bad = copy.deepcopy(VALID_RLS)
    bad["slots"].append({"index": 40, "column": "tmr_position_name"})
    errors = genmanifest.validate(base_manifest(bad))
    check("rls index missing from outputs.slots is an error",
          has_error_containing(errors, "index 40 is not declared in outputs.slots"), str(errors))

    # 9. outputs.slots has an index the rls policy never covers (drop passthrough).
    bad = copy.deepcopy(VALID_RLS)
    bad["passthrough"] = []
    errors = genmanifest.validate(base_manifest(bad))
    check("uncovered outputs.slots index is an error",
          has_error_containing(errors, "outputs.slots[7] is not covered"), str(errors))

    # 10. every error genmanifest attaches to the rls block carries the
    # "rls: " prefix (so they read as a coherent block regardless of which
    # validate_policy produced the underlying message).
    bad = copy.deepcopy(VALID_RLS)
    bad["garbage"] = 1
    errors = genmanifest.validate(base_manifest(bad))
    rls_errors = [e for e in errors if e.startswith("rls: ")]
    check("rls errors carry the 'rls: ' prefix",
          len(rls_errors) >= 1 and has_any(rls_errors, ["garbage"]), str(errors))

    # 11. normalize() passes rls through unchanged.
    normalized = genmanifest.normalize(base_manifest(VALID_RLS))
    check("normalize() passes rls through unchanged",
          normalized.get("rls") == VALID_RLS, str(normalized.get("rls")))

    # 11b. normalize() on a manifest with no rls does not invent one.
    normalized = genmanifest.normalize(base_manifest())
    check("normalize() adds no rls key when absent",
          "rls" not in normalized, str(normalized.keys()))

    # 12. rls must be an object.
    errors = genmanifest.validate(base_manifest(rls=["not", "an", "object"]))
    check("non-object rls is an error",
          has_any(errors, ["rls: must be an object", "rls: the policy must be an object"]),
          str(errors))

    # 13. slots must be a non-empty list.
    errors = genmanifest.validate(base_manifest(rls={"slots": []}))
    check("empty slots list is an error",
          has_any(errors, ["slots is required and must be a non-empty list",
                            "rls: slots: a non-empty list"]),
          str(errors))

    print("ALL PASS" if not FAILURES else f"FAILURES: {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
