from datetime import datetime
import hashlib
import io
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import streamlit as st

st.set_page_config(page_title="Smart Calling & WA Helper", layout="wide")

st.title("📚 Smart Lead Classifier, Calling & WA Helper")
st.caption("Fast, lightweight multi-caller lead dialer, smart WhatsApp templates, and live disposition tracker.")

ADMIN_PIN = "1234"  # Deploy karne se pehle apna secret Admin PIN set kar lena
AUTH_CONFIG_FILE = "caller_auth_config.json"


def get_default_webhook_url() -> str:
    try:
        secret_url = st.secrets.get("GSHEET_WEBHOOK_URL", "")
        if isinstance(secret_url, str) and secret_url.strip():
            return secret_url.strip()
    except Exception:
        pass
    return ""  # Yahan apna deployed Apps Script /exec URL daal dena: "https://script.google.com/macros/s/.../exec"


DEFAULT_GSHEET_WEBHOOK = get_default_webhook_url()

DEFAULT_CALLER_AUTH_MAP = {
    "Caller 1": {"id": "C1", "pin": "1101"},
    "Caller 2": {"id": "C2", "pin": "1102"},
    "Caller 3": {"id": "C3", "pin": "1103"},
    "Caller 4": {"id": "C4", "pin": "1104"},
    "Caller 5": {"id": "C5", "pin": "1105"},
}

DISPOSITION_OPTIONS = [
    "Interested",
    "Not Picked / Switched Off",
    "Call Cut",
    "Busy",
    "Doubt / Beginner",
    "Follow-Up",
    "Not Interested",
]

FOLLOW_UP_STATUSES = {
    "Follow-Up",
    "Doubt / Beginner",
    "Not Picked / Switched Off",
    "Call Cut",
    "Busy",
}

DISPOSITION_TO_WA_KEY = {
    "Interested": "Interested (Session Details & Onboarding)",
    "Not Picked / Switched Off": "Not Picked / Switched Off",
    "Call Cut": "Call Cut (Quick Follow-Up)",
    "Busy": "Busy (Call Back Later)",
    "Doubt / Beginner": "Doubt: Is This Really Free?",
    "Follow-Up": "Follow-Up: Will Check Later / Connect Tomorrow",
    "Not Interested": "Not Interested (Polite Close)",
}

DISPOSITION_TO_CALL_KEY = {
    "Interested": "Closing / WhatsApp Push",
    "Not Picked / Switched Off": "Opening (First 20 Seconds)",
    "Call Cut": "Opening (First 20 Seconds)",
    "Busy": "Opening (First 20 Seconds)",
    "Doubt / Beginner": "If Asked: 'Will There Be Any Fees?'",
    "Follow-Up": "Opening (First 20 Seconds)",
    "Not Interested": "Opening (First 20 Seconds)",
}

DEFAULT_LEADS = "Rahul, 9876543210\nAmit, 9123456789\nPriya, 9988776655"
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s\-().]{8,}\d)")


def clean_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) >= 10:
        digits = digits[-10:]
    if len(digits) == 10:
        return "91" + digits
    return digits


def safe_caller_slug(caller_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (caller_name or "team_member").strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "team_member"


def get_backup_file(caller_name: str) -> str:
    return f"caller_backup_{safe_caller_slug(caller_name)}.json"


def build_sync_key(caller_label: str, lead_no: int, phone: str, name: str) -> str:
    norm_phone = clean_phone(phone) or name.strip().lower()
    raw_key = f"{caller_label.strip().lower()}|{int(lead_no)}|{norm_phone}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


def load_auth_config() -> dict:
    merged = {k: dict(v) for k, v in DEFAULT_CALLER_AUTH_MAP.items()}
    if not os.path.exists(AUTH_CONFIG_FILE):
        return merged
    try:
        with open(AUTH_CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for slot, creds in DEFAULT_CALLER_AUTH_MAP.items():
            if isinstance(saved.get(slot), dict):
                cid = str(saved[slot].get("id", creds["id"])).strip().upper()
                cpin = str(saved[slot].get("pin", creds["pin"])).strip()
                merged[slot] = {
                    "id": cid or creds["id"],
                    "pin": cpin or creds["pin"],
                }
    except Exception:
        pass
    return merged


def save_auth_config(auth_map: dict):
    try:
        with open(AUTH_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(auth_map, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@st.cache_data(show_spinner=False, max_entries=64)
def parse_leads_text(raw_text: str):
    leads = []
    seen_phones = set()

    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue

        low = line.lower()
        if low in {"name, phone", "name,phone", "lead, phone", "lead,phone"}:
            continue

        found_phone = ""
        found_digits = ""
        for match in PHONE_PATTERN.finditer(line):
            candidate = match.group(0).strip()
            digits = re.sub(r"\D", "", candidate)
            if len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            if len(digits) >= 10:
                found_phone = candidate
                found_digits = digits[-10:]
                break

        if found_phone:
            name_part = line.replace(found_phone, " ")
            name_part = re.sub(r"[,|\t:/\-]+", " ", name_part)
            name_part = re.sub(r"\s+", " ", name_part).strip()
            name = name_part if name_part and not name_part.isdigit() else "Lead"
            phone = found_digits
        else:
            parts = [p.strip() for p in re.split(r"[,|\t]+", line) if p.strip()]
            name = parts[0] if parts else line
            phone = parts[1] if len(parts) >= 2 else ""

        norm = clean_phone(phone)
        if norm and norm in seen_phones:
            continue
        if norm:
            seen_phones.add(norm)

        leads.append({"name": name, "phone": phone})
    return leads[:500]


def split_leads_equally(raw_text: str, slots: list) -> dict:
    parsed = parse_leads_text(raw_text)
    num_slots = len(slots)
    buckets = {s: [] for s in slots}
    if not parsed or num_slots == 0:
        return {s: "" for s in slots}

    base_size, remainder = divmod(len(parsed), num_slots)
    start = 0
    for idx, slot in enumerate(slots):
        extra = 1 if idx < remainder else 0
        end = start + base_size + extra
        chunk = parsed[start:end]
        start = end
        buckets[slot] = "\n".join(
            f"{item['name']}, {item['phone']}".strip(", ") for item in chunk
        )
    return buckets


def parse_uploaded_leads_file(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    raw_bytes = uploaded_file.getvalue()
    text = raw_bytes.decode("utf-8-sig", errors="ignore")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("name,") or low.startswith("lead"):
            continue
        lines.append(line)
    return "\n".join(lines)


def analyze_lead_reply(reply_text: str):
    text = reply_text.lower().strip()
    if not text:
        return None

    not_interested_kw = [
        "not interested", "nhi", "nahi", "band karo", "wrong number",
        "mat karo", "stop", "no thanks", "timepass"
    ]
    call_cut_kw = ["cut", "kaat", "disconnect", "call cut"]
    busy_kw = ["busy", "baad", "kal", "sham", "abhi nahi", "later", "call back", "free hoke", "sunday", "weekend"]
    hot_kw = [
        "yes", "haan", "han", "link", "bhejo", "send", "join",
        "kab hai", "timing", "interested", "sikha", "seekhna", "details"
    ]
    doubt_kw = [
        "free", "paisa", "paise", "fees", "charge", "kuch nhi aata",
        "beginner", "new", "kaise", "kya hai", "sebi", "tip"
    ]

    if any(k in text for k in not_interested_kw) and not any(
        h in text for h in ["yes", "haan", "han", "bhejo", "link"]
    ):
        return {
            "status": "❌ Not Interested",
            "disposition": "Not Interested",
            "color": "error",
            "action": "Polite closing message bhej kar next lead par move karein.",
        }
    if any(k in text for k in call_cut_kw):
        return {
            "status": "✂️ Call Cut",
            "disposition": "Call Cut",
            "color": "warning",
            "action": "Short WhatsApp drop karein aur thodi der baad retry karein.",
        }
    if any(k in text for k in busy_kw):
        return {
            "status": "⏳ Busy / Follow-Up",
            "disposition": "Busy",
            "color": "info",
            "action": "Abhi push na karein — WhatsApp par short detail chhod kar baad mein follow up karein.",
        }
    if any(k in text for k in hot_kw):
        return {
            "status": "🔥 Hot / Interested Lead",
            "disposition": "Interested",
            "color": "success",
            "action": "Turant session details/link WhatsApp par bhejein aur aaj ki live class ke liye onboard karein.",
        }
    if any(k in text for k in doubt_kw):
        return {
            "status": "🤔 Doubt / Beginner Lead (High Potential)",
            "disposition": "Doubt / Beginner",
            "color": "warning",
            "action": "Clarify karein ki yeh 100% free educational session hai, phir session details bhejein.",
        }

    return {
        "status": "🟡 Follow-Up / Needs Clarification",
        "disposition": "Follow-Up",
        "color": "info",
        "action": "WhatsApp par short detail bhej kar confirmation lein.",
    }


def push_to_google_sheet(webhook_url: str, rows: list, live_activity: dict | None = None):
    payload_obj = {"rows": rows}
    if live_activity:
        payload_obj["live_activity"] = live_activity
    payload = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(
        webhook_url.strip(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, body


def push_to_google_sheet_async(webhook_url: str, rows: list, live_activity: dict | None = None):
    def _worker():
        try:
            push_to_google_sheet(webhook_url, rows, live_activity=live_activity)
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def fetch_live_sheet_data(webhook_url: str):
    url = webhook_url.strip()
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}action=live_view"
    req = urllib.request.Request(full_url, method="GET")
    with urllib.request.urlopen(req, timeout=12) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return json.loads(body)


def build_csv(records):
    output = io.StringIO()
    headers = ["Sync Key", "Lead #", "Name", "Phone", "Disposition", "Reply / Note", "Caller"]
    output.write(",".join(headers) + "\n")

    def esc(val):
        s = str(val or "").replace('"', '""')
        return f'"{s}"'

    for r in records:
        row = [
            esc(r.get("sync_key", "")),
            esc(r.get("lead_no", "")),
            esc(r.get("name", "")),
            esc(r.get("phone", "")),
            esc(r.get("disposition", "")),
            esc(r.get("note", "")),
            esc(r.get("caller", "")),
        ]
        output.write(",".join(row) + "\n")
    return output.getvalue().encode("utf-8-sig")


def build_raw_rows_csv(rows_2d: list) -> bytes:
    output = io.StringIO()
    for row in rows_2d:
        escaped = ['"' + str(cell or "").replace('"', '""') + '"' for cell in row]
        output.write(",".join(escaped) + "\n")
    return output.getvalue().encode("utf-8-sig")


def empty_profile(default_leads_text: str = ""):
    return {
        "lead_idx": 0,
        "done_indices": set(),
        "synced_indices": set(),
        "disposition_sheet": {},
        "raw_leads": default_leads_text,
    }


def save_caller_backup(caller_name: str, state_obj: dict):
    backup_path = get_backup_file(caller_name)
    try:
        data = {
            "caller": caller_name,
            "lead_idx": int(state_obj.get("lead_idx", 0)),
            "done_indices": sorted(int(i) for i in state_obj.get("done_indices", set())),
            "synced_indices": sorted(int(i) for i in state_obj.get("synced_indices", set())),
            "disposition_sheet": {
                str(k): v for k, v in state_obj.get("disposition_sheet", {}).items()
            },
            "raw_leads": state_obj.get("raw_leads", ""),
        }
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def load_caller_backup(caller_name: str):
    backup_path = get_backup_file(caller_name)
    if not os.path.exists(backup_path):
        return None
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            "lead_idx": int(raw.get("lead_idx", 0)),
            "done_indices": {int(i) for i in raw.get("done_indices", [])},
            "synced_indices": {int(i) for i in raw.get("synced_indices", [])},
            "disposition_sheet": {
                int(k): v for k, v in raw.get("disposition_sheet", {}).items()
            },
            "raw_leads": raw.get("raw_leads", ""),
        }
    except Exception:
        return None


if "caller_auth_map" not in st.session_state:
    st.session_state.caller_auth_map = load_auth_config()

if "verified_profiles" not in st.session_state:
    st.session_state.verified_profiles = {}

caller_auth_map = st.session_state.caller_auth_map

with st.sidebar:
    st.header("1. Caller Login & Session Settings")
    brand_name = st.text_input("Team / Desk Name", value="Sales & Learning Desk")

    selected_preset = st.selectbox(
        "Caller Slot",
        list(caller_auth_map.keys()),
        index=0,
    )
    entered_caller_id = st.text_input(
        "Enter Caller ID",
        placeholder="Example: C1",
        key=f"cid_input_{selected_preset}",
    )
    entered_caller_pin = st.text_input(
        "Enter PIN",
        type="password",
        placeholder="4-digit PIN",
        key=f"cpin_input_{selected_preset}",
    )

    expected_auth = caller_auth_map[selected_preset]
    is_caller_verified = (
        entered_caller_id.strip().upper() == expected_auth["id"].strip().upper()
        and entered_caller_pin.strip() == expected_auth["pin"].strip()
    )

    topic = st.selectbox(
        "Session Topic",
        [
            "Free Basics & Chart Reading Class",
            "Free Price Action & Market Structure Session",
            "Free Beginner Live Class",
        ],
    )
    class_time = st.text_input("Session Timing / Offer Line", value="Aaj sham 8 baje ki free live class")

    st.divider()
    st.header("2. Google Sheet Sync")
    gsheet_webhook = st.text_input(
        "Apps Script Web App URL",
        value=DEFAULT_GSHEET_WEBHOOK,
        placeholder="https://script.google.com/macros/s/.../exec",
    )
    auto_sync_sheet = st.checkbox(
        "Fast background sync on Save & Next",
        value=True,
    )

    st.divider()
    st.header("3. Admin Access")
    admin_pin_input = st.text_input("Admin PIN", type="password", placeholder="Enter Admin PIN", key="admin_pin_box")
    is_admin_verified = admin_pin_input.strip() == ADMIN_PIN

    if is_admin_verified:
        st.success("Admin access granted.")

        with st.expander("🔑 Manage Caller ID & PIN", expanded=True):
            manage_slot = st.selectbox(
                "Select Caller Slot",
                list(caller_auth_map.keys()),
                key="manage_id_pin_slot",
            )
            curr_creds = caller_auth_map[manage_slot]
            new_cid = st.text_input(
                f"{manage_slot} Caller ID",
                value=curr_creds["id"],
                key=f"edit_cid_{manage_slot}",
            )
            new_cpin = st.text_input(
                f"{manage_slot} PIN",
                value=curr_creds["pin"],
                key=f"edit_cpin_{manage_slot}",
            )
            if st.button(f"Save {manage_slot} Credentials", use_container_width=True, key=f"save_creds_{manage_slot}"):
                clean_id = new_cid.strip().upper() or curr_creds["id"]
                clean_pin = new_cpin.strip() or curr_creds["pin"]
                caller_auth_map[manage_slot] = {"id": clean_id, "pin": clean_pin}
                save_auth_config(caller_auth_map)
                st.success(f"{manage_slot} credentials updated.")
                st.rerun()

            st.caption("Active Caller Credentials:")
            creds_table = [
                {"Slot": k, "Caller ID": v["id"], "PIN": v["pin"]}
                for k, v in caller_auth_map.items()
            ]
            st.dataframe(creds_table, use_container_width=True, hide_index=True)

        with st.expander("5 Caller Lead Assign"):
            bulk_raw_text = st.text_area(
                "Bulk Split Leads Equally Across All 5 Callers (One per line: Name, Phone)",
                placeholder="Paste 100 or 150 leads here to split equally across Caller 1 to Caller 5...",
                height=110,
                key="admin_bulk_split_box",
            )
            if st.button("✂️ Split & Assign Equally to 5 Callers", use_container_width=True, key="admin_split_btn"):
                split_map = split_leads_equally(bulk_raw_text, list(caller_auth_map.keys()))
                has_any = any(bool(v.strip()) for v in split_map.values())
                if has_any:
                    for slot_name, chunk_text in split_map.items():
                        s_slug = safe_caller_slug(slot_name)
                        if s_slug not in st.session_state.verified_profiles:
                            st.session_state.verified_profiles[s_slug] = (
                                load_caller_backup(slot_name) or empty_profile("")
                            )
                        st.session_state.verified_profiles[s_slug]["raw_leads"] = chunk_text
                        st.session_state.verified_profiles[s_slug]["lead_idx"] = 0
                        save_caller_backup(slot_name, st.session_state.verified_profiles[s_slug])
                    st.success("Leads split equally across Caller 1 to Caller 5.")
                    st.rerun()
                else:
                    st.warning("Paste valid leads first to split.")

            st.divider()
            admin_target_caller = st.selectbox(
                "Select Target Caller",
                list(caller_auth_map.keys()),
                key="admin_target_caller_select",
            )
            t_slug = safe_caller_slug(admin_target_caller)
            if t_slug not in st.session_state.verified_profiles:
                st.session_state.verified_profiles[t_slug] = (
                    load_caller_backup(admin_target_caller) or empty_profile("")
                )
            t_profile = st.session_state.verified_profiles[t_slug]

            admin_uploaded = st.file_uploader(
                f"Upload CSV / TXT for {admin_target_caller}",
                type=["csv", "txt"],
                key=f"admin_upload_{t_slug}",
            )
            if admin_uploaded is not None:
                parsed_up = parse_uploaded_leads_file(admin_uploaded)
                if parsed_up:
                    t_profile["raw_leads"] = parsed_up
                    t_profile["lead_idx"] = 0
                    save_caller_backup(admin_target_caller, t_profile)
                    st.success(f"Uploaded leads saved for {admin_target_caller}.")

            admin_leads_text = st.text_area(
                f"Paste Leads for {admin_target_caller} (One per line: Name, Phone)",
                value=t_profile.get("raw_leads", ""),
                height=130,
                key=f"admin_paste_{t_slug}",
            )
            if st.button(f"Assign {admin_target_caller} Leads", use_container_width=True, key=f"admin_save_{t_slug}"):
                t_profile["raw_leads"] = admin_leads_text
                t_profile["lead_idx"] = 0
                save_caller_backup(admin_target_caller, t_profile)
                st.success(f"Leads assigned to {admin_target_caller}.")
                st.rerun()
    elif admin_pin_input.strip():
        st.caption("Invalid Admin PIN.")

if not is_caller_verified:
    st.warning(
        f"🔒 **{selected_preset} Locked:** Enter a valid **Caller ID** and **PIN** in the sidebar to access your leads, dialer, and disposition sheet."
    )
    st.info("Share only each caller's respective ID and PIN so lead data remains isolated per caller.")
else:
    caller_name = selected_preset
    current_slug = safe_caller_slug(caller_name)

    if current_slug not in st.session_state.verified_profiles:
        loaded = load_caller_backup(caller_name)
        if loaded:
            st.session_state.verified_profiles[current_slug] = loaded
        else:
            st.session_state.verified_profiles[current_slug] = empty_profile(DEFAULT_LEADS)

    profile = st.session_state.verified_profiles[current_slug]

    with st.sidebar:
        st.divider()
        st.header("4. Active Caller Leads")
        st.caption(f"Logged in as: **{caller_name}** (`{expected_auth['id']}`)")

        active_upload = st.file_uploader(
            "Upload CSV / TXT File",
            type=["csv", "txt"],
            key=f"caller_upload_{current_slug}",
        )
        if active_upload is not None:
            up_text = parse_uploaded_leads_file(active_upload)
            if up_text and up_text != profile.get("raw_leads", ""):
                profile["raw_leads"] = up_text
                profile["lead_idx"] = 0
                save_caller_backup(caller_name, profile)
                st.rerun()

        raw_leads = st.text_area(
            "Assigned Leads (One per line: Name, Phone)",
            value=profile.get("raw_leads", ""),
            height=140,
            key=f"caller_batch_{current_slug}",
        )
        if raw_leads != profile.get("raw_leads", ""):
            profile["raw_leads"] = raw_leads
            save_caller_backup(caller_name, profile)

    parsed_leads = parse_leads_text(raw_leads)
    total_leads = len(parsed_leads)

    if total_leads > 0:
        if profile["lead_idx"] >= total_leads:
            profile["lead_idx"] = total_leads - 1
        current_lead = parsed_leads[profile["lead_idx"]]
        default_name = current_lead["name"]
        default_phone = current_lead["phone"]
    else:
        default_name = "Rahul"
        default_phone = ""

    done_count = sum(1 for i in profile["done_indices"] if i < total_leads) if total_leads > 0 else 0

    if total_leads > 0:
        st.progress(
            done_count / total_leads,
            text=f"[{caller_name} | {expected_auth['id']}] Completed: {done_count} / {total_leads} leads | Current: #{profile['lead_idx'] + 1}",
        )

    q_col1, q_col2, q_col3, q_col4, q_col5 = st.columns([2.4, 1, 1.2, 1.1, 1])
    with q_col1:
        if total_leads > 0:
            is_done = profile["lead_idx"] in profile["done_indices"]
            saved_disp = profile["disposition_sheet"].get(profile["lead_idx"], {}).get("disposition", "")
            status_tag = f"✅ {saved_disp}" if saved_disp else ("✅ Done" if is_done else "📞 Pending")
            st.markdown(f"**{status_tag} — Lead {profile['lead_idx'] + 1} of {total_leads}:** `{default_name}`")
        else:
            st.markdown("**Active Lead:** Manual Mode")

    with q_col2:
        if st.button("⬅️ Prev", use_container_width=True, disabled=total_leads == 0 or profile["lead_idx"] == 0):
            profile["lead_idx"] -= 1
            save_caller_backup(caller_name, profile)
            st.rerun()

    with q_col3:
        next_clicked = st.button("Save & Next ➡️", type="primary", use_container_width=True, disabled=total_leads == 0)

    with q_col4:
        if total_leads > 0:
            jump_num = st.number_input(
                "Jump #",
                min_value=1,
                max_value=total_leads,
                value=profile["lead_idx"] + 1,
                label_visibility="collapsed",
                key=f"jump_{current_slug}_{profile['lead_idx']}",
            )
            if int(jump_num) - 1 != profile["lead_idx"]:
                profile["lead_idx"] = int(jump_num) - 1
                save_caller_backup(caller_name, profile)
                st.rerun()

    with q_col5:
        if st.button("🔄 Reset", use_container_width=True, disabled=total_leads == 0):
            profile["lead_idx"] = 0
            profile["done_indices"].clear()
            profile["disposition_sheet"].clear()
            profile["synced_indices"].clear()
            backup_path = get_backup_file(caller_name)
            if os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
            st.rerun()

    e_col1, e_col2, e_col3 = st.columns([1.4, 1.4, 1.2])
    with e_col1:
        lead_name = st.text_input(
            "Lead Name",
            value=default_name,
            key=f"name_{current_slug}_{profile['lead_idx']}",
        )
    with e_col2:
        lead_phone = st.text_input(
            "Lead Phone Number",
            value=default_phone,
            key=f"phone_{current_slug}_{profile['lead_idx']}",
        )
    with e_col3:
        existing_disp = profile["disposition_sheet"].get(profile["lead_idx"], {}).get("disposition")
        disp_select = st.selectbox(
            "Call Disposition",
            options=DISPOSITION_OPTIONS,
            index=DISPOSITION_OPTIONS.index(existing_disp) if existing_disp in DISPOSITION_OPTIONS else 0,
            key=f"disp_{current_slug}_{profile['lead_idx']}",
        )

    st.divider()

    st.subheader("🧠 1. Lead Reply Analyzer (Optional Note)")
    existing_note = profile["disposition_sheet"].get(profile["lead_idx"], {}).get("note", "")
    lead_reply = st.text_area(
        "What did the lead reply on WhatsApp or say on the call?",
        value=existing_note,
        placeholder="Example: 'Please share details', 'Is there any fee?', 'Call cut ho gaya', 'Busy right now'",
        height=68,
        key=f"reply_{current_slug}_{profile['lead_idx']}",
    )

    analysis = analyze_lead_reply(lead_reply)
    if analysis:
        if analysis["color"] == "success":
            st.success(f"**Status:** {analysis['status']} — {analysis['action']}")
        elif analysis["color"] == "warning":
            st.warning(f"**Status:** {analysis['status']} — {analysis['action']}")
        elif analysis["color"] == "error":
            st.error(f"**Status:** {analysis['status']} — {analysis['action']}")
        else:
            st.info(f"**Status:** {analysis['status']} — {analysis['action']}")

    if next_clicked and total_leads > 0:
        idx_now = profile["lead_idx"]
        final_disp = disp_select
        caller_label = f"{caller_name} ({expected_auth['id']})"
        saved_record = {
            "sync_key": build_sync_key(caller_label, idx_now + 1, lead_phone, lead_name),
            "lead_no": idx_now + 1,
            "name": lead_name,
            "phone": lead_phone,
            "disposition": final_disp,
            "note": lead_reply.strip(),
            "caller": caller_label,
        }
        profile["disposition_sheet"][idx_now] = saved_record
        profile["synced_indices"].discard(idx_now)
        profile["done_indices"].add(idx_now)

        next_idx = idx_now + 1 if idx_now < total_leads - 1 else idx_now
        if idx_now < total_leads - 1:
            profile["lead_idx"] = next_idx

        save_caller_backup(caller_name, profile)

        if auto_sync_sheet and gsheet_webhook.strip():
            live_info = {
                "caller": caller_label,
                "current_lead": f"#{next_idx + 1} - {parsed_leads[next_idx]['name']}" if total_leads else lead_name,
                "completed": f"{len(profile['done_indices'])}/{total_leads}",
                "last_disposition": final_disp,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            push_to_google_sheet_async(gsheet_webhook, [saved_record], live_activity=live_info)
            profile["synced_indices"].add(idx_now)

        st.rerun()

    st.divider()

    wa_messages = {
        "Interested (Session Details & Onboarding)": (
            f"Namaste {lead_name} ji 👋\n"
            f"{brand_name} se baat kar raha hoon.\n\n"
            f"Aapke interest ke liye thank you! Humne aapke liye *{topic}* rakhi hai ({class_time}) — yeh *100% free educational session* hai (koi tip ya paid signal nahi).\n\n"
            f"Confirm karne ke liye *YES* reply karein, main aapko session link aur reminder bhej deta hoon."
        ),
        "Not Picked / Switched Off": (
            f"Hi {lead_name} ji, abhi {brand_name} se call kiya tha par connect nahi ho paya.\n\n"
            f"Hum *{topic}* ({class_time}) ke liye 100% free educational guidance de rahe hain.\n"
            f"Jab free hon toh batayein, kya main aapko session ki details yahan bhej dun?"
        ),
        "Call Cut (Quick Follow-Up)": (
            f"Hi {lead_name} ji, abhi {brand_name} se call connect hua tha par shayad network ki wajah se cut ho gaya.\n\n"
            f"Humne aapke liye *{topic}* ({class_time}) rakhi hai jo bilkul *100% free educational class* hai.\n"
            f"Agar aap details chahte hain toh *YES* likh kar reply karein."
        ),
        "Busy (Call Back Later)": (
            f"Koi baat nahi {lead_name} ji 👍\n"
            f"Jab aap free hon tab aaram se dekh lijiyega. *{topic}* ({class_time}) bilkul *100% free educational session* hai.\n\n"
            f"Free hote hi *YES* reply kar dijiyega, main join link bhej dunga."
        ),
        "Doubt: Is This Really Free?": (
            f"Bilkul {lead_name} ji, yeh *{topic}* learning & awareness ke liye *100% free* hai.\n\n"
            f"Hamara focus sirf concepts samjhana hai taaki aap khud market ko better samajh sakein.\n"
            f"Kya main aapko {class_time} ki detail bhej dun?"
        ),
        "Follow-Up: Will Check Later / Connect Tomorrow": (
            f"Namaste {lead_name} ji 👍\n"
            f"Reminder ke liye message kar raha hoon — *{topic}* ({class_time}) ke liye aapki slot reserve hai.\n"
            f"Join karne ke liye *YES* reply kar dein."
        ),
        "Not Interested (Polite Close)": (
            f"Thank you {lead_name} ji aapke samay ke liye 🙏\n"
            f"Agar future mein kabhi free educational session join karna chahein toh zaroor batayein. Have a great day!"
        ),
    }

    call_scripts = {
        "Opening (First 20 Seconds)": (
            f"\"Hello {lead_name} ji, namaste! Main {brand_name} se bol raha hoon. Aapne market learning ke liye enquiry ki thi — "
            f"hum *{topic}* kara rahe hain jo bilkul free educational class hai. Kya aap abhi 30 second baat kar sakte hain?\""
        ),
        "If Asked: 'I Don't Know Anything About This'": (
            f"\"Bilkul koi dikkat nahi {lead_name} ji, yeh session shuruat se seekhne walon ke liye hi hai — bilkul simple bhasha mein. "
            f"Main aapko WhatsApp par short detail bhej deta hoon, pehle wahan dekh lijiyega.\""
        ),
        "If Asked: 'Will There Be Any Fees?'": (
            f"\"Nahi {lead_name} ji, yeh *{topic}* learning ke liye free hai. Main abhi aapko WhatsApp par exact details bhej raha hoon, aap khud check kar lijiyega.\""
        ),
        "Closing / WhatsApp Push": (
            f"\"Great {lead_name} ji! Main abhi aapko WhatsApp par {class_time} ki detail bhej raha hoon — wahan 'YES' bhej dijiyega taaki aapko reminder mil jaye.\""
        ),
    }

    wa_keys = list(wa_messages.keys())
    call_keys = list(call_scripts.keys())

    mapped_wa_key = DISPOSITION_TO_WA_KEY.get(disp_select, wa_keys[0])
    default_wa_idx = wa_keys.index(mapped_wa_key) if mapped_wa_key in wa_keys else 0

    mapped_call_key = DISPOSITION_TO_CALL_KEY.get(disp_select, call_keys[0])
    default_call_idx = call_keys.index(mapped_call_key) if mapped_call_key in call_keys else 0

    col1, col2 = st.columns(2)
    formatted_num = clean_phone(lead_phone)
    display_num = f"+{formatted_num}" if len(formatted_num) >= 10 else (lead_phone or "No Number")

    with col1:
        st.subheader("💬 2. Ready WhatsApp Message")
        st.caption(f"Auto-matched with disposition: **{disp_select}**")
        wa_type = st.selectbox(
            "Select Message Template",
            wa_keys,
            index=default_wa_idx,
            key=f"wa_type_{current_slug}_{profile['lead_idx']}_{disp_select}",
        )
        selected_wa_text = wa_messages[wa_type]
        st.code(selected_wa_text, language="text")

        if len(formatted_num) >= 10:
            encoded_msg = urllib.parse.quote(selected_wa_text)
            wa_url = f"https://wa.me/{formatted_num}?text={encoded_msg}"
            st.link_button(f"📲 Send on WhatsApp ({display_num})", wa_url, use_container_width=True)
        else:
            st.caption("Enter a 10-digit phone number to generate a direct WhatsApp link.")

    with col2:
        st.subheader("📞 3. Ready Calling Script")
        st.markdown("**Dial Number:**")
        st.code(display_num, language="text")

        call_type = st.selectbox(
            "Select Call Scenario",
            call_keys,
            index=default_call_idx,
            key=f"call_type_{current_slug}_{profile['lead_idx']}_{disp_select}",
        )
        st.code(call_scripts[call_type], language="text")

        if len(formatted_num) >= 10:
            tel_url = f"tel:+{formatted_num}"
            st.markdown(
                f"""
                <a href="{tel_url}" target="_top" style="
                    display: inline-block;
                    width: 100%;
                    text-align: center;
                    background-color: #16a34a;
                    color: #ffffff;
                    font-weight: 600;
                    padding: 0.6rem 1rem;
                    border-radius: 0.5rem;
                    text-decoration: none;
                    margin-top: 0.25rem;
                ">📞 Call Now ({display_num})</a>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.caption("Enter a 10-digit phone number to enable the Call Now button.")

    st.divider()
    st.subheader("📊 4. Disposition Sheet")

    sorted_keys = sorted(profile["disposition_sheet"].keys())
    sheet_rows = [profile["disposition_sheet"][k] for k in sorted_keys]

    if sheet_rows:
        int_count = sum(1 for r in sheet_rows if r["disposition"] == "Interested")
        not_int_count = sum(1 for r in sheet_rows if r["disposition"] == "Not Interested")
        fu_count = sum(1 for r in sheet_rows if r["disposition"] in FOLLOW_UP_STATUSES)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Logged", len(sheet_rows))
        m2.metric("Interested", int_count)
        m3.metric("Not Interested", not_int_count)
        m4.metric("Follow-Up / Cut / Busy", fu_count)

        filter_disp = st.selectbox(
            "Filter Disposition",
            ["All", "Due Follow-Ups"] + DISPOSITION_OPTIONS,
            key=f"filter_disp_select_{current_slug}",
        )

        if filter_disp == "All":
            filtered_rows = sheet_rows
        elif filter_disp == "Due Follow-Ups":
            filtered_rows = [r for r in sheet_rows if r["disposition"] in FOLLOW_UP_STATUSES]
        else:
            filtered_rows = [r for r in sheet_rows if r["disposition"] == filter_disp]

        st.dataframe(filtered_rows, use_container_width=True, hide_index=True)

        unsynced_keys = [k for k in sorted_keys if k not in profile["synced_indices"]]
        unsynced_rows = [profile["disposition_sheet"][k] for k in unsynced_keys]

        s_col1, s_col2 = st.columns(2)
        with s_col1:
            if st.button(
                f"☁️ Sync to Google Sheet ({len(unsynced_rows)} new)",
                use_container_width=True,
                disabled=not gsheet_webhook.strip() or not unsynced_rows,
                key=f"sync_btn_{current_slug}",
            ):
                caller_label = f"{caller_name} ({expected_auth['id']})"
                live_info = {
                    "caller": caller_label,
                    "current_lead": f"#{profile['lead_idx'] + 1} - {lead_name}",
                    "completed": f"{done_count}/{total_leads}",
                    "last_disposition": sheet_rows[-1]["disposition"],
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                try:
                    status_code, _ = push_to_google_sheet(
                        gsheet_webhook, unsynced_rows, live_activity=live_info
                    )
                    if 200 <= status_code < 300:
                        profile["synced_indices"].update(unsynced_keys)
                        save_caller_backup(caller_name, profile)
                        st.success(f"{len(unsynced_rows)} rows synced to Google Sheet.")
                        st.rerun()
                    else:
                        st.error(f"Google Sheet response status: {status_code}")
                except Exception as e:
                    st.error(f"Sync error: {e}")

        with s_col2:
            st.download_button(
                label="⬇️ Download Disposition Sheet (CSV)",
                data=build_csv(sheet_rows),
                file_name=f"disposition_{current_slug}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"dl_btn_{current_slug}",
            )
    else:
        st.info("No dispositions logged yet. Select a disposition and click **Save & Next ➡️** to populate the sheet.")

if is_admin_verified:
    st.divider()
    st.subheader("👀 5. Admin Live Team Monitor")

    local_team_rows = []
    for c_name, auth_info in caller_auth_map.items():
        c_slug = safe_caller_slug(c_name)
        prof = st.session_state.verified_profiles.get(c_slug) or load_caller_backup(c_name)
        if prof:
            p_leads = parse_leads_text(prof.get("raw_leads", ""))
            p_total = len(p_leads)
            p_idx = min(prof.get("lead_idx", 0), max(p_total - 1, 0)) if p_total > 0 else 0
            curr_lbl = f"#{p_idx + 1} - {p_leads[p_idx]['name']}" if p_total > 0 else "No Batch"
            disp_map = prof.get("disposition_sheet", {})
            last_rec = disp_map[max(disp_map.keys())] if disp_map else None
            local_team_rows.append({
                "Caller": c_name,
                "Caller ID": auth_info["id"],
                "Current Lead": curr_lbl,
                "Completed": f"{len(prof.get('done_indices', set()))}/{p_total}",
                "Logged": len(disp_map),
                "Last Disposition": last_rec["disposition"] if last_rec else "—",
            })
        else:
            local_team_rows.append({
                "Caller": c_name,
                "Caller ID": auth_info["id"],
                "Current Lead": "Not Started",
                "Completed": "0/0",
                "Logged": 0,
                "Last Disposition": "—",
            })

    st.dataframe(local_team_rows, use_container_width=True, hide_index=True)

    if st.button(
        "🔄 Refresh All Callers Live Data from Google Sheet",
        use_container_width=True,
        disabled=not gsheet_webhook.strip(),
        key="admin_refresh_gsheet",
    ):
        try:
            sheet_data = fetch_live_sheet_data(gsheet_webhook)
            st.session_state["live_sheet_cache"] = sheet_data.get("rows", [])
            st.json(sheet_data)
        except Exception:
            st.info(
                "Ensure your Apps Script deployment includes `doGet(e)` and is republished to fetch live sheet data."
            )

    cached_sheet_rows = st.session_state.get("live_sheet_cache", [])
    if cached_sheet_rows:
        remote_csv = build_raw_rows_csv(cached_sheet_rows)
        st.download_button(
            label="⬇️ Download All Callers Sheet from Google Sheet (CSV)",
            data=remote_csv,
            file_name="all_callers_live.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_all_callers_remote_csv",
        )
