// ==UserScript==
// @name         VGen — Show Minimum Prices on Service Cards
// @namespace    https://github.com/fuwamocoanon/comf
// @version      1.1.0
// @description  Overlays each service's minimum ("from $X") starting price onto every ServiceGridCard across vgen.co (search, browse, profiles, shops). Reads the numbers straight from vgen's own data so prices match what the app would show.
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
  // Stable part of the styled-components class on each card (hash suffix varies).
  const CARD_SELECTOR = '[class*="ServiceGridCard__GridCard"]';
  // Fields that may hold a price, in order of preference. Values are in CENTS.
  const PRICE_FIELDS = [
    'basePrice',            // the "starting at" price shown on a service's main section
    'startingPrice',
    'minPrice',
    'price',
    'minPriceInDefaultCurrency',
  ];
  const BADGE_CLASS = 'vgen-min-price';       // stable hook for styling from CSS
  const PROCESSED_ATTR = 'data-vgen-price';   // marks cards we've already handled

  // ---------------------------------------------------------------------------
  // Price index — populated from vgen's own data payloads
  // ---------------------------------------------------------------------------
  // Each record: { price (cents), currency, name, id }
  const byPath = new Map(); // "/username/service/slug" (lowercased) -> record
  const bySlug = new Map(); // "slug" (lowercased)                   -> record
  const byId   = new Map(); // serviceID                             -> record

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

  // Deep-scan any parsed JSON for objects that look like a service/listing.
  function indexPayload(node, depth) {
    if (!node || depth > 10) return;
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

    for (const key in node) {
      const val = node[key];
      if (val && typeof val === 'object') indexPayload(val, depth + 1);
    }
  }

  let scheduleInject = () => {};

  // Pull balanced JSON objects out of a blob. Handles both plain JSON and the
  // Next.js RSC "flight" stream (JSON objects embedded in a larger text body).
  function extractAndIndex(text) {
    if (!text) return;
    const before = byPath.size + bySlug.size + byId.size;

    // Fast path: the whole body is JSON.
    try {
      indexPayload(JSON.parse(text), 0);
    } catch {
      // Fallback: only bother scanning if a service-shaped key is present.
      if (text.indexOf('serviceName') !== -1 || text.indexOf('basePrice') !== -1) {
        scanEmbeddedObjects(text);
      }
    }

    const after = byPath.size + bySlug.size + byId.size;
    if (after > before) scheduleInject();
  }

  // Walk the string, JSON.parse each balanced {...} we can, index it, and skip
  // past it. String-aware so braces inside quotes don't break balancing.
  function scanEmbeddedObjects(text) {
    const len = text.length;
    for (let i = 0; i < len; i++) {
      if (text[i] !== '{') continue;
      let depth = 0, inStr = false, esc = false, end = -1;
      for (let j = i; j < len; j++) {
        const c = text[j];
        if (inStr) {
          if (esc) esc = false;
          else if (c === '\\') esc = true;
          else if (c === '"') inStr = false;
        } else if (c === '"') {
          inStr = true;
        } else if (c === '{') {
          depth++;
        } else if (c === '}') {
          depth--;
          if (depth === 0) { end = j; break; }
        }
      }
      if (end === -1) break;
      const slice = text.slice(i, end + 1);
      if (slice.indexOf('serviceName') !== -1 || slice.indexOf('basePrice') !== -1) {
        try { indexPayload(JSON.parse(slice), 0); } catch {}
      }
      i = end; // continue after this object
    }
  }

  // ---------------------------------------------------------------------------
  // Network hooks (installed at document-start) — capture vgen's data responses
  // ---------------------------------------------------------------------------
  function sameSiteVgen(url) {
    try {
      const h = new URL(url, location.href).host;
      return h === 'api.vgen.co' || h.endsWith('.vgen.co') || h === 'vgen.co';
    } catch { return false; }
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === 'function') {
    window.fetch = function (input, init) {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const p = nativeFetch.apply(this, arguments);
      if (sameSiteVgen(url)) {
        p.then((res) => {
          try {
            const ct = res.headers && res.headers.get('content-type');
            if (!ct || /json|text|component|plain/i.test(ct)) {
              res.clone().text().then(extractAndIndex).catch(() => {});
            }
          } catch {}
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
      this.__vgen = sameSiteVgen(url);
      return open.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      if (this.__vgen) {
        this.addEventListener('load', () => {
          try { extractAndIndex(this.responseText); } catch {}
        });
      }
      return send.apply(this, arguments);
    };
  }

  // Also index whatever the server already embedded in the page (Next.js data /
  // RSC flight scripts) — covers first paint before any client fetch happens.
  function indexInlineScripts() {
    const scripts = document.querySelectorAll(
      'script#__NEXT_DATA__, script[type="application/json"], script'
    );
    for (const s of scripts) {
      const t = s.textContent;
      if (t && (t.indexOf('serviceName') !== -1 || t.indexOf('basePrice') !== -1)) {
        extractAndIndex(t);
      }
    }
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

  // Find the service URL a card points at: an inner <a href*="/service/">, the
  // card itself if it's an anchor, or the nearest anchor ancestor.
  function cardServicePath(card) {
    let a = card.querySelector('a[href*="/service/"]');
    if (!a && card.matches('a[href]')) a = card;
    if (!a) a = card.closest('a[href*="/service/"]');
    if (!a) return null;
    try { return new URL(a.href, location.href).pathname; } catch { return null; }
  }

  function makeBadge(record) {
    const span = document.createElement('span');
    span.className = BADGE_CLASS;
    span.textContent = formatPrice(record.price, record.currency);
    // Inline styles so it works without any companion CSS; override via .vgen-min-price.
    span.style.cssText = [
      'position:absolute',
      'top:8px',
      'left:8px',
      'z-index:6',
      'padding:3px 9px',
      'border-radius:999px',
      'font:700 12px/1.4 inherit',
      'letter-spacing:.2px',
      'color:#fff',
      'background:rgba(0,0,0,.74)',
      'backdrop-filter:blur(2px)',
      '-webkit-backdrop-filter:blur(2px)',
      'box-shadow:0 1px 4px rgba(0,0,0,.25)',
      'pointer-events:none',
      'white-space:nowrap',
    ].join(';');
    return span;
  }

  function injectAll() {
    const cards = document.querySelectorAll(CARD_SELECTOR);
    for (const card of cards) {
      if (card.getAttribute(PROCESSED_ATTR) === 'done') continue;
      const path = cardServicePath(card);
      if (!path) continue;
      const record = lookup(path);
      if (!record) continue; // price not known yet — retry when more data arrives

      if (getComputedStyle(card).position === 'static') card.style.position = 'relative';
      card.appendChild(makeBadge(record));
      card.setAttribute(PROCESSED_ATTR, 'done');
    }
  }

  // Debounced scheduler shared by data ingest + DOM mutations.
  let raf = 0;
  scheduleInject = function () {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; injectAll(); });
  };

  function start() {
    indexInlineScripts();
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
