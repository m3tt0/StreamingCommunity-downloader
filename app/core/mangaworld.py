"""MangaWorld: search, the title page, a chapter's pages, and the chapter download.

Everything is server-rendered HTML: no API, no token, no session, no anti-bot
page. The archive (``/archive?keyword=``) lists sixteen titles per page and
carries the page count in its own hydration data; a title page lists every
chapter, grouped by volume when the site knows the volumes; the reader in list
mode (``/read/<id>/1?style=list``) holds every page of a chapter as an
``img.page-image``, in order. Images live on the source's own CDN, as static
files that need no Referer.

A chapter becomes one ``.cbz`` (a zip of the images plus a ``ComicInfo.xml``),
which is what Komga, Kavita and most readers file as one book — or a ``.zip``
(the same archive) or a ``.pdf`` (``manga_pdf``), per ``manga_format``. The archive is
written beside its final name under a dot-prefixed temporary name and moved into
place only once whole, so a library scan running meanwhile never indexes half a
chapter — the same reason the video remux stages a dot-prefixed sibling.

The host rotates TLD like the other sources. It is read per call from
``config.configured_mangaworld_host()``, never from the client, and a host that
stops answering nudges ``manga_recovery`` to look for the new one.
"""

import logging
import os
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

from app import config
from app.core import network
from app.core.headers import get_headers, sanitize_filename
from app.progress import DownloadCancelledError

logger = logging.getLogger(__name__)

SOURCE = "mangaworld"

TIMEOUT = 20
# Pages of one chapter fetched at once. Chapters themselves run under the
# panel's download slots, so the ceiling against the CDN is this times
# max_concurrent_downloads — small on purpose, images are a few hundred KB.
PAGE_WORKERS = 4
PAGE_RETRIES = 3

_MANGA_URL = re.compile(r"/manga/(\d+)/([a-z0-9-]+)")
_CHAPTER_URL = re.compile(r"/read/([0-9a-f]{24})")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
_TOTAL_PAGES = re.compile(r'"totalPages":(\d+)')
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)$", re.I)

# What the router accepts as identifiers, kept beside the parsers that produce
# them so the two cannot drift.
SLUG_PATTERN = r"^[a-z0-9-]{1,200}$"
CHAPTER_ID_PATTERN = r"^[0-9a-f]{24}$"


class SourceUnavailable(RuntimeError):
    """The configured host did not answer. The cue for the domain recovery."""


def host() -> str:
    return config.configured_mangaworld_host()


def _registrable(hostname: str) -> str:
    labels = hostname.split(".")
    return ".".join(labels[-2:])


def _nudge_recovery() -> None:
    """Look for the source's new domain, in the background and throttled.

    Fires on every failed read while the host is dead, which is exactly what
    the throttle inside ``manga_recovery.run_check`` absorbs.
    """
    if not config.get_settings().get("domain_auto_check_enabled", True):
        return
    from app.core import manga_recovery

    threading.Thread(target=manga_recovery.run_check, daemon=True,
                     name="manga-domain-check").start()


def _fetch(path: str, params: dict | None = None) -> str:
    current = host()
    try:
        r = requests.get(f"https://{current}{path}", params=params,
                         headers={"user-agent": get_headers()}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        _nudge_recovery()
        raise SourceUnavailable(network.explain(exc, current)) from exc
    if r.status_code >= 500:
        _nudge_recovery()
    r.raise_for_status()
    final = (urlparse(r.url).hostname or "").lower()
    if final and _registrable(final) != _registrable(current):
        # It still answered, through a redirect: the old domain pointing at the
        # new one. Worth proposing, not worth failing the read over.
        _nudge_recovery()
    return r.text


def looks_like_mangaworld(html: str) -> bool:
    """Whether a page is MangaWorld's own, by the site name it declares."""
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    return bool(meta and (meta.get("content") or "").strip().lower() == "mangaworld")


# ── Parsing ───────────────────────────────────────────────────────────────────

def _labelled(el) -> str:
    """The text of a "<span>Label:</span> value" block, without its label."""
    if el is None:
        return ""
    label = el.find("span")
    text = el.get_text(" ", strip=True)
    if label is not None:
        prefix = label.get_text(" ", strip=True)
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip(" :")


def _links(el) -> list[str]:
    return [a.get_text(strip=True) for a in el.find_all("a")] if el is not None else []


def _ids(href: str) -> tuple[int, str] | None:
    match = _MANGA_URL.search(href or "")
    return (int(match.group(1)), match.group(2)) if match else None


def _entry(el) -> dict | None:
    link = el.select_one("a.thumb") or el.select_one("a.manga-title")
    ids = _ids(link.get("href") if link else "")
    if not ids:
        return None
    title = el.select_one("a.manga-title")
    image = el.select_one("a.thumb img")
    return {
        "id": ids[0],
        "slug": ids[1],
        "name": (title.get_text(strip=True) if title else link.get("title", "")).strip(),
        "poster": image.get("src") if image else None,
        "kind": _labelled(el.select_one(".genre")) or None,
        "status": _labelled(el.select_one(".status")) or None,
        "author": _labelled(el.select_one(".author")) or None,
        "genres": _links(el.select_one(".genres")),
        "plot": _labelled(el.select_one(".story")) or None,
        "source": SOURCE,
    }


def search(query: str, page: int = 1) -> dict:
    """One page of the archive for ``query``: sixteen titles and the page count."""
    text = _fetch("/archive", {"keyword": query, "page": page})
    soup = BeautifulSoup(text, "lxml")
    results = [item for item in (_entry(e) for e in soup.select("div.entry")) if item]
    match = _TOTAL_PAGES.search(text)
    total = int(match.group(1)) if match else (page if results else 0)
    return {"results": results, "page": page, "total_pages": total,
            "has_more": page < total}


# The home page's own sections, by the heading the site gives them. Read by
# heading rather than by position or class: the headings are what a visitor
# sees, and the least likely part of the markup to be shuffled.
HOME_SECTIONS = (
    ("trending", "Capitoli di tendenza"),
    ("latest", "Ultimi capitoli aggiunti"),
    ("monthly", "Manga del mese"),
    ("new", "Ultime aggiunte"),
)


def _home_items(box) -> list[dict]:
    items: dict[int, dict] = {}
    for anchor in box.select('a[href*="/manga/"]'):
        href = anchor.get("href") or ""
        ids = None if "/read/" in href else _ids(href)
        if not ids:
            continue
        item = items.get(ids[0])
        if item is None:
            item = items[ids[0]] = {"id": ids[0], "slug": ids[1], "name": "",
                                    "poster": None, "note": None, "source": SOURCE}
            rank = anchor.find_previous_sibling(class_="indi") or \
                (anchor.parent.select_one(".indi") if anchor.parent else None)
            if rank and rank.get_text(strip=True).isdigit():
                item["rank"] = int(rank.get_text(strip=True))
        item["name"] = item["name"] or (anchor.get("title") or anchor.get_text(" ", strip=True))
        image = anchor.find("img")
        if image is not None and not item["poster"]:
            item["poster"] = image.get("src")
        note = anchor.select_one(".chapter")
        if note is not None and not item["note"]:
            item["note"] = note.get_text(" ", strip=True)
    return [i for i in items.values() if i["name"]]


def home_shelves() -> list[dict]:
    """The sections of MangaWorld's front page, as ``{key, title, items}``.

    A section is the nearest ancestor of its heading holding at least three
    titles: close enough not to swallow the next section, far enough to reach
    past the heading's own wrapper.
    """
    soup = BeautifulSoup(_fetch("/"), "lxml")
    shelves = []
    for key, heading in HOME_SECTIONS:
        node = next((h for h in soup.find_all(["h1", "h2", "h3", "h4"])
                     if h.get_text(" ", strip=True) == heading), None)
        box = node
        for _ in range(6):
            box = box.parent if box is not None else None
            if box is None:
                break
            items = _home_items(box)
            if len(items) >= 3:
                shelves.append({"key": key, "title": heading, "items": items[:30]})
                break
    return shelves


def _chapter(anchor, volume: int | None) -> dict | None:
    match = _CHAPTER_URL.search(anchor.get("href") or "")
    if not match:
        return None
    span = anchor.find("span")
    label = (span or anchor).get_text(" ", strip=True)
    number = None
    if label.lower().startswith("capitolo"):
        found = _NUMBER.search(label)
        number = found.group(1) if found else None
    date = anchor.select_one(".chap-date")
    return {
        "id": match.group(1),
        "label": label,
        "number": number,
        "volume": volume,
        "date": date.get_text(strip=True) if date else None,
    }


def _chapters(soup) -> list[dict]:
    """Every chapter, oldest first, each knowing its volume when the site does."""
    chapters: list[dict] = []
    volumes = soup.select(".volume-element")
    if volumes:
        for block in volumes:
            name = block.select_one(".volume-name")
            found = _NUMBER.search(name.get_text(strip=True)) if name else None
            volume = int(float(found.group(1))) if found else None
            chapters += [c for c in (_chapter(a, volume) for a in block.select(".chapter a.chap")) if c]
    else:
        chapters = [c for c in (_chapter(a, None) for a in soup.select(".chapter a.chap")) if c]

    seen, ordered = set(), []
    for chapter in reversed(chapters):  # the site lists newest first
        if chapter["id"] not in seen:
            seen.add(chapter["id"])
            ordered.append(chapter)
    return ordered


# The title page, kept briefly. A batch of N chapters is N jobs, each of which
# re-reads the title (see download_chapter): for a long series that page is
# well over a megabyte, so without this a whole-series download would fetch it
# once per chapter. Keyed on the host too, so a domain change is a miss.
MANGA_TTL = 600
_manga_cache: dict[tuple, tuple[float, dict]] = {}
_manga_lock = threading.Lock()


def clear_cache() -> None:
    with _manga_lock:
        _manga_cache.clear()


def get_manga(manga_id: int, slug: str, fresh: bool = False) -> dict:
    """The title page: details, and every chapter. Treat the result as read-only."""
    key = (host(), manga_id, slug)
    now = time.monotonic()
    if not fresh:
        with _manga_lock:
            hit = _manga_cache.get(key)
        if hit and now - hit[0] < MANGA_TTL:
            return hit[1]
    manga = _parse_manga(manga_id, slug)
    with _manga_lock:
        for stale in [k for k, (at, _) in _manga_cache.items() if now - at >= MANGA_TTL]:
            del _manga_cache[stale]
        _manga_cache[key] = (now, manga)
    return manga


def _parse_manga(manga_id: int, slug: str) -> dict:
    soup = BeautifulSoup(_fetch(f"/manga/{manga_id}/{slug}/"), "lxml")
    info = soup.select_one(".info")
    if info is None:
        raise LookupError(f"Manga {manga_id} non trovato")

    meta: dict[str, object] = {}
    for row in info.select(".meta-data > div"):
        label = row.find("span")
        if label is None:
            continue
        key = label.get_text(strip=True).rstrip(":").strip().lower()
        meta[key] = _links(row) if key == "generi" else _labelled(row)

    title = info.select_one("h1.name")
    cover = soup.select_one(".thumb img")
    plot = soup.select_one("#noidungm")
    year = _NUMBER.search(str(meta.get("anno di uscita") or ""))
    return {
        "id": manga_id,
        "slug": slug,
        "source": SOURCE,
        "name": title.get_text(strip=True) if title else slug,
        "poster": cover.get("src") if cover else None,
        "plot": plot.get_text(" ", strip=True) if plot else None,
        "kind": meta.get("tipo") or None,
        "status": meta.get("stato") or None,
        "author": meta.get("autore") or None,
        "artist": meta.get("artista") or None,
        "genres": meta.get("generi") or [],
        "year": year.group(1) if year else None,
        "url": f"https://{host()}/manga/{manga_id}/{slug}/",
        "chapters": _chapters(soup),
    }


def chapter_pages(manga_id: int, slug: str, chapter_id: str) -> list[str]:
    """The image URLs of one chapter, in reading order.

    Only images on the source's own registrable domain are accepted: the list
    comes from a page we do not control, and the panel is about to fetch every
    URL in it.
    """
    text = _fetch(f"/manga/{manga_id}/{slug}/read/{chapter_id}/1", {"style": "list"})
    soup = BeautifulSoup(text, "lxml")

    def order(img):
        found = _NUMBER.search(img.get("id") or "")
        return float(found.group(1)) if found else 0.0

    allowed = _registrable(host())
    urls: list[str] = []
    for img in sorted(soup.select("img.page-image"), key=order):
        src = (img.get("src") or img.get("data-src") or "").strip()
        parsed = urlparse(src)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or _registrable(hostname) != allowed:
            raise RuntimeError(f"Pagina su un host inatteso: {hostname or src[:80]}")
        if src not in urls:
            urls.append(src)
    if not urls:
        raise RuntimeError("Il capitolo non ha pagine")
    return urls


# ── Paths ─────────────────────────────────────────────────────────────────────

def _padded(number: str) -> str:
    whole, _, fraction = number.partition(".")
    return whole.zfill(4) + (f".{fraction}" if fraction else "")


def series_folder(title: str) -> str:
    return sanitize_filename(title)


# What a chapter can be saved as. The first is the default. CBZ and ZIP are the
# same archive under two names (Komga and Kavita read both, a file manager
# opens only the second); PDF is for readers that know nothing about comics.
FORMATS = ("cbz", "zip", "pdf")

# How chapters become files: one file each, or one per volume. Komga's model is
# series -> books, with nothing in between, so a book per chapter is what makes
# a long series read as a long flat list, and a book per volume is what makes it
# read like the shelf of tankobon it is. A chapter the source has not put in a
# volume yet stays a file of its own either way.
GROUPS = ("chapter", "volume")

# The chapters a file holds, written into the file itself. A volume grows while
# it is the newest one, so "the volume file exists" is not "every chapter of it
# is here": the manifest is what the library check reads.
_MANIFEST = "sc-panel:chapters="
_PDF_MANIFEST = re.compile(rb"/SCPanelChapters \(([0-9a-f,]*)\)")
_ZIP_MANIFEST = re.compile(re.escape(_MANIFEST) + r"([0-9a-f,]*)")


def _setting(key: str, allowed: tuple) -> str:
    value = str(config.get_settings().get(key) or "").lower()
    return value if value in allowed else allowed[0]


def configured_format() -> str:
    """The format chosen in Settings, or the default for anything unknown."""
    return _setting("manga_format", FORMATS)


def configured_group() -> str:
    """Chapter or volume, as chosen in Settings."""
    return _setting("manga_group", GROUPS)


def chapter_stem(title: str, chapter: dict) -> str:
    """``Title - Cap. 0012``; a chapter without a number keeps its label.

    Padded so a plain name sort is reading order, which is what a reader falls
    back on when it ignores ComicInfo.
    """
    number = chapter.get("number")
    tail = f"Cap. {_padded(number)}" if number else chapter.get("label") or chapter["id"]
    return sanitize_filename(f"{title} - {tail}")


def volume_stem(title: str, volume: int) -> str:
    """``Title - Vol. 007``: three digits, because a series can pass a hundred."""
    return sanitize_filename(f"{title} - Vol. {int(volume):03d}")


def chapter_filename(title: str, chapter: dict, fmt: str = FORMATS[0]) -> str:
    return f"{chapter_stem(title, chapter)}.{fmt}"


def chapter_path(root: str, title: str, chapter: dict, fmt: str = FORMATS[0]) -> str:
    return os.path.join(root, series_folder(title), chapter_filename(title, chapter, fmt))


def volume_path(root: str, title: str, volume: int, fmt: str = FORMATS[0]) -> str:
    return os.path.join(root, series_folder(title), f"{volume_stem(title, volume)}.{fmt}")


def _manifest(path: str) -> set[str]:
    """The chapter ids a file says it holds; empty when it says nothing."""
    try:
        if path.endswith(".pdf"):
            with open(path, "rb") as fh:
                # The info dictionary is written before any page, near the top.
                found = _PDF_MANIFEST.search(fh.read(256 * 1024))
            return set(found.group(1).decode().split(",")) - {""} if found else set()
        with zipfile.ZipFile(path) as archive:
            found = _ZIP_MANIFEST.search(archive.read("ComicInfo.xml").decode("utf-8", "replace"))
        return set(found.group(1).split(",")) - {""} if found else set()
    except (OSError, KeyError, zipfile.BadZipFile):
        return set()


def chapters_on_disk(root: str, manga: dict) -> list[str]:
    """Ids of the chapters already in the library, in any format or grouping.

    Every format counts: changing the setting must not make the library look
    empty and send a whole series to be downloaded again. A chapter inside a
    volume file counts when that file's manifest lists it.
    """
    folder = os.path.join(root, series_folder(manga["name"]))
    try:
        present = set(os.listdir(folder))
    except OSError:
        return []
    title = manga["name"]
    held: set[str] = set()
    for volume in {c["volume"] for c in manga["chapters"] if c.get("volume") is not None}:
        for fmt in FORMATS:
            name = f"{volume_stem(title, volume)}.{fmt}"
            if name in present:
                held |= _manifest(os.path.join(folder, name))
    return [c["id"] for c in manga["chapters"]
            if c["id"] in held
            or any(chapter_filename(title, c, f) in present for f in FORMATS)]


# ── Download ──────────────────────────────────────────────────────────────────

def _comic_info(manga: dict, book: dict, pages: int, chapter_ids: list[str]) -> str:
    fields = {
        "Title": book.get("label"),
        "Series": manga.get("name"),
        "Number": book.get("number"),
        "Volume": book.get("volume"),
        "Summary": manga.get("plot"),
        "Year": manga.get("year"),
        "Writer": manga.get("author"),
        "Penciller": manga.get("artist"),
        "Genre": ", ".join(manga.get("genres") or []) or None,
        "Web": manga.get("url"),
        "PageCount": pages,
        "LanguageISO": "it",
        "Manga": "YesAndRightToLeft",
        "Notes": _MANIFEST + ",".join(chapter_ids),
    }
    body = "".join(f"  <{k}>{escape(str(v))}</{k}>\n" for k, v in fields.items()
                   if v not in (None, ""))
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n' + body + "</ComicInfo>\n")


def _fetch_page(session: requests.Session, url: str, target: str,
                cancel_event) -> int:
    """Download one image to ``target``. Returns its size; raises after the retries."""
    last: Exception | None = None
    for attempt in range(PAGE_RETRIES):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download annullato dall'utente")
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            if not r.headers.get("content-type", "image/").startswith("image/"):
                raise RuntimeError(f"non è un'immagine ({r.headers.get('content-type')})")
            with open(target, "wb") as fh:
                fh.write(r.content)
            return len(r.content)
        except DownloadCancelledError:
            raise
        except Exception as exc:
            last = exc
            if cancel_event is not None and cancel_event.wait(1.5 * (attempt + 1)):
                raise DownloadCancelledError("Download annullato dall'utente")
    raise RuntimeError(f"pagina non scaricata: {last}")


def _pack(fmt: str, target: str, temp_dir: str, names: list[str],
          manga: dict, book: dict, chapter_ids: list[str]) -> None:
    pages = [os.path.join(temp_dir, n) for n in names]
    if fmt == "pdf":
        from app.core.manga_pdf import write_pdf

        write_pdf(target, pages, {
            "Title": f"{manga.get('name')} - {book.get('label') or ''}",
            "Author": ", ".join(dict.fromkeys(
                a for a in (manga.get("author"), manga.get("artist")) if a)),
            "Subject": manga.get("name"),
            "Keywords": ", ".join(manga.get("genres") or []),
        }, raw={"SCPanelChapters": ",".join(chapter_ids)})
        return
    # Stored, not deflated: the images are already compressed, and a deflate
    # pass would cost CPU to save nothing.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, path in zip(names, pages):
            archive.write(path, name)
        archive.writestr("ComicInfo.xml", _comic_info(manga, book, len(names), chapter_ids))


def _fetch_book(urls: list[str], names: list[str], temp_dir: str, bar, cancel_event) -> None:
    """Every page into ``temp_dir``, or an exception: never a partial set."""
    os.makedirs(temp_dir, exist_ok=True)
    session = requests.Session()
    session.headers.update({"user-agent": get_headers(), "referer": f"https://{host()}/"})

    def work(item):
        url, name = item
        size = _fetch_page(session, url, os.path.join(temp_dir, name), cancel_event)
        if bar:
            bar.update(1, bytes=size)

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(PAGE_WORKERS, len(urls)))) as pool:
            # list() so the first failure surfaces here; the pool drains the rest.
            list(pool.map(work, zip(urls, names)))
    finally:
        session.close()
    missing = [n for n in names if not os.path.isfile(os.path.join(temp_dir, n))]
    if missing:
        raise RuntimeError(f"Pagine mancanti: {len(missing)} di {len(names)}")


def _page_name(prefix: str, index: int, width: int, url: str) -> str:
    ext = _IMAGE_EXT.search(urlparse(url).path)
    return f"{prefix}{index:0{width}d}.{(ext.group(1) if ext else 'jpg').lower()}"


def _write_book(destination: str, fmt: str, temp_dir: str, names: list[str],
                manga: dict, book: dict, chapter_ids: list[str], replaces: list[str]) -> None:
    """Pack, move into place, then remove what the new file makes a duplicate.

    Staged under a dot-prefixed name beside the destination, so a library scan
    meanwhile never indexes half a book. ``replaces`` is removed only once the
    new file is whole: the same book in another format, or the chapter files a
    volume now holds — Komga would otherwise list both.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    staging = os.path.join(os.path.dirname(destination),
                           "." + os.path.basename(destination) + ".part")
    try:
        _pack(fmt, staging, temp_dir, names, manga, book, chapter_ids)
        os.replace(staging, destination)
    except BaseException:
        try:
            os.remove(staging)
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    for path in replaces:
        if path != destination:
            try:
                os.remove(path)
                logger.info("Replaced by %s: %s", os.path.basename(destination), path)
            except OSError:
                pass


def _start(progress_factory, total):
    return progress_factory(total=total, phase="pages") if progress_factory else None


def download_chapter(manga_id: int, slug: str, chapter_id: str, *,
                     output_dir: str, temp_dir: str, progress_factory=None,
                     cancel_event=None, fmt: str | None = None) -> str:
    """Fetch one chapter and file it as a ``.cbz``, ``.zip`` or ``.pdf``.

    ``fmt`` is the one asked for at submit time; None reads the setting when the
    job runs. Returns the file's path.

    Re-reads the title page rather than trusting anything the client sent: the
    name decides the path, and a retry hours later should use what the source
    says now. A chapter missing even one page fails, never packs: a book with a
    hole in it opens fine and is wrong, and nobody would know to fetch it again.
    """
    manga = get_manga(manga_id, slug)
    chapter = next((c for c in manga["chapters"] if c["id"] == chapter_id), None)
    if chapter is None:
        raise LookupError("Il capitolo non è più sulla fonte")
    fmt = fmt if fmt in FORMATS else configured_format()
    destination = chapter_path(output_dir, manga["name"], chapter, fmt)

    urls = chapter_pages(manga_id, slug, chapter_id)
    width = max(3, len(str(len(urls))))
    names = [_page_name("", i, width, u) for i, u in enumerate(urls, start=1)]
    bar = _start(progress_factory, len(urls))
    _fetch_book(urls, names, temp_dir, bar, cancel_event)
    if bar:
        bar.emit_status("packing")
    _write_book(destination, fmt, temp_dir, names, manga, chapter, [chapter_id],
                [chapter_path(output_dir, manga["name"], chapter, f) for f in FORMATS])
    if bar:
        bar.emit_status("done")
    logger.info("Manga chapter saved: %s", destination)
    return destination


def download_volume(manga_id: int, slug: str, volume: int, *,
                    output_dir: str, temp_dir: str, progress_factory=None,
                    cancel_event=None, fmt: str | None = None) -> str:
    """Fetch every chapter the source lists in ``volume`` into one book.

    The chapters come from the source when the job runs, not from the client:
    a volume still growing is packed with what it holds now, and its manifest
    says which chapters those were. Pages are named chapter-then-page, so the
    archive's own order is reading order. Once the book is whole, the chapter
    files it now contains leave the library.
    """
    manga = get_manga(manga_id, slug)
    chapters = [c for c in manga["chapters"] if c.get("volume") == volume]
    if not chapters:
        raise LookupError(f"Il volume {volume} non è più sulla fonte")
    fmt = fmt if fmt in FORMATS else configured_format()
    title = manga["name"]
    destination = volume_path(output_dir, title, volume, fmt)

    urls, names = [], []
    cw = max(3, len(str(len(chapters))))
    for ci, chapter in enumerate(chapters, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download annullato dall'utente")
        pages = chapter_pages(manga_id, slug, chapter["id"])
        pw = max(3, len(str(len(pages))))
        urls += pages
        names += [_page_name(f"{ci:0{cw}d}-", pi, pw, u) for pi, u in enumerate(pages, start=1)]

    bar = _start(progress_factory, len(urls))
    _fetch_book(urls, names, temp_dir, bar, cancel_event)
    if bar:
        bar.emit_status("packing")
    book = {"label": f"Volume {volume}", "number": str(volume), "volume": volume}
    replaces = [volume_path(output_dir, title, volume, f) for f in FORMATS]
    replaces += [chapter_path(output_dir, title, c, f) for c in chapters for f in FORMATS]
    _write_book(destination, fmt, temp_dir, names, manga, book,
                [c["id"] for c in chapters], replaces)
    if bar:
        bar.emit_status("done")
    logger.info("Manga volume saved: %s", destination)
    return destination
