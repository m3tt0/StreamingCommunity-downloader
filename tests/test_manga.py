"""MangaWorld: parsing, the chapter download, the domain recovery, the routes.

The HTML below is trimmed from the real pages (November 2026), keeping the
structure the parsers read: ``div.entry`` in the archive, ``.volume-element`` and
``a.chap`` on a title, ``img.page-image`` in the reader.
"""

import json
import os
import threading
import zipfile

import pytest

from app import config
from app.core import domain_recovery, manga_recovery, mangaworld
from app.progress import DownloadCancelledError
from tests.conftest import ALL, enable_open_mode, make_user, session_for

HOST = "www.mangaworld.mx"

SEARCH = """<html><head><meta property=og:site_name content=MangaWorld></head><body>
<div class="entry">
 <a class="thumb position-relative" href="https://www.mangaworld.mx/manga/1708/one-piece" title="One Piece">
  <img src="https://cdn.mangaworld.mx/mangas/op.jpg"></a>
 <div class="content">
  <p class="name m-0"><a class="manga-title" href="https://www.mangaworld.mx/manga/1708/one-piece">One Piece</a></p>
  <div class="genre"><span>Tipo:</span><a href="#">Manga</a></div>
  <div class="status"><span>Stato:</span><a href="#">In corso</a></div>
  <div class="author"><span>Autore:</span><a href="#">ODA Eiichiro</a></div>
  <div class="genres"><span>Generi:</span><a>Azione</a>, <a>Avventura</a></div>
  <div class="story"><span>Trama:</span> Rufy &amp; la ciurma.</div>
 </div>
</div>
<div class="entry"><a class="thumb" href="/not-a-manga"></a></div>
<script>["s0-17",4,{"totalPages":3,"page":1}]</script>
</body></html>"""

TITLE = """<html><head><meta property=og:site_name content=MangaWorld></head><body>
<div class="thumb"><img class="rounded" src="https://cdn.mangaworld.mx/mangas/op.jpg"></div>
<div class="info"><h1 class="name bigger">One Piece</h1>
 <div class="meta-data row">
  <div class="col-12"><span>Generi:</span><a>Azione</a><a>Avventura</a></div>
  <div class="col-12"><span>Autore:</span><a>ODA Eiichiro</a></div>
  <div class="col-12"><span>Tipo:</span><a>Manga</a></div>
  <div class="col-12"><span>Stato:</span><a>In corso</a></div>
  <div class="col-12"><span>Anno di uscita:</span><a>1997</a></div>
 </div>
</div>
<div id="noidungm">Monkey D. Rufy è un pirata.</div>
<div class="volume-element"><p class="volume-name">Volume 2</p>
 <div class="volume-chapters">
  <div class="chapter"><a class="chap" href="https://www.mangaworld.mx/manga/1708/one-piece/read/000000000000000000000003">
   <span>Capitolo 10.5</span><i class="chap-date">02 Gennaio 2021</i></a></div>
  <div class="chapter"><a class="chap" href="https://www.mangaworld.mx/manga/1708/one-piece/read/000000000000000000000002">
   <span>Capitolo 10</span><i class="chap-date">01 Gennaio 2021</i></a></div>
 </div></div>
<div class="volume-element"><p class="volume-name">Volume 01</p>
 <div class="volume-chapters">
  <div class="chapter"><a class="chap" href="https://www.mangaworld.mx/manga/1708/one-piece/read/000000000000000000000001">
   <span>Capitolo 00</span><i class="chap-date">03 Novembre 2020</i></a></div>
 </div></div>
</body></html>"""

ONESHOT = """<html><body><div class="info"><h1 class="name">Nami VS Kalifa</h1></div>
<div class="chapter pl-2"><a class="chap" href="https://www.mangaworld.mx/manga/3294/nami-vs-kalifa/read/649af99e8e54383f7f49f1ff">
<span>Oneshot</span></a></div></body></html>"""


def _reader(srcs):
    imgs = "".join(f'<img class="page-image img-fluid" id="page-{i}" src="{s}">'
                   for i, s in enumerate(srcs))
    # A preload of page 1 and the cover: neither is a page.
    return f'<html><body><img src="{srcs[0]}"><img src="https://cdn.mangaworld.mx/mangas/x.jpg">{imgs}</body></html>'


class Resp:
    def __init__(self, text="", status=200, url=None, content=b"", headers=None):
        self.text = text
        self.status_code = status
        self.url = url or f"https://{HOST}/"
        self.content = content
        self.headers = headers or {}
        self.ok = status < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _fresh_state():
    mangaworld.clear_cache()
    manga_recovery.clear_pending()
    manga_recovery._last_check_at = None
    yield
    mangaworld.clear_cache()
    manga_recovery.clear_pending()
    manga_recovery._last_check_at = None


@pytest.fixture
def site(monkeypatch):
    """Serve the pages above by path, and record every request."""
    pages = {
        "/archive": SEARCH,
        "/manga/1708/one-piece/": TITLE,
        "/manga/3294/nami-vs-kalifa/": ONESHOT,
    }
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append((url, params))
        path = url.split(HOST, 1)[-1]
        if "/read/" in path:
            return Resp(_reader([f"https://cdn.mangaworld.mx/chapters/x/{n}.png" for n in (1, 2, 3)]))
        if path in pages:
            return Resp(pages[path], url=url)
        return Resp("", status=404)

    monkeypatch.setattr(mangaworld.requests, "get", fake_get)
    monkeypatch.setattr(mangaworld, "_nudge_recovery", lambda: None)
    return calls


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_search_reads_entries_and_the_page_count(site):
    result = mangaworld.search("one piece")
    assert result["total_pages"] == 3 and result["has_more"] is True
    [item] = result["results"]  # the entry with no manga link is skipped
    assert item["id"] == 1708 and item["slug"] == "one-piece"
    assert item["name"] == "One Piece" and item["kind"] == "Manga"
    assert item["status"] == "In corso" and item["genres"] == ["Azione", "Avventura"]
    # Entities decoded once, by the parser: no html.unescape on top.
    assert item["plot"] == "Rufy & la ciurma."
    assert site[0][1] == {"keyword": "one piece", "page": 1}


def test_the_title_lists_chapters_oldest_first_with_their_volume(site):
    manga = mangaworld.get_manga(1708, "one-piece")
    assert manga["name"] == "One Piece" and manga["year"] == "1997"
    assert manga["genres"] == ["Azione", "Avventura"] and manga["author"] == "ODA Eiichiro"
    assert [(c["number"], c["volume"]) for c in manga["chapters"]] == [
        ("00", 1), ("10", 2), ("10.5", 2)]


def test_a_chapter_without_a_number_keeps_its_label(site):
    [chapter] = mangaworld.get_manga(3294, "nami-vs-kalifa")["chapters"]
    assert chapter["number"] is None and chapter["volume"] is None
    assert mangaworld.chapter_filename("Nami VS Kalifa", chapter) == "Nami VS Kalifa - Oneshot.cbz"


def test_the_title_page_is_read_once_per_ttl(site):
    mangaworld.get_manga(1708, "one-piece")
    mangaworld.get_manga(1708, "one-piece")
    assert len([c for c in site if c[0].endswith("/one-piece/")]) == 1
    mangaworld.get_manga(1708, "one-piece", fresh=True)
    assert len([c for c in site if c[0].endswith("/one-piece/")]) == 2


@pytest.mark.parametrize("number,expected", [
    ("7", "Manga - Cap. 0007.cbz"), ("10.5", "Manga - Cap. 0010.5.cbz"),
    ("00", "Manga - Cap. 0000.cbz"), ("1198", "Manga - Cap. 1198.cbz"),
])
def test_chapter_files_sort_in_reading_order(number, expected):
    assert mangaworld.chapter_filename("Manga", {"id": "x", "number": number}) == expected


def test_a_title_cannot_escape_the_library():
    path = mangaworld.chapter_path("/lib", "../../etc", {"id": "x", "number": "1"})
    assert os.path.dirname(path) == os.path.join("/lib", "etc")


def test_reader_pages_come_in_order_without_preloads(site):
    pages = mangaworld.chapter_pages(1708, "one-piece", "0" * 24)
    assert [p.rsplit("/", 1)[1] for p in pages] == ["1.png", "2.png", "3.png"]


def test_a_page_on_a_foreign_host_is_refused(monkeypatch):
    monkeypatch.setattr(mangaworld.requests, "get",
                        lambda *a, **k: Resp(_reader(["https://evil.example/1.png"])))
    with pytest.raises(RuntimeError, match="host inatteso"):
        mangaworld.chapter_pages(1708, "one-piece", "0" * 24)


def test_an_unreachable_host_is_a_source_error_and_starts_recovery(monkeypatch):
    nudged = []

    def boom(*a, **k):
        raise mangaworld.requests.ConnectionError("down")

    monkeypatch.setattr(mangaworld.requests, "get", boom)
    monkeypatch.setattr(mangaworld, "_nudge_recovery", lambda: nudged.append(1))
    with pytest.raises(mangaworld.SourceUnavailable):
        mangaworld.search("x")
    assert nudged


# ── Download ──────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, fail=()):
        self.headers = {}
        self.fail = set(fail)

    def get(self, url, timeout=None):
        name = url.rsplit("/", 1)[1]
        if name in self.fail:
            return Resp(status=503)
        return Resp(content=b"img-" + name.encode(), headers={"content-type": "image/png"})

    def close(self):
        pass


class Bar:
    def __init__(self):
        self.n, self.phases = 0, []

    def update(self, n=1, bytes=0):
        self.n += n

    def emit_status(self, phase):
        self.phases.append(phase)


def test_a_chapter_becomes_a_cbz_with_comicinfo(site, monkeypatch, tmp_path):
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())
    bar = Bar()
    out = mangaworld.download_chapter(
        1708, "one-piece", "000000000000000000000003",
        output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"),
        progress_factory=lambda total, phase: bar)

    assert out == str(tmp_path / "lib" / "One Piece" / "One Piece - Cap. 0010.5.cbz")
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        info = archive.read("ComicInfo.xml").decode()
    assert names == ["001.png", "002.png", "003.png", "ComicInfo.xml"]
    assert "<Number>10.5</Number>" in info and "<Volume>2</Volume>" in info
    assert "<Series>One Piece</Series>" in info and "<PageCount>3</PageCount>" in info
    assert bar.n == 3 and bar.phases == ["packing", "done"]
    assert not (tmp_path / "tmp").exists()
    # Nothing half-written left behind.
    assert os.listdir(tmp_path / "lib" / "One Piece") == ["One Piece - Cap. 0010.5.cbz"]


def test_a_missing_page_fails_the_chapter_and_writes_nothing(site, monkeypatch, tmp_path):
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession(fail={"2.png"}))
    monkeypatch.setattr(mangaworld, "PAGE_RETRIES", 1)
    with pytest.raises(RuntimeError, match="pagina non scaricata"):
        mangaworld.download_chapter(
            1708, "one-piece", "000000000000000000000001",
            output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"))
    assert not (tmp_path / "lib").exists()


def test_a_cancelled_chapter_stops(site, monkeypatch, tmp_path):
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelledError):
        mangaworld.download_chapter(
            1708, "one-piece", "000000000000000000000001",
            output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"),
            cancel_event=cancel)


# ── Domain recovery ───────────────────────────────────────────────────────────

LIVE = '<html><head><meta property="og:site_name" content="MangaWorld"></head></html>'


@pytest.fixture
def web(monkeypatch):
    """A small internet: host -> response, for requests made without redirects."""
    routes: dict[str, Resp] = {}

    def fake_get(url, **kwargs):
        assert kwargs.get("allow_redirects") is False, "recovery must never follow a redirect"
        host = url.split("//", 1)[1].split("/", 1)[0]
        return routes.get(host) or Resp(status=404)

    monkeypatch.setattr(manga_recovery.requests, "get", fake_get)
    monkeypatch.setattr(domain_recovery.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(domain_recovery, "_announce", lambda *a, **k: None)
    return routes


def _moved(to):
    return Resp(status=301, headers={"location": f"https://{to}/"})


def _set_host(data_file, host):
    data = json.loads(data_file.read_text())
    data["mangaworld_domain"] = host
    data_file.write_text(json.dumps(data))


def test_a_dead_host_is_replaced_by_where_an_old_one_redirects(web, _configured_domain):
    _set_host(_configured_domain, "www.mangaworld.mx")
    web["www.mangaworld.ac"] = _moved("www.mangaworld.zz")
    web["www.mangaworld.zz"] = Resp(LIVE)

    result = manga_recovery.run_check()
    assert result["candidate"] == "www.mangaworld.zz"
    # Proposed, not written.
    assert manga_recovery.pending()["host"] == "www.mangaworld.zz"
    assert config.configured_mangaworld_host() == "www.mangaworld.mx"


def test_ad_redirects_and_parking_pages_are_refused(web, _configured_domain):
    web["www.mangaworld.bz"] = Resp(status=302, headers={"location": "https://adnetwork.com/click"})
    web["www.mangaworld.io"] = Resp(status=302, headers={"location": "https://ww38.mangaworld.fun/"})
    web["adnetwork.com"] = Resp(LIVE)
    web["ww38.mangaworld.fun"] = Resp(LIVE)

    result = manga_recovery.run_check()
    assert result["candidate"] is None
    reasons = {r["host"]: r["reason"] for r in result["rejected"]}
    assert "non riconosciuto" in reasons["adnetwork.com"]
    assert "secondo livello" in reasons["ww38.mangaworld.fun"]


def test_a_host_that_only_redirects_is_not_verified(web, _configured_domain):
    # A parked domain forwarding to the real site must not pass as the site.
    web["www.mangaworld.mx"] = _moved("www.mangaworld.zz")
    web["www.mangaworld.zz"] = _moved("www.mangaworld.yy")
    assert manga_recovery.verify("www.mangaworld.zz") is False


def test_a_working_host_is_left_alone(web, _configured_domain):
    web["www.mangaworld.mx"] = Resp(LIVE)
    result = manga_recovery.run_check()
    assert result["current_ok"] and result["candidate"] is None


def test_applying_remembers_the_host_left_behind(web, _configured_domain):
    _set_host(_configured_domain, "www.mangaworld.ac")
    web["www.mangaworld.mx"] = Resp(LIVE)
    manga_recovery.apply_candidate("www.mangaworld.mx")
    data = json.loads(_configured_domain.read_text())
    assert data["mangaworld_domain"] == "www.mangaworld.mx"
    assert data[manga_recovery.HISTORY_KEY] == ["www.mangaworld.ac"]
    # The rest of data.json survives.
    assert data["domain"] == "example.test"


def test_auto_apply_writes_the_host(web, _configured_domain):
    data = json.loads(_configured_domain.read_text())
    data["settings"] = {"domain_auto_apply": True}
    _configured_domain.write_text(json.dumps(data))
    web["www.mangaworld.ac"] = _moved("www.mangaworld.zz")
    web["www.mangaworld.zz"] = Resp(LIVE)

    assert manga_recovery.run_check()["applied"] is True
    assert config.configured_mangaworld_host() == "www.mangaworld.zz"


def test_the_throttle_holds_between_checks(web, _configured_domain):
    assert manga_recovery.run_check()["checked"] is True
    assert manga_recovery.run_check()["checked"] is False
    assert manga_recovery.run_check(force=True)["checked"] is True


# ── Routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def downloader(client):
    enable_open_mode()
    return client


def test_downloading_queues_one_job_per_chapter_as_one_batch(downloader, site, stub_jobs):
    r = downloader.post("/api/manga/download", json={
        "manga_id": 1708, "slug": "one-piece",
        "chapter_ids": ["000000000000000000000002", "000000000000000000000003"]})
    assert r.status_code == 202, r.text
    assert r.json()["count"] == 2
    names = [call[0] for call in stub_jobs]
    assert names == ["submit_manga_chapter"] * 2
    # The name comes from the source, never from the client.
    assert {call[1][2] for call in stub_jobs} == {"One Piece"}
    assert {call[2]["batch_kind"] for call in stub_jobs} == {"manga"}


def test_only_missing_skips_what_is_in_the_library(downloader, site, stub_jobs, tmp_path,
                                                   _configured_domain):
    data = json.loads(_configured_domain.read_text())
    data["libraries"] = [{"type": "manga", "path": str(tmp_path / "manga")}]
    _configured_domain.write_text(json.dumps(data))
    folder = tmp_path / "manga" / "One Piece"
    folder.mkdir(parents=True)
    (folder / "One Piece - Cap. 0000.cbz").write_bytes(b"")

    title = downloader.get("/api/manga/title/1708/one-piece").json()
    assert title["on_disk"] == ["000000000000000000000001"]

    r = downloader.post("/api/manga/download", json={
        "manga_id": 1708, "slug": "one-piece", "only_missing": True})
    assert r.status_code == 202, r.text
    assert r.json()["skipped"] == 1 and r.json()["count"] == 2


def test_a_bad_slug_never_reaches_the_source(downloader, site):
    assert downloader.get("/api/manga/title/1708/..%2F..%2Fadmin").status_code in (404, 422)
    assert downloader.post("/api/manga/download", json={
        "manga_id": 1, "slug": "../x"}).status_code == 422
    assert not site


def test_manga_needs_the_download_permission(client, site):
    from app.auth import permissions

    user = make_user("reader", "jf-reader", int(permissions.Permission.REQUEST))
    session_for(client, user.id)
    assert client.get("/api/manga/search?q=x").status_code == 403


def test_applying_the_manga_candidate_writes_the_servers_host(client, web, _configured_domain):
    user = make_user("boss", "jf-boss", ALL)
    csrf = session_for(client, user.id)
    web["www.mangaworld.zz"] = Resp(LIVE)
    manga_recovery._set_pending({"host": "www.mangaworld.zz", "found_at": 0})

    wrong = client.post("/api/manga/domain/candidate/apply", json={"domain": "evil.example"},
                        headers={"X-CSRF-Token": csrf})
    assert wrong.status_code == 409
    ok = client.post("/api/manga/domain/candidate/apply", json={"domain": "www.mangaworld.zz"},
                     headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200, ok.text
    assert config.configured_mangaworld_host() == "www.mangaworld.zz"
    assert manga_recovery.pending() is None


def test_a_manga_library_can_be_configured(client, tmp_path):
    user = make_user("boss", "jf-boss", ALL)
    csrf = session_for(client, user.id)
    r = client.put("/api/domain/libraries", headers={"X-CSRF-Token": csrf}, json={
        "libraries": [{"type": "manga", "path": str(tmp_path / "manga")}],
        "excluded_folders": []})
    assert r.status_code == 200, r.text
    bad = client.put("/api/domain/libraries", headers={"X-CSRF-Token": csrf}, json={
        "libraries": [{"type": "comics", "path": str(tmp_path)}], "excluded_folders": []})
    assert bad.status_code == 422


# ── Output format ─────────────────────────────────────────────────────────────

def _png(path, size=(4, 6), mode="RGBA"):
    from PIL import Image

    Image.new(mode, size, (200, 10, 10, 128) if mode == "RGBA" else (200, 10, 10)).save(path)


def _jpeg(path, size=(5, 3)):
    from PIL import Image

    Image.new("RGB", size, (10, 200, 10)).save(path, "JPEG")


def test_the_pdf_embeds_jpegs_untouched_and_has_a_valid_xref(tmp_path):
    from app.core.manga_pdf import write_pdf

    _jpeg(tmp_path / "1.jpg")
    _png(tmp_path / "2.png")
    target = tmp_path / "out.pdf"
    write_pdf(str(target), [str(tmp_path / "1.jpg"), str(tmp_path / "2.png")],
              {"Title": "Città — Capitolo 1", "Author": None})
    data = target.read_bytes()

    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    # The JPEG's bytes are in the file as they were downloaded.
    assert (tmp_path / "1.jpg").read_bytes() in data
    assert b"/MediaBox [0 0 5 3]" in data and b"/MediaBox [0 0 4 6]" in data
    assert b"/Count 2" in data and b"/FlateDecode" in data
    # Every xref entry points at the object it names.
    xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    lines = data[xref:].split(b"\n")
    count = int(lines[1].split()[1])
    for number in range(1, count):
        offset = int(lines[2 + number].split()[0])
        assert data[offset:].startswith(f"{number} 0 obj".encode())
    # A title outside ASCII survives, as UTF-16.
    assert "Città".encode("utf-16-be").hex().upper().encode() in data


@pytest.mark.parametrize("fmt", mangaworld.FORMATS)
def test_a_chapter_is_saved_in_the_format_asked(fmt, site, monkeypatch, tmp_path):
    def real_images(url, timeout=None):
        from io import BytesIO
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (3, 3), (1, 2, 3)).save(buf, "PNG")
        return Resp(content=buf.getvalue(), headers={"content-type": "image/png"})

    session = FakeSession()
    session.get = real_images
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: session)
    out = mangaworld.download_chapter(
        1708, "one-piece", "000000000000000000000002",
        output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"), fmt=fmt)
    assert out.endswith(f"One Piece - Cap. 0010.{fmt}")
    if fmt == "pdf":
        assert open(out, "rb").read(5) == b"%PDF-"
    else:
        assert "ComicInfo.xml" in zipfile.ZipFile(out).namelist()


def test_the_setting_decides_when_the_download_does_not(site, monkeypatch, tmp_path,
                                                         _configured_domain):
    data = json.loads(_configured_domain.read_text())
    data["settings"] = {"manga_format": "zip"}
    _configured_domain.write_text(json.dumps(data))
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())
    out = mangaworld.download_chapter(
        1708, "one-piece", "000000000000000000000002",
        output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"))
    assert out.endswith(".zip")


def test_a_new_format_replaces_the_old_copy_and_both_count_as_present(site, monkeypatch, tmp_path):
    folder = tmp_path / "lib" / "One Piece"
    folder.mkdir(parents=True)
    (folder / "One Piece - Cap. 0010.cbz").write_bytes(b"old")
    (folder / "One Piece - Cap. 0000.pdf").write_bytes(b"other chapter")
    manga = mangaworld.get_manga(1708, "one-piece")
    assert set(mangaworld.chapters_on_disk(str(tmp_path / "lib"), manga)) == {
        "000000000000000000000001", "000000000000000000000002"}

    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())
    mangaworld.download_chapter(
        1708, "one-piece", "000000000000000000000002",
        output_dir=str(tmp_path / "lib"), temp_dir=str(tmp_path / "tmp"), fmt="zip")
    assert sorted(os.listdir(folder)) == ["One Piece - Cap. 0000.pdf", "One Piece - Cap. 0010.zip"]


def test_the_route_passes_the_format_and_refuses_an_unknown_one(downloader, site, stub_jobs):
    ok = downloader.post("/api/manga/download", json={
        "manga_id": 1708, "slug": "one-piece",
        "chapter_ids": ["000000000000000000000002"], "format": "pdf"})
    assert ok.status_code == 202, ok.text
    assert stub_jobs[0][2]["fmt"] == "pdf"
    assert downloader.post("/api/manga/download", json={
        "manga_id": 1708, "slug": "one-piece", "format": "epub"}).status_code == 422
    title = downloader.get("/api/manga/title/1708/one-piece").json()
    assert title["formats"] == list(mangaworld.FORMATS) and title["default_format"] == "cbz"


def test_the_settings_vocabulary_is_the_downloaders():
    import typing

    from app.routers.domain import SettingsUpdate

    annotation = SettingsUpdate.model_fields["manga_format"].annotation
    literal = next(a for a in typing.get_args(annotation) if typing.get_origin(a) is typing.Literal)
    assert typing.get_args(literal) == mangaworld.FORMATS
    assert config.SETTINGS_DEFAULTS["manga_format"] == mangaworld.FORMATS[0]
    group = SettingsUpdate.model_fields["manga_group"].annotation
    literal = next(a for a in typing.get_args(group) if typing.get_origin(a) is typing.Literal)
    assert typing.get_args(literal) == mangaworld.GROUPS
    assert config.SETTINGS_DEFAULTS["manga_group"] == mangaworld.GROUPS[0]


# ── A provider answering for the site ─────────────────────────────────────────

def test_a_certificate_failure_is_explained_and_names_the_issuer(monkeypatch):
    import ssl

    from app.core import network

    monkeypatch.setattr(network, "presented_issuer",
                        lambda host: "Wind Tre SpA — securityenforcementservice.windtre.it")
    failure = mangaworld.requests.exceptions.SSLError(
        ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    message = network.explain(failure, "streamingcommunityz.boo")
    assert "Wind Tre SpA" in message and "provider" in message and "HTTPS_PROXY" in message


def test_setting_an_isp_blocked_domain_says_why(client, monkeypatch):
    import ssl

    from app.core import network, page

    def blocked(*a, **k):
        raise page.requests.exceptions.SSLError(
            ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr(page.requests, "get", blocked)
    monkeypatch.setattr(network, "presented_issuer", lambda host: None)
    user = make_user("boss", "jf-boss", ALL)
    csrf = session_for(client, user.id)
    r = client.put("/api/domain", json={"domain": "streamingcommunityz.boo"},
                   headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400
    assert "provider" in r.json()["detail"]


# ── One book per volume ───────────────────────────────────────────────────────

def test_a_volume_is_one_book_in_reading_order_and_replaces_its_chapters(site, monkeypatch,
                                                                          tmp_path):
    folder = tmp_path / "lib" / "One Piece"
    folder.mkdir(parents=True)
    (folder / "One Piece - Cap. 0010.cbz").write_bytes(b"chapter file")
    (folder / "One Piece - Cap. 0000.cbz").write_bytes(b"volume 1, untouched")
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())

    out = mangaworld.download_volume(1708, "one-piece", 2, output_dir=str(tmp_path / "lib"),
                                     temp_dir=str(tmp_path / "tmp"))
    assert out == str(folder / "One Piece - Vol. 002.cbz")
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        info = archive.read("ComicInfo.xml").decode()
    # Two chapters of three pages each, chapter by chapter.
    assert names[:6] == ["001-001.png", "001-002.png", "001-003.png",
                         "002-001.png", "002-002.png", "002-003.png"]
    assert "<Number>2</Number>" in info and "<Title>Volume 2</Title>" in info
    # The chapter files the volume now holds are gone; other volumes' are not.
    assert sorted(os.listdir(folder)) == ["One Piece - Cap. 0000.cbz", "One Piece - Vol. 002.cbz"]

    manga = mangaworld.get_manga(1708, "one-piece")
    assert set(mangaworld.chapters_on_disk(str(tmp_path / "lib"), manga)) == {
        "000000000000000000000001", "000000000000000000000002", "000000000000000000000003"}


def test_a_volume_counts_only_the_chapters_its_manifest_lists(site, monkeypatch, tmp_path):
    # Packed when volume 2 held only chapter 10: 10.5 came later, and is missing.
    monkeypatch.setattr(mangaworld.requests, "Session", lambda: FakeSession())
    manga = mangaworld.get_manga(1708, "one-piece")
    folder = tmp_path / "lib" / "One Piece"
    folder.mkdir(parents=True)
    for fmt in ("cbz", "pdf"):
        target = folder / f"One Piece - Vol. 002.{fmt}"
        _png(tmp_path / "p.png", mode="RGB")
        mangaworld._pack(fmt, str(target), str(tmp_path), ["p.png"], manga,
                         {"label": "Volume 2", "number": "2", "volume": 2},
                         ["000000000000000000000002"])
        assert mangaworld._manifest(str(target)) == {"000000000000000000000002"}
        assert mangaworld.chapters_on_disk(str(tmp_path / "lib"), manga) == [
            "000000000000000000000002"]
        target.unlink()


def test_volume_mode_queues_each_touched_volume_once(downloader, site, stub_jobs):
    r = downloader.post("/api/manga/download", json={
        "manga_id": 1708, "slug": "one-piece", "group": "volume",
        "chapter_ids": ["000000000000000000000002", "000000000000000000000003"]})
    assert r.status_code == 202, r.text
    assert r.json()["volumes"] == 1 and r.json()["chapters"] == 0
    [(name, args, kwargs)] = stub_jobs
    assert name == "submit_manga_volume" and args[3] == 2
    assert kwargs["batch_kind"] == "manga_volumes"


def test_a_chapter_outside_any_volume_stays_a_chapter_in_volume_mode(downloader, site, stub_jobs):
    r = downloader.post("/api/manga/download", json={
        "manga_id": 3294, "slug": "nami-vs-kalifa", "group": "volume"})
    assert r.status_code == 202, r.text
    assert [c[0] for c in stub_jobs] == ["submit_manga_chapter"]


# ── The start view ────────────────────────────────────────────────────────────

HOME = """<html><body>
<div class="col-12"><h3>Capitoli di tendenza</h3><div class="row">
 <div class="entry vertical"><a class="thumb" href="https://www.mangaworld.mx/manga/1953/tower-of-god" title="Tower of God">
  <img src="https://cdn.mangaworld.mx/a.jpg"><div class="chapter">Capitolo 153</div></a>
  <p class="name"><a class="manga-title" href="https://www.mangaworld.mx/manga/1953/tower-of-god">Tower of God</a></p></div>
 <div class="entry vertical"><a class="thumb" href="https://www.mangaworld.mx/manga/1708/one-piece" title="One Piece"><img src="https://cdn.mangaworld.mx/b.jpg"></a></div>
 <div class="entry vertical"><a class="thumb" href="https://www.mangaworld.mx/manga/2278/berserk" title="Berserk"><img src="https://cdn.mangaworld.mx/c.jpg"></a>
  <a href="https://www.mangaworld.mx/manga/2278/berserk/read/000000000000000000000009">Leggi</a></div>
</div></div>
<div class="col-12"><h3>Manga del mese</h3>
 <div class="short"><div class="indi">1</div><p class="name">One Piece</p><a class="chap" href="https://www.mangaworld.mx/manga/1708/one-piece" title="One Piece">Leggi!</a></div>
 <div class="short"><div class="indi">2</div><a class="chap" href="https://www.mangaworld.mx/manga/2278/berserk" title="Berserk">Leggi!</a></div>
 <div class="short"><div class="indi">3</div><a class="chap" href="https://www.mangaworld.mx/manga/1953/tower-of-god" title="Tower of God">Leggi!</a></div>
</div>
</body></html>"""


def test_the_front_page_sections_become_shelves(monkeypatch):
    monkeypatch.setattr(mangaworld, "_fetch", lambda path, params=None: HOME)
    shelves = {s["key"]: s for s in mangaworld.home_shelves()}
    assert set(shelves) == {"trending", "monthly"}
    trending = shelves["trending"]["items"]
    assert [i["id"] for i in trending] == [1953, 1708, 2278]  # the /read/ link adds nothing
    assert trending[0]["note"] == "Capitolo 153" and trending[0]["poster"].endswith("a.jpg")
    assert [(i["name"], i.get("rank")) for i in shelves["monthly"]["items"]] == [
        ("One Piece", 1), ("Berserk", 2), ("Tower of God", 3)]


def test_the_home_route_serves_the_shelves_and_survives_a_dead_source(downloader, monkeypatch):
    from app.core import home

    home.clear_cache()
    monkeypatch.setattr(mangaworld, "_fetch", lambda path, params=None: HOME)
    assert len(downloader.get("/api/manga/home").json()["shelves"]) == 2
    home.clear_cache()

    def down(*a, **k):
        raise mangaworld.SourceUnavailable("down")

    monkeypatch.setattr(mangaworld, "_fetch", down)
    r = downloader.get("/api/manga/home")
    assert r.status_code == 200 and r.json()["shelves"] == []
    home.clear_cache()
