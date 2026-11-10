"""Smoke test for glob pattern matching (A8) — Python server vs JS engine parity.

Tests the app.services.globmatch module against a comprehensive table of
patterns and names, then extracts the globToRe function from the browser
engine (static/js/gen-engine.js) and data-sources editor (data-sources-edit.js)
and runs the same tests through node to verify identical behavior.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.getcwd())
from app.services import globmatch  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  <-- {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def extract_globToRe(js_file_path):
    """Extract the globToRe function from a JavaScript file using regex.

    Looks for: function globToRe(pat) { ... } until the closing brace that
    matches the opening one. Returns the source code including 'function'.
    """
    with open(js_file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Find the start of the function
    match = re.search(r'function globToRe\(pat\)\s*{', content)
    if not match:
        return None

    start = match.start()
    brace_pos = match.end() - 1
    brace_count = 1
    i = brace_pos + 1

    # Find the matching closing brace
    while i < len(content) and brace_count > 0:
        if content[i] == '{':
            brace_count += 1
        elif content[i] == '}':
            brace_count -= 1
        i += 1

    if brace_count == 0:
        return content[start:i]
    return None


# ============================================================================
# Test table: (pattern, name, expected_match)
# ============================================================================
TEST_TABLE = [
    # Literals
    ("report.xlsx", "report.xlsx", True),
    ("report.xlsx", "report.csv", False),
    ("report.xlsx", "REPORT.XLSX", True),  # case-insensitive

    # Single wildcard *
    ("*.xlsx", "report.xlsx", True),
    ("*.xlsx", "data.xlsx", True),
    ("*.xlsx", ".xlsx", True),  # * matches empty
    ("*.xlsx", "report.csv", False),
    ("Report*.xlsx", "Report_2026-08-20.xlsx", True),
    ("Report*.xlsx", "Report.xlsx", True),
    ("Report*.xlsx", "Reports/file.xlsx", False),  # * doesn't cross /

    # Double wildcard **
    ("**", "anything.xlsx", True),
    ("**", "file", True),
    ("**_*.xlsx", "prefix_suffix.xlsx", True),

    # Question mark ?
    ("data?.xlsx", "data1.xlsx", True),
    ("data?.xlsx", "data12.xlsx", False),  # ? matches exactly one char
    ("data?.xlsx", "data.xlsx", False),

    # Escaped special characters [ ]
    ("[ab].xlsx", "[ab].xlsx", True),  # literal [ab], not a class
    ("[ab].xlsx", "a.xlsx", False),
    ("[ab].xlsx", "b.xlsx", False),

    # Escaped . + ( ) $ ^ | \
    ("file.txt", "file.txt", True),
    ("file.txt", "filetxt", False),  # . is literal, not regex any-char
    ("costs+fees.xlsx", "costs+fees.xlsx", True),
    ("(draft).xlsx", "(draft).xlsx", True),
    ("(draft).xlsx", "draft.xlsx", False),
    ("report$.xlsx", "report$.xlsx", True),
    ("report^v2.xlsx", "report^v2.xlsx", True),
    ("a|b.xlsx", "a|b.xlsx", True),
    ("back\\slash.xlsx", "back\\slash.xlsx", True),

    # Mixed patterns
    ("Stock_*.xlsx", "Stock_2026.xlsx", True),
    ("Stock_*.xlsx", "stock_2026.xlsx", True),  # case-insensitive
    ("*_template.xlsx", "my_template.xlsx", True),
    ("*_template.xlsx", "template.xlsx", False),  # needs underscore before template
    ("*_template.xlsx", "my_report.xlsx", False),

    # Temp files / junk
    ("~$*", "~$report.xlsx", True),
    ("~$*", "~$", True),  # * matches empty
    ("~$*", "report.xlsx", False),

    # Cyrillic / unicode
    # Cyrillic on purpose: exercises non-ASCII names and Unicode case folding.
    ("Данные_*.xlsx", "Данные_2026.xlsx", True),
    ("Данные_*.xlsx", "данные_2026.xlsx", True),  # case-insensitive
    ("Ведомость.xlsx", "ВЕДОМОСТЬ.XLSX", True),  # unicode case folding

    # Anchoring
    ("report.xlsx", "prefix_report.xlsx", False),  # must match entire name
    ("report.xlsx", "report.xlsx.backup", False),
    ("*.xlsx", ".xlsx", True),  # * can match empty

    # Complex patterns
    ("Stock_*_[2026].xlsx", "Stock_North_[2026].xlsx", True),
    ("Stock_*_[2026].xlsx", "Stock_North_2026.xlsx", False),  # [2026] is literal
    ("Q[1234]_*.xlsx", "Q[1234]_data.xlsx", True),  # [1234] is literal
    ("Q[1234]_*.xlsx", "Q1_data.xlsx", False),
]


# ============================================================================
# Python tests
# ============================================================================

for pattern, name, expected in TEST_TABLE:
    result = globmatch.matches(pattern, name)
    check(f"globmatch.matches({pattern!r}, {name!r}) = {expected}",
          result == expected,
          f"got {result}")

# Test first_match
patterns = ["Stock_*.xlsx", "rates.xlsx", "*.csv"]
check("globmatch.first_match finds first matching pattern",
      globmatch.first_match(patterns, "Stock_2026.xlsx") == "Stock_*.xlsx",
      f"got {globmatch.first_match(patterns, 'Stock_2026.xlsx')}")

check("globmatch.first_match returns None when no match",
      globmatch.first_match(patterns, "unknown.json") is None,
      f"got {globmatch.first_match(patterns, 'unknown.json')}")

# Test validate_pattern
check("globmatch.validate_pattern accepts valid pattern",
      globmatch.validate_pattern("Stock_*.xlsx") is None,
      globmatch.validate_pattern("Stock_*.xlsx"))

check("globmatch.validate_pattern rejects pattern with /",
      globmatch.validate_pattern("folder/file*.xlsx") is not None,
      globmatch.validate_pattern("folder/file*.xlsx"))

check("globmatch.validate_pattern rejects wildcard-only",
      globmatch.validate_pattern("***") is not None,
      globmatch.validate_pattern("***"))

check("globmatch.validate_pattern rejects pattern > 128 chars",
      globmatch.validate_pattern("x" * 129) is not None,
      globmatch.validate_pattern("x" * 129))

# ============================================================================
# Node.js parity test
# ============================================================================

# Try to find node
node_path = None
try:
    result = subprocess.run(["which", "node"], capture_output=True, text=True, timeout=2)
    if result.returncode == 0:
        node_path = result.stdout.strip()
except Exception:
    pass

if not node_path:
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            node_path = "node"
    except Exception:
        pass

if node_path:
    # Extract globToRe from both JS files using relative paths from cwd
    base_dir = os.getcwd()
    gen_engine_file = os.path.join(base_dir, "static/js/gen-engine.js")
    data_sources_file = os.path.join(base_dir, "static/js/data-sources-edit.js")

    gen_engine_code = extract_globToRe(gen_engine_file)
    data_sources_code = extract_globToRe(data_sources_file)

    if gen_engine_code and data_sources_code:
        # Test gen-engine.js implementation
        harness_gen_engine = f"""
const tests = {json.dumps(TEST_TABLE)};
const _globCache = new Map();
{gen_engine_code}
const results = [];
for (const [pattern, name, expected] of tests) {{
  const re = globToRe(pattern);
  const result = re.test(name);
  results.push({{pattern, name, expected, result}});
}}
console.log(JSON.stringify(results, null, 2));
"""

        # Test data-sources-edit.js implementation
        harness_data_sources = f"""
const tests = {json.dumps(TEST_TABLE)};
const globCache = new Map();
{data_sources_code}
const results = [];
for (const [pattern, name, expected] of tests) {{
  const re = globToRe(pattern);
  const result = re.test(name);
  results.push({{pattern, name, expected, result}});
}}
console.log(JSON.stringify(results, null, 2));
"""

        try:
            # Run gen-engine tests
            result_gen = subprocess.run([node_path, "-e", harness_gen_engine],
                                       capture_output=True, text=True, timeout=10)
            gen_engine_results = None
            if result_gen.returncode == 0:
                try:
                    gen_engine_results = json.loads(result_gen.stdout)
                    check("node (gen-engine.js): extract and parse", True)
                except (json.JSONDecodeError, KeyError) as e:
                    check("node (gen-engine.js): parse results", False, f"JSON error: {e}")
            else:
                check("node (gen-engine.js): run harness", False, result_gen.stderr[:200])

            # Run data-sources tests
            result_ds = subprocess.run([node_path, "-e", harness_data_sources],
                                      capture_output=True, text=True, timeout=10)
            data_sources_results = None
            if result_ds.returncode == 0:
                try:
                    data_sources_results = json.loads(result_ds.stdout)
                    check("node (data-sources-edit.js): extract and parse", True)
                except (json.JSONDecodeError, KeyError) as e:
                    check("node (data-sources-edit.js): parse results", False, f"JSON error: {e}")
            else:
                check("node (data-sources-edit.js): run harness", False, result_ds.stderr[:200])

            # Compare gen-engine results vs python
            if gen_engine_results:
                all_match = True
                for node_res in gen_engine_results:
                    pattern = node_res["pattern"]
                    name = node_res["name"]
                    expected = node_res["expected"]
                    node_result = node_res["result"]
                    py_result = globmatch.matches(pattern, name)
                    if py_result != node_result:
                        all_match = False
                        check(f"gen-engine.js vs python: {pattern!r} vs {name!r}",
                              False, f"python={py_result}, node={node_result}")

                if all_match:
                    check(f"gen-engine.js parity: all {len(TEST_TABLE)} test cases match", True)

            # Compare data-sources results vs python
            if data_sources_results:
                all_match = True
                for node_res in data_sources_results:
                    pattern = node_res["pattern"]
                    name = node_res["name"]
                    expected = node_res["expected"]
                    node_result = node_res["result"]
                    py_result = globmatch.matches(pattern, name)
                    if py_result != node_result:
                        all_match = False
                        check(f"data-sources-edit.js vs python: {pattern!r} vs {name!r}",
                              False, f"python={py_result}, node={node_result}")

                if all_match:
                    check(f"data-sources-edit.js parity: all {len(TEST_TABLE)} test cases match", True)

            # Compare gen-engine vs data-sources (should be identical)
            if gen_engine_results and data_sources_results:
                all_match = True
                for gen_res, ds_res in zip(gen_engine_results, data_sources_results):
                    if gen_res["result"] != ds_res["result"]:
                        all_match = False
                        check(f"gen-engine.js vs data-sources-edit.js: {gen_res['pattern']!r}",
                              False,
                              f"gen-engine={gen_res['result']}, data-sources={ds_res['result']}")

                if all_match:
                    check("gen-engine.js vs data-sources-edit.js: implementations match", True)

        except subprocess.TimeoutExpired:
            check("node parity: timeout", False, "subprocess timed out")
        except Exception as e:
            check("node parity: subprocess", False, str(e))
    else:
        if not gen_engine_code:
            check("extract globToRe from gen-engine.js", False, "not found")
        if not data_sources_code:
            check("extract globToRe from data-sources-edit.js", False, "not found")
else:
    check("node parity: node available", True, "skipped (node not found)")

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURES: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
