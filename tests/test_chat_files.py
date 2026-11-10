"""Files in group chats: storage, kinds, the per-chat cap, retention, access and what an agent is handed."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image

from notify import chat_files
from notify.chat_files import NewFile
from notify.core import Invalid, NotFound
from notify.db import SCHEMA_VERSION, Database, iso, utcnow
from tests.test_chat_core import AGENT, build, join

MIB = 1 << 20


@pytest.fixture
def svc(tmp_path):
    s = build(tmp_path)
    yield s
    s.db.close()


def png(w=4, h=3, mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), (10, 120, 200) if mode == "RGB" else (10, 120, 200, 128)).save(buf, "PNG")
    return buf.getvalue()


def jpeg(w=4, h=3) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


def nf(name="a.txt", data=b"hello\n", ctype=None) -> NewFile:
    return NewFile(name, ctype, data)


def files_of(svc, mid):
    return svc.chat.files.for_messages([mid]).get(mid, [])


# ------------------------------------------------------------------ schema
def test_schema_5_and_table(tmp_path):
    db = Database(tmp_path / "n.sqlite3")
    assert SCHEMA_VERSION == 5
    cols = {r[1] for r in db.query("PRAGMA table_info(chat_files)")}
    assert {"id", "message_id", "group_id", "idx", "filename", "content_type", "size", "sha256", "kind", "width",
            "height", "data", "purged_at", "created_at"} <= cols
    db.close()


def test_upgrade_from_schema_4_adds_the_table(tmp_path):
    path = tmp_path / "n.sqlite3"
    db = Database(path)
    with db.tx() as c:
        c.execute("DROP TABLE chat_files")
        c.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    db.conn.close()
    db = Database(path)
    assert db.scalar("SELECT COUNT(*) FROM chat_files") == 0
    assert db.scalar("SELECT value FROM meta WHERE key='schema_version'") == "5"
    db.close()


# ------------------------------------------------------------------ kinds
def test_kind_is_decided_by_content_not_by_the_declared_type():
    assert chat_files.sniff("x.png", "image/png", png(7, 5)) == ("image", "image/png", 7, 5)
    assert chat_files.sniff("x.bin", "application/octet-stream", jpeg(6, 2)) == ("image", "image/jpeg", 6, 2)
    html = b"<html><script>alert(1)</script></html>"
    assert chat_files.sniff("x.png", "image/png", html)[0] == "other"  # says image, is not: never inline
    assert chat_files.sniff("x.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")[0] == "other"
    assert chat_files.sniff("log.txt", "text/plain", b"line\n")[:2] == ("text", "text/plain")
    assert chat_files.sniff("data.json", "application/json", b"{}")[0] == "text"
    assert chat_files.sniff("fix.patch", None, b"--- a\n+++ b\n")[0] == "text"
    assert chat_files.sniff("blob", None, b"plain words")[0] == "text"  # unknown type that reads as text
    assert chat_files.sniff("blob", None, b"\x00\x01\x02binary")[0] == "other"
    assert chat_files.sniff("log.txt", "text/plain", b"bad \xff\xfe utf8")[0] == "other"
    assert chat_files.sniff("a.pdf", "application/pdf", b"%PDF-1.7 text-like")[0] == "other"
    assert chat_files.sniff("x.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"junk")[0] == "other"  # magic only


def test_gif_and_webp_are_images():
    for fmt, ctype in (("GIF", "image/gif"), ("WEBP", "image/webp")):
        buf = io.BytesIO()
        Image.new("RGB", (5, 4), (1, 2, 3)).save(buf, fmt)
        assert chat_files.sniff("x", None, buf.getvalue()) == ("image", ctype, 5, 4)


# ------------------------------------------------------------------ posting
def test_agent_post_with_files(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    out = svc.chat.post(a["member_handle"], "see this", files=[nf("../../etc/shot.png", png(8, 6), "text/html"),
                                                               nf("run.log", b"ok\n")])
    assert out["files_expired"] == 0
    f1, f2 = out["files"]
    assert f1 == {"id": f1["id"], "name": "shot.png", "type": "image/png", "size": len(png(8, 6)), "kind": "image",
                  "width": 8, "height": 6, "expired": False}
    assert f1["id"].startswith("cf_") and (f2["name"], f2["kind"], f2["width"]) == ("run.log", "text", None)
    assert files_of(svc, out["message_id"]) == [f1, f2]
    assert svc.chat.files.member_get(b["member_handle"], f1["id"]).data == png(8, 6)


def test_file_only_message_and_its_intent(svc):
    a = join(svc, "s1")
    out = svc.chat.post(a["member_handle"], "", files=[nf("a.png", png()), nf("b.log")])
    assert svc.db.scalar("SELECT text FROM chat_messages WHERE id=?", (out["message_id"],)) == ""
    assert svc.db.scalar("SELECT intent FROM chat_members WHERE id=?", (a["member_id"],)) == "2 files: a.png, b.log"
    assert out["kind"] == "request"


def test_empty_text_without_files_is_still_refused(svc):
    a = join(svc, "s1")
    with pytest.raises(Invalid):
        svc.chat.post(a["member_handle"], "  ")
    with pytest.raises(Invalid):
        svc.chat.post(a["member_handle"], "", files=[])


def test_agent_limits(svc, monkeypatch):
    a = join(svc, "s1")
    with pytest.raises(Invalid, match="at most 10 files"):
        svc.chat.post(a["member_handle"], "x", files=[nf(f"{i}.txt") for i in range(11)])
    with pytest.raises(Invalid, match="empty"):
        svc.chat.post(a["member_handle"], "x", files=[nf("e.txt", b"")])
    monkeypatch.setattr(chat_files, "AGENT_POST_MAX", 100)
    with pytest.raises(Invalid, match="in total"):
        svc.chat.post(a["member_handle"], "x", files=[nf("a.txt", b"x" * 60), nf("b.txt", b"x" * 60)])
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files") == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE author='claude'") == 0  # nothing half-posted


def test_real_agent_limit_is_5_mib():
    assert chat_files.AGENT_POST_MAX == 5 * MIB and chat_files.MESSAGE_MAX == 10 * MIB
    assert chat_files.MAX_FILES == 10 and chat_files.CHAT_CAP == 100 * MIB


def test_summary_and_frame_line():
    fs = [{"id": "cf_1", "name": "a.png", "type": "image/png", "size": 831488, "kind": "image", "width": 1170,
           "height": 2532, "expired": False},
          {"id": "cf_2", "name": "b.log", "type": "text/plain", "size": 12, "kind": "text", "width": None,
           "height": None, "expired": True}]
    assert chat_files.summary(fs) == "2 files: a.png, b.log"
    assert chat_files.summary(fs[:1]) == "1 file: a.png"
    assert chat_files.frame_line(fs[0]) == "[file cf_1 · a.png · image 1170x2532 · 812 KB]"
    assert chat_files.frame_line(fs[1]) == "[file cf_2 · b.log · text · 12 B · expired]"


# ------------------------------------------------------------------ owner uploads
def test_owner_pending_then_post(svc):
    a = join(svc, "s1")
    gid = a["group_id"]
    p = svc.chat.files.add_pending(gid, nf("shot.png", png(9, 9)))
    assert p["kind"] == "image" and p["expired"] is False
    assert svc.db.scalar("SELECT message_id FROM chat_files WHERE id=?", (p["id"],)) is None
    out = svc.chat.post_owner(gid, "", files=[p["id"]])
    assert [f["id"] for f in out["files"]] == [p["id"]]
    assert svc.db.scalar("SELECT message_id FROM chat_files WHERE id=?", (p["id"],)) == out["message_id"]
    with pytest.raises(Invalid, match="attach it again"):  # a pending file is used once
        svc.chat.post_owner(gid, "again", files=[p["id"]])


def test_owner_post_rejects_unknown_other_group_and_duplicate_ids(svc):
    a = join(svc, "s1")
    other = join(svc, "s9", folder="/srv/other")
    p = svc.chat.files.add_pending(other["group_id"], nf())
    for ids in (["cf_nope"], [p["id"]]):
        with pytest.raises(Invalid, match="attach it again"):
            svc.chat.post_owner(a["group_id"], "x", files=ids)
    q = svc.chat.files.add_pending(a["group_id"], nf())
    with pytest.raises(Invalid):
        svc.chat.post_owner(a["group_id"], "x", files=[q["id"], q["id"]])
    assert svc.db.scalar("SELECT message_id FROM chat_files WHERE id=?", (q["id"],)) is None


def test_pending_refusals(svc):
    a = join(svc, "s1")
    with pytest.raises(Invalid, match="empty"):
        svc.chat.files.add_pending(a["group_id"], nf("e", b""))
    with pytest.raises(Invalid, match="10 MiB"):
        svc.chat.files.add_pending(a["group_id"], nf("big", b"x" * (10 * MIB + 1)))
    with pytest.raises(NotFound):
        svc.chat.files.add_pending("grp_nope", nf())
    with svc.db.tx() as c:
        c.execute("UPDATE chat_groups SET state='closed', closed_at=? WHERE id=?", (iso(utcnow()), a["group_id"]))
    with pytest.raises(Invalid, match="closed"):
        svc.chat.files.add_pending(a["group_id"], nf())


def test_pending_is_bounded_oldest_dropped_first(svc, monkeypatch):
    a = join(svc, "s1")
    gid = a["group_id"]
    ids = [svc.chat.files.add_pending(gid, nf(f"{i}.txt"))["id"] for i in range(11)]
    left = [r["id"] for r in svc.db.query("SELECT id FROM chat_files ORDER BY rowid")]
    assert left == ids[1:]
    monkeypatch.setattr(chat_files, "MESSAGE_MAX", 30)
    big = svc.chat.files.add_pending(gid, nf("big.txt", b"x" * 25))
    assert [r["id"] for r in svc.db.query("SELECT id FROM chat_files ORDER BY rowid")] == [big["id"]]


def test_discard_and_hourly_cleanup_of_pending(svc):
    a = join(svc, "s1")
    gid = a["group_id"]
    p, q = svc.chat.files.add_pending(gid, nf()), svc.chat.files.add_pending(gid, nf())
    posted = svc.chat.post_owner(gid, "x", files=[svc.chat.files.add_pending(gid, nf())["id"]])["files"][0]
    svc.chat.files.discard_pending(gid, p["id"])
    with pytest.raises(NotFound):
        svc.chat.files.discard_pending(gid, posted["id"])  # a posted file is not a pending one
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files WHERE id=?", (p["id"],)) == 0
    svc.chat.sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files WHERE id=?", (q["id"],)) == 1
    now = utcnow()
    svc.chat.now = lambda: now + timedelta(minutes=61)
    svc.chat.sweep()
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files WHERE id=?", (q["id"],)) == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files WHERE id=?", (posted["id"],)) == 1


def test_owner_get(svc):
    a = join(svc, "s1")
    m = svc.chat.post(a["member_handle"], "x", files=[nf("a.txt", b"abc")])
    f = svc.chat.files.owner_get(m["files"][0]["id"])
    assert (f.name, f.type, f.kind, f.data, f.expired, f.message_id) == ("a.txt", "text/plain", "text", b"abc", False,
                                                                        m["message_id"])
    with pytest.raises(NotFound):
        svc.chat.files.owner_get("cf_nope")


# ------------------------------------------------------------------ the cap
def test_cap_expires_the_oldest_files_of_the_chat(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "CHAT_CAP", 100)
    a = join(svc, "s1")
    h = a["member_handle"]
    f1 = svc.chat.post(h, "1", files=[nf("1.txt", b"x" * 40)])["files"][0]
    f2 = svc.chat.post(h, "2", files=[nf("2.txt", b"x" * 40)])["files"][0]
    out = svc.chat.post(h, "3", files=[nf("3.txt", b"x" * 40)])
    assert out["files_expired"] == 1
    assert [f["expired"] for f in files_of(svc, svc.db.scalar("SELECT message_id FROM chat_files WHERE id=?", (f1["id"],)))] == [True]
    assert svc.db.scalar("SELECT data FROM chat_files WHERE id=?", (f1["id"],)) is None
    assert svc.db.scalar("SELECT data FROM chat_files WHERE id=?", (f2["id"],)) is not None
    notes = [r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE author='system'")]
    assert "Older files expired to make room (1 file)" in notes
    out = svc.chat.post(h, "4", files=[nf("4.txt", b"x" * 90)])
    assert out["files_expired"] == 2
    assert "Older files expired to make room (2 files)" in [
        r["text"] for r in svc.db.query("SELECT text FROM chat_messages WHERE author='system'")]


def test_cap_counts_the_folders_whole_chat_and_ignores_pending(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "CHAT_CAP", 100)
    a = join(svc, "s1")
    old = svc.chat.post(a["member_handle"], "old", files=[nf("old.txt", b"x" * 70)])["files"][0]
    svc.chat.leave(a["member_handle"])  # the session closes; the folder's chat goes on in a new one
    b = join(svc, "s2")
    assert b["group_id"] != a["group_id"]
    svc.chat.files.add_pending(b["group_id"], nf("pending.txt", b"x" * 90))
    other = join(svc, "s3", folder="/srv/other")
    keep = svc.chat.post(other["member_handle"], "elsewhere", files=[nf("k.txt", b"x" * 90)])["files"][0]
    assert svc.chat.post(b["member_handle"], "fits", files=[nf("n.txt", b"x" * 20)])["files_expired"] == 0
    assert svc.chat.post(b["member_handle"], "over", files=[nf("m.txt", b"x" * 40)])["files_expired"] == 1
    assert svc.db.scalar("SELECT data FROM chat_files WHERE id=?", (old["id"],)) is None
    assert svc.db.scalar("SELECT data FROM chat_files WHERE id=?", (keep["id"],)) is not None


# ------------------------------------------------------------------ retention and deletion
def test_retention_purges_bytes_after_attachment_retention_days(tmp_path):
    s = build(tmp_path, attachment_retention_days=7)
    a = join(s, "s1")
    old = s.chat.post(a["member_handle"], "old", files=[nf()])["files"][0]
    new = s.chat.post(a["member_handle"], "new", files=[nf()])["files"][0]
    with s.db.tx() as c:
        c.execute("UPDATE chat_files SET created_at=? WHERE id=?", (iso(utcnow() - timedelta(days=8)), old["id"]))
    s.retention_sweep()
    row = s.db.one("SELECT data, purged_at, size FROM chat_files WHERE id=?", (old["id"],))
    assert row["data"] is None and row["purged_at"] and row["size"] == 6
    assert s.db.scalar("SELECT data FROM chat_files WHERE id=?", (new["id"],)) is not None
    f = s.chat.files.owner_get(old["id"])
    assert f.expired and f.data is None
    s.db.close()


def test_closed_group_retention_deletes_files(tmp_path):
    s = build(tmp_path, retention_days=30)
    a = join(s, "s1")
    s.chat.post(a["member_handle"], "x", files=[nf()])
    s.chat.files.add_pending(a["group_id"], nf())
    s.chat.leave(a["member_handle"])
    with s.db.tx() as c:
        c.execute("UPDATE chat_groups SET closed_at=? WHERE id=?", (iso(utcnow() - timedelta(days=40)), a["group_id"]))
    s.retention_sweep()
    assert s.db.scalar("SELECT COUNT(*) FROM chat_groups") == 0
    assert s.db.scalar("SELECT COUNT(*) FROM chat_files") == 0
    s.db.close()


def test_ageing_of_old_messages_in_an_open_group_deletes_their_files(tmp_path):
    s = build(tmp_path, retention_days=30)
    a = join(s, "s1")
    s.chat.post(a["member_handle"], "first")
    aged = s.chat.post(a["member_handle"], "aged", files=[nf()])
    kept = s.chat.post(a["member_handle"], "newest", files=[nf()])
    with s.db.tx() as c:
        c.execute("UPDATE chat_messages SET created_at=?", (iso(utcnow() - timedelta(days=40)),))
    s.retention_sweep()
    assert s.db.scalar("SELECT COUNT(*) FROM chat_messages WHERE id=?", (aged["message_id"],)) == 0
    assert [r["message_id"] for r in s.db.query("SELECT message_id FROM chat_files")] == [kept["message_id"]]
    s.db.close()


def test_deleting_a_folder_with_history_deletes_files(svc):
    a = join(svc, "s1")
    svc.chat.post(a["member_handle"], "x", files=[nf()])
    svc.chat.files.add_pending(a["group_id"], nf())
    svc.chat.leave(a["member_handle"])
    svc.chat.delete_folder("host-a", "/srv/app", with_history=True)
    assert svc.db.scalar("SELECT COUNT(*) FROM chat_files") == 0


# ------------------------------------------------------------------ access
def test_member_access_is_its_own_open_group(svc):
    a, b = join(svc, "s1"), join(svc, "s2")
    other = join(svc, "s9", folder="/srv/other")
    f = svc.chat.post(a["member_handle"], "x", files=[nf("a.txt", b"abc")])["files"][0]
    assert svc.chat.files.member_get(b["member_handle"], f["id"]).data == b"abc"
    assert svc.chat.files.member_get(a["member_handle"], f["id"]).data == b"abc"  # its own too
    with pytest.raises(NotFound):
        svc.chat.files.member_get(other["member_handle"], f["id"])
    with pytest.raises(NotFound):
        svc.chat.files.member_get(b["member_handle"], "cf_nope")
    p = svc.chat.files.add_pending(a["group_id"], nf())
    with pytest.raises(NotFound):  # not posted yet: nobody's to read
        svc.chat.files.member_get(b["member_handle"], p["id"])
    svc.chat.leave(b["member_handle"])
    with pytest.raises(NotFound):
        svc.chat.files.member_get(b["member_handle"], f["id"])


def test_files_of_an_earlier_session_need_a_used_history_approval(svc):
    a = join(svc, "s1")
    f = svc.chat.post(a["member_handle"], "x", files=[nf("a.txt", b"abc")])["files"][0]
    svc.chat.leave(a["member_handle"])
    b, c2 = join(svc, "s2"), join(svc, "s3")
    with pytest.raises(NotFound):
        svc.chat.files.member_get(b["member_handle"], f["id"])
    rid = svc.chat.history(b["member_handle"], "need the old log")["request_id"]
    svc.chat.decide_history(rid, True)
    with pytest.raises(NotFound):  # approved, not read yet
        svc.chat.files.member_get(b["member_handle"], f["id"])
    assert svc.chat.history(b["member_handle"])["status"] == "approved"
    assert svc.chat.files.member_get(b["member_handle"], f["id"]).data == b"abc"
    with pytest.raises(NotFound):  # the approval was this member's, not the group's
        svc.chat.files.member_get(c2["member_handle"], f["id"])
    now = utcnow()
    svc.chat.now = lambda: now + timedelta(minutes=31)
    with pytest.raises(NotFound):
        svc.chat.files.member_get(b["member_handle"], f["id"])


def test_history_approval_covers_only_what_the_history_showed(svc):
    old = join(svc, "s0")
    stale = svc.chat.post(old["member_handle"], "long ago", files=[nf("old.txt", b"old")])["files"][0]
    svc.chat.leave(old["member_handle"])
    with svc.db.tx() as c:  # closed longer ago than group_history reaches back
        c.execute("UPDATE chat_groups SET closed_at=? WHERE id=?", (iso(utcnow() - timedelta(days=8)), old["group_id"]))
    a = join(svc, "s1")
    recent = svc.chat.post(a["member_handle"], "recent", files=[nf("new.txt", b"new")])["files"][0]
    svc.chat.leave(a["member_handle"])
    elsewhere = join(svc, "s8", folder="/srv/other")
    foreign = svc.chat.post(elsewhere["member_handle"], "x", files=[nf("f.txt", b"f")])["files"][0]
    svc.chat.leave(elsewhere["member_handle"])
    b = join(svc, "s2")
    svc.chat.decide_history(svc.chat.history(b["member_handle"], "why")["request_id"], True)
    shown = {m["id"] for m in svc.chat.history(b["member_handle"])["messages"]}
    assert svc.db.scalar("SELECT message_id FROM chat_files WHERE id=?", (recent["id"],)) in shown
    assert svc.chat.files.member_get(b["member_handle"], recent["id"]).data == b"new"
    for fid in (stale["id"], foreign["id"]):
        with pytest.raises(NotFound):
            svc.chat.files.member_get(b["member_handle"], fid)


# ------------------------------------------------------------------ what an agent is handed
def test_for_agent_text_pages_on_line_boundaries(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "TEXT_PAGE", 25)
    a = join(svc, "s1")
    body = b"".join(b"line %02d\n" % i for i in range(10))  # 80 bytes, 8 per line
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("a.log", body)])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    assert v.kind == "text" and v.text == "line 00\nline 01\nline 02\n" and v.image is None
    assert v.note == "bytes 0–24 of 80; call again with offset=24"
    v = svc.chat.files.for_agent(f, offset=72)
    assert v.text == "line 09\n" and v.note == "bytes 72–80 of 80"
    v = svc.chat.files.for_agent(f, offset=0, limit=9)
    assert v.text == "line 00\n"
    assert svc.chat.files.for_agent(f, offset=500).text == ""
    with pytest.raises(Invalid):
        svc.chat.files.for_agent(f, offset=-1)


def test_for_agent_text_without_newlines_cuts_on_a_character(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "TEXT_PAGE", 4)
    a = join(svc, "s1")
    body = "aééé".encode()  # 1 + 3x2 bytes
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("a.txt", body)])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    assert v.text == "aé" and v.note.endswith("offset=3")
    assert svc.chat.files.for_agent(f, offset=3).text == "éé"


def test_for_agent_small_image_is_passed_as_is(svc):
    a = join(svc, "s1")
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("a.png", png(50, 40))])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    assert (v.kind, v.image, v.image_type, v.text) == ("image", png(50, 40), "image/png", None)
    assert v.note == "image 50x40"


def test_for_agent_scales_a_big_image(svc):
    a = join(svc, "s1")
    post = svc.chat.post(a["member_handle"], "x", files=[nf("big.png", png(3200, 1600, "RGBA")), nf("big.jpg", jpeg(2000, 4000))])
    p, j = (svc.chat.files.owner_get(f["id"]) for f in post["files"])
    v = svc.chat.files.for_agent(p)
    im = Image.open(io.BytesIO(v.image))
    assert (v.kind, v.image_type, im.format, im.size) == ("image", "image/png", "PNG", (1568, 784))
    assert v.note == "image scaled from 3200x1600 to 1568x784"
    v = svc.chat.files.for_agent(j)
    im = Image.open(io.BytesIO(v.image))
    assert (v.image_type, im.format, im.size) == ("image/jpeg", "JPEG", (784, 1568))


def test_for_agent_scales_by_bytes_too(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "IMAGE_MAX_BYTES", 200)
    a = join(svc, "s1")
    noisy = Image.effect_noise((300, 300), 80).convert("RGB")
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("n.png", buf.getvalue())])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    assert v.kind == "image" and v.image_type == "image/jpeg" and "scaled from 300x300" in v.note  # a PNG that stays too heavy becomes a JPEG


def test_for_agent_refuses_to_decode_too_many_pixels(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "IMAGE_MAX_PIXELS", 1000 * 1000)
    a = join(svc, "s1")
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("huge.png", png(2000, 2000))])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    assert v.kind == "other" and v.image is None
    assert v.note == f"image 2000x2000 is too large to show; save it with: notify-chat file {f.id}"


def test_only_a_jpeg_may_exceed_the_whole_decode_limit(svc, monkeypatch):
    monkeypatch.setattr(chat_files, "IMAGE_MAX_PIXELS_WHOLE", 1000 * 1000)
    a = join(svc, "s1")
    post = svc.chat.post(a["member_handle"], "x", files=[nf("big.png", png(2000, 2000)), nf("big.jpg", jpeg(2000, 2000))])
    p, j = (svc.chat.files.owner_get(f["id"]) for f in post["files"])
    assert svc.chat.files.for_agent(p).kind == "other"  # a PNG is decoded whole: 4 bytes per pixel
    v = svc.chat.files.for_agent(j)  # a JPEG is decoded at reduced size
    assert v.kind == "image" and Image.open(io.BytesIO(v.image)).size == (1568, 1568)


def test_scaling_never_holds_two_full_size_pictures(svc, monkeypatch):
    """The decoded picture is shrunk in place; a copy of it at full size doubled the memory peak."""
    copies = []
    real = Image.Image.copy
    monkeypatch.setattr(Image.Image, "copy", lambda im: (copies.append(im.size), real(im))[1])
    a = join(svc, "s1")
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("big.png", png(3000, 2000))])["files"][0]["id"])
    assert svc.chat.files.for_agent(f).kind == "image"
    assert [c for c in copies if c[0] > chat_files.IMAGE_MAX_SIDE] == []


def test_scaling_runs_one_at_a_time(svc, monkeypatch):
    import threading
    a = join(svc, "s1")
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("big.png", png(2400, 1800))])["files"][0]["id"])
    inside, peak = [0], [0]
    real = chat_files._encode

    def slow(im, as_png):
        inside[0] += 1
        peak[0] = max(peak[0], inside[0])
        threading.Event().wait(0.05)
        try:
            return real(im, as_png)
        finally:
            inside[0] -= 1
    monkeypatch.setattr(chat_files, "_encode", slow)
    ts = [threading.Thread(target=svc.chat.files.for_agent, args=(f,)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 1


def test_real_image_limits():
    assert chat_files.IMAGE_MAX_SIDE == 1568 and chat_files.IMAGE_MAX_PIXELS == 40_000_000
    assert chat_files.IMAGE_MAX_PIXELS_WHOLE == 12_000_000
    assert chat_files.IMAGE_MAX_BYTES == 3_750_000 and chat_files.TEXT_PAGE == 100_000


def test_for_agent_animated_gif_gives_the_first_frame(svc):
    a = join(svc, "s1")
    frames = [Image.new("RGB", (2000, 1000), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    f = svc.chat.files.owner_get(svc.chat.post(a["member_handle"], "x", files=[nf("a.gif", buf.getvalue())])["files"][0]["id"])
    v = svc.chat.files.for_agent(f)
    im = Image.open(io.BytesIO(v.image))
    assert im.size == (1568, 784) and getattr(im, "n_frames", 1) == 1
    r, g, b = im.convert("RGB").getpixel((10, 10))
    assert r > 200 and g < 60


def test_for_agent_other_and_expired(svc):
    a = join(svc, "s1")
    post = svc.chat.post(a["member_handle"], "x", files=[nf("a.pdf", b"%PDF-1.7\x00\x01", "application/pdf"), nf("b.txt")])
    o = svc.chat.files.owner_get(post["files"][0]["id"])
    v = svc.chat.files.for_agent(o)
    assert (v.kind, v.text, v.image) == ("other", None, None)
    assert v.note == f"a.pdf (application/pdf, 10 B) is not shown here; save it with: notify-chat file {o.id}"
    with svc.db.tx() as c:
        c.execute("UPDATE chat_files SET data=NULL, purged_at='2026-11-10T12:00:00Z' WHERE id=?", (post["files"][1]["id"],))
    v = svc.chat.files.for_agent(svc.chat.files.owner_get(post["files"][1]["id"]))
    assert (v.kind, v.note) == ("expired", "file expired on 2026-11-10")


def test_agent_client_constant_is_importable():
    assert AGENT.id == "agent"
