"""Manga from MangaWorld: search, the title, chapter downloads, and its domain.

Downloading is the only thing a manga can be: there is no request queue or
followed series for it yet, so every route here takes DOWNLOAD, the permission
that starts a download directly. The domain routes mirror ``/api/domain``'s
candidate flow: the panel proposes, a person with MANAGE_SETTINGS applies, and
the host written is the server's own, the body only confirming it.
"""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request as HttpRequest
from pydantic import BaseModel, Field

from app.auth.deps import require
from app.auth.permissions import Permission
from app.core import home as core_home, manga_recovery, mangaworld
from app.jobs import _get_library_path, job_manager
from app.routers import downloads

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/manga", tags=["manga"])

CAN_DOWNLOAD = [Depends(require(Permission.DOWNLOAD))]
CAN_MANAGE = [Depends(require(Permission.MANAGE_SETTINGS))]
CAN_READ = [Depends(require(Permission.DOWNLOAD, Permission.MANAGE_SETTINGS, mode="or"))]


def _source_error(exc: Exception) -> HTTPException:
    if isinstance(exc, mangaworld.SourceUnavailable):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    logger.warning("MangaWorld read failed: %s", exc)
    return HTTPException(status_code=502, detail="Impossibile leggere MangaWorld")


async def _read(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise _source_error(exc)


@router.get("/home", dependencies=CAN_DOWNLOAD)
async def home_page():
    """The sections of MangaWorld's own front page, for the start of the page.

    An unreadable front page is an empty list, not an error: the search box
    still works, and the shelves are a convenience.
    """
    shelves = await asyncio.to_thread(core_home.shelves, mangaworld.SOURCE, mangaworld.host())
    return {"shelves": shelves}


@router.get("/search", dependencies=CAN_DOWNLOAD)
async def search(q: str = Query(..., min_length=1, max_length=200),
                 page: int = Query(default=1, ge=1, le=200)):
    return await _read(mangaworld.search, q, page=page)


def _on_disk(manga: dict) -> list[str]:
    return mangaworld.chapters_on_disk(_get_library_path("manga"), manga)


@router.get("/title/{manga_id}/{slug}", dependencies=CAN_DOWNLOAD)
async def title(manga_id: int, slug: str = Path(..., pattern=mangaworld.SLUG_PATTERN),
                fresh: bool = False):
    manga = await _read(mangaworld.get_manga, manga_id, slug, fresh=fresh)
    on_disk = await asyncio.to_thread(_on_disk, manga)
    # The default format rides along: the page offers it pre-selected, and
    # reading Settings would take MANAGE_SETTINGS, which a downloader may lack.
    return {**manga, "on_disk": on_disk, "formats": list(mangaworld.FORMATS),
            "default_format": mangaworld.configured_format(),
            "default_group": mangaworld.configured_group()}


class MangaDownloadRequest(BaseModel):
    """Which chapters of what. Never a name or a path: those come from the source.

    ``chapter_ids`` absent means every chapter the title lists.
    """
    manga_id: int
    slug: str = Field(pattern=mangaworld.SLUG_PATTERN)
    chapter_ids: list[str] | None = Field(default=None, max_length=downloads.MAX_BATCH_JOBS)
    only_missing: bool = False
    # None: the format in Settings, read when each chapter runs.
    format: Literal["cbz", "zip", "pdf"] | None = None
    # None: the grouping in Settings. "volume" downloads every volume the chosen
    # chapters belong to, whole; a chapter in no volume stays a file of its own.
    group: Literal["chapter", "volume"] | None = None


@router.post("/download", status_code=202, dependencies=CAN_DOWNLOAD)
async def download(body: MangaDownloadRequest, http_request: HttpRequest):
    user_id = downloads._acting_user_id(http_request)

    def work():
        manga = mangaworld.get_manga(body.manga_id, body.slug)
        chapters = manga["chapters"]
        if body.chapter_ids is not None:
            wanted = set(body.chapter_ids)
            chapters = [c for c in chapters if c["id"] in wanted]
        skipped = 0
        if body.only_missing:
            present = set(_on_disk(manga))
            kept = [c for c in chapters if c["id"] not in present]
            skipped, chapters = len(chapters) - len(kept), kept
        if not chapters and skipped:
            raise HTTPException(status_code=409, detail="Tutti i capitoli sono già in libreria")
        if not chapters:
            raise HTTPException(status_code=409, detail="Nessun capitolo da scaricare")

        group = body.group or mangaworld.configured_group()
        # Volume mode: every volume a chosen chapter belongs to, once, whole —
        # a volume is one book, so half of one is not something to write.
        volumes = sorted({c["volume"] for c in chapters if c.get("volume") is not None}) \
            if group == "volume" else []
        loose = [c for c in chapters if group != "volume" or c.get("volume") is None]
        total = len(volumes) + len(loose)
        if total > downloads.MAX_BATCH_JOBS:
            raise HTTPException(
                status_code=400,
                detail=f"Troppi file in un'unica richiesta ({total}, massimo "
                       f"{downloads.MAX_BATCH_JOBS}): scegline una parte, o scarica per volume",
            )

        kind = "manga_volumes" if volumes else "manga"
        common = dict(fmt=body.format, user_id=user_id, batch_kind=kind,
                      batch_label=manga["name"])

        def volume_job(volume):
            return lambda batch_id: job_manager.submit_manga_volume(
                body.manga_id, body.slug, manga["name"], volume, batch_id=batch_id, **common)

        def chapter_job(chapter):
            return lambda batch_id: job_manager.submit_manga_chapter(
                body.manga_id, body.slug, manga["name"], chapter, batch_id=batch_id, **common)

        submits = [volume_job(v) for v in volumes] + [chapter_job(c) for c in loose]
        result = downloads._run_batch(kind, manga["name"], user_id, submits)
        return {**result, "skipped": skipped, "status": "queued",
                "volumes": len(volumes), "chapters": len(loose)}

    try:
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as exc:
        raise _source_error(exc)


# ── Domain ────────────────────────────────────────────────────────────────────

class DomainConfirm(BaseModel):
    domain: str


@router.get("/domain", dependencies=CAN_READ)
def domain_state():
    return {"host": mangaworld.host(), "candidate": manga_recovery.pending()}


@router.post("/domain/check", dependencies=CAN_MANAGE)
async def check_domain():
    return await asyncio.to_thread(manga_recovery.run_check, True)


@router.post("/domain/candidate/apply", dependencies=CAN_MANAGE)
async def apply_candidate(body: DomainConfirm):
    candidate = manga_recovery.pending()
    if not candidate:
        raise HTTPException(status_code=409, detail="Nessun dominio proposto")
    if body.domain.strip() != candidate["host"]:
        raise HTTPException(status_code=409,
                            detail="Il dominio proposto è cambiato: ricarica la pagina")
    try:
        await asyncio.to_thread(manga_recovery.apply_candidate, candidate["host"])
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"host": candidate["host"]}


@router.post("/domain/candidate/dismiss", dependencies=CAN_MANAGE)
def dismiss_candidate():
    manga_recovery.clear_pending()
    return {"ok": True}
