from __future__ import annotations

import httpx
import pytest

from notify.arr import translate
from tests.conftest import TOKENS, wait_for

T = ["user:alex"]

RADARR_DOWNLOAD = {
    "eventType": "Download", "instanceName": "Radarr", "applicationUrl": "",
    "movie": {"id": 1, "title": "Dune: Part Two", "year": 2024, "tmdbId": 693134, "imdbId": "tt15239678"},
    "remoteMovie": {"tmdbId": 693134, "imdbId": "tt15239678", "title": "Dune: Part Two", "year": 2024},
    "movieFile": {"id": 7, "relativePath": "Dune.Part.Two.mkv", "quality": "Bluray-1080p",
                  "releaseGroup": "FLUX", "size": 9_000_000_000},
    "isUpgrade": False, "downloadClient": "qBittorrent", "downloadId": "ABC123",
}


def test_radarr_download_reads_like_a_person_wrote_it():
    n = translate(RADARR_DOWNLOAD, T)
    assert n.title == "Radarr: Downloaded"
    assert n.message.splitlines() == ["Dune: Part Two (2024)", "Bluray-1080p · FLUX · 8.4 GB"]
    assert n.severity.value == "normal"
    assert n.url == "https://www.imdb.com/title/tt15239678/" and n.url_title == "IMDb"
    assert n.idempotency_key == "arr:Radarr:Download:ABC123"
    assert n.source == "Radarr" and n.tags == ["arr", "radarr", "download"]
    assert n.data == {"event": "Download", "instance": "Radarr", "download_id": "ABC123",
                      "tmdb_id": 693134, "imdb_id": "tt15239678"}


def test_upgrade_grab_and_test_events():
    assert translate({**RADARR_DOWNLOAD, "isUpgrade": True}, T).title == "Radarr: Upgraded"
    grab = translate({"eventType": "Grab", "instanceName": "Radarr", "movie": RADARR_DOWNLOAD["movie"],
                      "release": {"quality": "WEBDL-2160p", "releaseGroup": "NTb", "indexer": "NZBgeek",
                                  "size": 20 * 1024 ** 3},
                      "downloadClient": "SABnzbd", "downloadId": "X1"}, T)
    assert grab.severity.value == "low"
    assert grab.message.splitlines() == ["Dune: Part Two (2024)", "WEBDL-2160p · NTb · 20.0 GB",
                                         "via NZBgeek (SABnzbd)"]
    test = translate({"eventType": "Test", "instanceName": "Radarr 4K", "movie": {"title": "Test Title"}}, T)
    assert (test.title, test.message, test.idempotency_key) == ("Radarr 4K: Test", "Radarr 4K can reach hnl-notify.", None)


def test_sonarr_episodes():
    base = {"eventType": "Download", "instanceName": "Sonarr",
            "series": {"title": "Severance", "year": 2022, "tvdbId": 371980},
            "episodeFile": {"quality": "WEBDL-1080p"}, "downloadId": "S1"}
    one = translate({**base, "episodes": [{"seasonNumber": 2, "episodeNumber": 3, "title": "Who Is Alive?"}]}, T)
    assert one.message.splitlines()[0] == "Severance (2022) S02E03 · Who Is Alive?"
    assert one.url == "https://thetvdb.com/dereferrer/series/371980"
    many = translate({**base, "episodes": [{"seasonNumber": 1, "episodeNumber": i} for i in range(1, 6)]}, T)
    assert many.message.splitlines()[0] == "Severance (2022) S01E01, S01E02, S01E03 +2 more"


def test_lidarr_and_health_and_update():
    lid = translate({"eventType": "Grab", "artist": {"name": "Björk"}, "albums": [{"title": "Homogenic"}]}, T)
    assert lid.title == "Lidarr: Grabbed" and lid.message.startswith("Björk – Homogenic")
    err = translate({"eventType": "Health", "instanceName": "Prowlarr", "level": "error",
                     "message": "All indexers are unavailable", "type": "IndexerStatusCheck",
                     "wikiUrl": "https://wiki.servarr.com/prowlarr/system#indexers"}, T)
    assert (err.title, err.severity.value, err.message) == ("Prowlarr: Health error", "high",
                                                            "All indexers are unavailable")
    assert err.url_title == "Troubleshooting" and err.idempotency_key is None
    warn = translate({"eventType": "Health", "level": "warning", "message": "m"}, T)
    assert warn.severity.value == "normal"
    upd = translate({"eventType": "ApplicationUpdate", "instanceName": "Sonarr",
                     "previousVersion": "4.0.1", "newVersion": "4.0.2"}, T)
    assert upd.message == "4.0.1 → 4.0.2"


def test_manual_interaction_is_high_and_lists_reasons():
    n = translate({"eventType": "ManualInteractionRequired", "instanceName": "Radarr",
                   "movie": {"title": "Heat", "year": 1995}, "downloadId": "D9",
                   "downloadStatusMessages": [{"title": "Heat.mkv", "messages": ["Not an upgrade", "Sample"]}]}, T)
    assert n.severity.value == "high" and n.title == "Radarr: Import needs attention"
    assert n.message.splitlines() == ["Heat (1995)", "Not an upgrade", "Sample"]


def test_unknown_event_and_garbage_fields_still_produce_a_message():
    n = translate({"eventType": "SomethingNew", "movie": "not-a-dict", "episodes": 5}, T)
    assert n.title == "Radarr: SomethingNew" and n.message == "SomethingNew"
    with pytest.raises(ValueError):
        translate({"hello": 1}, T)


# ------------------------------------------------------------------ over HTTP

def arr_post(live, payload, who="sender", targets="user:alex", auth=None):
    return httpx.post(f"{live.url}/v1/ingest/arr", params={"targets": targets}, json=payload, timeout=10,
                      auth=auth if auth is not None else ("radarr", TOKENS[who]))


def test_ingest_with_basic_auth_dispatches_and_dedupes(live, fake):
    r = arr_post(live, RADARR_DOWNLOAD)
    assert r.status_code == 202, r.text
    assert r.headers["Location"] == f"/v1/notifications/{r.json()['id']}"
    wait_for(lambda: fake.messages)
    assert fake.messages[0]["title"] == "Radarr: Downloaded"
    again = arr_post(live, RADARR_DOWNLOAD)
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json()["id"] == r.json()["id"]


def test_ingest_put_bearer_and_token_as_username(live):
    r = httpx.put(f"{live.url}/v1/ingest/arr?targets=user:alex", json={"eventType": "Test"}, timeout=10,
                  headers={"Authorization": f"Bearer {TOKENS['sender']}"})
    assert r.status_code == 202, r.text
    assert arr_post(live, {"eventType": "Test"}, auth=(TOKENS["sender"], "")).status_code == 202


def test_ingest_rejections(live):
    assert arr_post(live, {"eventType": "Test"}, auth=("radarr", "wrong")).status_code == 401
    assert arr_post(live, {"eventType": "Test"}, who="reader").status_code == 403
    assert arr_post(live, {"no": "event"}).status_code == 422
    assert arr_post(live, ["x"]).status_code == 422
    assert arr_post(live, {"eventType": "Test"}, targets="user:nobody").status_code == 422
    assert arr_post(live, {"eventType": "Test"}, targets=" , ").status_code == 422
    r = httpx.post(f"{live.url}/v1/ingest/arr?targets=user:alex", content=b"{not json", timeout=10,
                   auth=("radarr", TOKENS["sender"]))
    assert r.status_code == 422


def test_comma_separated_targets(live):
    r = arr_post(live, {"eventType": "Test"}, targets="user:alex,device:alex-phone")
    assert r.status_code == 202 and len(r.json()["deliveries"]) == 1
