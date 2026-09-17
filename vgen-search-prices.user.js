// ==UserScript==
// @name         VGen Search — Show Minimum Prices
// @namespace    https://github.com/fuwamocoanon/comf
// @version      1.0.0
// @description  Shows each listing's minimum ("from $X") starting price on vgen.co search/browse cards. Reads the numbers straight from vgen's own API responses, so prices always match what the app would show.
// @author       fuwamocoanon
// @match        https://vgen.co/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Config
  // ---------------------------------------------------------------------------
  const API_HOST = 'api.vgen.co';
  // Fields that may hold a price, in order of preference. Values are in CENTS.
  const PRICE_FIELDS = [
    'basePrice',            // the "starting at" price shown on a service's main section
    'startingPrice',
    'minPrice',
    'price',
    'minPriceInDefaultCurrency',
  ];
  const BADGE_CLASS = 'vgen-min-price';       // stable hook for styling from CSS
  const PROCESSED_ATTR = 'data-vgen-price';   // marks anchors we've already handled

  // ---------------------------------------------------------------------------
  // Price index — populated from the app's own API responses
  // ---------------------------------------------------------------------------
  // Each record: { price (cents), currency, name, id }
  const byPath = new Map(); // "/username/service/slug" (lowercased) -> record
  const bySlug = new Map(); // "slug" (lowercased)                    -> record
  const byId   = new Map(); // serviceID                              -> record

  function slugify(s) {
    return String(s)
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  function firstDefined(obj, keys) {
    for (const k of keys) {
      const v = obj[k];
      if (typeof v === 'number' && Number.isFinite(v)) return v;
    }
    return undefined;
  }

  function pickUsername(obj) {
    if (typeof obj.username === 'string') return obj.username;
    const nested = obj.user || obj.owner || obj.seller || obj.artist;
    if (nested && typeof nested === 'object' && typeof nested.username === 'string') {
      return nested.username;
    }
    if (typeof obj.sellerUsername === 'string') return obj.sellerUsername;
    if (typeof obj.ownerUsername === 'string') return obj.ownerUsername;
    return undefined;
  }

  // Deep-scan any JSON payload for objects that look like a service/listing and
  // record their price so we can match them to cards in the DOM.
  function indexPayload(node, depth) {
    if (!node || depth > 8) return;
    if (Array.isArray(node)) {
      for (const item of node) indexPayload(item, depth + 1);
      return;
    }
    if (typeof node !== 'object') return;

    const name = node.serviceName || node.name || node.title;
    const price = firstDefined(node, PRICE_FIELDS);
    if (typeof name === 'string' && name && price !== undefined) {
      const record = {
        price,
        currency: typeof node.currency === 'string' ? node.currency : 'USD',
        name,
        id: node.serviceID || node._id || node.id,
      };
      const slug = slugify(name);
      if (slug && !bySlug.has(slug)) bySlug.set(slug, record);
      if (record.id) byId.set(String(record.id), record);
      const username = pickUsername(node);
      if (username && slug) {
        byPath.set(`/${username}/service/${slug}`.toLowerCase(), record);
      }
    }

    // Recurse into children (services, products, hits, results, items, ...).
    for (const key in node) {
      const val = node[key];
      if (val && typeof val === 'object') indexPayload(val, depth + 1);
    }
  }

  let scheduleInject = () => {};
  function ingest(text) {
    let data;
    try { data = JSON.parse(text); } catch { return; }
    const before = byPath.size + bySlug.size + byId.size;
    indexPayload(data, 0);
    const after = byPath.size + bySlug.size + byId.size;
    if (after > before) scheduleInject();
  }

  // ---------------------------------------------------------------------------
  // Network hooks — capture responses from api.vgen.co (installed at document-start)
  // ---------------------------------------------------------------------------
  function isApi(url) {
    try { return new URL(url, location.href).host === API_HOST; }
    catch { return false; }
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === 'function') {
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const p = nativeFetch.apply(this, arguments);
      if (isApi(url)) {
        p.then((res) => {
          try { res.clone().text().then(ingest).catch(() => {}); } catch {}
        }).catch(() => {});
      }
      return p;
    };
  }

  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const open = XHR.prototype.open;
    const send = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      this.__vgenApi = isApi(url);
      return open.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      if (this.__vgenApi) {
        this.addEventListener('load', () => {
          try {
            const ct = this.getResponseHeader && this.getResponseHeader('content-type');
            if (!ct || /json|text/i.test(ct)) ingest(this.responseText);
          } catch {}
        });
      }
      return send.apply(this, arguments);
    };
  }

  // ---------------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------------
  function formatPrice(cents, currency) {
    if (cents === 0) return 'Inquiry';
    try {
      const fmt = new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: currency || 'USD',
        minimumFractionDigits: 0,
        maximumFractionDigits: 2,
      });
      return 'from ' + fmt.format(cents / 100);
    } catch {
      return 'from ' + (cents / 100).toFixed(2) + ' ' + (currency || 'USD');
    }
  }

  // ---------------------------------------------------------------------------
  // DOM injection
  // ---------------------------------------------------------------------------
  function lookup(pathname) {
    const path = pathname.toLowerCase();
    if (byPath.has(path)) return byPath.get(path);
    const m = path.match(/\/service\/([^/?#]+)/);
    if (m && bySlug.has(m[1])) return bySlug.get(m[1]);
    return undefined;
  }

  function makeBadge(record) {
    const span = document.createElement('span');
    span.className = BADGE_CLASS;
    span.textContent = formatPrice(record.price, record.currency);
    // Inline styles so it works without the companion CSS; override via .vgen-min-price.
    span.style.cssText = [
      'position:absolute',
      'top:8px',
      'left:8px',
      'z-index:5',
      'padding:2px 8px',
      'border-radius:999px',
      'font:600 12px/1.5 inherit',
      'color:#fff',
      'background:rgba(0,0,0,.72)',
      'backdrop-filter:blur(2px)',
      'pointer-events:none',
      'white-space:nowrap',
    ].join(';');
    return span;
  }

  function injectAll() {
    const anchors = document.querySelectorAll('a[href*="/service/"]');
    for (const a of anchors) {
      if (a.getAttribute(PROCESSED_ATTR) === 'done') continue;
      let pathname;
      try { pathname = new URL(a.href, location.href).pathname; } catch { continue; }
      const record = lookup(pathname);
      if (!record) continue; // price not known yet — try again on next API response

      // Anchor for the absolute badge; make sure it establishes a positioning context.
      const host = a;
      const pos = getComputedStyle(host).position;
      if (pos === 'static') host.style.position = 'relative';
      host.appendChild(makeBadge(record));
      host.setAttribute(PROCESSED_ATTR, 'done');
    }
  }

  // Debounced scheduler shared by API ingest + DOM mutations.
  let raf = 0;
  scheduleInject = function () {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; injectAll(); });
  };

  function start() {
    injectAll();
    const observer = new MutationObserver(scheduleInject);
    observer.observe(document.body, { childList: true, subtree: true });
    // Re-scan on client-side navigation (vgen is a Next.js SPA).
    const wrap = (fn) => function () { const r = fn.apply(this, arguments); scheduleInject(); return r; };
    history.pushState = wrap(history.pushState);
    history.replaceState = wrap(history.replaceState);
    window.addEventListener('popstate', scheduleInject);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
