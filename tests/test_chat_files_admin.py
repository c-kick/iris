"""Chat files, the owner's side: upload (raw PUT, session only, through BodyLimit), the post naming the files,
the message list carrying them, and the download route with its headers."""

from __future__ import annotations

import httpx

from tests.test_admin import H  # noqa: F401
from tests.test_chat_admin import _join, ctx, fresh  # noqa: F401  (fixtures)

def _png() -> bytes:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 3), (200, 30, 90)).save(buf, "PNG")
    return buf.getvalue()


PNG = _png()


def _up(b, gid, data, name="a.png", ctype="image/png"):
    return b.put(f"/admin/api/chats/{gid}/files", params={"name": name}, content=data,
                 headers={**H, "Content-Type": ctype})


def test_upload_post_list_download(ctx):
    srv, b, bearer = ctx
    j = _join(srv, "host-a", "/srv/one", "s1")
    gid = j["group_id"]
    r = _up(b, gid, PNG)
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["kind"] == "image" and f["name"] == "a.png" and f["id"].startswith("cf_")
    p = b.post(f"/admin/api/chats/{gid}/messages", json={"text": "", "files": [f["id"]]}, headers=H)
    assert p.status_code == 201, p.text
    msgs = b.get(f"/admin/api/chats/{gid}/messages").json()["messages"]
    mine = [m for m in msgs if m.get("files")]
    assert len(mine) == 1 and mine[0]["files"][0]["id"] == f["id"]
    d = b.get(f"/admin/api/chats/files/{f['id']}")
    assert d.status_code == 200 and d.content == PNG
    assert d.headers["content-type"] == "image/png"
    assert d.headers["content-disposition"].startswith("inline;")
    assert d.headers["x-content-type-options"] == "nosniff"
    assert d.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert d.headers["cache-control"] == "private, no-cache", "nothing served from cache after sign-out"
    again = b.get(f"/admin/api/chats/files/{f['id']}", headers={"If-None-Match": d.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


def test_html_named_png_downloads_never_inline(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    f = _up(b, gid, b"<html><script>alert(1)</script></html>", name="x.png").json()
    assert f["kind"] != "image"
    b.post(f"/admin/api/chats/{gid}/messages", json={"text": "look", "files": [f["id"]]}, headers=H)
    d = b.get(f"/admin/api/chats/files/{f['id']}")
    assert d.headers["content-type"] == "application/octet-stream"
    assert d.headers["content-disposition"].startswith("attachment;")


def test_large_upload_passes_body_limit_with_session_only(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    big = b"x" * (3 << 20)  # above the 64 KiB unauthenticated and 1 MiB default caps
    assert _up(b, gid, big, name="big.log", ctype="text/plain").status_code == 201
    # without a session: refused before the body is read (401 above 64 KiB; 413 once above the 1 MiB default)
    anon = httpx.put(f"{srv.url}/admin/api/chats/{gid}/files?name=a", content=b"x" * (100 << 10), headers=H)
    assert anon.status_code == 401
    assert httpx.put(f"{srv.url}/admin/api/chats/{gid}/files?name=a", content=big, headers=H).status_code in (401, 413)
    assert _up(b, gid, b"x" * ((10 << 20) + 1), name="huge.bin", ctype="application/octet-stream").status_code == 413


def test_upload_and_download_need_the_signed_in_page(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    r = httpx.put(f"{srv.url}/admin/api/chats/{gid}/files?name=a.png", content=PNG,
                  headers={**bearer, **H, "Content-Type": "image/png"})
    assert r.status_code == 403
    f = _up(b, gid, PNG).json()
    assert httpx.get(f"{srv.url}/admin/api/chats/files/{f['id']}", headers=bearer).status_code == 403


def test_upload_needs_admin_header_and_same_origin(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    r = b.put(f"/admin/api/chats/{gid}/files?name=a.png", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 403
    r = b.put(f"/admin/api/chats/{gid}/files?name=a.png", content=PNG,
              headers={**H, "Content-Type": "image/png", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_discard_pending(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    f = _up(b, gid, PNG).json()
    assert b.delete(f"/admin/api/chats/{gid}/files/{f['id']}", headers=H).status_code == 204
    p = b.post(f"/admin/api/chats/{gid}/messages", json={"text": "", "files": [f["id"]]}, headers=H)
    assert p.status_code in (404, 422)


def test_bearer_admin_sees_no_file_names(ctx):
    srv, b, bearer = ctx
    gid = _join(srv, "host-a", "/srv/one", "s1")["group_id"]
    f = _up(b, gid, PNG, name="secret-plan.png").json()
    b.post(f"/admin/api/chats/{gid}/messages", json={"text": "x", "files": [f["id"]]}, headers=H)
    r = httpx.get(f"{srv.url}/admin/api/chats/{gid}/messages", headers=bearer)
    assert "secret-plan" not in r.text
