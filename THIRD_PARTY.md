# Third-party components

Versions are taken from the code (deploy/fetch-*.py, requirements.txt, embedded version strings and headers, pyodide-lock.json), not from memory. "Downloaded" entries are not in git; they are fetched at build time into gitignored directories. See NOTICE for the legal notes, including psycopg (LGPL-3.0) and AI-generated media.

| File or directory | Project | Version | License | Origin | Status / license file |
|---|---|---|---|---|---|
| `static/js/vendor/dagre.min.js` | Dagre (@dagrejs/dagre) | 1.1.1 | MIT | https://unpkg.com/@dagrejs/dagre@1.1.1/dist/dagre.min.js | in git; static/js/vendor/dagre.LICENSE |
| `static/js/vendor/xlsx.full.min.js` | SheetJS Community Edition | 0.20.3 | Apache-2.0 | https://sheetjs.com | in git; static/js/vendor/xlsx.LICENSE |
| `static/vendor/pdf-lib.min.js` | pdf-lib | 1.x (not embedded) | MIT (contains Microsoft helper code, Apache-2.0) | https://github.com/Hopding/pdf-lib | in git; static/vendor/pdf-lib.LICENSE |
| `static/vendor/pdfjs/` | PDF.js | 4.10.38 | Apache-2.0 | Mozilla PDF.js | in git; static/vendor/pdfjs/LICENSE |
| `static/fonts/inter-tight-*.woff2` | Inter Tight | n/a | OFL-1.1 | Google Fonts css2 (static/css/fonts.css) | in git; static/fonts/INTER-OFL.txt |
| `static/fonts/jetbrains-mono-*.woff2` | JetBrains Mono | n/a | OFL-1.1 | Google Fonts css2 (static/css/fonts.css) | in git; static/fonts/JETBRAINS-MONO-OFL.txt |
| `static/fonts/unbounded-*.woff2` | Unbounded | n/a | OFL-1.1 | Google Fonts | in git; static/fonts/UNBOUNDED-OFL.txt |
| `static/pyodide/` | Pyodide | v314.0.2 | MPL-2.0 | cdn.jsdelivr.net/pyodide/v314.0.2/full/ | downloaded: deploy/fetch-pyodide.py |
| `static/pyodide/ (pandas)` | pandas (Pyodide build) | 3.0.2 (from pyodide-lock.json) | BSD-3-Clause | Pyodide distribution | downloaded |
| `static/pyodide/ (numpy)` | numpy (Pyodide build) | 2.4.3 | BSD-3-Clause | Pyodide distribution | downloaded |
| `static/pyodide/ (others)` | python-calamine, xlrd, duckdb, micropip | 0.6.2, 2.0.2, 1.5.1, 0.11.1 | per package | Pyodide distribution | downloaded |
| `static/pyodide/wheels/` | openpyxl, et_xmlfile | 3.1.5, 2.0.0 | per package | PyPI (sha256-verified) | downloaded |
| `static/duckdb-wasm/` | @duckdb/duckdb-wasm | 1.32.0 (engine v1.4.3) | MIT | registry.npmjs.org | downloaded: deploy/fetch-duckdb-wasm.py |
| `static/duckdb-ext/` | DuckDB extensions | v1.4.3 | MIT | extensions.duckdb.org | downloaded |
| `static/arrow-esm/` | apache-arrow | 17.0.0 | Apache-2.0 | registry.npmjs.org | downloaded |
| `static/arrow-esm/ (deps)` | flatbuffers; tslib | 24.12.23; 2.8.1 | Apache-2.0; 0BSD | registry.npmjs.org | downloaded |
| `(not shipped) agent kit` | psycopg 3 [binary] | chosen by pip at kit build | LGPL-3.0 | PyPI | downloaded by deploy/make-agent-kit.sh; see NOTICE section 3 |
| `static/img/quests/, learn/, docs/hero.webp, static/video/season2-intro.mp4` | AI-generated media (Higgsfield) | n/a | see NOTICE section 4 | generated | in git |

## Python dependencies

Packages in `requirements.txt` (fastapi, uvicorn, jinja2, sqlalchemy, alembic, pandas, openpyxl, duckdb, and others) are installed by pip and are not vendored in this repository. Each carries its own license; check the installed metadata.
