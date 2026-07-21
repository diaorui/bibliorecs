from datetime import datetime, date

import config
import api
import db

def sync_history(conn, bc_token, session_id, account_id, library_id):
    before = db.get_borrow_event_ids(conn)
    total_new = 0
    page = 0

    while True:
        data = api.fetch_borrowing_history(bc_token, session_id, account_id, page)
        entities = data.get("entities", {})
        history = entities.get("borrowingHistory", {})

        if not history:
            break

        page_known = all(
            eid in before for eid in history
        )
        if page_known:
            break

        for eid, entry in history.items():
            if eid in before:
                continue
            mid = entry.get("metadataId")
            if not mid:
                continue
            db.upsert_borrow_event(
                conn, library_id, mid, entry.get("checkedoutDate"),
                "history", eid, 0
            )
            total_new += 1

        page += 1

    conn.commit()
    return total_new, page


def sync_checkouts(conn, bc_token, session_id, account_id, library_id):
    data = api.fetch_current_checkouts(bc_token, session_id, account_id)
    checkouts = data.get("entities", {}).get("checkouts", {})

    db.clear_current_checkouts(conn)
    count = 0
    for cid, checkout in checkouts.items():
        mid = checkout.get("metadataId")
        if not mid:
            continue
        db.upsert_borrow_event(
            conn, library_id, mid, checkout.get("dueDate"),
            "checkout", f"co_{cid}", 1
        )
        count += 1

    conn.commit()
    return count


def auto_renew_checkouts(conn, bc_token, session_id, account_id, library_id):
    data = api.fetch_current_checkouts(bc_token, session_id, account_id)
    checkouts = data.get("entities", {}).get("checkouts", {})

    now = date.today()
    renew_ids = []
    mid_by_cid = {}

    for cid, co in checkouts.items():
        actions = co.get("actions") or []
        if "renew" not in actions:
            continue
        due = co.get("dueDate")
        if not due:
            continue
        try:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_left = (due_date - now).days
        if days_left <= config.AUTO_RENEW_DAYS_BEFORE_DUE:
            renew_ids.append(cid)
            mid_by_cid[cid] = co.get("metadataId")

    if not renew_ids:
        print(f"  No checkouts need renewal (threshold: {config.AUTO_RENEW_DAYS_BEFORE_DUE} days)")
        return 0

    print(f"  Renewing {len(renew_ids)} checkout(s): {renew_ids}")

    try:
        result = api.renew_checkouts(bc_token, session_id, account_id, renew_ids)
    except Exception as e:
        print(f"  Renewal failed: {e}")
        return 0

    renewed_ents = result.get("entities", {}).get("checkouts", {}) or {}
    failures_raw = result.get("failures") or []

    failures = {}
    if isinstance(failures_raw, dict):
        for k, v in failures_raw.items():
            failures[str(k)] = str(v)
    elif isinstance(failures_raw, list):
        for item in failures_raw:
            if not isinstance(item, dict):
                continue
            cid = item.get("checkoutId") or item.get("id") or ""
            if cid:
                failures[str(cid)] = item.get("message") or item.get("error") or str(item)

    updated = 0
    for cid in renew_ids:
        if cid in failures:
            mid = mid_by_cid.get(cid)
            reason = failures[cid]
            print(f"    {mid} ({cid}): renewal failed — {reason}")
            continue
        entity = renewed_ents.get(cid) or {}
        new_due = entity.get("dueDate")
        mid = mid_by_cid.get(cid)
        if mid and new_due:
            conn.execute(
                "UPDATE borrow_events SET checkout_date = ? WHERE metadata_id = ? AND library_id = ? AND is_current = 1",
                (new_due, mid, library_id)
            )
            updated += 1

    if updated:
        conn.commit()

    print(f"  {updated} checkouts renewed (due dates updated in DB)")
    return updated
