"""Prove that the wheels in wheels/ suffice for an install WITHOUT A NETWORK on the target
machine (Windows x64, CPython 3.12), which pip on macOS cannot check:
it evaluates environment markers against the current system, not against --platform.

    check_wheels.py <wheels directory> [--python 3.12.10] [spec ...]

We check three independent properties:
  1. CONTENTS: every requested wheel is in the directory (checked against the order
     list, not against what is there: a `pip download` that failed on one
     package leaves a directory that looks plausible).
  2. CLOSURE: every dependency whose marker is TRUE in the target environment
     is present in the directory.
  3. TAG COMPATIBILITY: every wheel really installs on cp312/win_amd64.

The wheel list is a PARAMETER, not a name in the code: phases 7-9 add
pandas/numpy/calamine to the kit, and the single source of truth about the contents is the same
list make-agent-kit.sh downloads them by. Extras are parsed from the spec
(`psycopg[binary]` → the "binary" extra is taken into account when parsing markers), and the Python
version comes from `--python`, so that the ABI tag is not written here as a second copy.
"""
import re
import sys
import zipfile
from pathlib import Path


def _parse_argv(argv: list[str]) -> tuple[Path, str, list[str]]:
    """(directory, full python version, specs). Without specs: the previous behaviour."""
    args, pyver = [], "3.12.10"
    i = 0
    while i < len(argv):
        if argv[i] == "--python":
            # Without a value (`--python` at the end of the line or before another option)
            # this used to fail with a raw IndexError instead of the usage text:
            # make-agent-kit.sh calls the script, and a person who sees a traceback
            # does not understand what is wrong with their call.
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                raise SystemExit("--python requires a version, for example "
                                 "`--python 3.12.10`.\n\n" + __doc__)
            pyver = argv[i + 1]
            i += 2
            continue
        if argv[i].startswith("--"):
            raise SystemExit(f"unknown option {argv[i]!r}.\n\n" + __doc__)
        args.append(argv[i])
        i += 1
    if not args:
        raise SystemExit(__doc__)
    return Path(args[0]), pyver, args[1:] or ["psycopg[binary]"]


WHEELS, PYVER, SPECS = _parse_argv(sys.argv[1:])
PYTAG = "cp" + "".join(PYVER.split(".")[:2])          # 3.12.10 -> cp312

ENV = {
    "sys_platform": "win32",
    "platform_system": "Windows",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "implementation_name": "cpython",
    "python_version": ".".join(PYVER.split(".")[:2]),
    "python_full_version": PYVER,
}

_CMP = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b, ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
_TERM = re.compile(r'\s*([A-Za-z_]+)\s*(==|!=|<=|>=|<|>)\s*"([^"]*)"\s*')


def _ver(s):
    return tuple(int(p) for p in re.findall(r"\d+", s))


def eval_marker(marker: str, extras: set) -> bool:
    """Markers in these METADATA files are only comparisons joined by and/or, without
    parentheses. We parse exactly this grammar and eval nothing."""
    def term(t):
        m = _TERM.fullmatch(t)
        if not m:
            raise ValueError(f"term not parsed: {t!r}")
        name, op, lit = m.groups()
        if name == "extra":
            return _CMP[op](lit in extras, True) if op == "==" else lit not in extras
        if name not in ENV:
            raise ValueError(f"unknown marker variable: {name}")
        left, right = ENV[name], lit
        if "version" in name:
            left, right = _ver(left), _ver(right)
        return _CMP[op](left, right)

    # or binds weaker than and
    return any(all(term(t) for t in part.split(" and ")) for part in marker.split(" or "))


def norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def parse_spec(spec: str) -> tuple[str, set[str]]:
    """`psycopg[binary]>=3.1` -> (`psycopg`, {`binary`}). The version is ignored:
    honouring it is pip's job; ours is the contents and the tags."""
    name, _, rest = spec.partition("[")
    extras = {e.strip() for e in rest.partition("]")[0].split(",") if e.strip()}
    return norm(re.split(r"[<>=!~ (]", name.strip(), maxsplit=1)[0]), extras


WANTED = dict(parse_spec(s) for s in SPECS)


present, tags = {}, {}
for whl in sorted(WHEELS.glob("*.whl")):
    z = zipfile.ZipFile(whl)
    meta = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")][0]
    text = z.read(meta).decode()
    name = re.search(r"^Name:\s*(.+)$", text, re.M).group(1).strip()
    present[norm(name)] = (whl.name, text)
    tags[whl.name] = whl.stem.split("-")[-3:]

failures = []


def check(label, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  <-- {extra}" if extra and not ok else ""))
    if not ok:
        failures.append(label)


print(f"directory: {WHEELS}   wheels: {len(present)}   "
      f"target: {PYTAG}/win_amd64\n")

# 1. Contents: what was ordered is in the directory
for pkg in sorted(WANTED):
    check(f"{pkg}: wheel downloaded", pkg in present,
          "not in the directory: pip download failed or the name diverged from the spec")

# 2. Dependency closure
for pkg, (fname, text) in sorted(present.items()):
    extras = WANTED.get(pkg, set())
    for line in re.findall(r"^Requires-Dist:\s*(.+)$", text, re.M):
        req, _, marker = line.partition(";")
        dep = norm(re.split(r"[\[<>=!~ (]", req.strip(), maxsplit=1)[0])
        needed = True if not marker.strip() else eval_marker(marker.strip(), extras)
        if needed:
            check(f"{pkg}: requires {dep} -> present in the directory", dep in present,
                  f"no wheel for {dep} (marker: {marker.strip() or 'no marker'})")

# 3. Tag compatibility with the target ABI / win_amd64
for fname, (py, abi, plat) in tags.items():
    ok = (plat in ("any", "win_amd64")
          and (py in ("py2.py3", "py3") or PYTAG in py.split(".")))
    check(f"{fname}: tag installs on {PYTAG}/win_amd64", ok, f"{py}-{abi}-{plat}")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILURES: " + ", ".join(failures))
sys.exit(1 if failures else 0)
