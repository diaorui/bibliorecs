import config
import api
import db


def sync_history(conn, bc_token, session_id, account_id):
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
                conn, mid, entry.get("checkedoutDate"),
                "history", eid, 0
            )
            total_new += 1

        page += 1

    conn.commit()
    return total_new, page


def sync_checkouts(conn, bc_token, session_id, account_id):
    data = api.fetch_current_checkouts(bc_token, session_id, account_id)
    checkouts = data.get("entities", {}).get("checkouts", {})

    db.clear_current_checkouts(conn)
    count = 0
    for cid, checkout in checkouts.items():
        mid = checkout.get("metadataId")
        if not mid:
            continue
        db.upsert_borrow_event(
            conn, mid, checkout.get("dueDate"),
            "checkout", f"co_{cid}", 1
        )
        count += 1

    conn.commit()
    return count
