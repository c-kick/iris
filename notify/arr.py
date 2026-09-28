"""Translate Radarr / Sonarr / Lidarr / Prowlarr "Webhook" connection payloads into notifications.

The *arr apps share one webhook family: every body carries ``eventType`` and
usually ``instanceName``; the subject lives in ``movie`` (Radarr), ``series`` +
``episodes`` (Sonarr) or ``artist`` + ``albums`` (Lidarr). Everything is read
defensively so a new app version or an unknown event still produces a message.
"""

from __future__ import annotations

from typing import Any

from .models import NotificationIn, Severity

TITLE_MAX = 1000
MESSAGE_MAX = 10000

# eventType -> (verb for the title, default severity)
EVENTS: dict[str, tuple[str, Severity]] = {
    "Test": ("Test", Severity.low),
    "Grab": ("Grabbed", Severity.low),
    "Download": ("Downloaded", Severity.normal),
    "Rename": ("Renamed", Severity.min),
    "Retag": ("Retagged", Severity.min),
    "MovieAdded": ("Added", Severity.low),
    "SeriesAdd": ("Added", Severity.low),
    "ArtistAdd": ("Added", Severity.low),
    "AlbumAdd": ("Added", Severity.low),
    "MovieDelete": ("Deleted", Severity.low),
    "SeriesDelete": ("Deleted", Severity.low),
    "ArtistDelete": ("Deleted", Severity.low),
    "AlbumDelete": ("Deleted", Severity.low),
    "MovieFileDelete": ("File deleted", Severity.low),
    "EpisodeFileDelete": ("File deleted", Severity.low),
    "HealthRestored": ("Health restored", Severity.low),
    "ApplicationUpdate": ("Updated", Severity.low),
    "ManualInteractionRequired": ("Import needs attention", Severity.high),
}
HEALTH_SEVERITY = {"error": Severity.high, "warning": Severity.normal}
# Events tied to one download; keyed on downloadId so a re-sent webhook does not ping twice.
DOWNLOAD_EVENTS = {"Grab", "Download", "ManualInteractionRequired"}


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _size(n: Any) -> str | None:
    if not isinstance(n, (int, float)) or n <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _named(obj: dict[str, Any], key: str = "title") -> str | None:
    name = obj.get(key)
    if not name:
        return None
    year = obj.get("year")
    return f"{name} ({year})" if year else str(name)


def _episodes(eps: list[Any]) -> str | None:
    eps = [_d(e) for e in eps if isinstance(e, dict)]
    codes = [f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}" for e in eps
             if isinstance(e.get("seasonNumber"), int) and isinstance(e.get("episodeNumber"), int)]
    if not codes:
        return None
    if len(codes) == 1:
        title = eps[0].get("title")
        return f"{codes[0]} · {title}" if title else codes[0]
    shown = ", ".join(codes[:3])
    return shown + (f" +{len(codes) - 3} more" if len(codes) > 3 else "")


def _subject(p: dict[str, Any]) -> str | None:
    """One line naming what the event is about."""
    if movie := _d(p.get("movie")):
        return _named(movie)
    if series := _d(p.get("series")):
        name = _named(series)
        eps = _episodes(_l(p.get("episodes")))
        return f"{name} {eps}" if name and eps else name
    if artist := _d(p.get("artist")):
        name = artist.get("name") or artist.get("artistName")
        albums = [a.get("title") for a in map(_d, _l(p.get("albums"))) if a.get("title")]
        if not albums and (single := _d(p.get("album")).get("title")):
            albums = [single]
        return f"{name} – {', '.join(albums)}" if name and albums else name
    return None


def _details(p: dict[str, Any]) -> list[str]:
    """Quality / group / size / indexer, whichever the event carries."""
    f = _d(p.get("movieFile")) or _d(p.get("episodeFile")) or _d((_l(p.get("episodeFiles")) or [None])[0]) \
        or _d((_l(p.get("trackFiles")) or [None])[0])
    rel = _d(p.get("release"))
    info = _d(p.get("downloadInfo"))
    quality = f.get("quality") or rel.get("quality") or info.get("quality")
    parts = [str(quality) if quality else None,
             f.get("releaseGroup") or rel.get("releaseGroup"),
             _size(rel.get("size") or f.get("size") or info.get("size"))]
    line = " · ".join(x for x in parts if x)
    out = [line] if line else []
    if rel.get("indexer"):
        out.append(f"via {rel['indexer']}" + (f" ({p['downloadClient']})" if p.get("downloadClient") else ""))
    return out


def _link(p: dict[str, Any]) -> tuple[str | None, str | None]:
    if p.get("wikiUrl"):
        return str(p["wikiUrl"]), "Troubleshooting"
    for key in ("movie", "series", "remoteMovie"):
        obj = _d(p.get(key))
        if obj.get("imdbId"):
            return f"https://www.imdb.com/title/{obj['imdbId']}/", "IMDb"
        if key != "series" and obj.get("tmdbId"):
            return f"https://www.themoviedb.org/movie/{obj['tmdbId']}", "TMDB"
        if key == "series" and obj.get("tvdbId"):
            return f"https://thetvdb.com/dereferrer/series/{obj['tvdbId']}", "TheTVDB"
    if p.get("applicationUrl"):
        return str(p["applicationUrl"]), None
    return None, None


def _app_name(p: dict[str, Any]) -> str:
    if p.get("instanceName"):
        return str(p["instanceName"])
    for key, app in (("movie", "Radarr"), ("series", "Sonarr"), ("artist", "Lidarr")):
        if key in p:
            return app
    return "*arr"


def translate(p: dict[str, Any], targets: list[str]) -> NotificationIn:
    """Build a notification from one *arr webhook body. Raises ValueError without an eventType."""
    event = p.get("eventType")
    if not isinstance(event, str) or not event:
        raise ValueError("not an *arr webhook: missing eventType")
    app = _app_name(p)
    verb, severity = EVENTS.get(event, (event, Severity.normal))
    subject = _subject(p)
    lines: list[str] = []

    if event == "Download" and p.get("isUpgrade"):
        verb = "Upgraded"
    if event == "Health":
        level = str(p.get("level") or "").lower()
        verb, severity = f"Health {level or 'issue'}", HEALTH_SEVERITY.get(level, Severity.low)
    if event in ("Health", "HealthRestored"):
        lines.append(str(p.get("message") or p.get("type") or "Health check changed"))
    elif event == "ApplicationUpdate":
        lines.append(f"{p.get('previousVersion', '?')} → {p.get('newVersion', '?')}")
    elif event == "Test":
        lines.append(f"{app} can reach hnl-notify.")
    else:
        lines.append(subject or p.get("message") or event)
        lines += _details(p)
        if event == "ManualInteractionRequired":
            for status in map(_d, _l(p.get("downloadStatusMessages"))):
                lines += [str(m) for m in _l(status.get("messages")) if m]
        if reason := p.get("deleteReason"):
            lines.append(f"Reason: {reason}")

    url, url_title = _link(p)
    download_id = p.get("downloadId")
    key = f"arr:{app}:{event}:{download_id}" if event in DOWNLOAD_EVENTS and download_id else None
    data = {k: v for k, v in {
        "event": event, "instance": app, "download_id": download_id,
        "tmdb_id": _d(p.get("movie")).get("tmdbId"), "tvdb_id": _d(p.get("series")).get("tvdbId"),
        "imdb_id": _d(p.get("movie")).get("imdbId") or _d(p.get("series")).get("imdbId"),
    }.items() if v is not None}
    return NotificationIn(
        targets=targets,
        title=f"{app}: {verb}"[:TITLE_MAX],
        message="\n".join(lines)[:MESSAGE_MAX],
        severity=severity,
        url=url, url_title=url_title,
        tags=["arr", app.lower()[:64], event.lower()[:64]],
        source=app[:200],
        data=data,
        idempotency_key=key[:200] if key else None,
    )
