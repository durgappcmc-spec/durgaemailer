# NOTE: Metrics computed client-side from Apps Script list payloads.
# Tracking UI focuses on draft-originated outreach that was sent (incl. send-from-Gmail).
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

from config import APP_NAME, settings
from core.auth_ui import logout_button, require_login
from core.ist_time import format_ist, parse_tracking_dt
from core.prospect_list import designation_for_row, titles_by_email

st.set_page_config(page_title=f"Tracking · {APP_NAME}", page_icon="📬", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("📬 Tracking")
st.caption(
    "Shows opens/clicks for Relay **drafts you sent** from Gmail (and Relay sends). "
    "Tracking survives deploys in Google Sheets. "
    "Gmail image proxy often marks opens as bots — keep Exclude bots on and trust clicks more."
)
if st.button("Refresh data", type="secondary"):
    st.cache_data.clear()
    st.rerun()


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


def _clean_eid(val: object) -> str:
    s = str(val or "").strip()
    if not s or "\n" in s or "http" in s:
        return ""
    if s.endswith(".gif"):
        s = s[:-4]
    return s if len(s) >= 32 else ""


def _is_draft_outreach(row: dict) -> bool:
    """Keep draft-pipeline / sent outreach; drop diagnostics and empty backfills."""
    src = str(row.get("source") or "").lower()
    email = str(row.get("recipient_email") or "").strip()
    if src in ("diag", "test") or src.startswith("diag"):
        return False
    if "backfill" in src and not email:
        return False
    if "draft" in src:
        return bool(email) or bool(row.get("subject"))
    # Scheduled / direct sends with a recipient still count
    if email and src not in ("",):
        return True
    return bool(email)


def _parse_dt(val: str) -> datetime | None:
    return parse_tracking_dt(val)


@st.cache_data(ttl=120)
def _prospect_titles_by_email() -> dict[str, str]:
    try:
        return titles_by_email()
    except Exception:
        return {}


def _queue_followup(
    email: str, subject: str, name: str = "", designation: str = ""
) -> None:
    who = name or email
    title = (designation or "").strip()
    label = who
    if title and title.lower() not in who.lower():
        label = f"{who}, {title}" if name else title
    prior = subject or "our recent note"
    st.session_state.force_prompt = (
        f'Draft a polite follow-up email to {email}'
        + (f" ({label})" if label and label.lower() not in email.lower() else "")
        + f' about "{prior}". Reference the prior outreach briefly and ask for a short call '
        f"or next step. Keep it concise."
    )
    st.switch_page("pages/1_💬_Chat.py")


tab_overview, tab_followups, tab_hot, tab_lookup, tab_replies = st.tabs(
    ["Overview", "Follow-ups", "Hot Leads", "Contact Lookup", "Replies"]
)

with tab_overview:
    campaign = st.selectbox("Campaign filter", ["(all)"] + [], key="trk_campaign")
    campaign_text = st.text_input(
        "Or type campaign name", value="" if campaign == "(all)" else campaign
    )
    exclude_bots = st.toggle("Exclude bots", value=True)
    sends = _list_tracking("sends", campaign_text, exclude_bots)
    opens = _list_tracking("opens", campaign_text, exclude_bots)
    clicks = _list_tracking("clicks", campaign_text, exclude_bots)

    send_rows = [r for r in (sends.get("rows") or []) if _is_draft_outreach(r)]
    open_rows = opens.get("rows") or []
    click_rows = clicks.get("rows") or []
    if exclude_bots:
        open_rows = [r for r in open_rows if not r.get("is_bot")]
        click_rows = [r for r in click_rows if not r.get("is_bot")]

    send_ids = {_clean_eid(r.get("email_id")) for r in send_rows}
    send_ids.discard("")
    open_rows = [r for r in open_rows if _clean_eid(r.get("email_id")) in send_ids]
    click_rows = [r for r in click_rows if _clean_eid(r.get("email_id")) in send_ids]

    n_sent = len(send_rows)
    n_opened = len({_clean_eid(r.get("email_id")) for r in open_rows if _clean_eid(r.get("email_id"))})
    n_clicked = len(
        {_clean_eid(r.get("email_id")) for r in click_rows if _clean_eid(r.get("email_id"))}
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Sent (drafts)", n_sent)
    m2.metric("Opened", n_opened, f"{(n_opened / n_sent * 100) if n_sent else 0:.1f}%")
    m3.metric("Clicked", n_clicked, f"{(n_clicked / n_sent * 100) if n_sent else 0:.1f}%")
    m4.metric("Bot filter", "ON" if exclude_bots else "OFF")
    st.caption(
        "Only draft-originated / recipient-tagged outreach. Apple Mail / Gmail prefetch can inflate opens."
    )
    for label, payload in [("sends", sends), ("opens", opens), ("clicks", clicks)]:
        if payload.get("error"):
            st.warning(f"{label}: {payload['error']}")

with tab_followups:
    st.caption(
        "Opened (and optionally clicked) **draft sends** with recipient + subject — for follow-up outreach. "
        "First / last open times are Indian Standard Time."
    )
    include_unopened = st.toggle("Include tracked sends with no opens yet", value=False)
    exclude_bots_f = st.toggle("Exclude bot opens", value=True, key="fu_bots")
    sends_f = _list_tracking("sends", exclude_bots=False)
    opens_f = _list_tracking("opens", exclude_bots=False)
    clicks_f = _list_tracking("clicks", exclude_bots=False)

    send_rows_f = [r for r in (sends_f.get("rows") or []) if _is_draft_outreach(r)]
    prospect_titles = _prospect_titles_by_email()
    open_rows_f = opens_f.get("rows") or []
    click_rows_f = clicks_f.get("rows") or []
    if exclude_bots_f:
        open_rows_f = [r for r in open_rows_f if not r.get("is_bot")]
        click_rows_f = [r for r in click_rows_f if not r.get("is_bot")]

    open_by: dict[str, list] = defaultdict(list)
    for r in open_rows_f:
        eid = _clean_eid(r.get("email_id"))
        if eid:
            open_by[eid].append(r)
    click_by: dict[str, list] = defaultdict(list)
    for r in click_rows_f:
        eid = _clean_eid(r.get("email_id"))
        if eid:
            click_by[eid].append(r)

    fu_rows: list[dict] = []
    for srow in send_rows_f:
        eid = _clean_eid(srow.get("email_id"))
        if not eid:
            continue
        olist = open_by.get(eid) or []
        clist = click_by.get(eid) or []
        if not include_unopened and not olist and not clist:
            continue
        opens_at = [str(x.get("opened_at") or "") for x in olist if x.get("opened_at")]
        fu_rows.append(
            {
                "recipient_email": srow.get("recipient_email") or "",
                "recipient_name": srow.get("recipient_name") or "",
                "designation": designation_for_row(srow, prospect_titles),
                "subject": srow.get("subject") or "",
                "campaign": srow.get("campaign") or "",
                "source": srow.get("source") or "",
                "sent_at": srow.get("sent_at") or "",
                "opens": len(olist),
                "clicks": len(clist),
                "first_open": min(opens_at) if opens_at else "",
                "last_open": max(opens_at) if opens_at else "",
                "email_id": eid,
            }
        )
    fu_rows.sort(
        key=lambda r: (r["opens"] > 0, r["clicks"], r["opens"], r["last_open"]),
        reverse=True,
    )
    for r in fu_rows:
        r["first_open"] = format_ist(r["first_open"]) if r["first_open"] else ""
        r["last_open"] = format_ist(r["last_open"]) if r["last_open"] else ""
    opened_only = [r for r in fu_rows if r["opens"] > 0 or r["clicks"] > 0]
    c1, c2, c3 = st.columns(3)
    c1.metric("In list", len(fu_rows))
    c2.metric("Opened / clicked", len(opened_only))
    c3.metric(
        "Missing recipient",
        sum(1 for r in fu_rows if not r["recipient_email"]),
    )
    df_fu = pd.DataFrame(fu_rows)
    if df_fu.empty:
        st.info(
            "No follow-up rows yet. After recipients open tracked draft emails, they appear here."
        )
    else:
        st.dataframe(df_fu, use_container_width=True)
        st.download_button(
            "⬇ Follow-ups CSV",
            df_fu.to_csv(index=False).encode("utf-8"),
            "tracking_followups.csv",
            "text/csv",
        )
        st.markdown("**Create follow-up draft**")
        for i, r in enumerate(opened_only[:40]):
            if not r["recipient_email"]:
                continue
            cols = st.columns([4, 1])
            cols[0].markdown(
                f"**{r['recipient_email']}**"
                + (f" · {r['recipient_name']}" if r.get("recipient_name") else "")
                + (f"  \n{r['designation']}" if r.get("designation") else "")
                + f"  \n{(r.get('subject') or '')[:100]}"
                + (
                    f"  \nFirst open: {r['first_open']} · Last open: {r['last_open']}"
                    if r.get("first_open") or r.get("last_open")
                    else ""
                )
            )
            if cols[1].button("Follow up", key=f"fu_btn_{i}_{r['email_id'][:8]}"):
                _queue_followup(
                    r["recipient_email"],
                    r["subject"],
                    r.get("recipient_name") or "",
                    r.get("designation") or "",
                )

with tab_hot:
    st.caption(
        "Opened draft sends with full email details. Create a follow-up draft in Chat in one click. "
        "First / last open times are Indian Standard Time."
    )
    days = st.slider("Days window", 1, 60, 14)
    min_opens = st.slider("Min opens", 1, 20, 1)
    exclude_bots_h = st.toggle("Exclude bot opens", value=True, key="hot_bots")
    sends = _list_tracking("sends")
    opens = _list_tracking("opens", exclude_bots=False)
    clicks = _list_tracking("clicks", exclude_bots=False)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    send_rows = [r for r in (sends.get("rows") or []) if _is_draft_outreach(r)]
    prospect_titles = _prospect_titles_by_email()
    send_by_eid = {_clean_eid(r.get("email_id")): r for r in send_rows if _clean_eid(r.get("email_id"))}

    open_rows = opens.get("rows") or []
    click_rows = clicks.get("rows") or []
    if exclude_bots_h:
        open_rows = [r for r in open_rows if not r.get("is_bot")]
        click_rows = [r for r in click_rows if not r.get("is_bot")]

    # Aggregate per email_id so subject stays attached to the opened send
    stats: dict[str, dict] = {}
    for r in open_rows:
        eid = _clean_eid(r.get("email_id"))
        if not eid or eid not in send_by_eid:
            continue
        dt = _parse_dt(r.get("opened_at"))
        if dt and dt < cutoff:
            continue
        srow = send_by_eid[eid]
        email = (srow.get("recipient_email") or "").strip()
        if not email:
            continue
        stt = stats.setdefault(
            eid,
            {
                "recipient_email": email,
                "recipient_name": srow.get("recipient_name") or "",
                "designation": designation_for_row(srow, prospect_titles),
                "subject": srow.get("subject") or "",
                "campaign": srow.get("campaign") or "",
                "source": srow.get("source") or "",
                "sent_at": srow.get("sent_at") or "",
                "email_id": eid,
                "opens": 0,
                "clicks": 0,
                "first_open": "",
                "last_open": "",
            },
        )
        stt["opens"] += 1
        ts = str(r.get("opened_at") or "")
        if ts:
            if not stt["first_open"] or ts < stt["first_open"]:
                stt["first_open"] = ts
            if not stt["last_open"] or ts > stt["last_open"]:
                stt["last_open"] = ts

    for r in click_rows:
        eid = _clean_eid(r.get("email_id"))
        if not eid or eid not in send_by_eid:
            continue
        dt = _parse_dt(r.get("clicked_at"))
        if dt and dt < cutoff:
            continue
        srow = send_by_eid[eid]
        email = (srow.get("recipient_email") or "").strip()
        if not email:
            continue
        stt = stats.setdefault(
            eid,
            {
                "recipient_email": email,
                "recipient_name": srow.get("recipient_name") or "",
                "designation": designation_for_row(srow, prospect_titles),
                "subject": srow.get("subject") or "",
                "campaign": srow.get("campaign") or "",
                "source": srow.get("source") or "",
                "sent_at": srow.get("sent_at") or "",
                "email_id": eid,
                "opens": 0,
                "clicks": 0,
                "first_open": "",
                "last_open": "",
            },
        )
        stt["clicks"] += 1

    hot = [
        v
        for v in stats.values()
        if v["opens"] >= min_opens or v["clicks"] > 0
    ]
    hot.sort(key=lambda x: (x["clicks"], x["opens"], x["last_open"]), reverse=True)
    for row in hot:
        row["first_open"] = format_ist(row["first_open"]) if row["first_open"] else ""
        row["last_open"] = format_ist(row["last_open"]) if row["last_open"] else ""
    df = pd.DataFrame(hot)
    if df.empty:
        st.info(
            "No hot leads yet from draft sends with opens. "
            "When a prospect opens a tracked draft email, they show here with full details."
        )
    else:
        st.dataframe(
            df[
                [
                    c
                    for c in [
                        "recipient_email",
                        "recipient_name",
                        "designation",
                        "subject",
                        "opens",
                        "clicks",
                        "first_open",
                        "last_open",
                        "campaign",
                        "sent_at",
                        "email_id",
                    ]
                    if c in df.columns
                ]
            ],
            use_container_width=True,
        )
        st.download_button(
            "⬇ CSV",
            df.to_csv(index=False).encode("utf-8"),
            "hot_leads.csv",
            "text/csv",
        )
        st.markdown("**Create follow-up**")
        for i, row in enumerate(hot[:50]):
            cols = st.columns([4, 1])
            cols[0].markdown(
                f"**{row['recipient_email']}**"
                + (f" · {row['recipient_name']}" if row.get("recipient_name") else "")
                + (f"  \n{row['designation']}" if row.get("designation") else "")
                + f"  \n{(row.get('subject') or '')[:100]}  \n"
                f"Opens: {row['opens']} · Clicks: {row['clicks']}"
                + (
                    f" · First open: {row['first_open']}"
                    if row.get("first_open")
                    else ""
                )
                + (
                    f" · Last open: {row['last_open']}"
                    if row.get("last_open")
                    else ""
                )
            )
            if cols[1].button("Follow up", key=f"hot_fu_{i}_{row['email_id'][:8]}"):
                _queue_followup(
                    row["recipient_email"],
                    row.get("subject") or "",
                    row.get("recipient_name") or "",
                    row.get("designation") or "",
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
            if _is_draft_outreach(r)
            and (r.get("recipient_email") or "").lower() == lookup_email.lower()
        ]
        if not mine:
            st.info("No draft sends found for that address.")
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
                if (srow.get("recipient_email") or "").strip():
                    if st.button("Create follow-up", key=f"lookup_fu_{eid}"):
                        _queue_followup(
                            srow["recipient_email"],
                            srow.get("subject") or "",
                            srow.get("recipient_name") or "",
                        )

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
