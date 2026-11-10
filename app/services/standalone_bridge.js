<script>
/* The report's data is embedded in this same file (app/services/standalone.py).
   The report receives it with the same op_file message the platform uses to feed it:
   a different sender, the same protocol. There is no network here at all. */
(function () {
  'use strict';
  var SLOTS = __OP_SLOTS__;
  function bytes(b64) {
    var bin = atob(b64), n = bin.length, out = new Uint8Array(n);
    for (var i = 0; i < n; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  function raw(slot) {
    var u8 = bytes(slot.b64);
    if (slot.enc !== 'gzip') return Promise.resolve(u8.buffer);
    if (typeof DecompressionStream === 'undefined') {
      return Promise.reject(new Error(__OP_T_OLD__));
    }
    return new Response(new Blob([u8]).stream()
      .pipeThrough(new DecompressionStream('gzip'))).arrayBuffer();
  }
  /* We do not fail silently: a report without data looks "as intended", and this
     is the only place where we can say what exactly went wrong. */
  function fail(text) {
    var d = document.createElement('div');
    d.setAttribute('style', 'position:fixed;left:0;right:0;top:0;z-index:2147483647;'
      + 'padding:10px 14px;background:#7f1d1d;color:#fff;'
      + 'font:14px/1.4 system-ui,sans-serif');
    d.textContent = __OP_T_FAIL__ + text;
    (document.body || document.documentElement).appendChild(d);
  }
  function feed() {
    SLOTS.forEach(function (slot) {
      raw(slot).then(function (buf) {
        window.postMessage({ type: 'op_file', slot: slot.slot,
                             name: slot.name, data: buf }, '*');
      }, function (err) { fail(String((err && err.message) || err)); });
    });
  }
  /* After load: by this moment the report has already attached its message handler —
     on the platform the first op_file arrives exactly the same way, not earlier. */
  if (document.readyState === 'complete') feed();
  else window.addEventListener('load', feed);
})();
</script>
