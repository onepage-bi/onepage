/* `t` / `tn` for the Node harnesses.
 *
 * Not a fake: it loads the real static/js/i18n.js with no catalogue, which is
 * exactly the lang=en browser — `t('Version {n}', {n: 3})` comes back as
 * «Version 3». That is why every DOM test asserts on the English text (the
 * catalogue key, `{ctx:…}` stripped), and why a broken key (a lost space, a
 * dropped parameter) shows up as a failing assertion instead of passing silently.
 *
 * Require it FIRST, before the harness installs its `global.window` stub: with
 * no window in sight i18n.js binds t/tn to globalThis, which is where the
 * evaluated browser module looks them up.
 *
 * To exercise a real translation, call OP_I18N.init('ru', {...}) afterwards.
 */
'use strict';
const path = require('path');
require(path.join(__dirname, '..', 'static', 'js', 'i18n.js'));
module.exports = globalThis.OP_I18N;
