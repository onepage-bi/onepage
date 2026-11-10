# Server-stored user files

Two features let a user put bytes on our disk: dashboard **slot files** (and the
generator bundles / run aux files around them) and, since presentations v1,
**presentation slides** — the `image`, `pdf` and `html` kinds.

Presentation slides are content of the **same class** as slot files, and are
handled by the same rules:

- **Sniffed, not declared.** The kind is decided by the leading bytes —
  `\x89PNG\r\n\x1a\n`, `\xff\xd8\xff`, `%PDF-`, or a whole HTML document
  (optional BOM/whitespace/comments, then `<!doctype html` / `<html`) — and
  nothing else is accepted. The `Content-Type` the browser sends and the
  extension the user typed are metadata; an SVG or a bare HTML fragment is a
  **415**. An HTML *document* is a first-class slide kind since 2026-08-23
  (decks made in Claude), but it is treated as the script it is: rendered only
  inside `<iframe sandbox="allow-scripts">` (the dashboard-iframe model, no
  `allow-same-origin`), and the file response itself carries
  `Content-Security-Policy: sandbox allow-scripts`, so even a direct navigation
  to the file URL runs it in an opaque origin with no access to our cookies or
  DOM. SVG stays refused: image slides are trusted as images downstream.
- **Size-capped during the read.** 5 MB for an image, 20 MB for a PDF,
  10 MB for an HTML slide. The cap
  follows the sniff, so a PDF is not judged by the image limit, and the read
  aborts with **413** the moment the cap is passed — nothing over the cap is
  fully buffered by the handler and nothing reaches the uploads directory.
  (Honest limit: Starlette's multipart parser has already spooled the body to
  its own temp file before the handler runs, so the cap protects our storage and
  our memory, not the machine's temp space. That is the same exposure every
  other upload route on this server has.)
- **The stored path is built from ids.** `_presentations/{presentation_id}/{item_id}.{png|jpg|pdf}`
  — the extension comes from a whitelist and every other component is a uuid the
  server minted. A filename carrying `../` is not sanitized; it simply never
  participates in building the path. The original name is kept as a column, for
  display only.
- **Owner-only.** In v1 a presentation and its slides are visible to their owner
  and nobody else. Every route answers **404** to a non-owner — never 403, which
  would confirm the id exists.
- **Served defensively.** `GET /presentations/{id}/file/{item_id}` sends the
  stored content type with `X-Content-Type-Options: nosniff`,
  `Content-Disposition: inline; filename="{item_id}.{ext}"` (never the
  uploader's name) and `Cache-Control: private, no-store`.
- **Audited.** Upload, edit, reorder and delete each write an `AuditLog` row
  under `resource_type = "presentation"`.
- **Never sent to any model.** Slide files are bytes on disk served back to
  their owner. No LLM path reads them, and none should be added: the data
  boundary rule that keeps dashboard cell values off the network covers this
  content too.

Browser clients must upload with **FormData**. `OriginGuard` refuses a
state-changing request that carries a body with no `Content-Type` (an untyped
body is exactly the CORS-simple shape a cross-origin CSRF write uses, and
FastAPI would still parse it), and FormData makes the browser set
`multipart/form-data` itself. Do not hand-build the request body.

Pinned by `tests/smoke_presentation_uploads.py` (sniffing, both caps with a
disk-is-still-empty assertion, the `../` filename, the serving headers,
owner-only, and the OriginGuard cases) and `tests/smoke_presentations.py`
(owner-only CRUD, the dashboard-access rule for slides, the note cap, and
deletion removing the files from disk).

## PDF slides: client-side split, server-side sniff per page

Presentations can turn an uploaded PDF into one slide per page
(`static/js/presentations.js splitPdf`, vendored `static/vendor/pdf-lib.min.js`
— committed, not gitignored, so it ships in patches; loaded lazily, only when
a PDF actually needs splitting). This is a **UX convenience, not a trust
boundary**: `splitPdf` runs `PDFLib.PDFDocument.load(bytes, {ignoreEncryption:
true})` and re-saves each page as its own PDF entirely in the browser, then
uploads each page through the **same** `POST /presentations/{id}/upload`
route as any other file. The server does not know or care that a batch of
uploads came from one split source — every one of them is sniffed
independently by `_sniff(head)` against the magic-byte table above, sized
against `_MAX_PDF`, and given its own uuid path, exactly like a single
manually-picked PDF. A malformed or truncated page that `pdf-lib` still
produced bytes for gets no special pass: if what lands in the request body
does not start with `%PDF-`, it is rejected with the same **415** any other
mistyped upload gets.

## Employees CSV import

`POST /api/admin/employees/import` (`app/api/employees.py`,
`require_permission("manage_users")`) loads an administrator's CSV export of
Greenplum's `warehouse.v_employees` view — the server never holds a
Greenplum driver or credentials, only the file the administrator exported.

- **Size-capped during the read**: `file.read(MAX_IMPORT_BYTES + 1)` reads at
  most one byte over the 10 MB cap (`MAX_IMPORT_BYTES = 10 * 1024 * 1024`);
  anything over is a **413** (`"File is larger than 10 MB"`) before parsing starts.
  Row count is capped too — `MAX_IMPORT_ROWS = 50_000`, enforced while
  iterating the CSV reader, also a **413** past the limit.
- **Decode fallbacks, not a silent guess**: `_decode(raw)` tries
  `utf-8-sig` then `cp1251` (DBeaver's Russian-locale CSV export encoding);
  neither decoding is a **415** (`"File cannot be read as UTF-8 or CP1251"`).
  The delimiter is sniffed from the header line (`;` wins ties over `,`).
  A file whose header lacks both `id` and `person_id` is rejected as **422**
  rather than silently imported as zero rows — a wrong file fails loudly.
- **Column whitelist is the sensitive-data filter**: `COLUMN_MAP` lists 27
  CSV headers the importer will read; every column the export might carry
  that isn't in that map — `birthdate`, `mobilephone`, `gender`, every
  `vacation*` column — is dropped at parse time and never reaches the
  `employees` table, whatever the uploaded file contains. This is the same
  posture as the manifest/model whitelists elsewhere in the platform: an
  allowlist of expected columns, not a denylist of sensitive ones, so a
  Greenplum view change that adds a new sensitive column ships closed by
  default rather than open.
- The import is **soft-sync, not append-only**: every employee row not seen
  in the current upload is flipped `is_active = False` (never deleted), so
  a partial or stale export doesn't silently keep leavers listed as active.
- Every import is audited (`employees_import`, actor + totals) and recorded
  in `employee_syncs` for the status card on `/admin/employees`.

Pinned by `tests/smoke_employees.py` (decode fallback incl. a `cp1251`-encoded
fixture, the whitelist dropping sensitive columns, the size/row caps, the
malformed-header 422, and the `is_active` soft-deactivation on re-import).
