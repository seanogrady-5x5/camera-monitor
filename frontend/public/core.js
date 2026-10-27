/* Shared data layer, helpers and clip player for the design-concept pages
   (/ ambient monitor, /mission, /dashboard). Exposes a single global: CM. */
(function () {
  const CM = {};
  const bust = p => `${p}${p.includes('?') ? '&' : '?'}_=${Date.now()}`;

  // ---------------------------------------------------------------- API

  CM.json = async path => {
    const r = await fetch(bust(path));
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      throw new Error(b.detail || r.statusText);
    }
    return r.json();
  };
  CM.config = () => CM.json('/api/config');
  CM.status = id => CM.json(`/api/status/${id}`).catch(() => null);
  CM.frameSrc = id => bust(`/api/frame/${id}`);
  CM.reviews = (status = 'new') => CM.json(`/api/reviews?status=${status}`).then(d => d.reviews).catch(() => []);
  CM.timelapses = () => CM.json('/api/timelapses').then(d => d.timelapses).catch(() => []);
  CM.retain = (kind, id, retention) =>
    fetch(`/api/${kind === 'timelapse' ? 'timelapses' : 'reviews'}/${id}/retention`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ retention }),
    }).catch(() => {});

  // Discard all undecided alerts for this frontend; resolves to the number cleared.
  CM.clearAlerts = () => fetch('/api/reviews/clear', { method: 'POST' })
    .then(r => (r.ok ? r.json() : { cleared: 0 })).then(d => d.cleared).catch(() => 0);

  // Load an image URL; resolves when decoded so callers can crossfade cleanly.
  CM.loadImage = src => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });

  // ---------------------------------------------------------------- Stats

  CM.stats = async () => {
    try {
      const { stats } = await CM.json('/api/stats');
      return stats.map(s => {
        const n = Number(s.state);
        const ok = s.state != null && s.state !== '' && s.state !== 'unavailable' && s.state !== 'unknown' && !Number.isNaN(n);
        return { ...s, value: ok ? n : null };
      });
    } catch { return null; }
  };

  CM.fmt = s => (s == null || s.value == null) ? '—' : (s.decimals != null ? s.value.toFixed(s.decimals) : String(s.value));

  // Classify a stat by its unit/label so concepts can pick the right widget.
  CM.kind = s => {
    const u = (s.unit || '').toLowerCase();
    const l = (s.label || '').toLowerCase();
    if (u.includes('°')) return 'temp';
    if (u === '%') return 'pct';
    if (u === 'kw' || u === 'w') {
      if (l.includes('net') || l.includes('batt')) return 'net';
      if (l.includes('load')) return 'load';
      return 'power';
    }
    return 'other';
  };
  CM.find = (stats, kind) => (stats || []).find(s => CM.kind(s) === kind);
  CM.all = (stats, kind) => (stats || []).filter(s => CM.kind(s) === kind);

  // 24h history per stat (one point/minute), kept in this browser for sparklines.
  const HKEY = 'cm-history-v1';
  let hist = {};
  try { hist = JSON.parse(localStorage.getItem(HKEY) || '{}'); } catch { hist = {}; }
  CM.record = stats => {
    if (!stats) return;
    const now = Date.now();
    for (const s of stats) {
      if (s.value == null) continue;
      const a = hist[s.label] || (hist[s.label] = []);
      const last = a[a.length - 1];
      if (last && now - last[0] < 60000) { last[1] = s.value; continue; }
      a.push([now, s.value]);
      while (a.length && a[0][0] < now - 24 * 3600e3) a.shift();
    }
    try { localStorage.setItem(HKEY, JSON.stringify(hist)); } catch { /* storage unavailable */ }
  };
  CM.series = label => hist[label] || [];
  CM.seriesMax = label => CM.series(label).reduce((m, p) => Math.max(m, Math.abs(p[1])), 0);

  CM.sparkPath = (pts, w, h, pad = 2) => {
    if (pts.length < 2) return '';
    const vs = pts.map(p => p[1]);
    let lo = Math.min(...vs), hi = Math.max(...vs);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const t0 = pts[0][0], span = (pts[pts.length - 1][0] - t0) || 1;
    return pts.map((p, i) => {
      const x = pad + (w - 2 * pad) * ((p[0] - t0) / span);
      const y = pad + (h - 2 * pad) * (1 - (p[1] - lo) / (hi - lo));
      return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join('');
  };
  CM.spark = (label, w = 120, h = 32, cls = 'spark') => {
    const d = CM.sparkPath(CM.series(label), w, h);
    return `<svg class="${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${d ? `<path d="${d}"/>` : ''}</svg>`;
  };

  // ---------------------------------------------------------------- Time

  CM.rel = ms => {
    if (!ms) return '';
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return 'just now';
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' });
  };
  // green = fresh, amber = no capture for 5 min, red = failing, off = unknown
  CM.fresh = st => {
    const t = st && st.last_fetched_utc ? Date.parse(st.last_fetched_utc) : null;
    if (st && st.last_error) return { state: 'bad', t };
    if (!t) return { state: 'off', t };
    return { state: Date.now() - t > 300000 ? 'warn' : 'ok', t };
  };
  CM.time = () => new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  CM.date = () => new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  CM.shortWhen = ms => new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

  CM.reviewMeta = r => {
    const sev = r.severity ? r.severity[0].toUpperCase() + r.severity.slice(1) : '';
    return [sev, (r.objects || []).join(', '), r.duration != null ? `${r.duration}s` : '',
      r.start_time ? CM.shortWhen(r.start_time * 1000) : ''].filter(Boolean).join(' · ');
  };
  CM.timelapseMeta = t => {
    const kept = t.retention === 'keep_forever' ? 'Kept ∞' : t.retention === 'keep7' ? 'Kept 7d' : '';
    return ['Timelapse', t.frame_count != null ? `${t.frame_count} frames` : '',
      t.covers_end_utc ? `to ${CM.shortWhen(Date.parse(t.covers_end_utc))}` : '', kept].filter(Boolean).join(' · ');
  };

  // ---------------------------------------------------------------- Icons

  CM.icon = {
    camera: '<path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2"/>',
    grid: '<rect width="7" height="7" x="3" y="3" rx="1.5"/><rect width="7" height="7" x="14" y="3" rx="1.5"/><rect width="7" height="7" x="3" y="14" rx="1.5"/><rect width="7" height="7" x="14" y="14" rx="1.5"/>',
    bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    bookmark: '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    film: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M17 3v18"/><path d="M3 7.5h4"/><path d="M17 7.5h4"/><path d="M3 12h18"/><path d="M3 16.5h4"/><path d="M17 16.5h4"/>',
    sweep: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    infinity: '<path d="M18.18 8c5.1 0 5.1 8 0 8-5.1 0-7.13-8-12.74-8-4.58 0-4.58 8 0 8 5.61 0 7.64-8 12.74-8z"/>',
    skip: '<path d="m5 4 10 8-10 8V4z"/><path d="M19 5v14"/>',
    prev: '<path d="m15 18-6-6 6-6"/>',
    next: '<path d="m9 18 6-6-6-6"/>',
    close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
    expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    collapse: '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/>',
    pause: '<rect width="4" height="16" x="6" y="4" rx="1"/><rect width="4" height="16" x="14" y="4" rx="1"/>',
    play: '<path d="m6 4 14 8-14 8V4z"/>',
    battery: '<rect width="16" height="10" x="2" y="7" rx="2"/><path d="M22 11v2"/>',
    zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    thermo: '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  };
  CM.svg = (name, cls = '') =>
    `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${CM.icon[name] || name}</svg>`;

  CM.esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---------------------------------------------------------------- Fullscreen

  CM.fullscreenButton = btn => {
    if (!document.documentElement.requestFullscreen) { btn.style.display = 'none'; return; }
    const paint = () => {
      const on = Boolean(document.fullscreenElement);
      btn.innerHTML = CM.svg(on ? 'collapse' : 'expand');
      btn.title = on ? 'Exit full screen' : 'Full screen';
    };
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen().catch(() => {});
    });
    document.addEventListener('fullscreenchange', paint);
    paint();
  };

  // ---------------------------------------------------------------- Clip player (modal)

  const PLAYER_CSS = `
  .cmp{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.94);display:flex;flex-direction:column;color:#eee;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;animation:cmp-in .25s ease}
  @keyframes cmp-in{from{opacity:0}to{opacity:1}}
  .cmp-top{display:flex;align-items:center;gap:14px;padding:14px 18px}
  .cmp-title{flex:1;min-width:0}
  .cmp-title b{display:block;font-size:1.15rem;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cmp-title span{display:block;font-size:.8rem;color:#9a9aa0;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cmp-count{font-size:.78rem;font-weight:600;color:#aaa;padding:3px 10px;border-radius:99px;background:rgba(255,255,255,.08)}
  .cmp-ib{width:44px;height:44px;border:0;border-radius:12px;background:rgba(255,255,255,.07);color:#ddd;display:inline-flex;
          align-items:center;justify-content:center;cursor:pointer}
  .cmp-ib:hover{background:rgba(255,255,255,.14)} .cmp-ib svg{width:20px;height:20px}
  .cmp-stage{position:relative;flex:1;min-height:0}
  .cmp-stage video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;opacity:0}
  .cmp-stage video.on{opacity:1}
  .cmp-nav{position:absolute;top:50%;transform:translateY(-50%);z-index:2}
  .cmp-nav.l{left:14px}.cmp-nav.r{right:14px}
  .cmp-prog{height:3px;background:rgba(255,255,255,.08)} .cmp-prog div{height:100%;width:0;background:#5b8cff;transition:width .1s linear}
  .cmp-actions{display:flex;justify-content:center;gap:10px;padding:16px}
  .cmp-actions button{height:50px;display:inline-flex;align-items:center;gap:8px;padding:0 20px;border:0;border-radius:14px;
       background:rgba(255,255,255,.07);color:#eee;font:inherit;font-size:.95rem;font-weight:600;cursor:pointer}
  .cmp-actions button svg{width:18px;height:18px}
  .cmp-actions button:hover{background:rgba(255,255,255,.13)}
  .cmp-actions .discard{color:#ff8a80}.cmp-actions .keep{color:#7ee2a0}
  .cmp-actions button:disabled{opacity:.45}
  .cmp-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#888}
  .cmp-empty svg{width:44px;height:44px;stroke-width:1.5}
  .cmp-sp{display:none}
  /* Inline mode: mounted inside a host element (e.g. a hero panel) */
  .cmp.inline{position:absolute;inset:0;z-index:6;background:#000}
  .cmp.inline .cmp-sp{display:block;flex:1}
  .cmp.inline .cmp-top{position:absolute;top:0;left:0;right:0;z-index:3;padding:14px;gap:10px;pointer-events:none}
  .cmp.inline .cmp-top>*{pointer-events:auto}
  .cmp.inline .cmp-title{flex:0 1 auto;min-width:0;max-width:62%;padding:9px 14px;border-radius:14px;
       background:rgba(8,9,12,.62);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.06)}
  .cmp.inline .cmp-title b{font-size:1.05rem}
  .cmp.inline .cmp-count,.cmp.inline .cmp-top .cmp-ib{background:rgba(8,9,12,.62);backdrop-filter:blur(12px);
       -webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.06)}
  .cmp.inline .cmp-count{height:44px;display:inline-flex;align-items:center;padding:0 14px;border-radius:12px}
  .cmp.inline .cmp-stage{position:absolute;inset:0}
  .cmp.inline .cmp-nav{background:rgba(8,9,12,.5)}
  .cmp.inline .cmp-prog{position:absolute;left:0;right:0;bottom:0;z-index:3}
  .cmp.inline .cmp-actions{position:absolute;bottom:18px;left:50%;transform:translateX(-50%);z-index:3;padding:6px;gap:8px;
       border-radius:18px;background:rgba(8,9,12,.62);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
       border:1px solid rgba(255,255,255,.06)}
  .cmp.inline .cmp-actions button{height:46px;padding:0 14px}
  .cmp-actions button{white-space:nowrap}`;

  let cssInjected = false;
  const injectCss = () => {
    if (cssInjected) return;
    const st = document.createElement('style');
    st.textContent = PLAYER_CSS;
    document.head.appendChild(st);
    cssInjected = true;
  };

  /* Open a full-screen double-buffered player.
     kind: 'new' | 'retained' | 'timelapse'. onChange() is called after a retention
     decision so the host page can refresh its lists. Pass `mount` (an element) to
     play inline inside it instead of as a full-screen overlay; onShow(item) fires
     for each clip shown and onClose() when the player closes. */
  CM.openPlayer = ({ items, index = 0, kind = 'new', allowDownload = false, onChange = () => {},
                     mount = null, onShow = () => {}, onClose = () => {} }) => {
    injectCss();
    const list = items.slice();
    const isTl = kind === 'timelapse';
    const actions = kind === 'new' ? ['discard', 'keep7', 'keep_forever', 'skip']
      : kind === 'retained' ? ['discard', 'keep_forever', 'skip']
      : ['keep7', 'keep_forever', 'skip'];
    const LABEL = { discard: ['Discard', 'trash', 'discard'], keep7: ['Keep 7 days', 'clock', 'keep'],
      keep_forever: ['Keep forever', 'infinity', 'keep'], skip: ['Skip', 'skip', ''] };

    const el = document.createElement('div');
    el.className = mount ? 'cmp inline' : 'cmp';
    el.innerHTML = `
      <div class="cmp-top">
        <div class="cmp-title"><b></b><span></span></div>
        <span class="cmp-sp"></span>
        <span class="cmp-count"></span>
        ${isTl && allowDownload ? `<button class="cmp-ib" data-a="dl" title="Download">${CM.svg('download')}</button>` : ''}
        <button class="cmp-ib" data-a="close" title="${mount ? 'Back to live' : 'Close'}">${CM.svg('close')}</button>
      </div>
      <div class="cmp-stage">
        <video muted playsinline preload="auto"></video><video muted playsinline preload="auto"></video>
        <button class="cmp-ib cmp-nav l" data-a="prev" title="Previous">${CM.svg('prev')}</button>
        <button class="cmp-ib cmp-nav r" data-a="next" title="Next">${CM.svg('next')}</button>
      </div>
      <div class="cmp-prog"><div></div></div>
      <div class="cmp-actions">${actions.map(a =>
        `<button class="${LABEL[a][2]}" data-r="${a}">${CM.svg(LABEL[a][1])}${LABEL[a][0]}</button>`).join('')}</div>`;
    (mount || document.body).appendChild(el);

    const vids = [...el.querySelectorAll('video')];
    const prog = el.querySelector('.cmp-prog div');
    let act = 0, pos = index;
    const A = () => vids[act], I = () => vids[1 - act];

    const preload = (v, i) => {
      if (i >= 0 && i < list.length) {
        if (v.dataset.i !== String(i)) { v.src = list[i].clip_url; v.dataset.i = String(i); v.load(); }
      } else { v.removeAttribute('src'); v.dataset.i = ''; }
    };
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      vids.forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
      el.remove();
      document.removeEventListener('keydown', onKey);
      onClose();
    };
    const empty = () => {
      vids.forEach(v => { v.pause(); v.classList.remove('on'); });
      el.querySelector('.cmp-stage').insertAdjacentHTML('beforeend',
        `<div class="cmp-empty">${CM.svg('check')}<div>All done</div></div>`);
      el.querySelector('.cmp-actions').style.visibility = 'hidden';
      setTimeout(close, 1200);
    };
    const show = (i, seamless = false) => {
      if (!list.length) { empty(); return; }
      i = ((i % list.length) + list.length) % list.length;   // loop
      pos = i;
      if (seamless && I().dataset.i === String(i)) act = 1 - act; else preload(A(), i);
      vids.forEach((v, n) => v.classList.toggle('on', n === act));
      A().play().catch(() => {});
      prog.style.width = '0%';
      const it = list[i];
      el.querySelector('.cmp-title b').textContent = it.camera;
      el.querySelector('.cmp-title span').textContent = isTl ? CM.timelapseMeta(it) : CM.reviewMeta(it);
      el.querySelector('.cmp-count').textContent = `${i + 1} / ${list.length}`;
      preload(I(), (i + 1) % list.length);
      onShow(it);
    };
    vids.forEach(v => {
      v.addEventListener('timeupdate', e => {
        if (e.target !== A()) return;
        const d = e.target.duration;
        prog.style.width = d && isFinite(d) ? `${(e.target.currentTime / d) * 100}%` : '0%';
      });
      v.addEventListener('ended', e => { if (e.target === A()) show(pos + 1, true); });
    });
    el.addEventListener('click', async e => {
      const b = e.target.closest('button');
      if (!b) return;
      const a = b.dataset.a, r = b.dataset.r;
      if (a === 'close') return close();
      if (a === 'prev') return show(pos - 1);
      if (a === 'next') return show(pos + 1);
      if (a === 'dl') { const x = document.createElement('a'); x.href = `${list[pos].clip_url}?download=1`; x.click(); return; }
      if (!r) return;
      if (r === 'skip') return show(pos + 1);
      el.querySelectorAll('.cmp-actions button').forEach(x => { x.disabled = true; });
      await CM.retain(isTl ? 'timelapse' : 'review', list[pos].id, r);
      el.querySelectorAll('.cmp-actions button').forEach(x => { x.disabled = false; });
      if (isTl) { list[pos].retention = r; show(pos + 1); }
      else { list.splice(pos, 1); vids.forEach(v => { v.dataset.i = ''; }); show(Math.min(pos, list.length - 1)); }
      onChange();
    });
    const onKey = e => {
      if (e.key === 'Escape') close();
      else if (e.key === 'ArrowRight') show(pos + 1);
      else if (e.key === 'ArrowLeft') show(pos - 1);
    };
    document.addEventListener('keydown', onKey);
    show(pos);
    return { close };
  };

  window.CM = CM;
})();
