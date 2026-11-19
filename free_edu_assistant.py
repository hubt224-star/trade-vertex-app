import json
import os
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trade Vertex - Lead Management", layout="wide")

BACKUP_FILE = "disposition_backup.csv"
COLS = ["lead_no", "name", "phone", "disposition", "note", "caller"]
DISPOSITIONS = [
    "Interested",
    "Not Interested",
    "Follow-Up / Doubt",
    "Switched Off / No Answer",
    "Wrong Number",
]


def load_saved_disposition():
    if os.path.exists(BACKUP_FILE):
        try:
            df = pd.read_csv(BACKUP_FILE, dtype=str).fillna("")
            clean_rows = []
            for r in df.to_dict(orient="records"):
                row = {col: str(r.get(col, "") or "").strip() for col in COLS}
                if any(row.values()):
                    clean_rows.append(row)
            return clean_rows
        except Exception:
            return []
    return []


def save_disposition_backup(rows):
    try:
        clean_rows = []
        for r in rows:
            row = {col: str(r.get(col, "") or "").strip() for col in COLS}
            if any(row.values()):
                clean_rows.append(row)
        df = pd.DataFrame(clean_rows, columns=COLS)
        df.to_csv(BACKUP_FILE, index=False, encoding="utf-8-sig")
    except Exception:
        pass


def parse_batch_leads(raw_text):
    leads = []
    for idx, line in enumerate((raw_text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "," in line:
            parts = [p.strip() for p in line.split(",", 1)]
            name, phone = parts[0], parts[1]
        else:
            parts = line.split()
            name = " ".join(parts[:-1]).strip() if len(parts) > 1 else parts[0]
            phone = parts[-1].strip() if len(parts) > 1 else ""
        leads.append({"lead_no": str(idx), "name": name, "phone": phone})
    return leads[:150]


def post_to_webhook(webhook_url, rows):
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = Request(
        webhook_url.strip(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


# State initialization with auto-restore from local CSV backup
if "disposition_rows" not in st.session_state:
    st.session_state.disposition_rows = load_saved_disposition()

if "synced_signatures" not in st.session_state:
    st.session_state.synced_signatures = {
        (
            r.get("lead_no", ""),
            r.get("name", ""),
            r.get("phone", ""),
            r.get("disposition", ""),
            r.get("note", ""),
            r.get("caller", ""),
        )
        for r in st.session_state.disposition_rows
    }

# Sidebar controls
with st.sidebar:
    st.header("1. Session & Caller Setup")
    caller_name = st.text_input("Employee / Caller Name", value="Team Member")
    topic_focus = st.selectbox(
        "Topic / Focus",
        [
            "Free Basics & Chart Reading Class",
            "Smart Money Concepts (SMC) Session",
            "Demat Account & Onboarding Support",
            "Options & Risk Management Workshop",
        ],
    )
    offer_line = st.text_input(
        "Class Timing / Offer Line",
        value="Aaj sham 8 baje ki free live class",
    )

    st.markdown("---")
    st.header("2. Google Sheet Sync (Optional)")
    webhook_url = st.text_input(
        "Apps Script Web App URL",
        placeholder="https://script.google.com/macros/s/.../exec",
    )

    st.markdown("---")
    st.header("3. Batch Leads (Up to 150+)")
    st.caption("150 tak leads paste karo (Har line: Naam, Number)")
    batch_text = st.text_area(
        "Paste Leads",
        height=180,
        placeholder="Rahul, 9876543210\nAman, 9876543211",
    )
    parsed_leads = parse_batch_leads(batch_text)
    st.caption(f"Loaded Leads: {len(parsed_leads)} / 150")

# Main Disposition Section
st.title("Trade Vertex — Lead & Disposition Desk")
st.caption(f"Focus: {topic_focus} | Offer: {offer_line}")

with st.form("log_disposition_form", clear_on_submit=True):
    c1, c2, c3 = st.columns([2, 2, 2])
    if parsed_leads:
        options_map = {
            f"#{item['lead_no']} - {item['name']} ({item['phone']})": item
            for item in parsed_leads
        }
        selected_label = c1.selectbox("Select Lead", list(options_map.keys()))
        chosen = options_map[selected_label]
        lead_no_val = chosen["lead_no"]
        name_val = chosen["name"]
        phone_val = chosen["phone"]
    else:
        lead_no_val = c1.text_input("Lead #", value=str(len(st.session_state.disposition_rows) + 1))
        name_val = c2.text_input("Name")
        phone_val = c3.text_input("Phone")

    d1, d2, d3 = st.columns([2, 3, 2])
    disp_val = d1.selectbox("Disposition", DISPOSITIONS)
    note_val = d2.text_input("Note")
    caller_val = d3.text_input("Caller", value=caller_name)

    submitted = st.form_submit_button("Save Disposition", use_container_width=True)
    if submitted:
        new_row = {
            "lead_no": str(lead_no_val).strip(),
            "name": str(name_val).strip(),
            "phone": str(phone_val).strip(),
            "disposition": str(disp_val).strip(),
            "note": str(note_val).strip(),
            "caller": str(caller_val or caller_name).strip(),
        }
        st.session_state.disposition_rows.append(new_row)
        save_disposition_backup(st.session_state.disposition_rows)
        st.success("Disposition saved & backed up locally.")
        st.rerun()

st.markdown("---")
st.subheader("4. Disposition Sheet (Interested / Not Interested Tracker)")

rows = st.session_state.disposition_rows
df_all = pd.DataFrame(rows, columns=COLS) if rows else pd.DataFrame(columns=COLS)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Logged", len(df_all))
m2.metric("Interested", int((df_all["disposition"] == "Interested").sum()) if len(df_all) else 0)
m3.metric("Not Interested", int((df_all["disposition"] == "Not Interested").sum()) if len(df_all) else 0)
m4.metric(
    "Follow-Up / Doubt",
    int((df_all["disposition"] == "Follow-Up / Doubt").sum()) if len(df_all) else 0,
)

filter_choice = st.selectbox("Filter Disposition", ["All"] + DISPOSITIONS)
if filter_choice != "All" and len(df_all) > 0:
    df_view = df_all[df_all["disposition"] == filter_choice]
else:
    df_view = df_all

st.dataframe(df_view, use_container_width=True, hide_index=True)

# Unsynced rows calculation
unsynced_rows = []
for r in rows:
    sig = (
        r.get("lead_no", ""),
        r.get("name", ""),
        r.get("phone", ""),
        r.get("disposition", ""),
        r.get("note", ""),
        r.get("caller", ""),
    )
    if sig not in st.session_state.synced_signatures:
        unsynced_rows.append(r)

b1, b2 = st.columns(2)

with b1:
    sync_clicked = st.button(
        f"Sync to Google Sheet ({len(unsynced_rows)} new)",
        use_container_width=True,
        disabled=not bool(webhook_url.strip()) or len(unsynced_rows) == 0,
    )
    if sync_clicked:
        try:
            resp_text = post_to_webhook(webhook_url, unsynced_rows)
            for r in unsynced_rows:
                st.session_state.synced_signatures.add(
                    (
                        r.get("lead_no", ""),
                        r.get("name", ""),
                        r.get("phone", ""),
                        r.get("disposition", ""),
                        r.get("note", ""),
                        r.get("caller", ""),
                    )
                )
            st.success(f"Synced {len(unsynced_rows)} row(s) to Google Sheet. ({resp_text})")
            st.rerun()
        except Exception as e:
            st.error(f"Google Sheet sync failed: {e}")

with b2:
    csv_bytes = df_all.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="Download Disposition Sheet (CSV)",
        data=csv_bytes,
        file_name="trade_vertex_disposition.csv",
        mime="text/csv",
        use_container_width=True,
    )
