/* StreamingCommunity Web Panel — page-manga.js */
//
// Manga from MangaWorld: search, then a title's chapters, grouped by volume,
// to pick from. One page with two views, both addressable:
//
//   #/manga?q=one+piece           the search
//   #/manga?id=1708&slug=one-piece  a title
//
// The server decides names and paths; the client only says which chapters of
// which title (POST /api/manga/download).

const _mg = {
  q: '',
  page: 1,
  results: [],
  hasMore: false,
  loading: false,
  // The title on screen, as /api/manga/title answered it, or null.
  title: null,
  // What the address asked for, applied before the loader runs.
  wantId: null,
  wantSlug: null,
  selected: new Set(),
  onDisk: new Set(),
  // The format the next download asks for; null until a title says the default.
  format: null,
  candidate: null,
  // The front page's sections; null until fetched, or after an empty answer.
  shelves: null,
  // The grouping the next download asks for: "chapter" or "volume".
  group: null,
};

// ── Entering the page ─────────────────────────────────────────────────────────

function mangaPageEnter() {
  loadMangaDomain();
  if (_mg.wantId && _mg.wantSlug) {
    if (!_mg.title || String(_mg.title.id) !== String(_mg.wantId)) {
      openManga(_mg.wantId, _mg.wantSlug);
    } else {
      _showMangaView('detail');
    }
    return;
  }
  _showMangaView('browse');
  const input = document.getElementById('manga-search-input');
  if (input.value.trim() !== _mg.q) input.value = _mg.q;
  if (!_mg.q) { _showMangaHome(); return; }
  _setMangaHomeVisible(false);
  if (!_mg.results.length) mangaSearch();
}

function _showMangaView(view) {
  document.getElementById('manga-browse').style.display = view === 'browse' ? '' : 'none';
  document.getElementById('manga-detail').style.display = view === 'detail' ? '' : 'none';
}

// ── The start view: MangaWorld's own front page ───────────────────────────────

function _setMangaHomeVisible(on) {
  document.getElementById('manga-hero').style.display = on ? '' : 'none';
  document.getElementById('manga-home').style.display = on ? '' : 'none';
}

async function _showMangaHome() {
  _setMangaHomeVisible(true);
  const host = document.getElementById('manga-home');
  if (_mg.shelves) { _renderMangaShelves(); return; }
  host.innerHTML = _shelfSkeletons();
  try {
    const data = await api.get('/api/manga/home');
    _mg.shelves = data.shelves || [];
  } catch (e) { _mg.shelves = []; }
  // Only an empty list is not cached client-side: the next visit tries again.
  _renderMangaShelves();
  if (!_mg.shelves.length) _mg.shelves = null;
}

function _renderMangaShelves() {
  const host = document.getElementById('manga-home');
  host.innerHTML = '';
  const shelves = _mg.shelves || [];
  shelves.forEach(shelf => {
    const section = document.createElement('div');
    section.className = 'shelf';
    section.innerHTML =
      `<div class="shelf-head"><div class="shelf-title">${escapeHtml(shelf.title)}</div></div>` +
      '<div class="shelf-rail-wrap">' +
        '<button class="shelf-arrow shelf-arrow-prev" hidden aria-label="Titoli precedenti">' +
          '<i class="ti ti-chevron-left"></i></button>' +
        '<div class="shelf-rail"></div>' +
        '<button class="shelf-arrow shelf-arrow-next" hidden aria-label="Titoli successivi">' +
          '<i class="ti ti-chevron-right"></i></button>' +
      '</div>';
    _renderMangaCards(shelf.items, section.querySelector('.shelf-rail'), 'shelf-item');
    host.appendChild(section);
    _wireShelfArrows(section);
  });
  // The headline's backdrop, from covers already fetched. Decoration only.
  const art = document.getElementById('manga-hero-art');
  const covers = [...new Map(shelves.flatMap(s => s.items).filter(i => i.poster)
    .map(i => [i.id, i])).values()].slice(0, 7);
  art.innerHTML = covers
    .map(i => `<span style="background-image:url('${encodeURI(i.poster)}')"></span>`).join('');
}

// ── Search ────────────────────────────────────────────────────────────────────

// Bumped by every new search and by clearing the box, so an answer arriving
// for a query the user has already abandoned is dropped instead of drawn.
let _mangaSearchSeq = 0;

async function mangaSearch(append = false) {
  const q = document.getElementById('manga-search-input').value.trim();
  if (!q) { mangaClearSearch(); return; }
  if (_mg.loading && append) return;
  const container = document.getElementById('manga-results');
  const seq = append ? _mangaSearchSeq : ++_mangaSearchSeq;
  if (!append) {
    _mg.q = q; _mg.page = 1; _mg.results = [];
    _setMangaHomeVisible(false);
    container.innerHTML = '<div class="col-12 text-muted small">Ricerca in corso...</div>';
    syncHash('manga');
  }
  _mg.loading = true;
  try {
    const data = await api.get(`/api/manga/search?q=${encodeURIComponent(_mg.q)}&page=${_mg.page}`);
    if (seq !== _mangaSearchSeq) return;
    if (!append) container.innerHTML = '';
    _mg.results.push(...data.results);
    _mg.hasMore = !!data.has_more;
    _renderMangaCards(data.results, container);
    if (!_mg.results.length) {
      container.innerHTML = '<div class="col-12 text-muted">Nessun manga trovato.</div>';
    }
  } catch (e) {
    if (seq !== _mangaSearchSeq) return;
    container.innerHTML = `<div class="col-12 text-danger">${escapeHtml(errText(e, 'Ricerca non riuscita'))}</div>`;
    _mg.hasMore = false;
    loadMangaDomain();
  } finally {
    if (seq === _mangaSearchSeq) {
      _mg.loading = false;
      document.getElementById('manga-more').style.display = _mg.hasMore ? '' : 'none';
    }
  }
}

// An emptied box is "no search": back to the start view, address included.
function mangaClearSearch() {
  _mangaSearchSeq++;
  _mg.loading = false;
  _mg.q = ''; _mg.page = 1; _mg.results = []; _mg.hasMore = false;
  document.getElementById('manga-search-input').value = '';
  document.getElementById('manga-results').innerHTML = '';
  document.getElementById('manga-more').style.display = 'none';
  syncHash('manga');
  _showMangaHome();
}

function mangaInput(el) {
  if (!el.value.trim() && _mg.q) mangaClearSearch();
}

function mangaMore() {
  if (!_mg.hasMore || _mg.loading) return;
  _mg.page += 1;
  mangaSearch(true);
}

// Cards carry the title's own id and slug, so a card opens the same way from
// the search grid and from any shelf.
function _renderMangaCards(items, container, wrapperClass = 'col-6 col-sm-4 col-md-3 col-lg-2') {
  items.forEach(item => {
    const card = document.createElement('div');
    card.className = wrapperClass;
    const poster = item.poster
      ? `<img src="${escapeHtml(item.poster)}" alt="" loading="lazy" onerror="posterError(this)">` : '';
    const rank = item.rank ? `<div class="mg-rank">${item.rank}</div>` : '';
    const sub = item.note || item.status || '';
    card.innerHTML = `
      <div class="result-card" data-action="manga:open"
           data-id="${escapeHtml(String(item.id))}" data-slug="${escapeHtml(item.slug)}">
        <div class="poster-wrap">
          ${poster}
          <div class="poster-noimg" style="${item.poster ? 'display:none' : ''}">&#128214;</div>
          <div class="poster-overlay"></div>
          ${rank}
        </div>
        <div class="card-meta">
          <div class="card-title-text" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div>
          <div class="card-badges">
            ${item.kind ? `<span class="badge bg-orange-lt">${escapeHtml(item.kind)}</span>` : ''}
            ${sub ? `<span style="font-size:10px;color:var(--text-muted)">${escapeHtml(sub)}</span>` : ''}
          </div>
        </div>
      </div>`;
    container.appendChild(card);
  });
}

// ── Domain ────────────────────────────────────────────────────────────────────

async function loadMangaDomain() {
  if (!can('DOWNLOAD') && !can('MANAGE_SETTINGS')) return;
  try {
    const data = await api.get('/api/manga/domain');
    _mg.candidate = data.candidate || null;
  } catch (e) { _mg.candidate = null; }
  const banner = document.getElementById('manga-domain-banner');
  if (!_mg.candidate) { banner.style.display = 'none'; return; }
  document.getElementById('manga-domain-host').textContent = _mg.candidate.host;
  banner.style.display = '';
}

async function applyMangaDomain() {
  if (!_mg.candidate) return;
  const btn = document.getElementById('manga-domain-apply');
  btn.disabled = true;
  try {
    // Echoed back as a confirmation: the server writes what it found, never this.
    const data = await api.post('/api/manga/domain/candidate/apply', {domain: _mg.candidate.host});
    showToast(`MangaWorld: ora su ${data.host}`, 'success');
    await loadMangaDomain();
  } catch (e) {
    showToast(errText(e, 'Impossibile applicare il dominio'), 'danger');
  } finally { btn.disabled = false; }
}

async function dismissMangaDomain() {
  if (!await scConfirm('Ignorare il dominio trovato? MangaWorld resta sul dominio attuale.')) return;
  try { await api.post('/api/manga/domain/candidate/dismiss'); } catch (e) { /* best effort */ }
  await loadMangaDomain();
}

// ── A title ───────────────────────────────────────────────────────────────────

async function openManga(id, slug, fresh = false) {
  _mg.wantId = String(id); _mg.wantSlug = slug;
  _showMangaView('detail');
  syncHash('manga');
  const box = document.getElementById('manga-detail');
  if (!fresh) box.innerHTML = '<div class="text-muted py-4">Caricamento capitoli...</div>';
  try {
    const data = await api.get(`/api/manga/title/${encodeURIComponent(id)}/${encodeURIComponent(slug)}${fresh ? '?fresh=1' : ''}`);
    // The user may have gone back to the results while this was loading.
    if (_mg.wantId !== String(id)) return;
    _mg.title = data;
    _mg.onDisk = new Set(data.on_disk || []);
    _mg.selected = new Set();
    // The last format picked on this page wins over the default, until reload.
    if (!_mg.format) _mg.format = data.default_format || 'cbz';
    if (!_mg.group) _mg.group = data.default_group || 'chapter';
    _renderMangaDetail();
  } catch (e) {
    box.innerHTML = `<button class="btn btn-sm btn-ghost-secondary mb-3" data-action="manga:back">
        <i class="ti ti-arrow-left me-1"></i>Risultati</button>
      <div class="text-danger">${escapeHtml(errText(e, 'Impossibile leggere il manga'))}</div>`;
    loadMangaDomain();
  }
}

function mangaBack() {
  _mg.wantId = null; _mg.wantSlug = null; _mg.title = null;
  _showMangaView('browse');
  syncHash('manga');
  if (_mg.q && !_mg.results.length) mangaSearch();
}

// Volumes newest first, as the source shows them; chapters inside each in
// reading order. A title without volumes is one group.
function _volumes(chapters) {
  const groups = new Map();
  for (const ch of chapters) {
    const key = ch.volume == null ? 'none' : String(ch.volume);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ch);
  }
  return [...groups.entries()].sort((a, b) => {
    if (a[0] === 'none') return -1;
    if (b[0] === 'none') return 1;
    return Number(b[0]) - Number(a[0]);
  });
}

function _chapterRow(ch) {
  const have = _mg.onDisk.has(ch.id);
  const checked = _mg.selected.has(ch.id) ? 'checked' : '';
  return `<label class="mg-chapter${have ? ' is-have' : ''}">
      <input type="checkbox" class="form-check-input" data-change="manga:toggle"
             data-id="${escapeHtml(ch.id)}" ${checked}>
      <span class="mg-ch-label">${escapeHtml(ch.label)}</span>
      ${have ? '<i class="ti ti-circle-check mg-have" title="Già in libreria"></i>' : ''}
      <span class="mg-ch-date">${escapeHtml(ch.date || '')}</span>
    </label>`;
}

function _renderMangaDetail() {
  const t = _mg.title;
  const box = document.getElementById('manga-detail');
  const groups = _volumes(t.chapters);
  const facts = [t.kind, t.status, t.year, t.author].filter(Boolean).map(escapeHtml).join(' · ');
  const genres = (t.genres || []).map(g => `<span class="badge bg-secondary-lt">${escapeHtml(g)}</span>`).join(' ');
  const volumesHtml = groups.map(([key, chapters], i) => {
    const name = key === 'none' ? (groups.length > 1 ? 'Senza volume' : 'Capitoli') : `Volume ${key}`;
    const have = chapters.filter(c => _mg.onDisk.has(c.id)).length;
    const all = chapters.every(c => _mg.selected.has(c.id));
    return `<details class="mg-volume"${i === 0 ? ' open' : ''}>
        <summary>
          <input type="checkbox" class="form-check-input" data-change="manga:toggleVolume"
                 data-vol="${escapeHtml(key)}" ${all ? 'checked' : ''} title="Seleziona il volume">
          <span class="mg-vol-name">${escapeHtml(name)}</span>
          <span class="mg-vol-count">${chapters.length} cap.${have ? ` · ${have} in libreria` : ''}</span>
        </summary>
        <div class="mg-chapters">${chapters.map(_chapterRow).join('')}</div>
      </details>`;
  }).join('');

  box.innerHTML = `
    <button class="btn btn-sm btn-ghost-secondary mb-3" data-action="manga:back">
      <i class="ti ti-arrow-left me-1"></i>Risultati
    </button>
    <div class="mg-head">
      ${t.poster ? `<img class="mg-cover" src="${escapeHtml(t.poster)}" alt="" onerror="this.remove()">` : ''}
      <div class="mg-info">
        <h2 class="mg-title">${escapeHtml(t.name)}</h2>
        <div class="mg-facts">${facts}</div>
        <div class="mg-genres">${genres}</div>
        ${t.plot ? `<p class="mg-plot">${escapeHtml(t.plot)}</p>` : ''}
      </div>
    </div>
    <div class="mg-actions">
      <span class="mg-summary" id="manga-summary"></span>
      <button class="btn btn-sm btn-ghost-secondary" data-action="manga:selectAll">Seleziona tutti</button>
      <button class="btn btn-sm btn-ghost-secondary" data-action="manga:selectNone">Nessuno</button>
      <button class="btn btn-sm btn-ghost-secondary" data-action="manga:reload" title="Rilegge i capitoli dalla fonte">
        <i class="ti ti-refresh"></i></button>
      <select class="form-select form-select-sm mg-format" title="Un file per capitolo o per volume" data-change="manga:group">
        <option value="chapter"${_mg.group === 'volume' ? '' : ' selected'}>Per capitolo</option>
        <option value="volume"${_mg.group === 'volume' ? ' selected' : ''}>Per volume</option>
      </select>
      <select class="form-select form-select-sm mg-format" id="manga-format" title="Formato dei file" data-change="manga:format">
        ${(t.formats || ['cbz']).map(f => `<option value="${escapeHtml(f)}"${f === _mg.format ? ' selected' : ''}>${escapeHtml(f.toUpperCase())}</option>`).join('')}
      </select>
      <button class="btn btn-sm btn-outline-primary" data-action="manga:downloadMissing">
        <i class="ti ti-download me-1"></i>Scarica mancanti</button>
      <button class="btn btn-sm btn-primary" data-action="manga:downloadSelected" id="manga-dl-selected">
        <i class="ti ti-download me-1"></i>Scarica selezionati</button>
    </div>
    <div class="mg-volumes">${volumesHtml || '<p class="text-muted">Nessun capitolo pubblicato.</p>'}</div>`;
  _updateMangaSummary();
}

function _updateMangaSummary() {
  const t = _mg.title;
  if (!t) return;
  const el = document.getElementById('manga-summary');
  const parts = [`${t.chapters.length} capitoli`];
  if (_mg.onDisk.size) parts.push(`${_mg.onDisk.size} in libreria`);
  if (_mg.selected.size) parts.push(`<strong>${_mg.selected.size} selezionati</strong>`);
  el.innerHTML = parts.join(' · ');
  document.getElementById('manga-dl-selected').disabled = !_mg.selected.size;
}

function mangaToggle(d, el) {
  if (el.checked) _mg.selected.add(d.id); else _mg.selected.delete(d.id);
  _updateMangaSummary();
}

function mangaToggleVolume(d, el) {
  const ids = _mg.title.chapters
    .filter(c => (c.volume == null ? 'none' : String(c.volume)) === d.vol)
    .map(c => c.id);
  ids.forEach(id => el.checked ? _mg.selected.add(id) : _mg.selected.delete(id));
  el.closest('details').querySelectorAll('[data-change="manga:toggle"]')
    .forEach(box => { box.checked = el.checked; });
  _updateMangaSummary();
}

function mangaSelect(all) {
  _mg.selected = all ? new Set(_mg.title.chapters.map(c => c.id)) : new Set();
  document.querySelectorAll('#manga-detail input[type=checkbox]').forEach(box => { box.checked = all; });
  _updateMangaSummary();
}

async function mangaDownload(onlyMissing) {
  const t = _mg.title;
  if (!t) return;
  const ids = onlyMissing ? null : [..._mg.selected];
  if (!onlyMissing && !ids.length) return;
  if (onlyMissing) {
    const missing = t.chapters.length - _mg.onDisk.size;
    if (!await scConfirm(`Scaricare i ${missing} capitoli di «${t.name}» non ancora in libreria?`)) return;
  }
  try {
    const r = await api.post('/api/manga/download', {
      manga_id: t.id, slug: t.slug, chapter_ids: ids, only_missing: onlyMissing,
      format: _mg.format,
      group: _mg.group,
    });
    const skipped = r.skipped ? ` (${r.skipped} già in libreria)` : '';
    const what = [r.volumes ? `${r.volumes} volumi` : '', r.chapters ? `${r.chapters} capitoli` : '']
      .filter(Boolean).join(' e ');
    showToast(`${what} in coda${skipped}`, 'success', {label: 'Vai ai download', page: 'downloads'});
    mangaSelect(false);
  } catch (e) {
    showToast(errText(e, 'Download non avviato'), 'danger');
  }
}

registerActions({
  'manga:search':           () => mangaSearch(),
  'manga:more':             () => mangaMore(),
  'manga:open':             d => openManga(d.id, d.slug),
  'manga:input':            (d, el) => mangaInput(el),
  'manga:clear':            () => mangaClearSearch(),
  'manga:back':             () => mangaBack(),
  'manga:toggle':           (d, el) => mangaToggle(d, el),
  'manga:toggleVolume':     (d, el) => mangaToggleVolume(d, el),
  'manga:selectAll':        () => mangaSelect(true),
  'manga:selectNone':       () => mangaSelect(false),
  'manga:reload':           () => _mg.title && openManga(_mg.title.id, _mg.title.slug, true),
  'manga:downloadSelected': () => mangaDownload(false),
  'manga:downloadMissing':  () => mangaDownload(true),
  'manga:format':           (d, el) => { _mg.format = el.value; },
  'manga:group':            (d, el) => { _mg.group = el.value; },
  'manga:domainApply':      () => applyMangaDomain(),
  'manga:domainDismiss':    () => dismissMangaDomain(),
});

registerPageHash('manga', {
  read: () => (_mg.wantId
    ? { params: { id: _mg.wantId, slug: _mg.wantSlug } }
    : { params: { q: _mg.q } }),
  apply: (params) => {
    if (params.id && params.slug) {
      _mg.wantId = String(params.id); _mg.wantSlug = params.slug;
    } else {
      _mg.wantId = null; _mg.wantSlug = null; _mg.title = null;
      // A bare #/manga (the sidebar link) keeps the last search on screen.
      if ('q' in params && params.q !== _mg.q) { _mg.q = params.q; _mg.results = []; }
    }
  },
});
