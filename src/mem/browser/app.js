const TOKEN = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const api = (p, o = {}) => fetch(p, { ...o, headers: { ...o.headers, 'X-Mem-Token': TOKEN } }).then(r => r.json());
let pages = [], byName = {}, selected = null, matchSet = null, showCards = false;
let sim, nodeSel, linkSel, graphData;

const listEl = document.getElementById('list'), q = document.getElementById('q');
const metaEl = document.getElementById('meta'), textEl = document.getElementById('text');
const saveBtn = document.getElementById('save'), statusEl = document.getElementById('status'), eventsEl = document.getElementById('events');

function renderList() {
  const filter = q.value.trim().toLowerCase();
  const rows = pages.filter(p => (showCards || !p.tags.includes('skill-card')))
    .filter(p => matchSet ? matchSet.has(p.filename) : (!filter || (p.title + ' ' + p.filename + ' ' + p.summary).toLowerCase().includes(filter)))
    .sort((a, b) => matchSet ? (matchSet.get(a.filename) - matchSet.get(b.filename)) : (b.heat - a.heat) || a.filename.localeCompare(b.filename));
  document.getElementById('count').textContent = rows.length;
  listEl.innerHTML = rows.map(p => `<div class="row ${p.filename === selected ? 'sel' : ''}" data-f="${esc(p.filename)}">
    <span class="t"><span class="dot ${p.volatility}"></span>${esc(p.title)}</span>
    <span class="m">${p.heat} hits · ${esc(p.freshness)}${p.due ? ' <span class="due">◯</span>' : ''}</span></div>`).join('');
}
listEl.addEventListener('click', e => { const r = e.target.closest('.row'); if (r) select(r.dataset.f); });

let current = null, mode = 'read';
const viewEl = document.getElementById('view'), menuEl = document.getElementById('menu');

async function select(f) {
  selected = f; renderList(); highlight();
  const p = await api('/api/page/' + encodeURIComponent(f));
  if (p.error) { statusEl.textContent = p.error; return; }
  current = p;
  metaEl.innerHTML = `<b>${esc(f)}</b><br>${esc(p.freshness)}${p.due ? ' <span class="due">· due or unverified claims are hypotheses until checked</span>' : ''}`;
  textEl.value = p.text; saveBtn.disabled = mode !== 'edit';
  statusEl.textContent = 'browsing logs no usage events; verdicts and saves do';
  eventsEl.innerHTML = p.events.slice().reverse().map(e => `<div>${esc(e.ts.slice(0, 16))} ${esc(e.event)}${e.verdict ? ' ' + esc(e.verdict) : ''}${e.claim ? ' (' + esc(e.claim) + ')' : ''}${e.query ? ' “' + esc(e.query) + '”' : ''}${e.note ? ' — ' + esc(e.note) : ''}</div>`).join('') || '<div>no ledger events yet</div>';
  renderView();
}

// Read mode: frontmatter as a header, markers as status chips attached to
// the sentence they qualify. The sentence is the text between the previous
// sentence end and the marker (markers sit before the final period).
const MARK = /(NOTE|TODO)\(([a-z-]+):([^)]*)\)/g;
function chipFor(m) {
  const info = (current.markers || []).find(x => x.raw === m[0]) || {};
  const [family, kind, rest] = [m[1], m[2], m[3]];
  const parts = rest.split(':');
  if (family === 'NOTE' && kind === 'verified') {
    const cls = info.due ? 'due' : 'v', age = daysSince(parts[1]);
    return { cls, html: `<span class="chip ${cls}" data-claim="${esc(parts[0])}" title="${info.due ? 'verified ' + age + 'd ago, past this page\u2019s half-life: check it' : 'verified ' + esc(parts[1])}">${info.due ? '⏲' : '✓'} ${esc(parts[0])} · ${age}d</span>` };
  }
  if (family === 'NOTE' && kind === 'unverified') return { cls: 'u', html: `<span class="chip u" data-claim="${esc(parts[0])}" title="a hypothesis; nobody has checked it">? ${esc(parts[0])}</span>` };
  if (family === 'NOTE' && kind === 'as-of') return { cls: 'asof', html: `<span class="chip asof" title="an observation; true as of this date">as of ${esc(parts[0])}</span>` };
  if (family === 'TODO' && kind === 'investigate') return { cls: 'todo', html: `<span class="chip todo" title="a declared gap; it is on the bounty board">gap: ${esc(parts[0])}</span>` };
  return null;
}
const daysSince = d => Math.max(0, Math.round((Date.now() - new Date(d + 'T00:00:00Z')) / 86400000));
function decorate(md) {
  // wrap the sentence preceding each marker, then replace the marker with its chip
  return md.split('\n').map(line => {
    let out = '', last = 0;
    for (const m of line.matchAll(MARK)) {
      const chip = chipFor(m); if (!chip) continue;
      const before = line.slice(last, m.index);
      const cut = Math.max(before.lastIndexOf('. '), before.lastIndexOf('! '), before.lastIndexOf('? '), before.lastIndexOf(': '));
      const head = cut >= 0 ? before.slice(0, cut + 2) : '', sentence = cut >= 0 ? before.slice(cut + 2) : before;
      out += head + (sentence.trim() && chip.cls !== 'asof' && chip.cls !== 'todo' ? `<span class="claim ${chip.cls}">${sentence.trimEnd()}</span>` : sentence.trimEnd()) + chip.html;
      last = m.index + m[0].length;
    }
    return out + line.slice(last);
  }).join('\n');
}
function renderView() {
  if (!current) return;
  const fm = current.frontmatter || {};
  const body = current.text.startsWith('---\n') ? current.text.slice(current.text.indexOf('\n---\n') + 5) : current.text;
  const cites = (fm.citations || []).map(c => /^https?:/.test(c) ? `<a class="cite" href="${esc(c)}" target="_blank">${esc(c)}</a>` : `<code class="cite">${esc(c)}</code>`).join('');
  const header = `<div class="fm"><h2>${esc(fm.title || current.filename)}</h2>${fm.summary ? esc(fm.summary) : ''}
    <div>${(fm.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('')}<span class="tag">${esc(current.volatility)}${current.half_life_days ? ' · ' + current.half_life_days + 'd' : ''}</span></div>
    ${cites ? '<div style="margin-top:6px">' + cites + '</div>' : ''}
    <div style="margin-top:6px">${esc(fm.updated ? 'updated ' + String(fm.updated).slice(0, 10) : '')}</div></div>`;
  // page bodies are often written by agents that read untrusted text, so their HTML is never trusted
  viewEl.innerHTML = header + DOMPurify.sanitize(marked.parse(decorate(body)));
}
viewEl.addEventListener('click', e => {
  const chip = e.target.closest('.chip[data-claim]'); if (!chip) return;
  const claim = chip.dataset.claim, r = chip.getBoundingClientRect();
  menuEl.style.display = 'block'; menuEl.style.left = Math.min(r.left, window.innerWidth - 260) + 'px'; menuEl.style.top = (r.bottom + 6) + 'px';
  menuEl.innerHTML = `<div class="h">claim <b>${esc(claim)}</b> in ${esc(current.filename)}</div>
    <button data-v="verified">✓ I checked it against its source and it holds</button>
    <button data-v="outdated">✗ it is wrong or stale (add a note)</button>`;
  menuEl.onclick = async ev => {
    const b = ev.target.closest('button'); if (!b) return;
    let note = null;
    if (b.dataset.v === 'outdated') { note = prompt('What is wrong, and what is right now?'); if (note === null) return; }
    menuEl.style.display = 'none'; statusEl.textContent = 'recording…';
    const res = await api('/api/claim', { method: 'POST', body: JSON.stringify({ filename: current.filename, verdict: b.dataset.v, claim, note }) });
    if (res.error) { statusEl.textContent = 'rejected: ' + res.error; return; }
    statusEl.textContent = (b.dataset.v === 'verified' ? 'stamped ' : 'flagged ') + claim + ' · ' + res.freshness;
    current = res; textEl.value = res.text; metaEl.innerHTML = `<b>${esc(current.filename)}</b><br>${esc(res.freshness)}`; renderView();
    pages = await api('/api/pages'); renderList();
  };
});
document.addEventListener('click', e => { if (!e.target.closest('#menu') && !e.target.closest('.chip')) menuEl.style.display = 'none'; });
function setMode(m) {
  mode = m; document.getElementById('mRead').classList.toggle('on', m === 'read'); document.getElementById('mEdit').classList.toggle('on', m === 'edit');
  viewEl.style.display = m === 'read' ? 'block' : 'none'; textEl.style.display = m === 'edit' ? 'block' : 'none';
  saveBtn.disabled = !(m === 'edit' && current);
}
document.getElementById('mRead').onclick = () => setMode('read');
document.getElementById('mEdit').onclick = () => setMode('edit');
setMode('read');

saveBtn.onclick = async () => {
  saveBtn.disabled = true; statusEl.textContent = 'saving…';
  const r = await api('/api/page/' + encodeURIComponent(selected), { method: 'PUT', body: textEl.value });
  saveBtn.disabled = false;
  if (r.error) { statusEl.textContent = 'rejected: ' + r.error; return; }
  statusEl.textContent = 'saved · ' + r.freshness;
  metaEl.innerHTML = `<b>${esc(selected)}</b><br>${esc(r.freshness)}`;
  current = r; renderView();
  await reload();
};

let searchTimer;
q.addEventListener('input', () => {
  clearTimeout(searchTimer); matchSet = null; renderList(); highlight();
  const v = q.value.trim();
  if (v.split(/\s+/).length >= 2) searchTimer = setTimeout(async () => {
    const hits = await api('/api/search?q=' + encodeURIComponent(v));
    if (Array.isArray(hits)) { matchSet = new Map(hits.map((h, i) => [h.filename, i])); renderList(); highlight(); }
  }, 350);
});

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
const color = v => ({ fast: 'var(--fast)', slow: 'var(--slow)', stable: 'var(--stable)' })[v] || 'var(--card)';

function drawGraph(g) {
  graphData = g;
  const svg = d3.select('svg'); svg.selectAll('*').remove();
  const W = svg.node().clientWidth, H = svg.node().clientHeight;
  const root = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 6]).on('zoom', e => root.attr('transform', e.transform)));
  const visible = n => showCards || !n.card;
  const nodes = g.nodes.filter(visible), ids = new Set(nodes.map(n => n.id));
  const links = g.links.filter(l => ids.has(l.source.id || l.source) && ids.has(l.target.id || l.target));
  sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(l => 40 + l.distance * 220).strength(l => 1 - l.distance))
    .force('charge', d3.forceManyBody().strength(-60))
    .force('center', d3.forceCenter(W / 2, H / 2))
    .force('collide', d3.forceCollide(d => radius(d) + 2));
  linkSel = root.append('g').attr('stroke', '#2f3542').selectAll('line').data(links).join('line').attr('stroke-width', l => 2.5 - l.distance * 3);
  nodeSel = root.append('g').selectAll('circle').data(nodes).join('circle')
    .attr('r', radius).attr('fill', d => color(d.card ? 'card' : d.volatility))
    .attr('stroke', d => d.due ? 'var(--due)' : '#0b0d11').attr('stroke-width', d => d.due ? 2 : 1)
    .style('cursor', 'pointer')
    .on('click', (_, d) => select(d.id))
    .on('mousemove', (e, d) => { const t = document.getElementById('tip'); t.style.display = 'block'; t.style.left = (e.offsetX + 14) + 'px'; t.style.top = (e.offsetY + 14) + 'px'; t.textContent = d.title; })
    .on('mouseleave', () => document.getElementById('tip').style.display = 'none')
    .call(d3.drag().on('start', (e, d) => { sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end', (e, d) => { sim.alphaTarget(0); d.fx = null; d.fy = null; }));
  sim.on('tick', () => {
    linkSel.attr('x1', l => l.source.x).attr('y1', l => l.source.y).attr('x2', l => l.target.x).attr('y2', l => l.target.y);
    nodeSel.attr('cx', d => d.x).attr('cy', d => d.y);
  });
  highlight();
}
const radius = d => 4 + Math.sqrt(d.heat || 0) * 2.2;
function highlight() {
  if (!nodeSel) return;
  nodeSel.attr('opacity', d => (matchSet && !matchSet.has(d.id)) ? 0.12 : 1)
    .attr('stroke', d => d.id === selected ? '#ffffff' : (d.due ? 'var(--due)' : '#0b0d11'))
    .attr('stroke-width', d => d.id === selected ? 3 : (d.due ? 2 : 1));
  if (linkSel) linkSel.attr('opacity', l => (matchSet && !(matchSet.has(l.source.id) && matchSet.has(l.target.id))) ? 0.15 : 0.9);
}
document.getElementById('cards').onchange = e => { showCards = e.target.checked; renderList(); drawGraph(graphData); };

async function reload() {
  const res = await api('/api/pages');
  if (res.error) { statusEl.textContent = res.error; return; }
  pages = res; byName = Object.fromEntries(pages.map(p => [p.filename, p]));
  renderList(); drawGraph(await api('/api/graph'));
}
reload();
