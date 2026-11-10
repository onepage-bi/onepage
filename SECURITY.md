# Security Policy

## Reporting a vulnerability

Please report privately. Use **GitHub Security Advisories** for this
repository: the "Report a vulnerability" button under the Security tab. Please
do not open a public issue or pull request for a vulnerability, and do not
include real company data or personal data in a report; a synthetic
reproduction is enough.

Include what you did, what you expected, what happened, the version or commit,
and whether a non-default setting was involved (for example the authentication
mode).

**Response time.** This is maintained by a very small team, so we make no
guarantees. We aim to acknowledge a report within a week, but cannot promise
that, and we cannot promise a fix date. We will tell you what we decide.

## Supported versions

There is no public release history yet. Only the latest release published in
this repository is considered supported.

## What counts as a vulnerability

These are the actual trust boundaries of the product. A report is in scope if
it shows a way to cross one of them:

- **Report access bypass.** Reading, changing or deleting a report (its HTML,
  versions, data files, annotations) without the access the model grants:
  owner, admin, explicit grant, public, or share link. Includes share links
  that outlive revocation or reach beyond their scope.
- **Row-level security leak (ADR-18).** A viewer receiving rows of a
  slot file that the row-level rules should have filtered out, or any other
  route that returns unfiltered slot-file data around the server-side
  filtering. Design in `docs/PERF_DECISIONS.md`, ADR-18.
- **Iframe sandbox escape.** Reports are untrusted HTML written by a language
  model and shown in a sandboxed iframe (`templates/pages/viewer.html`; the
  message contract is in `.claude/skills/dashboard-protocols`). Script in a
  report reading the host page, cookies or the session, calling platform APIs
  as the viewer, or messaging the parent in a way the protocol does not intend
  is in scope.
- **Session forgery or fixation.** Obtaining, forging or reusing a session
  without authenticating, or bypassing password, OIDC or SAML sign-in.
  The development bypass (`OP_DEV_BYPASS_AUTH`) is for local use only;
  an instance exposed with it enabled is a misconfiguration, not a
  vulnerability.
- **Cross-site request forgery** past the origin check (`OriginGuard`) on a
  state-changing request.
- **Data values reaching a model prompt.** The platform's design says values
  from the data must not be sent to a language model; only context and
  structure (see `docs/CHAT_PANE.md` and `docs/LLM_PROVIDERS.md`). A path
  that puts data values into a prompt, or sends data to a provider the
  administrator did not configure, is in scope.
- **Upload handling** flaws described in `docs/SECURITY_UPLOADS.md`
  (for example, an uploaded file executing outside the sandbox).
- **Anonymizer leak.** The `/anonymizer` page is designed to work entirely in
  the browser; any way to make it send the data elsewhere is in scope.

## Out of scope

- Findings that require a deliberately unsafe configuration (dev bypass on a
  public host, default or empty secrets in `.env`).
- Denial of service by an authenticated administrator against their own
  instance, and volumetric attacks.
- Missing hardening headers without a demonstrated exploit.
- Vulnerabilities in third-party dependencies with no reachable path here;
  please report those upstream.
- SAML sign-in problems on instances that did not install
  `requirements-saml.txt` (the feature is off then).

## Status

OIDC sign-in was tested against a provider stub, not against a live Keycloak
or Entra. Please treat your own deployment accordingly and put it behind your
usual network controls.

## Disclosure

Please give us reasonable time to fix an issue before publishing details. We
will credit reporters who want credit.
