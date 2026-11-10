/* t() inside a Worker.
 * A Worker has no `window`, so the page's global t() is out of reach and the
 * catalogue is not loaded here. This shim keeps the call shape identical to the
 * rest of the codebase — the strings ARE catalogue keys and the extractor
 * collects them — but it always returns the English source (with its
 * `{ctx:N}` disambiguator stripped, as the page's t() does) and its
 * parameters filled in. Making them translate needs the refactor from the
 * i18n design §3: the worker returns a CODE, the main thread renders the text
 * with the real t(). Until then these messages stay English in every language,
 * the same graceful degradation as an untranslated key.
 *
 * It lives in its own module because every explore2 module that builds an
 * error message calls t(): a function-declaration in worker.js is module-local
 * and left guard.js/engine.js/cache.js/xlsx.js with a ReferenceError on the
 * very error they were trying to report (found by smoke_explore_chrome 2026-09-06).
 */
const CTX = /\s*\{ctx:[^}]*\}/;
export function t(key, params) {
  key = String(key).replace(CTX, '');
  if (!params) return key;
  return key.replace(/\{(\w+)\}/g, function (m, name) {
    var v = params[name];
    return v === undefined || v === null ? m : String(v);
  });
}
