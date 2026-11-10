/* OnePage — client performance beacon.
   Collects NavigationTiming, /render iframe resource timings and custom
   op:* measures; batches them and ships via sendBeacon when the page is
   hidden/closed. Near-zero overhead: observers only push to an array. */

(function () {
  if (!('performance' in window) || !window.OP_USER) return;

  var batch = [];
  var navSent = false;

  function pushNav() {
    if (navSent) return;
    var e = performance.getEntriesByType('navigation')[0];
    if (!e || !e.loadEventEnd) return;
    navSent = true;
    var fcp = performance.getEntriesByName('first-contentful-paint')[0];
    batch.push({
      t: 'nav',
      page: location.pathname,
      ttfb: Math.round(e.responseStart),
      fcp: fcp ? Math.round(fcp.startTime) : 0,
      dcl: Math.round(e.domContentLoadedEventEnd),
      load: Math.round(e.loadEventEnd),
      size: e.transferSize || 0
    });
  }

  function pushResource(e) {
    // Only dashboard renders + slot data — the payloads that actually hurt.
    if (e.name.indexOf('/render/') === -1) return;
    batch.push({
      t: 'res',
      page: location.pathname,
      url: e.name.replace(location.origin, ''),
      dur: Math.round(e.duration),
      size: e.transferSize || 0
    });
  }

  function pushMeasure(e) {
    if (e.name.indexOf('op:') !== 0) return;
    batch.push({
      t: 'mark',
      page: location.pathname,
      name: e.name,
      dur: Math.round(e.duration)
    });
  }

  try {
    new PerformanceObserver(function (list) {
      list.getEntries().forEach(pushResource);
    }).observe({ type: 'resource', buffered: true });
    new PerformanceObserver(function (list) {
      list.getEntries().forEach(pushMeasure);
    }).observe({ type: 'measure', buffered: true });
  } catch (err) { /* old browser — nav timing alone still ships */ }

  function flush() {
    pushNav();
    if (!batch.length) return;
    var payload = JSON.stringify({ events: batch.splice(0, 200) });
    batch.length = 0;
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/perf/client', new Blob([payload], { type: 'application/json' }));
    } else {
      fetch('/api/perf/client', {
        method: 'POST', body: payload, keepalive: true,
        headers: { 'Content-Type': 'application/json' }
      }).catch(function () {});
    }
  }

  addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flush();
  });
})();
