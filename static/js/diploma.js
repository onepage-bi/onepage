/* Diploma: print and "Download PDF".
 *
 * Why the PDF is built from a raster rather than text. The repository already has
 * pdf-lib (vendored for slicing presentations, `static/js/presentations.js`),
 * but its built-in fonts — Helvetica and family — do not know Cyrillic at all:
 * a Cyrillic title would turn into a row of empty boxes. A real text PDF
 * would need @pdf-lib/fontkit plus a TTF with Cyrillic — two new
 * dependencies for the ability to select text, which nobody uses on a
 * certificate. So the sheet is drawn into an A4-sized canvas at 300 dpi and
 * placed into the PDF as one image: no difference in print, and the font is ours.
 *
 * The geometry here repeats `static/css/diploma.css`. Both sides compute from
 * the sheet width, so the numbers are fractions, not pixels: an edit in one place
 * needs the same edit in the other, and that is the only price for the
 * screen and the file looking the same.
 */
(function () {
  if (typeof document === 'undefined') return;
  var sheet = document.getElementById('dp-sheet');
  if (!sheet) return;

  var W = 3508, H = 2480;                    // A4 landscape, 300 dpi
  var d = sheet.dataset;
  var INK = '#10131a', ACCENT = '#2f7bff', DIM = '#6b7787', GOLD = '#7d8796', FADE = '#8a94a1';
  var SANS = '"Inter Tight", -apple-system, "Segoe UI", Roboto, Arial, sans-serif';
  var MONO = '"JetBrains Mono", ui-monospace, Menlo, Consolas, monospace';

  function load(src) {
    return new Promise(function (res, rej) {
      var im = new Image();
      im.onload = function () { res(im); };
      im.onerror = function () { rej(new Error(src)); };
      im.src = src;
    });
  }

  function left(ctx, text, x, y, font, color) {
    if (!text) return;
    ctx.font = font;
    ctx.fillStyle = color;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(String(text), x, y);
  }

  /* letter-spacing is not supported in canvas everywhere — space letters by hand. */
  function track(ctx, text, x, y, font, color, spacing) {
    if (!text) return;
    ctx.font = font;
    ctx.fillStyle = color;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    var chars = String(text).split('');
    for (var i = 0; i < chars.length; i++) {
      ctx.fillText(chars[i], x, y);
      x += ctx.measureText(chars[i]).width + spacing;
    }
  }

  function round(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  async function draw() {
    var canvas = document.createElement('canvas');
    canvas.width = W; canvas.height = H;
    var ctx = canvas.getContext('2d');

    // The sheet is white, as on screen; the only "decoration" is an accent bar
    // on top and a glass object in the corner. Typography carries everything else.
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, W, H);
    var grad = ctx.createLinearGradient(0, 0, W, 0);
    grad.addColorStop(0, '#2f7bff');
    grad.addColorStop(0.34, '#2f7bff');
    grad.addColorStop(0.60, '#9ec2ff');
    grad.addColorStop(1, '#f0d9bd');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, W * 0.009);

    try {
      var art = await load(d.art);
      var aw = W * 0.26, ah = aw * (art.height / art.width);
      ctx.drawImage(art, W * 0.945 - aw, H * 0.88 - ah, aw, ah);
    } catch (e) { /* without the object the sheet is still a sheet */ }

    // Fractions of the sheet width — the same numbers as in `.dp-*` (cqw), only here
    // they are multiplied by W by hand. The text's left edge is 9 % of the width.
    var X = W * 0.09;
    var y = H * 0.12 + W * 0.023;

    // Mark: the blue square with "OP" and a caption beside it.
    var mk = W * 0.023, r = W * 0.005;
    ctx.fillStyle = '#2f7bff';
    round(ctx, X, y - mk, mk, mk, r);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = '700 ' + Math.round(W * 0.0105) + 'px ' + SANS;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('OP', X + mk / 2, y - mk / 2 + W * 0.0006);
    ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left';
    track(ctx, ('ONEPAGE · ' + (d.school || '')).toUpperCase(), X + mk + W * 0.007,
          y - mk * 0.32, '500 ' + Math.round(W * 0.010) + 'px ' + MONO, GOLD, W * 0.0018);

    y += W * 0.034;
    left(ctx, d.diploma, X, y + W * 0.042, '700 ' + Math.round(W * 0.054) + 'px ' + SANS, INK);
    y += W * 0.042;

    ctx.fillStyle = ACCENT;
    round(ctx, X, y + W * 0.017, W * 0.07, W * 0.0042, W * 0.0021);
    ctx.fill();
    y += W * 0.017 + W * 0.028;

    left(ctx, d.lead, X, y + W * 0.0135, '400 ' + Math.round(W * 0.0135) + 'px ' + SANS, DIM);
    y += W * 0.0135 + W * 0.007;
    left(ctx, d.name, X, y + W * 0.034, '600 ' + Math.round(W * 0.034) + 'px ' + SANS, INK);
    y += W * 0.034;
    var role = [d.title, d.dept].filter(Boolean).join(' · ');
    if (role) {
      y += W * 0.008;
      left(ctx, role, X, y + W * 0.0125, '400 ' + Math.round(W * 0.0125) + 'px ' + SANS, DIM);
      y += W * 0.0125;
    }
    y += W * 0.026;
    left(ctx, d.courseWord, X, y + W * 0.0135, '400 ' + Math.round(W * 0.0135) + 'px ' + SANS, DIM);
    y += W * 0.0135 + W * 0.007;
    left(ctx, d.course, X, y + W * 0.025, '600 ' + Math.round(W * 0.025) + 'px ' + SANS, ACCENT);
    y += W * 0.025 + W * 0.016;
    left(ctx, d.meta || '', X, y + W * 0.011, '400 ' + Math.round(W * 0.011) + 'px ' + MONO, DIM);

    // Footer: a line and two service captions at the edges.
    var fy = H * 0.89;
    ctx.strokeStyle = '#e6e9ef';
    ctx.lineWidth = Math.max(1, W * 0.0008);
    ctx.beginPath();
    ctx.moveTo(X, fy);
    ctx.lineTo(W - X, fy);
    ctx.stroke();
    ctx.font = '400 ' + Math.round(W * 0.010) + 'px ' + MONO;
    ctx.fillStyle = FADE;
    ctx.textAlign = 'left';
    ctx.fillText(d.dateWord + ' ' + d.date, X, fy + W * 0.019);
    ctx.textAlign = 'right';
    ctx.fillText(d.numberWord + ' ' + d.number, W - X, fy + W * 0.019);
    return canvas;
  }

  function pdfLib() {
    if (window.PDFLib) return Promise.resolve(window.PDFLib);
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = '/static/vendor/pdf-lib.min.js';
      s.onload = function () { res(window.PDFLib); };
      s.onerror = function () { rej(new Error('pdf-lib')); };
      document.head.appendChild(s);
    });
  }

  function fileName() {
    var safe = (d.course || 'course').replace(/[^\w\u0430-\u044f\u0451\u0410-\u042f\u0401 -]+/g, '').trim().replace(/\s+/g, '-');
    return 'OnePage-' + safe + '-' + d.number + '.pdf';
  }

  document.getElementById('dp-print')?.addEventListener('click', function () { window.print(); });

  document.getElementById('dp-pdf')?.addEventListener('click', async function (e) {
    var btn = e.currentTarget;
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = t('Building the PDF…');
    try {
      // Fonts must be ready BEFORE drawing: otherwise canvas draws the sheet
      // in a system font and the file will not match what the person saw.
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
      var canvas = await draw();
      // JPEG, not PNG: the same sheet as PNG weighs 7.8 MB — nobody sends that
      // by email. The background here is photographic; quality 0.92 is plenty.
      var shot = await new Promise(function (res) { canvas.toBlob(res, 'image/jpeg', 0.92); });
      var bytes = new Uint8Array(await shot.arrayBuffer());
      var lib = await pdfLib();
      var doc = await lib.PDFDocument.create();
      var page = doc.addPage([841.89, 595.28]);          // A4 landscape, in points
      var img = await doc.embedJpg(bytes);
      page.drawImage(img, { x: 0, y: 0, width: 841.89, height: 595.28 });
      doc.setTitle(d.diploma + ' · ' + d.course);
      var out = await doc.save();
      var url = URL.createObjectURL(new Blob([out], { type: 'application/pdf' }));
      var a = document.createElement('a');
      a.href = url; a.download = fileName();
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    } catch (err) {
      btn.textContent = t('Could not build the PDF — try “Print”');
      setTimeout(function () { btn.textContent = label; btn.disabled = false; }, 3200);
      return;
    }
    btn.textContent = label;
    btn.disabled = false;
  });
})();
