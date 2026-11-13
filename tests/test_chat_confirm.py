"""Owner confirmation binds a decision to an immutable action and member context."""
from datetime import timedelta
import threading
import secrets

import pytest

from notify.core import Conflict, NotFound
from tests.test_chat_core import join
from tests.test_chat_wakes import svc  # noqa: F401
from tests.test_chat_admin import ctx, _join  # noqa: F401
from tests.test_admin import fresh, H  # noqa: F401

ACTION = 'Deploy prepared release'
TARGET = 'iris on host-a'
DETAILS = 'image: iris:0.24.2\ndigest: sha256:abcdef\ncommand: docker compose up -d'


def ask(svc, member):
    return svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, secrets.token_hex(16))['request_id']


def status(svc, member, rid, details=DETAILS):
    return svc.chat.confirm_status(member['member_handle'], rid, ACTION, TARGET, details)


def test_one_decision_retryable_but_changed_snapshot_refused(svc):
    member = join(svc, 'requester')
    rid = ask(svc, member)
    svc.chat.decide_confirm(rid, True)
    assert status(svc, member, rid)['status'] == 'approved'
    assert status(svc, member, rid)['details'] == DETAILS
    with pytest.raises(Conflict, match='changed'):
        status(svc, member, rid, details=DETAILS + ' changed')
    with pytest.raises(Conflict):
        svc.chat.decide_confirm(rid, False)
    other = join(svc, 'other')
    with pytest.raises(NotFound):
        status(svc, other, rid)


def test_approved_and_pending_requests_expire(svc):
    member = join(svc, 'requester')
    pending, approved = ask(svc, member), ask(svc, member)
    svc.chat.decide_confirm(approved, True)
    svc.chat.now.advance(1800)
    assert status(svc, member, pending)['status'] == 'expired'
    assert status(svc, member, approved)['status'] == 'expired'
    with pytest.raises(Conflict):
        svc.chat.decide_confirm(pending, True)


def test_rejoin_invalidates_old_decision_even_when_member_id_survives(svc):
    member = join(svc, 'requester')
    rid = ask(svc, member)
    svc.chat.decide_confirm(rid, True)
    replacement = join(svc, 'requester')
    assert replacement['member_id'] == member['member_id']
    with pytest.raises(Conflict, match='earlier context'):
        status(svc, replacement, rid)
    assert svc.db.scalar('SELECT state FROM chat_confirm_requests WHERE id=?', (rid,)) == 'cancelled'


def test_leave_cancels_pending_and_approved(svc):
    member = join(svc, 'requester')
    a, b = ask(svc, member), ask(svc, member)
    svc.chat.decide_confirm(b, True)
    svc.chat.leave(member['member_handle'])
    assert [r['state'] for r in svc.db.query('SELECT state FROM chat_confirm_requests ORDER BY rowid')] == ['cancelled', 'cancelled']
    with pytest.raises(Conflict):
        svc.chat.decide_confirm(a, True)


def test_concurrent_owner_decisions_one_winner(svc):
    member = join(svc, 'requester')
    rid = ask(svc, member)
    gate = threading.Barrier(2)
    outcomes = []
    def decide(approve):
        gate.wait()
        try:
            svc.chat.decide_confirm(rid, approve)
            outcomes.append('accepted')
        except Conflict:
            outcomes.append('conflict')
    threads = [threading.Thread(target=decide, args=(approve,)) for approve in (True, False)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(outcomes) == ['accepted', 'conflict']


def test_owner_ui_shows_complete_snapshot_and_requires_session(ctx):
    srv, browser, bearer = ctx
    member = _join(srv, 'host-a', '/confirm', 's1')
    rid = ask(srv.service, member)
    path = f"/admin/api/chats/{member['group_id']}/messages"
    response = browser.get(path).json()
    card = next(m['confirm_request'] for m in response['messages'] if 'confirm_request' in m)
    assert (card['action'], card['target'], card['details']) == (ACTION, TARGET, DETAILS)
    assert response['pending_confirm_requests'] == [rid]
    assert response['pending_confirm_cards'][0]['confirm_request']['details'] == DETAILS
    assert browser.get(path, headers=bearer).json()['pending_confirm_cards'] == []
    assert browser.get('/admin/api/chats').json()['folders'][0]['pending_confirm_requests'] == 1
    url = f'/admin/api/chats/confirm-requests/{rid}/approve'
    assert browser.post(url, headers=bearer).status_code == 403
    assert browser.post(url).status_code == 403
    assert browser.post(url, headers=H).status_code == 204
    assert browser.get(path).json()['pending_confirm_requests'] == []
    assert browser.get(path).json()['pending_confirm_cards'] == []
    assert browser.get('/admin/api/chats').json()['folders'][0]['pending_confirm_requests'] == 0
    assert browser.post(url, headers=H).status_code == 409


def test_pinned_cards_include_all_pending_requests_after_incremental_poll(ctx):
    srv, browser, bearer = ctx
    member = _join(srv, 'host-a', '/pinned', 's1')
    a, b = ask(srv.service, member), ask(srv.service, member)
    last_message = srv.service.db.scalar('SELECT message_id FROM chat_confirm_requests WHERE id=?', (b,))
    path = f"/admin/api/chats/{member['group_id']}/messages?after={last_message}"
    response = browser.get(path).json()
    assert response['messages'] == []
    cards = response['pending_confirm_cards']
    assert {card['confirm_request']['id'] for card in cards} == {a, b}
    assert all(card['confirm_request']['details'] == DETAILS for card in cards)
    assert browser.post(f'/admin/api/chats/confirm-requests/{a}/deny', headers=H).status_code == 204
    assert [card['confirm_request']['id'] for card in browser.get(path).json()['pending_confirm_cards']] == [b]
    assert browser.get(path, headers=bearer).json()['pending_confirm_cards'] == []


def test_reset_cancels_pending_and_approved_before_context_replacement(svc):
    from tests.test_chat_core import AGENT
    from tests.test_chat_reset import _claim, _free
    member = join(svc, 'reset')
    pending, approved = ask(svc, member), ask(svc, member)
    svc.chat.decide_confirm(approved, True)
    _free(svc, 'reset')
    svc.chat.post_owner(member['group_id'], f"/clear @{member['name']}")
    reset = _claim(svc)
    svc.chat.reset_done(AGENT, reset['reset_id'], 'relay-a', 'reset')
    assert [r['state'] for r in svc.db.query('SELECT state FROM chat_confirm_requests ORDER BY rowid')] == ['cancelled', 'cancelled']
    for rid in (pending, approved):
        with pytest.raises(Conflict, match='reset is in progress'):
            status(svc, member, rid)
    with pytest.raises(Conflict, match='reset is in progress'):
        ask(svc, member)


def test_retry_key_is_idempotent_and_rejects_changed_snapshot(svc):
    member = join(svc, 'retry')
    key = 'stable-key-123'
    first = svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, key)
    again = svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, key)
    assert first['request_id'] == again['request_id']
    assert svc.db.scalar('SELECT COUNT(*) FROM chat_confirm_requests') == 1
    with pytest.raises(Conflict, match='different action'):
        svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS + ' modified', key)


def test_expiry_wake_never_changes_authoritative_result(svc):
    member = join(svc, 'expiry')
    rid = ask(svc, member)
    svc.chat.decide_confirm(rid, True)
    svc.chat.now.advance(1800)
    svc.chat.expire_confirmations()
    assert status(svc, member, rid)['status'] == 'expired'
    assert svc.db.scalar('SELECT state FROM chat_confirm_requests WHERE id=?', (rid,)) == 'expired'


def test_mcp_confirmation_end_to_end_and_member_scope(chatlive):
    from tests.test_chat_api import join as api_join
    from tests.test_mcp import run_session, payload
    srv = chatlive
    requester = api_join(srv, sid='confirm-one')
    other = api_join(srv, sid='confirm-two')
    args = {'member_handle': requester['member_handle'], 'action': ACTION, 'target': TARGET,
            'details': DETAILS, 'request_key': 'stable_request_123'}
    async def create(session):
        return await session.call_tool('group_confirm_request', args)
    first = run_session(srv, 'hosta', create)
    assert not first.isError
    rid = payload(first)['request_id']
    srv.service.chat.decide_confirm(rid, True)
    async def check(session, handle):
        return await session.call_tool('group_confirm_status', {k: v for k, v in args.items() if k != 'request_key'} | {'member_handle': handle, 'request_id': rid})
    approved = run_session(srv, 'hosta', lambda s: check(s, requester['member_handle']))
    assert payload(approved)['status'] == 'approved'
    wrong = run_session(srv, 'hosta', lambda s: check(s, other['member_handle']))
    assert wrong.isError
    wrong_host = run_session(srv, 'hostb', lambda s: check(s, requester['member_handle']))
    assert wrong_host.isError


from tests.test_chat_api import chatlive  # noqa: E402,F401


def test_requester_can_cancel_and_others_cannot(svc):
    member = join(svc, 'requester')
    other = join(svc, 'other')
    rid = ask(svc, member)
    with pytest.raises(NotFound):
        svc.chat.confirm_cancel(other['member_handle'], rid)
    assert svc.chat.confirm_cancel(member['member_handle'], rid)['status'] == 'cancelled'
    assert status(svc, member, rid)['status'] == 'cancelled'
    with pytest.raises(Conflict):
        svc.chat.decide_confirm(rid, True)


def test_cancel_approved_revokes_future_status(svc):
    member = join(svc, 'requester')
    rid = ask(svc, member)
    svc.chat.decide_confirm(rid, True)
    svc.chat.confirm_cancel(member['member_handle'], rid)
    assert status(svc, member, rid)['status'] == 'cancelled'


def test_denial_is_retryable_and_never_becomes_approval(svc):
    member = join(svc, 'denied')
    rid = ask(svc, member)
    svc.chat.decide_confirm(rid, False)
    assert status(svc, member, rid)['status'] == 'denied'
    assert status(svc, member, rid)['status'] == 'denied'
    with pytest.raises(Conflict):
        svc.chat.decide_confirm(rid, True)


def test_owner_card_escapes_and_keeps_long_snapshot():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('confirm-card.test.mjs'))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_retry_sends_one_notice_without_action_details(svc, monkeypatch):
    member = join(svc, 'notice')
    svc.config.server.chat_alert_targets = ['user:alex']
    sent = []
    monkeypatch.setattr(svc, 'submit', lambda _client, notification: sent.append(notification))
    key = 'retry-key-1234'
    svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, key)
    svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, key)
    assert len(sent) == 1
    assert DETAILS not in sent[0].message and ACTION not in sent[0].message


def test_concurrent_request_retries_create_one_snapshot(svc):
    member = join(svc, 'retry-race')
    barrier = threading.Barrier(2)
    ids = []
    def request():
        barrier.wait()
        ids.append(svc.chat.confirm_request(member['member_handle'], ACTION, TARGET, DETAILS, 'concurrent-key')['request_id'])
    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(ids) == 2 and ids[0] == ids[1]
    assert svc.db.scalar('SELECT COUNT(*) FROM chat_confirm_requests') == 1


def test_queued_and_claimed_reset_blocks_and_permanently_cancels_consent(svc):
    from tests.test_chat_core import AGENT
    from tests.test_chat_reset import _claim, _free
    member = join(svc, 'reset-pending')
    pending, approved = ask(svc, member), ask(svc, member)
    svc.chat.decide_confirm(approved, True)
    _free(svc, 'reset-pending')
    svc.chat.request_reset(member['group_id'], member_ids=[member['member_id']])
    for rid in (pending, approved):
        with pytest.raises(Conflict):
            status(svc, member, rid)
        with pytest.raises(Conflict):
            svc.chat.decide_confirm(rid, True)
    with pytest.raises(Conflict, match='reset is in progress'):
        ask(svc, member)
    reset = _claim(svc)
    with pytest.raises(Conflict, match='reset is in progress'):
        ask(svc, member)
    svc.chat.reset_done(AGENT, reset['reset_id'], 'relay-a', 'typed')
    # A failed reset does not resurrect approval; a fresh request is possible.
    with pytest.raises(Conflict, match='earlier context'):
        status(svc, member, approved)
    assert svc.db.scalar('SELECT state FROM chat_confirm_requests WHERE id=?', (approved,)) == 'cancelled'
    assert ask(svc, member)
