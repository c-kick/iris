"""Draft editing delays closure without retaining the draft contents."""
from tests.test_chat_core import join
from tests.test_chat_wakes import svc  # noqa: F401
from tests.test_admin import fresh  # noqa: F401

A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def edit(svc, gid, browser, active, sequence=1, expires_ms=None):
    if expires_ms is None:
        expires_ms = int(svc.chat.now().timestamp() * 1000) + 600_000 if active else 0
    svc.chat.draft_lease(gid, browser, active, sequence, expires_ms)


def state(svc, gid):
    return svc.db.scalar('SELECT state FROM chat_groups WHERE id=?', (gid,))


def test_last_member_leaves_but_lead_role_ends_and_draft_expires(svc):
    member = join(svc, 'lead')
    gid = member['group_id']
    svc.chat.lead_owner(member['member_id'])
    edit(svc, gid, A, True)
    svc.chat.leave(member['member_handle'], 'ended')
    assert state(svc, gid) == 'open'
    assert svc.db.scalar('SELECT is_lead FROM chat_members WHERE id=?', (member['member_id'],)) == 0
    svc.chat.now.advance(599)
    svc.chat.sweep()
    assert state(svc, gid) == 'open'
    svc.chat.now.advance(1)
    svc.chat.sweep()
    assert state(svc, gid) == 'closed'


def test_edit_extends_lease_and_clearing_one_tab_preserves_another(svc):
    member = join(svc, 'member')
    gid = member['group_id']
    edit(svc, gid, A, True)
    svc.chat.now.advance(500)
    edit(svc, gid, B, True)
    svc.chat.leave(member['member_handle'], 'ended')
    edit(svc, gid, A, False, 2)
    assert state(svc, gid) == 'open'
    svc.chat.now.advance(599)
    svc.chat.sweep()
    assert state(svc, gid) == 'open'
    edit(svc, gid, B, False, 2)
    assert state(svc, gid) == 'closed'


def test_expiry_does_not_close_when_another_agent_joins(svc):
    member = join(svc, 'first')
    gid = member['group_id']
    edit(svc, gid, A, True)
    svc.chat.leave(member['member_handle'], 'stale')
    assert join(svc, 'second')['group_id'] == gid
    svc.chat.now.advance(601)
    svc.chat.sweep()
    assert state(svc, gid) == 'open'


def test_stale_sweep_keeps_edited_chat_open(svc):
    from tests.test_chat_core import AGENT
    member = join(svc, 'stale')
    gid = member['group_id']
    edit(svc, gid, A, True)
    for _ in range(7):
        svc.chat.heartbeat(AGENT, [])
        svc.chat.now.advance(30)
    svc.chat.heartbeat(AGENT, [])
    svc.chat.sweep()
    assert svc.db.scalar('SELECT leave_reason FROM chat_members WHERE id=?', (member['member_id'],)) == 'stale'
    assert state(svc, gid) == 'open'
    svc.chat.now.advance(391)
    svc.chat.sweep()
    assert state(svc, gid) == 'closed'


def test_admin_lease_requires_session_and_valid_id(ctx):
    from tests.test_admin import H
    from tests.test_chat_admin import _join
    srv, browser, bearer = ctx
    member = _join(srv, 'host-a', '/lease', 's1')
    path = f"/admin/api/chats/{member['group_id']}/draft-lease"
    assert browser.post(path, json={'browser_id': A, 'active': True, 'sequence': 1, 'expires_ms': int(srv.service.chat.now().timestamp()*1000)+600000}, headers=H).status_code == 204
    assert browser.post(path, json={'browser_id': 'bad', 'active': True, 'sequence': 1, 'expires_ms': 0}, headers=H).status_code == 422
    assert browser.post(path, json={'browser_id': A, 'active': True, 'sequence': 1, 'expires_ms': int(srv.service.chat.now().timestamp()*1000)+600000}, headers=bearer).status_code == 403
    srv.service.chat.leave(member['member_handle'])
    assert state(srv.service, member['group_id']) == 'open'
    assert browser.post(path, json={'browser_id': A, 'active': False, 'sequence': 2, 'expires_ms': 0}, headers=H).status_code == 204
    assert state(srv.service, member['group_id']) == 'closed'


from tests.test_chat_admin import ctx  # noqa: E402,F401


def test_delayed_edit_cannot_renew_or_override_clear(svc):
    member = join(svc, 'delayed')
    gid = member['group_id']
    deadline = int(svc.chat.now().timestamp() * 1000) + 600_000
    svc.chat.now.advance(500)
    edit(svc, gid, A, True, 1, deadline)
    svc.chat.leave(member['member_handle'], 'ended')
    svc.chat.now.advance(100)
    svc.chat.sweep()
    assert state(svc, gid) == 'closed'


def test_out_of_order_edit_cannot_undo_newer_clear(svc):
    member = join(svc, 'out-of-order')
    gid = member['group_id']
    edit(svc, gid, A, True, 1)
    edit(svc, gid, A, False, 3)
    edit(svc, gid, A, True, 2)
    svc.chat.leave(member['member_handle'], 'ended')
    assert state(svc, gid) == 'closed'


def test_legacy_idle_leave_respects_draft_lease(svc):
    member = join(svc, 'idle')
    gid = member['group_id']
    edit(svc, gid, A, True)
    with svc.db.tx() as c:
        c.execute("UPDATE chat_members SET left_at=?, leave_reason='idle' WHERE id=?", (svc.chat.now().isoformat(), member['member_id']))
    svc.chat.leave(member['member_handle'], 'ended')
    assert state(svc, gid) == 'open'
    svc.chat.now.advance(600)
    svc.chat.sweep()
    assert state(svc, gid) == 'closed'


def test_second_edit_extends_expiry_from_the_edit(svc):
    member = join(svc, 'editing')
    gid = member['group_id']
    edit(svc, gid, A, True, 1)
    svc.chat.now.advance(500)
    edit(svc, gid, A, True, 2)
    svc.chat.leave(member['member_handle'], 'ended')
    svc.chat.now.advance(599)
    svc.chat.sweep()
    assert state(svc, gid) == 'open'
    svc.chat.now.advance(1)
    svc.chat.sweep()
    assert state(svc, gid) == 'closed'


def test_browser_lease_queue():
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('draft-browser.test.mjs'))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
