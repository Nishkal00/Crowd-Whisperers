'use strict';
/* Poltergeist control room: reads the live state stream and draws it. */

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const SVGNS = 'http://www.w3.org/2000/svg';
const LEVEL = { safe: 'Safe', warning: 'Warning', critical: 'Critical', nodata: 'No data', stale: 'No data' };
const OPEN = ['new', 'dispatching', 'accepted', 'unanswered'];
const LANG_NAME = { en: 'English', hi: 'हिन्दी', kn: 'ಕನ್ನಡ' };
const LANG_WORD = { en: 'English', hi: 'Hindi', kn: 'Kannada' };

const S = {
  venue: null, state: null, offset: 0, camera: null,
  sound: false, playing: false, focus: null,
  seen: new Set(), first: true, cache: {},
  drawing: false, pts: [],
};

const now = () => Date.now() / 1000 + S.offset;
const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
function dur(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return m ? `${m} min ${String(s % 60).padStart(2, '0')} s` : `${s} s`;
}
function once(section, key, fn) {
  if (S.cache[section] === key) return;
  S.cache[section] = key;
  fn();
}
function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
const centroid = poly => [poly.reduce((a, p) => a + p[0], 0) / poly.length, poly.reduce((a, p) => a + p[1], 0) / poly.length];
const postName = id => (S.venue.posts.find(p => p.id === id) || {}).name || id;

/* ---------------- start ---------------- */
async function init() {
  S.venue = await (await fetch('/api/venue')).json();
  S.camera = Object.keys(S.venue.cameras || { cam1: {} })[0];
  $('#venueName').textContent = S.venue.venue || '';
  $('#camName').textContent = (S.venue.cameras?.[S.camera]?.name) || S.camera;
  buildOverlay();
  buildCams();
  buildMap();
  buildSend();
  bindUI();
  startVideo();
  connect();
  setInterval(tick, 500);
}

function connect() {
  const es = new EventSource('/api/stream');
  es.onmessage = e => {
    const st = JSON.parse(e.data);
    S.offset = st.now - Date.now() / 1000;
    S.state = st;
    setLink(true);
    render(st);
  };
  es.onerror = () => setLink(false);
}

function setLink(up) {
  const el = $('#link');
  el.textContent = up ? 'Live' : 'Reconnecting to the server';
  el.classList.toggle('link-down', !up);
}

function startVideo() {
  const img = $('#feedImg');
  img.onerror = () => setTimeout(() => { img.src = `/api/video/${S.camera}?t=${Date.now()}`; }, 2000);
  img.src = `/api/video/${S.camera}`;
}

/* ---------------- render ---------------- */
function render(st) {
  // the venue was switched (from this page or another): the page is built for the old one, so reload
  if (!S.venueKey) S.venueKey = st.venue_key;
  else if (st.venue_key && st.venue_key !== S.venueKey) { location.reload(); return; }
  document.querySelectorAll('#venueSwitch [data-venue]').forEach(b => b.setAttribute('aria-pressed', b.dataset.venue === st.venue_key));
  // header
  $('#openCount').textContent = st.stats.open;
  $('#openVital').classList.toggle('alarm', st.stats.open > 0);
  $('#avgResp').textContent = st.stats.avg_response_s == null ? '–' : dur(st.stats.avg_response_s);

  // camera
  const cam = st.cameras[S.camera];
  const live = cam && now() - cam.last_frame < 5;
  $('#waiting').hidden = !!live;
  if (cam && cam.size) $('#feed').style.aspectRatio = `${cam.size[0]} / ${cam.size[1]}`;

  const zones = Object.fromEntries(st.zones.map(z => [z.id, z]));
  renderOverlay(zones);
  renderRings(st.incidents);
  renderZones(st.zones);
  renderAlerts(st);
  renderMap(zones, st.incidents);
  renderPA(st.pa, st.voice);
  const auto = $('#autoBtn');
  auto.textContent = st.auto_alerts ? 'Automatic alerts: on' : 'Automatic alerts: off';
  auto.setAttribute('aria-pressed', !!st.auto_alerts);
  auto.classList.toggle('warn-off', !st.auto_alerts);
  markCams();
  renderSummary(st);
  renderHeat(zones);
  renderSend(st);

  const unk = $('#unknownZones');
  unk.hidden = !st.unknown_zones.length;
  unk.textContent = st.unknown_zones.length
    ? `Getting data for ${st.unknown_zones.map(z => `"${z}"`).join(', ')}, which isn't in venue.json. Add it there or fix the zone name.` : '';

  // new alerts
  for (const inc of st.incidents) {
    if (!OPEN.includes(inc.status) || S.seen.has(inc.id)) continue;
    S.seen.add(inc.id);
    if (!S.first) onNewAlert(inc);
  }
  S.first = false;
}

/* camera overlay: zones drawn on top of the video */
/* the outline of a zone as seen by the current camera: its own camera, or a close-up cut from it */
function polygonFor(id) {
  const z = S.venue.zones[id];
  if (z.camera === S.camera && z.polygon) return z.polygon;
  return S.venue.zone_views?.[id]?.[S.camera] || null;
}

function buildOverlay() {
  const ov = $('#overlay');
  const labels = $('#labels');
  ov.innerHTML = '';
  labels.innerHTML = '';
  for (const [id, z] of Object.entries(S.venue.zones)) {
    const poly = polygonFor(id);
    if (!poly) continue;
    ov.appendChild(svg('polygon', { id: `zp-${id}`, class: 'zpoly nodata', points: poly.map(p => p.join(',')).join(' ') }));
    const [x, y] = (z.camera === S.camera && z.label_at) || centroid(poly);
    const lab = document.createElement('div');
    lab.className = 'zlabel nodata';
    lab.id = `zl-${id}`;
    lab.style.left = `${x * 100}%`;
    lab.style.top = `${y * 100}%`;
    lab.innerHTML = `<span>${esc(z.name || id)}</span><span class="d"></span>`;
    labels.appendChild(lab);
  }
  ov.appendChild(svg('polyline', { id: 'drawLine', fill: 'none', stroke: '#FDF9E0', 'stroke-width': 2.5,
    'stroke-dasharray': '6 5', 'vector-effect': 'non-scaling-stroke', points: '' }));
}

function renderOverlay(zones) {
  for (const id of Object.keys(S.venue.zones)) {
    const z = zones[id];
    const poly = $(`#zp-${id}`);
    const lab = $(`#zl-${id}`);
    if (!poly || !z) continue;
    poly.setAttribute('class', `zpoly ${z.level}`);
    lab.className = `zlabel ${z.level}`;
    lab.querySelector('.d').textContent = z.density == null || z.level === 'stale' ? '' : `${z.density.toFixed(1)}/m²`;
  }
}

function renderRings(incidents) {
  const live = incidents.filter(i => OPEN.includes(i.status) && i.bbox && i.camera === S.camera);
  once('rings', JSON.stringify([S.camera, live.map(i => [i.id, i.bbox, i.label])]), () => {
    $('#rings').innerHTML = live.map(i => {
      const [x, y, w, h] = i.bbox;
      const size = Math.max(w, h * 9 / 16) * 100 * 1.7 + 3;
      return `<div class="ring" style="left:${(x + w / 2) * 100}%;top:${(y + h / 2) * 100}%;width:${size}%;aspect-ratio:1">
        <span>${esc(i.label)}</span></div>`;
    }).join('');
  });
}

/* zones list with the 1 m² squares */
function sqm(density) {
  const n = density == null ? 0 : density;
  const full = Math.floor(n);
  const frac = n - full;
  const count = Math.min(12, full + (frac > 0.05 ? 1 : 0));
  // sunflower layout: evenly packed, gets tight as density rises
  const c = count > 1 ? 34 / Math.sqrt(count - 0.5) : 0;
  let dots = '';
  for (let i = 0; i < count; i++) {
    const r = c * Math.sqrt(i + 0.5);
    const a = i * 2.39996;
    const op = i === count - 1 && frac > 0.05 && i >= full ? Math.max(0.3, frac) : 1;
    dots += `<circle cx="${(50 + r * Math.cos(a)).toFixed(1)}" cy="${(50 + r * Math.sin(a)).toFixed(1)}" r="11" opacity="${op.toFixed(2)}"/>`;
  }
  return `<svg class="sqm" viewBox="0 0 100 100" aria-hidden="true"><rect x="2" y="2" width="96" height="96" rx="4"/>${dots}</svg>`;
}

function renderZones(list) {
  const key = JSON.stringify(list.map(z => [z.id, z.level, z.density == null ? null : z.density.toFixed(1), z.count, z.trend, z.minutes_to_critical, z.surge]));
  once('zones', key, () => {
    $('#zoneList').innerHTML = list.map(z => {
      const name = S.venue.zones[z.id]?.name || z.id;
      const meta = [];
      if (z.level === 'stale') meta.push('<span>No data in the last few seconds</span>');
      else if (z.level === 'nodata') meta.push('<span>Waiting for data</span>');
      else {
        if (z.count != null) meta.push(`<span>${z.count.toLocaleString()} people</span>`);
        meta.push(`<span>${z.trend}</span>`);
        if (z.minutes_to_critical != null && z.level !== 'critical') {
          const m = z.minutes_to_critical;
          meta.push(`<span class="eta">${m < 1 ? 'critical in under a minute' : `critical in ${Math.ceil(m)} min`}</span>`);
        }
        if (z.surge) meta.push('<span class="flag">surge</span>');
      }
      const num = z.density == null || z.level === 'stale' ? '–' : z.density.toFixed(1);
      return `<li class="zone ${z.level}">
        ${sqm(z.level === 'stale' ? null : z.density)}
        <div><div class="zname">${esc(name)}</div><div class="zmeta">${meta.join('')}</div></div>
        <div class="zden"><span class="num">${num}</span><span class="unit">people per m²</span><span class="pill">${LEVEL[z.level]}</span></div>
      </li>`;
    }).join('');
  });
}

/* alerts: one big card for the alert that needs attention, the rest as rows */
function renderAlerts(st) {
  const open = st.incidents.filter(i => OPEN.includes(i.status));
  const closed = st.incidents.filter(i => !OPEN.includes(i.status)).slice(0, 3);
  if (!open.find(i => i.id === S.focus)) S.focus = open.length ? open[0].id : null;
  const main = open.find(i => i.id === S.focus);
  const tg = st.telegram;
  const key = JSON.stringify([S.focus, open.map(i => [i.id, i.status, i.current_post, i.timeline.length, i.label, i.has_snapshot]),
    closed.map(i => [i.id, i.status]), tg.state, tg.detail]);
  once('alerts', key, () => {
    let html = '';
    if (!main) {
      html += `<div class="calm"><b>No open alerts</b>
        <p>When a zone turns critical or the cameras spot an incident, it appears here and goes to the nearest help post.</p>
        <p>${esc(tgLine(tg))}</p></div>`;
    } else {
      html += alertCard(main, st);
    }
    const rest = open.filter(i => i !== main);
    if (rest.length || closed.length) {
      html += '<div class="others">';
      for (const i of rest) {
        html += `<button class="other open" data-focus="${i.id}"><span class="t">${esc(i.label)} at ${esc(i.zone_name)}</span>
          <span class="s">${statusWord(i)}</span></button>`;
      }
      if (closed.length) html += '<p class="earlier">Earlier</p>';
      for (const i of closed) {
        const how = i.status === 'cleared' ? 'cleared' : 'resolved';
        html += `<div class="other done"><span class="t">${esc(i.label)} at ${esc(i.zone_name)}</span>
          <span class="s">${how} after ${dur((i.closed_at || i.opened_at) - i.opened_at)}</span></div>`;
      }
      html += '</div>';
    }
    $('#alertBody').innerHTML = html;
    if (main && S.fresh === main.id) $('#alertBody .alert')?.classList.add('fresh');
  });
}

function tgLine(tg) {
  if (tg.state === 'connected') return `Telegram: ${tg.detail}.`;
  if (tg.state === 'error') return `Telegram problem: ${tg.detail}`;
  if (tg.state === 'starting') return 'Telegram: connecting.';
  return 'Telegram isn\'t set up, so the control room accepts alerts for the posts.';
}

function statusWord(i) {
  if (i.status === 'accepted') return `${postName(i.accepted_by)} on the way`;
  if (i.status === 'unanswered') return 'nobody accepted';
  if (i.status === 'dispatching') return `waiting for ${postName(i.current_post)}`;
  return 'new';
}

function alertCard(i, st) {
  const taken = i.status === 'accepted';
  const steps = i.timeline.map(t => {
    const bad = t.kind === 'unanswered' ? ' bad' : '';
    return `<li class="done${bad}">${esc(t.text)}<time>${clock(t.t)}</time></li>`;
  });
  const cur = i.chain.find(c => c.id === i.current_post);
  if (i.status === 'dispatching' && cur) {
    const deadline = i.sent_at + st.escalate_after_s;
    const via = cur.telegram ? 'on Telegram' : 'by phone or radio';
    steps.push(`<li class="now">Waiting for ${esc(cur.name)} to accept ${via}
      <span class="countdown" data-deadline="${deadline}"></span></li>`);
  }
  if (i.status !== 'accepted') steps.push('<li class="todo">Accepted</li>');
  steps.push('<li class="todo">Resolved</li>');

  const zfacts = `<span data-zone-facts="${esc(i.zone)}" data-type="${esc(i.type)}"></span>`;
  const note = i.note ? ` ${esc(i.note)}.` : '';
  const actions = [];
  if (i.status === 'dispatching' && cur) actions.push(`<button class="btn solid" data-act="accept" data-id="${i.id}" data-post="${esc(cur.id)}">Accept for ${esc(cur.name)}</button>`);
  if (i.status !== 'accepted') actions.push(`<button class="btn" data-act="next" data-id="${i.id}">${i.status === 'unanswered' ? 'Try the posts again' : 'Send to next post'}</button>`);
  actions.push(`<button class="btn ${taken ? 'solid' : 'quiet'}" data-act="resolve" data-id="${i.id}">Mark resolved</button>`);

  return `<article class="alert ${taken ? 'taken' : ''}">
    <div class="alert-head"><h3>${esc(i.label)} at ${esc(i.zone_name)}</h3><span class="age" data-since="${i.opened_at}"></span></div>
    <p class="facts">${zfacts}${note}</p>
    <div class="alert-body">
      ${i.has_snapshot ? `<img class="snap" src="/api/incidents/${i.id}/snapshot.jpg" alt="Camera snapshot when the alert opened">` : '<div class="snap"></div>'}
      <ol class="steps">${steps.join('')}</ol>
    </div>
    <div class="actions">${actions.join('')}</div>
    <p class="channel">${esc(tgLine(st.telegram))}</p>
  </article>`;
}

/* venue map: zones, help posts, and the route of the current dispatch */
function buildMap() {
  const map = $('#map');
  const pts = [];
  for (const z of Object.values(S.venue.zones)) (z.map || []).forEach(p => pts.push(p));
  for (const p of S.venue.posts || []) if (p.pos) pts.push(p.pos);
  for (const l of S.venue.map_lines || []) l.forEach(p => pts.push(p));
  if (!pts.length) { map.parentElement.hidden = true; return; }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const pad = 8;
  const [x0, y0] = [Math.min(...xs) - pad, Math.min(...ys) - pad];
  const [w, h] = [Math.max(...xs) - x0 + pad + 30, Math.max(...ys) - y0 + pad];  // room for post names on the right
  map.setAttribute('viewBox', `${x0} ${y0} ${w} ${h}`);
  S.mapBox = [x0, y0, w, h];
  for (const l of S.venue.map_lines || []) map.appendChild(svg('polyline', { class: 'mline', points: l.map(p => p.join(',')).join(' '), fill: 'none' }));
  for (const [id, z] of Object.entries(S.venue.zones)) {
    if (!z.map) continue;
    map.appendChild(svg('polygon', { id: `mz-${id}`, class: 'mzone nodata', points: z.map.map(p => p.join(',')).join(' ') }));
  }
  for (const [id, z] of Object.entries(S.venue.zones)) {
    if (!z.map) continue;
    const xsZ = z.map.map(p => p[0]), ysZ = z.map.map(p => p[1]);
    const tall = Math.max(...ysZ) - Math.min(...ysZ) > Math.max(...xsZ) - Math.min(...xsZ);
    const t = svg('text', { class: 'mlabel', x: Math.min(...xsZ) + 1.5, y: tall ? Math.max(...ysZ) - 2 : Math.min(...ysZ) + 4.2 });
    if (tall) { t.setAttribute('transform', `rotate(-90 ${Math.min(...xsZ) + 4.6} ${Math.max(...ysZ) - 2})`); t.setAttribute('x', Math.min(...xsZ) + 4.6); }
    t.textContent = z.name;
    map.appendChild(t);
  }
  map.appendChild(svg('g', { id: 'routes' }));
  for (const p of S.venue.posts || []) {
    if (!p.pos) continue;
    const g = svg('g', { class: 'mpost', id: `mp-${p.id}` });
    g.appendChild(svg('circle', { cx: p.pos[0], cy: p.pos[1], r: 1.8 }));
    const t = svg('text', { x: p.pos[0] + 2.8, y: p.pos[1] + 1 });
    t.textContent = p.name;
    g.appendChild(t);
    map.appendChild(g);
  }
}

function renderMap(zones, incidents) {
  for (const id of Object.keys(S.venue.zones)) {
    const el = $(`#mz-${id}`);
    if (el && zones[id]) el.setAttribute('class', `mzone ${zones[id].level}`);
  }
  const live = incidents.filter(i => ['dispatching', 'accepted'].includes(i.status) && i.current_post);
  once('routes', JSON.stringify(live.map(i => [i.id, i.current_post, i.status])), () => {
    const g = $('#routes');
    if (!g) return;
    g.innerHTML = '';
    document.querySelectorAll('.mpost.on').forEach(e => e.classList.remove('on'));
    for (const i of live) {
      const z = S.venue.zones[i.zone];
      const p = S.venue.posts.find(p => p.id === i.current_post);
      if (!z?.map || !p?.pos) continue;
      const [cx, cy] = centroid(z.map);
      const taken = i.status === 'accepted';
      g.appendChild(svg('line', { class: `mroute ${taken ? 'taken' : ''}`, x1: cx, y1: cy, x2: p.pos[0], y2: p.pos[1] }));
      const c = i.chain.find(c => c.id === p.id);
      if (c && c.distance_m != null) {
        const t = svg('text', { class: 'mdist', x: (cx + p.pos[0]) / 2 + 1.5, y: (cy + p.pos[1]) / 2 });
        t.textContent = `${c.distance_m} m`;
        g.appendChild(t);
      }
      $(`#mp-${p.id}`)?.classList.add('on');
    }
  });
}

/* PA panel */
function renderPA(pa, voice) {
  once('pa', JSON.stringify(pa ? [pa.ref, pa.title, pa.texts?.en] : null), () => {
    const body = $('#paBody');
    if (!pa || !pa.texts) {
      body.innerHTML = '<p class="empty">Nothing to announce. When a zone gets crowded or an alert needs the crowd to move, a ready announcement appears here.</p>';
      return;
    }
    const sub = pa.kind !== 'crowd' ? 'Tells the crowd what to do while help arrives'
      : pa.redirect ? `Sends people to ${esc(pa.redirect)}` : 'Asks people to wait and follow staff';
    body.innerHTML = `<div><p class="pa-title">${esc(pa.title)}</p><p class="pa-sub">${sub}</p></div>
      <div class="langs">${['en', 'hi', 'kn'].map(l => `<div class="lang" lang="${l}" data-lang="${l}"><span class="ln">${LANG_NAME[l]}</span><p>${esc(pa.texts[l])}</p></div>`).join('')}</div>
      <div class="pa-actions"><button class="btn solid" id="paPlay">Play announcement</button><span class="pa-status" id="paStatus"></span></div>`;
    $('#paPlay').onclick = () => playPA(pa.texts);
  });
  const st = $('#paStatus');
  if (st && !S.playing && voice.available === false && !st.dataset.set) {
    st.textContent = 'Online voice is unavailable, so this computer\'s voice will read it.';
  }
}

/* ---------------- ticking text: ages and countdowns ---------------- */
function tick() {
  $('#clock').textContent = clock(now());
  document.querySelectorAll('[data-since]').forEach(el => { el.textContent = `open ${dur(now() - +el.dataset.since)}`; });
  document.querySelectorAll('[data-deadline]').forEach(el => {
    const left = +el.dataset.deadline - now();
    el.textContent = left > 0 ? `Moves to the next post in ${Math.ceil(left)} s` : 'Moving to the next post';
  });
  if (!S.state) return;
  const zones = Object.fromEntries(S.state.zones.map(z => [z.id, z]));
  document.querySelectorAll('[data-zone-facts]').forEach(el => {
    const z = zones[el.dataset.zoneFacts];
    if (!z || z.density == null) { el.textContent = ''; return; }
    const crowd = ['crush_risk', 'surge'].includes(el.dataset.type);
    el.textContent = crowd
      ? `${z.density.toFixed(1)} people per m² now, ${z.trend}${z.surge ? ', surging' : ''}.`
      : `${z.density.toFixed(1)} people per m² in this zone.`;
  });
  const cam = S.state.cameras[S.camera];
  $('#camStatus').textContent = cam ? `last frame ${Math.max(0, now() - cam.last_frame).toFixed(1)} s ago` : 'no frames yet';
}

/* ---------------- actions ---------------- */
function bindUI() {
  $('#alertBody').addEventListener('click', async e => {
    const f = e.target.closest('[data-focus]');
    if (f) { S.focus = +f.dataset.focus; S.cache.alerts = null; render(S.state); return; }
    const b = e.target.closest('[data-act]');
    if (!b) return;
    b.disabled = true;
    const body = b.dataset.post ? JSON.stringify({ post: b.dataset.post }) : '{}';
    await fetch(`/api/incidents/${b.dataset.id}/${b.dataset.act}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
  });

  $('#soundBtn').onclick = () => {
    S.sound = !S.sound;
    const b = $('#soundBtn');
    b.setAttribute('aria-pressed', S.sound);
    b.textContent = S.sound ? 'Sound on' : 'Turn on sound';
    if (S.sound) unlockAudio();
  };

  for (const id of ['sendZone', 'sendTemplate', 'sendStaff', 'sendPeople']) {
    $(`#${id}`).onchange = () => renderSend(S.state);
  }
  $('#sendBtn').onclick = sendManual;
  $('#venueSwitch').onclick = async e => {
    const b = e.target.closest('[data-venue]');
    if (!b || b.dataset.venue === S.state?.venue_key) return;
    if (!confirm(`Switch the control room to ${b.textContent}? Open alerts and the current feeds are cleared.`)) return;
    await fetch(`/api/venue/${b.dataset.venue}`, { method: 'POST' }).catch(() => {});
  };
  $('#autoBtn').onclick = async () => {
    const on = !(S.state && S.state.auto_alerts);
    if (!on && !confirm('Turn off automatic alerts? The cameras will stop raising alerts. Manual, SOS and staff alerts still work.')) return;
    await fetch('/api/auto-alerts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }) }).catch(() => {});
  };

  // drawing zones on the camera view
  const feed = $('#feed');
  $('#drawBtn').onclick = () => {
    S.drawing = !S.drawing;
    S.pts = [];
    $('#drawer').hidden = !S.drawing;
    feed.classList.toggle('drawing', S.drawing);
    $('#drawBtn').textContent = S.drawing ? 'Stop drawing' : 'Draw a zone';
    drawRender();
  };
  feed.addEventListener('click', e => {
    if (!S.drawing) return;
    const r = feed.getBoundingClientRect();
    S.pts.push([+((e.clientX - r.left) / r.width).toFixed(4), +((e.clientY - r.top) / r.height).toFixed(4)]);
    drawRender();
  });
  $('#drawUndo').onclick = () => { S.pts.pop(); drawRender(); };
  $('#drawClear').onclick = () => { S.pts = []; drawRender(); };
  $('#drawCopy').onclick = async () => {
    const text = `"polygon": ${JSON.stringify(S.pts)}`;
    try { await navigator.clipboard.writeText(text); $('#drawCopy').textContent = 'Copied'; }
    catch { $('#drawCopy').textContent = 'Select the text above and copy it'; }
    setTimeout(() => { $('#drawCopy').textContent = 'Copy points'; }, 1800);
  };
}

function drawRender() {
  const pts = S.drawing && S.pts.length ? [...S.pts, S.pts[0]] : [];
  $('#drawLine').setAttribute('points', pts.map(p => p.join(',')).join(' '));
  $('#drawOut').textContent = `"polygon": ${JSON.stringify(S.pts)}`;
}

/* ---------------- sound ---------------- */
let actx = null;
function unlockAudio() {
  try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); } catch { /* no audio */ }
  if ('speechSynthesis' in window) speechSynthesis.getVoices();
}

function wail() {
  // the banshee: a short rising two-tone call before the announcement
  return new Promise(res => {
    if (!actx) return res();
    const o = actx.createOscillator(), g = actx.createGain();
    o.type = 'sine';
    const t = actx.currentTime;
    o.frequency.setValueAtTime(520, t);
    o.frequency.linearRampToValueAtTime(880, t + 0.45);
    o.frequency.linearRampToValueAtTime(520, t + 0.9);
    o.frequency.linearRampToValueAtTime(880, t + 1.35);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.18, t + 0.08);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 1.5);
    o.connect(g).connect(actx.destination);
    o.start(t); o.stop(t + 1.55);
    o.onended = res;
  });
}

function playUrl(url) {
  return new Promise(res => {
    const a = new Audio(url);
    a.onended = () => res(true);
    a.onerror = () => res(false);
    a.play().catch(() => res(false));
  });
}

function speak(text, lang) {
  return new Promise(res => {
    if (!('speechSynthesis' in window)) return res(false);
    const code = { en: 'en-IN', hi: 'hi-IN', kn: 'kn-IN' }[lang];
    const v = speechSynthesis.getVoices().find(v => v.lang.toLowerCase().startsWith(lang));
    if (!v && lang !== 'en') return res(false);
    const u = new SpeechSynthesisUtterance(text);
    if (v) u.voice = v;
    u.lang = code;
    u.onend = () => res(true);
    u.onerror = () => res(false);
    speechSynthesis.speak(u);
  });
}

async function playPA(texts) {
  if (S.playing) return;
  S.playing = true;
  unlockAudio();
  const status = msg => { const el = $('#paStatus'); if (el) { el.textContent = msg; el.dataset.set = '1'; } };
  const btn = $('#paPlay');
  if (btn) btn.disabled = true;
  try {
    await wail();
    const missing = [];
    for (const lang of ['en', 'hi', 'kn']) {
      document.querySelectorAll('.lang').forEach(el => el.classList.toggle('speaking', el.dataset.lang === lang));
      status(`Playing ${LANG_WORD[lang]}`);
      const ok = await playUrl(`/api/pa/audio?lang=${lang}&text=${encodeURIComponent(texts[lang])}`) || await speak(texts[lang], lang);
      if (!ok) missing.push(LANG_WORD[lang]);
    }
    status(missing.length ? `Played. No ${missing.join(' or ')} voice on this computer.` : `Played at ${clock(now())}`);
  } finally {
    document.querySelectorAll('.lang').forEach(el => el.classList.remove('speaking'));
    S.playing = false;
    const b = $('#paPlay');
    if (b) b.disabled = false;
  }
}

function onNewAlert(inc) {
  S.focus = inc.id;
  S.fresh = inc.id;
  S.cache.alerts = null;
  render(S.state);
  if (!S.sound) return;
  // the announcement texts arrive a moment after the alert; wait for them
  let tries = 0;
  const wait = setInterval(() => {
    const pa = S.state.pa;
    if (pa && pa.for === 'incident' && pa.ref === inc.id) { clearInterval(wait); playPA(pa.texts); }
    else if (++tries > 10) { clearInterval(wait); wail(); }
  }, 300);
}

/* ---------------- camera switcher ---------------- */
function buildCams() {
  $('#camTabs').innerHTML = Object.entries(S.venue.cameras || {})
    .map(([id, c]) => `<button class="btn quiet cam-tab" data-cam="${esc(id)}">${esc(c.name || id)}</button>`).join('');
  $('#camTabs').onclick = e => {
    const b = e.target.closest('[data-cam]');
    if (b && !b.disabled && b.dataset.cam !== S.camera) setCamera(b.dataset.cam);
  };
  $('#camName').textContent = S.venue.cameras?.[S.camera]?.name || S.camera;
  markCams();
}

function setCamera(cam) {
  S.camera = cam;
  $('#camName').textContent = S.venue.cameras?.[cam]?.name || cam;
  $('#feedImg').src = `/api/video/${cam}?t=${Date.now()}`;
  buildOverlay();
  markCams();
  if (S.state) render(S.state);
}

/* a camera with no frames yet is shown but can't be selected */
function markCams() {
  const live = S.state?.cameras || {};
  document.querySelectorAll('.cam-tab').forEach(b => {
    const id = b.dataset.cam;
    b.setAttribute('aria-pressed', id === S.camera);
    b.disabled = !live[id] && id !== S.camera;
    b.title = b.disabled ? 'No feed from this camera yet' : '';
  });
}

/* ---------------- summary strip: the venue at a glance ---------------- */
function renderSummary(st) {
  const live = st.zones.filter(z => z.level !== 'stale' && z.level !== 'nodata');
  const people = live.reduce((a, z) => a + (z.count || 0), 0);
  const level = lvl => live.filter(z => z.level === lvl).length;
  const name = id => S.venue.zones[id]?.name || id;
  const hot = live.filter(z => z.density != null).sort((a, b) => b.density - a.density)[0];
  const soon = live.filter(z => z.minutes_to_critical != null && z.level !== 'critical')
    .sort((a, b) => a.minutes_to_critical - b.minutes_to_critical)[0];
  const sub = st.subscribers || { staff: 0, public: 0 };
  const cards = [
    ['People counted', people.toLocaleString(), `in ${live.length} of ${st.zones.length} zones`],
    ['Zones now', `${level('critical')} critical · ${level('warning')} warning`, `${level('safe')} safe`],
    ['Most crowded', hot ? `${hot.density.toFixed(1)} /m²` : '–', hot ? name(hot.id) : 'no data yet'],
    ['Next to turn critical', soon ? `${Math.ceil(soon.minutes_to_critical)} min` : 'none in sight',
      soon ? name(soon.id) : 'no zone is rising fast'],
    ['Subscribed on Telegram', `${sub.staff} staff · ${sub.public} people`, tgShort(st.telegram)],
  ];
  once('summary', JSON.stringify(cards), () => {
    $('#summary').innerHTML = cards.map(([k, v, s]) =>
      `<div class="stat"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span><span class="s">${esc(s)}</span></div>`).join('');
  });
}

function tgShort(tg) {
  return tg.state === 'connected' ? 'bot connected' : tg.state === 'starting' ? 'bot connecting' : 'bot not connected';
}

/* ---------------- live heatmap: each zone over the last 30 seconds ---------------- */
const HEAT_CELLS = 60;          // one cell per status update (about 0.5 s), so 30 seconds
const LEVEL_COLOUR = { safe: '#5BD18B', warning: '#FFC23D', critical: '#FF5A4E' };

function renderHeat(zones) {
  S.hist = S.hist || {};
  for (const id of Object.keys(S.venue.zones)) {
    const z = zones[id];
    const live = z && z.density != null && z.level !== 'stale';
    const arr = S.hist[id] || (S.hist[id] = []);
    arr.push(live ? { d: z.density, level: z.level } : null);
    if (arr.length > HEAT_CELLS) arr.shift();
  }
  // rebuilt on every update: the strips scroll, so there's no cheap cache key
  {
    $('#heatGrid').innerHTML = Object.entries(S.venue.zones).map(([id, zc]) => {
      const arr = S.hist[id] || [];
      const cells = Array.from({ length: HEAT_CELLS }, (_, i) => {
        const s = arr[arr.length - HEAT_CELLS + i];
        if (!s) return '<i class="empty"></i>';
        return `<i style="background:${LEVEL_COLOUR[s.level] || '#8C8577'}" title="${s.d.toFixed(1)} people per m²"></i>`;
      }).join('');
      const last = arr.filter(Boolean).at(-1);
      const now = last ? `<span class="hz-now" style="color:${LEVEL_COLOUR[last.level]}">${last.d.toFixed(1)}</span> <span class="hz-unit">/m²</span>`
        : '<span class="hz-now">–</span>';
      return `<div class="hz">
        <div class="hz-name">${esc(zc.name || id)}</div>
        <div class="hz-value">${now}</div>
        <div class="hz-strip" aria-hidden="true">${cells}</div>
      </div>`;
    }).join('') + '<div class="hz-axis"><span></span><span></span><span class="ax"><span>30 s ago</span><span>now</span></span></div>';
  }
}

/* ---------------- send a message on Telegram ---------------- */
function buildSend() {
  $('#sendZone').innerHTML = Object.entries(S.venue.zones)
    .map(([id, z]) => `<option value="${esc(id)}">${esc(z.name || id)}</option>`).join('');
}

function renderSend(st) {
  if (!st || !$('#sendZone').options.length) return;
  const zone = $('#sendZone').value;
  const name = S.venue.zones[zone]?.name || zone;
  const sub = st.subscribers || { staff: 0, public_by_zone: {} };
  const people = (sub.public_by_zone || {})[zone] || 0;
  const staffOn = $('#sendStaff').checked, peopleOn = $('#sendPeople').checked;
  const live = st.telegram.state === 'connected';

  const reach = [];
  if (staffOn) reach.push(`${sub.staff} staff`);
  if (peopleOn) reach.push(`${people} ${people === 1 ? 'person' : 'people'} at ${name}`);
  let text;
  if (!live) text = `Telegram isn't connected, so nothing can be sent. ${tgLine(st.telegram)}`;
  else if (!reach.length) text = 'Choose who to send to.';
  else text = `Reaches ${reach.join(' and ')}.${staffOn && !sub.staff ? ' No staff have subscribed yet.' : ''}`;
  $('#sendReach').textContent = text;
  $('#sendBtn').disabled = !live || (!staffOn && !peopleOn);
}

async function sendManual() {
  const st = S.state;
  const zone = $('#sendZone').value;
  const staffOn = $('#sendStaff').checked, peopleOn = $('#sendPeople').checked;
  const name = S.venue.zones[zone]?.name || zone;
  const people = (st.subscribers.public_by_zone || {})[zone] || 0;
  const who = [staffOn && `${st.subscribers.staff} staff (with Accept)`,
    peopleOn && `${people} ${people === 1 ? 'person' : 'people'} at ${name}`].filter(Boolean).join(' and ');
  const what = $('#sendTemplate').selectedOptions[0].textContent;
  if (!confirm(`Send "${what}" to ${who} on Telegram?`)) return;

  const btn = $('#sendBtn'), status = $('#sendStatus');
  btn.disabled = true;
  status.textContent = 'Sending…';
  try {
    const res = await fetch('/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zone, template: $('#sendTemplate').value, staff: staffOn, public: peopleOn, note: $('#sendNote').value }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `the server said ${res.status}`);
    const done = [];
    if (body.staff_incident) done.push('staff alert opened');
    if (peopleOn) done.push(`${body.public_sent} ${body.public_sent === 1 ? 'person' : 'people'} messaged`);
    status.textContent = `Sent at ${clock(now())}: ${done.join(', ')}.`;
    $('#sendNote').value = '';
  } catch (e) {
    status.textContent = `Not sent: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

init();
