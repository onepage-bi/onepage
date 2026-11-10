/* The "Navigators" section: show the upload form and hide it.
 * There is nothing else to do here — the form is ordinary and submitted by the browser. */
(function () {
  'use strict';
  var add = document.getElementById('nav-add');
  var form = document.getElementById('nav-form');
  var cancel = document.getElementById('nav-cancel');
  if (!add || !form) return;
  add.addEventListener('click', function () {
    form.hidden = false;
    var t = document.getElementById('nav-title');
    if (t) t.focus();
  });
  if (cancel) cancel.addEventListener('click', function () { form.hidden = true; });
})();
