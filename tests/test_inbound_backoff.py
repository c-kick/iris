from __future__ import annotations

from types import SimpleNamespace

from notify.worker import reconcile_inbound


def prov(**kw):
    base = dict(replies_ready=True, imap_host="h", imap_port=993, imap_user="u", imap_password="old",
                authserv_id="mx", reply_address="r@example.org", reply_inbox="INBOX", reply_processed="P",
                reply_quarantine="Q")
    return SimpleNamespace(**{**base, **kw})


def test_config_change_clears_backoff_and_removed_providers_are_dropped():
    status = {"mail": {"last_error": "x"}, "gone": {"last_error": "y"}, "off": {"last_error": "z"}}
    failures, next_at, prints = {"mail": 3, "gone": 2, "off": 1}, {"mail": 9e9, "gone": 9e9, "off": 9e9}, {}
    provs = {"mail": prov(), "off": prov(replies_ready=False)}
    reconcile_inbound(provs, status, failures, next_at, prints)
    assert set(status) == {"mail"} and set(failures) == {"mail"} and set(next_at) == {"mail"}  # first sight: kept
    assert failures["mail"] == 3
    reconcile_inbound(provs, status, failures, next_at, prints)  # unchanged: backoff stays
    assert failures["mail"] == 3 and next_at["mail"] == 9e9
    provs["mail"] = prov(imap_password="new")
    reconcile_inbound(provs, status, failures, next_at, prints)
    assert "mail" not in failures and "mail" not in next_at and "mail" in status
