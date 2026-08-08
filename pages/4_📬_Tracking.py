# NOTE: Metrics computed client-side from Apps Script list payloads.
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

from config import APP_NAME, settings

st.set_page_config(page_title=f"Tracking · {APP_NAME}", page_icon="📬", layout="wide")
st.title("📬 Tracking")


def _apps_post(payload: dict) -> dict:
    url = settings.APPS_SCRIPT_TRACKING_URL
    if not url:
        return {"ok": False, "error": "APPS_SCRIPT_TRACKING_URL not set"}
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@st.cache_data(ttl=300)
def _list_tracking(what: str, campaign: str = "", exclude_bots: bool = True) -> dict:
    return _apps_post(
        {
            "action": "list",
            "what": what,
            "campaign": campaign,
            "exclude_bots": exclude_bots,
        }
    )


tab_overview, tab_hot, tab_lookup, tab_replies = st.tabs(
    ["Overview", "Hot Leads", "Contact Lookup", "Replies"]
)

with tab_overview:
    campaign = st.selectbox("Campaign filter", ["(all)"] + [], key="trk_campaign")
    # Allow free-text campaign too
    campaign_text = st.text_input("Or type campaign name", value="" if campaign == "(all)" else campaign)
    exclude_bots = st.toggle("Exclude bots", value=True)
    sends = _list_tracking("sends", campaign_text, exclude_bots)
    opens = _list_tracking("opens", campaign_text, exclude_bots)
    clicks = _list_tracking("clicks", campaign_text, exclude_bots)

    send_rows = sends.get("rows") or []
    open_rows = opens.get("rows") or []
    click_rows = clicks.get("rows") or []
    if exclude_bots:
        open_rows = [r for r in open_rows if not r.get("is_bot")]
        click_rows = [r for r in click_rows if not r.get("is_bot")]

    n_sent = len(send_rows)
    n_opened = len({r.get("email_id") for r in open_rows if r.get("email_id")})
    n_clicked = len({r.get("email_id") for r in click_rows if r.get("email_id")})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sent", n_sent)
    m2.metric("Opened", n_opened, f"{(n_opened / n_sent * 100) if n_sent else 0:.1f}%")
    m3.metric("Clicked", n_clicked, f"{(n_clicked / n_sent * 100) if n_sent else 0:.1f}%")
    m4.metric("Bot filter", "ON" if exclude_bots else "OFF")
    st.caption(
        "Apple Mail / Gmail image prefetch can inflate open rates — use bot filter and hot-lead clicks."
    )
    for label, payload in [("sends", sends), ("opens", opens), ("clicks", clicks)]:
        if payload.get("error"):
            st.warning(f"{label}: {payload['error']}")

with tab_hot:
    days = st.slider("Days window", 1, 60, 14)
    min_opens = st.slider("Min opens", 1, 20, 2)
    sends = _list_tracking("sends")
    opens = _list_tracking("opens")
    clicks = _list_tracking("clicks")
    cutoff = datetime.utcnow() - timedelta(days=days)

    def _parse_dt(val: str) -> datetime | None:
        if not val:
            return None
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None

    email_by_id = {
        r.get("email_id"): r.get("recipient_email")
        for r in (sends.get("rows") or [])
        if r.get("email_id")
    }
    stats: dict[str, dict] = defaultdict(lambda: {"opens": 0, "clicks": 0, "email": ""})
    for r in opens.get("rows") or []:
        if r.get("is_bot"):
            continue
        dt = _parse_dt(r.get("opened_at"))
        if dt and dt < cutoff:
            continue
        eid = r.get("email_id")
        email = email_by_id.get(eid) or ""
        if not email:
            continue
        stats[email]["opens"] += 1
        stats[email]["email"] = email
    for r in clicks.get("rows") or []:
        if r.get("is_bot"):
            continue
        dt = _parse_dt(r.get("clicked_at"))
        if dt and dt < cutoff:
            continue
        eid = r.get("email_id")
        email = email_by_id.get(eid) or ""
        if not email:
            continue
        stats[email]["clicks"] += 1
        stats[email]["email"] = email

    hot = [
        v
        for v in stats.values()
        if v["opens"] >= min_opens or v["clicks"] > 0
    ]
    hot.sort(key=lambda x: (x["clicks"], x["opens"]), reverse=True)
    df = pd.DataFrame(hot)
    st.dataframe(df, use_container_width=True)
    if not df.empty:
        st.download_button(
            "⬇ CSV",
            df.to_csv(index=False).encode("utf-8"),
            "hot_leads.csv",
            "text/csv",
        )

with tab_lookup:
    lookup_email = st.text_input("Recipient email")
    if lookup_email:
        sends = _list_tracking("sends")
        opens = _list_tracking("opens", exclude_bots=False)
        clicks = _list_tracking("clicks", exclude_bots=False)
        mine = [
            r
            for r in (sends.get("rows") or [])
            if (r.get("recipient_email") or "").lower() == lookup_email.lower()
        ]
        if not mine:
            st.info("No sends found for that address.")
        for srow in mine:
            eid = srow.get("email_id")
            with st.expander(f"{srow.get('sent_at')} — {srow.get('subject')} ({eid})"):
                st.write(srow)
                o = [r for r in (opens.get("rows") or []) if r.get("email_id") == eid]
                c = [r for r in (clicks.get("rows") or []) if r.get("email_id") == eid]
                st.markdown(f"**Opens ({len(o)})**")
                st.dataframe(pd.DataFrame(o) if o else pd.DataFrame())
                st.markdown(f"**Clicks ({len(c)})**")
                st.dataframe(pd.DataFrame(c) if c else pd.DataFrame())

with tab_replies:
    days_r = st.slider("Reply days window", 1, 90, 30, key="reply_days")
    data = _apps_post({"action": "list_replies", "days": days_r})
    rows = data.get("rows") or data.get("replies") or []
    if data.get("error"):
        st.warning(data["error"])
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.caption("Sequences auto-paused when a real (non-OOO) reply is detected.")
    else:
        st.info("No replies logged yet.")
