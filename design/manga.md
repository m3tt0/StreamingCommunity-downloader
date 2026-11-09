# Manga from MangaWorld — design notes

Why the manga section is built the way it is. The README says how to use it; CLAUDE.md lists the
rules; this file keeps the reasoning, so a later change can tell a decision from an accident.

## Scope

Download-only and gated by the `DOWNLOAD` permission. There is no request queue and no followed
manga yet: both would need a content key and a "new chapter" diff of their own, and the video
versions of those features carry years of edge cases that do not transfer as is.

The client sends ids — manga id, slug, chapter ids, volume numbers — never a name or a path. Every
job re-reads the title page when it runs: the name decides the path, and a retry hours later should
use what the source says then.

## The source

MangaWorld is server-rendered HTML; there is no API to call.

| What | Where |
|---|---|
| Search | `/archive?keyword=…&page=N`, 16 per page, `"totalPages":N` in an inline script |
| Title | `/manga/{id}/{slug}/`, chapters grouped by volume, newest first |
| Pages | `/manga/{id}/{slug}/read/{chapter}/1?style=list`, every `img.page-image`, ordered by `id="page-N"` |
| Front page | four sections found by their visible heading, not by position or class |

A page image is accepted only on the configured host's registrable domain (the CDN is
`cdn.mangaworld.mx`). A reader page that pointed elsewhere would otherwise make the panel fetch
arbitrary URLs.

Title pages are cached for 10 minutes, keyed on the host; the front page uses `app/core/home.py`'s
cache, keyed on `(source, host)` and cleared at both places the host is written.

## Files

- `Title/Title - Cap. 0012.cbz` per chapter, `Title/Title - Vol. 003.cbz` per volume. The padding
  makes a plain name sort reading order, which is what a reader falls back on when it ignores
  metadata.
- CBZ/ZIP are `ZIP_STORED`: the pages are already JPEG/PNG/WebP, deflating them costs CPU for
  nothing. `ComicInfo.xml` carries series, number, volume, plot, authors, genres and
  `Manga=YesAndRightToLeft`.
- PDF is written by `manga_pdf.py`, not Pillow: Pillow re-encodes every page as JPEG, a second
  generation of artefacts on line art. JPEG pages go in byte for byte (`DCTDecode`), anything else
  losslessly (`FlateDecode`), streamed so a long chapter never sits in memory.
- Every file is staged as a dot-prefixed `.part` beside its destination and `os.replace`d, so a
  library scan never indexes half a book. Same-name siblings in other formats are removed only once
  the new file is complete.
- A chapter missing one page fails. A book with a hole in it opens fine and is wrong, and nobody
  would know to fetch it again.

## Chapters or volumes

Komga's model is series → books with nothing in between. One book per chapter makes a long series
a flat list of hundreds; one book per volume reads like the shelf of tankōbon it is. Hence the
`manga_group` setting (`chapter` / `volume`), overridable per download.

In volume mode a request names chapters, and the panel downloads every volume those chapters belong
to, whole — half a volume is not a book. A chapter the source has not put in a volume yet stays a
file of its own. The chapter list of a volume is read when the job runs.

Because the newest volume keeps growing, "the volume file exists" does not mean "the volume is
complete". Every file therefore carries a manifest of the chapter ids it holds — ComicInfo `Notes`
(`sc-panel:chapters=…`) or a `/SCPanelChapters` key in the PDF info dictionary — and
`chapters_on_disk()` reads it. *Download missing* then re-packs a volume that gained chapters. A
finished volume removes the chapter files it now contains, so Komga does not list both.

## Domain recovery

MangaWorld publishes no page announcing its domain, but its old domains answer with a 301 to the
current one, and lapsed ones were bought and redirect to ad networks or parking pages. So candidates
are the one-hop `Location` of every host used before plus a seed list — never followed — and those
hosts directly. Each candidate goes through `domain_recovery.is_plausible()` with MangaWorld's own
name pattern (second-level domain, public DNS), then must serve the site itself
(`og:site_name`) with redirects off. The ad redirects fail the name pattern, the parking pages fail
the second-level rule. The result is proposed, never adopted, unless `domain_auto_apply` is on.

## Network errors

An Italian provider blocking a site intercepts the TLS handshake by name and presents its own
certificate (measured: Wind Tre, `securityenforcementservice.windtre.it`). Requests reports
`CERTIFICATE_VERIFY_FAILED`, which reads like a broken install. `network.explain()` connects once
more without verification — sending nothing — only to read the issuer, and names it. The fix it
suggests is a different network path (VPN, `HTTPS_PROXY`/`ALL_PROXY`, `socks5h://` supported by
PySocks); turning verification off is never an option.
