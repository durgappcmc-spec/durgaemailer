# NOTE: Bulk templates substitute {first_name}, {name}, {title}, {company}.
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from config import APP_NAME
from core.auth_ui import logout_button, require_login
from gmail_client.attachments import files_to_attachments
from gmail_client.send import create_draft, send_email
from scheduling.client import cancel_scheduled, list_scheduled, schedule_batch, schedule_email
from scheduling.sequences import schedule_sequence

st.set_page_config(page_title=f"Schedule · {APP_NAME}", page_icon="📅", layout="wide")
if not require_login():
    st.stop()
logout_button()

st.title("📅 Schedule & Send")

tab_send, tab_single, tab_bulk, tab_seq, tab_queue = st.tabs(
    ["Send / Draft now", "Schedule single", "Bulk", "Sequence", "Queue"]
)

tomorrow = datetime.now() + timedelta(days=1)
default_date = tomorrow.date()
default_time = datetime.strptime("09:30", "%H:%M").time()

with tab_send:
    st.caption("Send immediately via Gmail API, or save as a Gmail draft. Attachments supported.")
    to = st.text_input("To (email)", key="now_to")
    name = st.text_input("Recipient name", key="now_name")
    subject = st.text_input("Subject", key="now_subject")
    campaign = st.text_input("Campaign", key="now_campaign")
    body = st.text_area("HTML body", value="<p>Hi {name},</p>", height=200, key="now_body")
    files = st.file_uploader(
        "Attachments",
        accept_multiple_files=True,
        key="now_files",
    )
    c1, c2 = st.columns(2)
    send_now = c1.button("📨 Send now", type="primary", key="now_send")
    draft_now = c2.button("📝 Save draft", key="now_draft")
    if send_now or draft_now:
        html = body.replace("{name}", name or "").replace("{{name}}", name or "")
        atts = files_to_attachments(list(files) if files else [])
        gmail_atts = [{"name": a["name"], "data": a["data"]} for a in atts]
        if send_now:
            st.json(
                send_email(
                    to=to,
                    subject=subject,
                    html_body=html,
                    recipient_name=name,
                    attachments=gmail_atts or None,
                    campaign=campaign,
                    source="ui_send",
                )
            )
        else:
            st.json(
                create_draft(
                    to=to,
                    subject=subject,
                    html_body=html,
                    recipient_name=name,
                    attachments=gmail_atts or None,
                    campaign=campaign,
                    source="ui_draft",
                    track=False,
                )
            )

with tab_single:
    to = st.text_input("To (email)", key="single_to")
    name = st.text_input("Recipient name", key="single_name")
    subject = st.text_input("Subject", key="single_subject")
    campaign = st.text_input("Campaign", key="single_campaign")
    d = st.date_input("Send date", value=default_date, key="single_date")
    t = st.time_input("Send time", value=default_time, key="single_time")
    body = st.text_area("HTML body", value="<p>Hi {{name}},</p>", height=200, key="single_body")
    files = st.file_uploader(
        "Attachments (stored as base64 for Apps Script)",
        accept_multiple_files=True,
        key="single_files",
    )
    if st.button("📤 Schedule", key="single_go"):
        send_at = datetime.combine(d, t)
        html = body.replace("{{name}}", name or "").replace("{name}", name or "")
        atts = files_to_attachments(list(files) if files else [])
        result = schedule_email(
            recipient_email=to,
            subject=subject,
            html_body=html,
            send_at=send_at,
            recipient_name=name,
            campaign=campaign,
            attachments=atts or None,
        )
        st.json(result)

with tab_bulk:
    prospects = [
        p
        for p in (st.session_state.get("last_prospects") or [])
        if not p.get("error") and p.get("email")
    ]
    st.write(f"Prospects with email in session: **{len(prospects)}**")
    b_subj = st.text_input("Subject template", "Quick idea for {company}", key="bulk_subj")
    b_body = st.text_area(
        "HTML body template",
        "<p>Hi {first_name},</p><p>Noticed your work as {title} at {company}…</p>",
        height=180,
        key="bulk_body",
    )
    stagger = st.slider("Stagger minutes between sends", 1, 60, 5)
    start = st.text_input(
        "Start datetime (ISO)",
        (datetime.now() + timedelta(days=1))
        .replace(hour=9, minute=30, second=0, microsecond=0)
        .isoformat(),
        key="bulk_start",
    )
    b_campaign = st.text_input("Campaign", "bulk", key="bulk_campaign")
    bulk_files = st.file_uploader(
        "Shared attachments for every recipient",
        accept_multiple_files=True,
        key="bulk_files",
    )
    if st.button("📤 Schedule batch") and prospects:
        try:
            cursor = datetime.fromisoformat(start)
        except Exception:
            cursor = datetime.now() + timedelta(days=1)
        atts = files_to_attachments(list(bulk_files) if bulk_files else [])
        jobs = []
        for p in prospects:
            html = (
                b_body.replace("{first_name}", p.get("first_name") or "")
                .replace("{name}", p.get("name") or "")
                .replace("{title}", p.get("title") or "")
                .replace("{company}", p.get("company") or "")
            )
            subj = (
                b_subj.replace("{first_name}", p.get("first_name") or "")
                .replace("{name}", p.get("name") or "")
                .replace("{title}", p.get("title") or "")
                .replace("{company}", p.get("company") or "")
            )
            jobs.append(
                {
                    "recipient_email": p["email"],
                    "recipient_name": p.get("name") or "",
                    "subject": subj,
                    "html_body": html,
                    "send_at": cursor.isoformat(),
                    "campaign": b_campaign,
                    "source": p.get("source") or "bulk",
                    "attachments": atts,
                }
            )
            cursor += timedelta(minutes=stagger)
        st.json(schedule_batch(jobs))

with tab_seq:
    if "seq_steps" not in st.session_state:
        st.session_state.seq_steps = [
            {
                "delay_days": 0,
                "delay_hours": 0,
                "subject": "Intro",
                "html_body": "<p>Hi!</p>",
            }
        ]
    seq_email = st.text_input("Recipient email", key="seq_email")
    seq_name = st.text_input("Recipient name", key="seq_name")
    seq_campaign = st.text_input("Campaign", "sequence", key="seq_campaign")
    if st.button("➕ Add step"):
        st.session_state.seq_steps.append(
            {"delay_days": 3, "delay_hours": 0, "subject": "", "html_body": "<p></p>"}
        )
    for i, step in enumerate(st.session_state.seq_steps):
        st.markdown(f"**Step {i + 1}**")
        c1, c2 = st.columns(2)
        step["delay_days"] = c1.number_input(
            "Delay days",
            min_value=0,
            value=int(step.get("delay_days") or 0),
            key=f"sd_{i}",
        )
        step["delay_hours"] = c2.number_input(
            "Delay hours",
            min_value=0,
            value=int(step.get("delay_hours") or 0),
            key=f"sh_{i}",
        )
        step["subject"] = st.text_input(
            "Subject", value=step.get("subject") or "", key=f"ss_{i}"
        )
        step["html_body"] = st.text_area(
            "HTML body", value=step.get("html_body") or "", key=f"sb_{i}", height=120
        )
    if st.button("📤 Schedule sequence"):
        prospect = {"email": seq_email, "name": seq_name, "source": "sequence"}
        st.json(
            schedule_sequence(
                prospect,
                st.session_state.seq_steps,
                campaign=seq_campaign,
                business_hours_only=True,
            )
        )

with tab_queue:
    data = list_scheduled(status="pending")
    rows = data.get("rows") or data.get("items") or data.get("scheduled") or []
    if isinstance(data, dict) and data.get("error"):
        st.warning(data["error"])
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
        for i, row in enumerate(rows):
            email = row.get("recipient_email") or row.get("email")
            if email and st.button(f"Cancel {email}", key=f"cancel_{i}_{email}"):
                st.json(cancel_scheduled(recipient_email=email))
                st.rerun()
    else:
        st.info("No pending scheduled emails (or Apps Script not configured).")
