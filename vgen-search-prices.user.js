// ==UserScript==
// @name         VGen — Show Minimum Prices on Service Cards
// @namespace    https://github.com/fuwamocoanon/comf
// @version      2.4.0
// @description  Overlays each service's minimum ("from $X") starting price onto every ServiceGridCard across vgen.co (search, browse, profiles, shops). Reads prices from vgen's own commission-services API and matches them to cards by gallery-image ID.
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
  const PRICE_FIELDS = ['basePrice', 'startingPrice', 'minPrice', 'price', 'minPriceInDefaultCurrency'];
  const BADGE_CLASS = 'vgen-min-price';       // stable hook for styling from CSS
  const PROCESSED_ATTR = 'data-vgen-price';   // marks cards we've already handled

  const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  // A service image lives under /uploads/<userID>/<category>/.../<imageID>.<ext>.
  // Category is usually "services" but also "verified" (watermarked thumbnails),
  // etc. We take the last UUID (the image ID) as the join key, but skip
  // "avatars"/"banners" — those are creator-level and shared across cards.
  const UPLOAD_RE = new RegExp('uploads/' + UUID + '/([a-z0-9]+)/([^"\'\\s)]*)', 'gi');
  const UUID_G = new RegExp(UUID, 'gi');
  const SKIP_CATEGORIES = { avatars: 1, banners: 1, avatar: 1, banner: 1 };

  // Extract the service-image IDs referenced in a string (join key for cards).
  function serviceImageIds(str) {
    if (typeof str !== 'string') return [];
    const out = [];
    UPLOAD_RE.lastIndex = 0;
    let m;
    while ((m = UPLOAD_RE.exec(str))) {
      if (SKIP_CATEGORIES[m[1].toLowerCase()]) continue;
      const tail = m[2];
      UUID_G.lastIndex = 0;
      let u, last = null;
      while ((u = UUID_G.exec(tail))) last = u[0];       // filename UUID = image ID
      if (last) out.push(last.toLowerCase());
    }
    return out;
  }

  // ---------------------------------------------------------------------------
  // Price index — populated from vgen's API/data payloads
  // ---------------------------------------------------------------------------
  // record: { price (cents), currency }
  const byImage   = new Map(); // gallery imageID    -> record  (primary join for cards)
  const byYoutube = new Map(); // youtube videoID    -> record  (cards with video galleries)
  const byId      = new Map(); // serviceID          -> record
  const byPath    = new Map(); // "/user/service/slug" -> record (other page types)
  const bySlug    = new Map(); // "slug"             -> record

  // Extract YouTube video IDs from any URL/string (case-sensitive IDs).
  const YT_RES = [
    /youtu\.be\/([A-Za-z0-9_-]{11})/g,
    /youtube\.com\/(?:watch\?[^"'\s]*\bv=|embed\/|v\/|vi(?:_webp)?\/)([A-Za-z0-9_-]{11})/g,
    /[?&]v=([A-Za-z0-9_-]{11})/g,
  ];
  function youtubeIds(str) {
    if (typeof str !== 'string') return [];
    const out = [];
    for (const re of YT_RES) { re.lastIndex = 0; let m; while ((m = re.exec(str))) out.push(m[1]); }
    return out;
  }

  function slugify(s) {
    return String(s).toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  }
  // Gather every string value nested anywhere under a value.
  function collectStrings(v, out, depth) {
    depth = depth || 0;
    if (depth > 6 || v == null) return;
    if (typeof v === 'string') { out.push(v); return; }
    if (Array.isArray(v)) { for (const x of v) collectStrings(x, out, depth + 1); return; }
    if (typeof v === 'object') { for (const k in v) collectStrings(v[k], out, depth + 1); }
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
    const n = obj.user || obj.owner || obj.seller || obj.artist;
    if (n && typeof n === 'object' && typeof n.username === 'string') return n.username;
    if (typeof obj.sellerUsername === 'string') return obj.sellerUsername;
    return undefined;
  }

  // Deep-scan any parsed JSON for service-shaped objects and index them.
  function indexPayload(node, depth) {
    if (!node || depth > 10) return;
    if (Array.isArray(node)) { for (const it of node) indexPayload(it, depth + 1); return; }
    if (typeof node !== 'object') return;

    const price = firstDefined(node, PRICE_FIELDS);
    const looksLikeService = price !== undefined &&
      (node.serviceID || node.galleryItems || node.serviceName || node._id);
    if (looksLikeService) {
      const record = { price, currency: typeof node.currency === 'string' ? node.currency : 'USD' };
      if (node.serviceID) byId.set(String(node.serviceID), record);

      // Index EVERY service-image ID this service references -> its price. Cards
      // may show a gallery image, a video's poster/thumbnail, a header, or a
      // showcase image, and each is a distinct `services/<imageID>` URL. Scan
      // the whole service object for every such URL so the card's actual
      // thumbnail always has a match, whichever field it came from.
      const urls = [];
      collectStrings(node, urls);
      for (const u of urls) {
        for (const id of serviceImageIds(u)) byImage.set(id, record);
        // Some services use YouTube videos as gallery items; the card shows the
        // youtube thumbnail, so index the video ID too.
        for (const yid of youtubeIds(u)) byYoutube.set(yid, record);
      }
      // Also catch gallery items that store a bare YouTube ID on a typed entry.
      const gi = node.galleryItems || node.gallery || node.images;
      if (Array.isArray(gi)) {
        for (const it of gi) {
          if (!it || typeof it !== 'object') continue;
          const type = String(it.type || it.mediaType || '').toUpperCase();
          if (!type.includes('YOUTUBE') && !type.includes('VIDEO')) continue;
          for (const k of ['videoID', 'videoId', 'youtubeID', 'youtubeId', 'externalID', 'externalId', 'id', 'url', 'src']) {
            const v = it[k];
            if (typeof v !== 'string') continue;
            const ids = youtubeIds(v);
            if (ids.length) ids.forEach((y) => byYoutube.set(y, record));
            else if (/^[A-Za-z0-9_-]{11}$/.test(v)) byYoutube.set(v, record);
          }
        }
      }

      // Also index by name/path for pages that link straight to a service.
      const name = node.serviceName || node.name || node.title;
      if (typeof name === 'string' && name) {
        const slug = slugify(name);
        if (slug && !bySlug.has(slug)) bySlug.set(slug, record);
        const username = pickUsername(node);
        if (username && slug) byPath.set(`/${username}/service/${slug}`.toLowerCase(), record);
      }
    }

    for (const key in node) {
      const val = node[key];
      if (val && typeof val === 'object') indexPayload(val, depth + 1);
    }
  }

  let scheduleInject = () => {};

  function extractAndIndex(text) {
    if (!text) return;
    const before = byImage.size + byId.size + byPath.size + bySlug.size;
    try {
      indexPayload(JSON.parse(text), 0);
    } catch {
      if (text.indexOf('basePrice') !== -1 || text.indexOf('serviceID') !== -1) {
        scanEmbeddedObjects(text); // Next.js RSC "flight" streams etc.
      }
    }
    if (byImage.size + byId.size + byPath.size + bySlug.size > before) scheduleInject();
  }

  // Pull balanced {...} objects out of a mixed text blob (string-aware).
  function scanEmbeddedObjects(text) {
    const len = text.length;
    for (let i = 0; i < len; i++) {
      if (text[i] !== '{') continue;
      let depth = 0, inStr = false, esc = false, end = -1;
      for (let j = i; j < len; j++) {
        const c = text[j];
        if (inStr) {
          if (esc) esc = false; else if (c === '\\') esc = true; else if (c === '"') inStr = false;
        } else if (c === '"') inStr = true;
        else if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { end = j; break; } }
      }
      if (end === -1) break;
      const slice = text.slice(i, end + 1);
      if (slice.indexOf('basePrice') !== -1) { try { indexPayload(JSON.parse(slice), 0); } catch {} }
      i = end;
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
    window.fetch = function (input) {
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
    const open = XHR.prototype.open, send = XHR.prototype.send;
    XHR.prototype.open = function (m, url) { this.__vgen = sameSiteVgen(url); return open.apply(this, arguments); };
    XHR.prototype.send = function () {
      if (this.__vgen) {
        this.addEventListener('load', () => { try { extractAndIndex(this.responseText); } catch {} });
      }
      return send.apply(this, arguments);
    };
  }

  // Index data already embedded server-side (Next.js data / RSC flight scripts).
  function indexInlineScripts() {
    for (const s of document.querySelectorAll('script')) {
      const t = s.textContent;
      if (t && (t.indexOf('basePrice') !== -1)) extractAndIndex(t);
    }
  }

  // ---------------------------------------------------------------------------
  // Formatting
  // ---------------------------------------------------------------------------
  function formatPrice(cents, currency) {
    if (cents === 0) return 'Inquiry';
    try {
      return 'from ' + new Intl.NumberFormat(undefined, {
        style: 'currency', currency: currency || 'USD',
        minimumFractionDigits: 0, maximumFractionDigits: 2,
      }).format(cents / 100);
    } catch {
      return 'from ' + (cents / 100).toFixed(2) + ' ' + (currency || 'USD');
    }
  }

  // ---------------------------------------------------------------------------
  // DOM injection
  // ---------------------------------------------------------------------------
  // Collect the gallery image IDs referenced anywhere inside a card (img src,
  // srcset, background styles, data attributes — so we don't depend on markup).
  function cardImageIDs(card) {
    return serviceImageIds(card.innerHTML);
  }

  function cardServicePath(card) {
    let a = card.querySelector('a[href*="/service/"]');
    if (!a && card.matches('a[href*="/service/"]')) a = card;
    if (!a) a = card.closest && card.closest('a[href*="/service/"]');
    if (!a) return null;
    try { return new URL(a.href, location.href).pathname.toLowerCase(); } catch { return null; }
  }

  const UUID_RE = new RegExp(UUID, 'gi');
  // Any serviceID referenced in the card (e.g. a link ending in the serviceID).
  function cardServiceIds(card) {
    const ids = [];
    UUID_RE.lastIndex = 0;
    let m;
    while ((m = UUID_RE.exec(card.innerHTML))) ids.push(m[0].toLowerCase());
    return ids;
  }

  function priceForCard(card) {
    // 1) Match by gallery/media image ID against the commission-services data.
    for (const id of cardImageIDs(card)) {
      const rec = byImage.get(id);
      if (rec) return formatPrice(rec.price, rec.currency);
    }
    // 2) Match by YouTube video ID (cards whose gallery is YouTube videos).
    for (const yid of youtubeIds(card.innerHTML)) {
      const rec = byYoutube.get(yid);
      if (rec) return formatPrice(rec.price, rec.currency);
    }
    // 3) Match by any serviceID referenced in the card (service-detail links).
    for (const id of cardServiceIds(card)) {
      const rec = byId.get(id);
      if (rec) return formatPrice(rec.price, rec.currency);
    }
    // 4) A card that links straight to a service (other page layouts).
    const path = cardServicePath(card);
    if (path) {
      if (byPath.has(path)) { const r = byPath.get(path); return formatPrice(r.price, r.currency); }
      const sm = path.match(/\/service\/([^/?#]+)/);
      if (sm && bySlug.has(sm[1])) { const r = bySlug.get(sm[1]); return formatPrice(r.price, r.currency); }
    }
    // 5) A price already rendered in the card (e.g. detail pages).
    return readCardPrice(card);
  }

  const PRICE_RE = /(?:from\s*)?(?:US|A|C|NZ|S)?[$€£¥₩₱₹]\s?\d[\d.,]*/i;
  function readCardPrice(card) {
    for (const el of card.querySelectorAll('[class*="Text__StyledSpan"], [class*="Price"], span, p, b, strong')) {
      if (el.children.length) continue;
      const t = (el.textContent || '').trim();
      if (!t || t.length > 24) continue;
      const mm = t.match(PRICE_RE);
      if (mm) return mm[0].replace(/\s+/g, ' ').trim();
    }
    return null;
  }

  function makeBadge(text) {
    const span = document.createElement('span');
    span.className = BADGE_CLASS;
    span.textContent = text;
    span.style.cssText = [
      'position:absolute', 'top:8px', 'left:8px', 'z-index:6',
      'padding:3px 9px', 'border-radius:999px',
      'font:700 12px/1.4 inherit', 'letter-spacing:.2px',
      'color:#fff', 'background:rgba(0,0,0,.74)',
      'backdrop-filter:blur(2px)', '-webkit-backdrop-filter:blur(2px)',
      'box-shadow:0 1px 4px rgba(0,0,0,.25)', 'pointer-events:none', 'white-space:nowrap',
    ].join(';');
    return span;
  }

  function injectAll() {
    for (const card of document.querySelectorAll(CARD_SELECTOR)) {
      if (card.getAttribute(PROCESSED_ATTR) === 'done') continue;
      const text = priceForCard(card);
      if (!text) continue; // no data for this card yet — retry when more arrives
      if (getComputedStyle(card).position === 'static') card.style.position = 'relative';
      card.appendChild(makeBadge(text));
      card.setAttribute(PROCESSED_ATTR, 'done');
    }
  }

  let raf = 0;
  scheduleInject = function () {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; injectAll(); });
  };

  // Small debug surface for diagnosing misses from the console.
  window.__vgenPriceDebug = {
    counts: () => ({ byImage: byImage.size, byYoutube: byYoutube.size, byId: byId.size, byPath: byPath.size, bySlug: bySlug.size }),
    hasImage: (id) => byImage.has(String(id).toLowerCase()),
    hasYoutube: (id) => byYoutube.has(String(id)),
    hasId: (id) => byId.has(String(id).toLowerCase()),
    priceForCard,
  };

  function start() {
    indexInlineScripts();
    injectAll();
    // Watch node additions AND src/srcset/style changes, so lazy-loaded card
    // images (placeholder -> real URL) trigger a re-match.
    new MutationObserver(scheduleInject).observe(document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['src', 'srcset', 'style'],
    });
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
