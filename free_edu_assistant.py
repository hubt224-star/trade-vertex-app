from datetime import datetime
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import streamlit as st

st.set_page_config(page_title="Market Metrix — Smart Calling & WA Helper", layout="wide")

st.title("📈 Market Metrix — Lead Classifier, Calling & WA Helper")
st.caption("Fast multi-caller lead dialer, mandatory Call/WA action lock, speed audit, and Admin live monitor.")

ADMIN_PIN = "1234"  # Deploy karne se pehle apna secret Admin PIN set kar lena
AUTH_CONFIG_FILE = "caller_auth_config.json"
FAST_SKIP_THRESHOLD_SEC = 5.0  # Agar 5 second se kam mein lead save ho toh Fast Skip flag hoga


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

QUICK_DISPOSITIONS = [
    ("✅ Interested", "Interested"),
    ("📵 Not Picked", "Not Picked / Switched Off"),
    ("✂️ Call Cut", "Call Cut"),
    ("⏳ Busy", "Busy"),
    ("❌ Not Int.", "Not Interested"),
]

FOLLOW_UP_STATUSES = {
    "Follow-Up",
    "Doubt / Beginner",
    "Not Picked / Switched Off",
    "Call Cut",
    "Busy",
}

DISPOSITION_TO_WA_KEY = {
    "Interested": "Interested (Seat Confirmation & Hook)",
    "Not Picked / Switched Off": "Not Picked / Switched Off (Curiosity Hook)",
    "Call Cut": "Call Cut (Short Value Hook)",
    "Busy": "Busy (Quick Evening Reminder Hook)",
    "Doubt / Beginner": "Doubt: Is This Really Free? (Zero-Risk Hook)",
    "Follow-Up": "Follow-Up: Limited Seat Reminder",
    "Not Interested": "Not Interested (Polite Door-Open Close)",
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
            "action": "Short WhatsApp hook drop karein aur thodi der baad retry karein.",
        }
    if any(k in text for k in busy_kw):
        return {
            "status": "⏳ Busy / Follow-Up",
            "disposition": "Busy",
            "color": "info",
            "action": "Abhi push na karein — WhatsApp par short value hook chhod kar baad mein follow up karein.",
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
    headers = ["Sync Key", "Lead #", "Name", "Phone", "Disposition", "Action Taken", "Duration (s)", "Reply / Note", "Caller"]
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
            esc(r.get("action_taken", "")),
            esc(r.get("duration_sec", "")),
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
        "action_log": {},  # {idx_str: {"call": bool, "wa": bool}}
        "lead_opened_at": time.time(),
        "raw_leads": default_leads_text,
        "master_leads": default_leads_text,
        "in_followup_queue": False,
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
            "action_log": state_obj.get("action_log", {}),
            "raw_leads": state_obj.get("raw_leads", ""),
            "master_leads": state_obj.get("master_leads", state_obj.get("raw_leads", "")),
            "in_followup_queue": bool(state_obj.get("in_followup_queue", False)),
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
        raw_text = raw.get("raw_leads", "")
        return {
            "lead_idx": int(raw.get("lead_idx", 0)),
            "done_indices": {int(i) for i in raw.get("done_indices", [])},
            "synced_indices": {int(i) for i in raw.get("synced_indices", [])},
            "disposition_sheet": {
                int(k): v for k, v in raw.get("disposition_sheet", {}).items()
            },
            "action_log": raw.get("action_log", {}),
            "lead_opened_at": time.time(),
            "raw_leads": raw_text,
            "master_leads": raw.get("master_leads", raw_text),
            "in_followup_queue": bool(raw.get("in_followup_queue", False)),
        }
    except Exception:
        return None


def get_action_status(profile_obj: dict, idx: int) -> dict:
    alog = profile_obj.setdefault("action_log", {})
    return alog.setdefault(str(idx), {"call": False, "wa": False})


def format_action_label(act_dict: dict) -> str:
    c_done = bool(act_dict.get("call"))
    w_done = bool(act_dict.get("wa"))
    if c_done and w_done:
        return "Call + WA"
    if c_done:
        return "Call Only"
    if w_done:
        return "WA Only"
    return "No Action"


def commit_lead_disposition(
    profile_obj: dict,
    caller_display_name: str,
    caller_code: str,
    idx_now: int,
    name_val: str,
    phone_val: str,
    disp_val: str,
    note_val: str,
    total_count: int,
    leads_list: list,
    webhook_url: str,
    auto_sync_enabled: bool,
):
    act_status = get_action_status(profile_obj, idx_now)
    action_label = format_action_label(act_status)
    opened_ts = float(profile_obj.get("lead_opened_at", time.time()))
    elapsed_sec = round(max(time.time() - opened_ts, 0.5), 1)
    is_fast_skip = elapsed_sec < FAST_SKIP_THRESHOLD_SEC

    caller_label = f"{caller_display_name} ({caller_code})"
    saved_record = {
        "sync_key": build_sync_key(caller_label, idx_now + 1, phone_val, name_val),
        "lead_no": idx_now + 1,
        "name": name_val,
        "phone": phone_val,
        "disposition": disp_val,
        "action_taken": action_label,
        "duration_sec": elapsed_sec,
        "fast_skip": is_fast_skip,
        "note": note_val.strip(),
        "caller": caller_label,
    }
    profile_obj["disposition_sheet"][idx_now] = saved_record
    profile_obj["synced_indices"].discard(idx_now)
    profile_obj["done_indices"].add(idx_now)

    next_idx = idx_now + 1 if idx_now < total_count - 1 else idx_now
    if idx_now < total_count - 1:
        profile_obj["lead_idx"] = next_idx
    profile_obj["lead_opened_at"] = time.time()

    save_caller_backup(caller_display_name, profile_obj)

    if auto_sync_enabled and webhook_url.strip():
        live_info = {
            "caller": caller_label,
            "current_lead": f"#{next_idx + 1} - {leads_list[next_idx]['name']}" if total_count else name_val,
            "completed": f"{len(profile_obj['done_indices'])}/{total_count}",
            "last_disposition": disp_val,
            "action_taken": action_label,
            "duration_sec": elapsed_sec,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        push_to_google_sheet_async(webhook_url, [saved_record], live_activity=live_info)
        profile_obj["synced_indices"].add(idx_now)


if "caller_auth_map" not in st.session_state:
    st.session_state.caller_auth_map = load_auth_config()

if "verified_profiles" not in st.session_state:
    st.session_state.verified_profiles = {}

caller_auth_map = st.session_state.caller_auth_map
slot_names = list(caller_auth_map.keys())

# Restore login credentials from query params across refresh / new tab until Logout
qp = st.query_params
qp_slot = str(qp.get("slot", "")).strip()
qp_cid = str(qp.get("cid", "")).strip().upper()
qp_cpin = str(qp.get("cpin", "")).strip()
qp_act = str(qp.get("act", "")).strip()
qp_act_idx = str(qp.get("act_idx", "")).strip()

if qp_slot in caller_auth_map and qp_cid and qp_cpin:
    expected_qp = caller_auth_map[qp_slot]
    if qp_cid == expected_qp["id"].strip().upper() and qp_cpin == expected_qp["pin"].strip():
        st.session_state["active_slot"] = qp_slot
        st.session_state[f"cid_input_{qp_slot}"] = qp_cid
        st.session_state[f"cpin_input_{qp_slot}"] = qp_cpin

default_slot_idx = 0
if "active_slot" in st.session_state and st.session_state["active_slot"] in slot_names:
    default_slot_idx = slot_names.index(st.session_state["active_slot"])

with st.sidebar:
    st.header("1. Caller Login & Session Settings")
    brand_name = st.text_input("Brand / Desk Name", value="Market Metrix")

    selected_preset = st.selectbox(
        "Caller Slot",
        slot_names,
        index=default_slot_idx,
    )
    st.session_state["active_slot"] = selected_preset

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

    if is_caller_verified:
        st.query_params["slot"] = selected_preset
        st.query_params["cid"] = entered_caller_id.strip().upper()
        st.query_params["cpin"] = entered_caller_pin.strip()

        if st.button("🚪 Logout Caller", use_container_width=True, key="caller_logout_btn"):
            st.query_params.clear()
            st.session_state.pop("active_slot", None)
            st.session_state.pop(f"cid_input_{selected_preset}", None)
            st.session_state.pop(f"cpin_input_{selected_preset}", None)
            st.rerun()

    topic = st.selectbox(
        "Session Topic",
        [
            "Smart Money & Chart Reading Masterclass",
            "Price Action & Market Structure Live Session",
            "Zero-to-Pro Stock Market Foundation Class",
        ],
    )
    class_time = st.text_input("Session Timing / Offer Line", value="Aaj sham 8 baje ki free live class")
    session_link = st.text_input(
        "Live Session / Join Link",
        value="",
        placeholder="https://meet.google.com/... or https://youtube.com/...",
    )

    st.divider()
    st.header("2. Auto-Refresh & Google Sheet Sync")
    auto_refresh_enabled = st.checkbox("Auto-Refresh (1.5s)", value=True)
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
                slot_names,
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
                split_map = split_leads_equally(bulk_raw_text, slot_names)
                has_any = any(bool(v.strip()) for v in split_map.values())
                if has_any:
                    for slot_name, chunk_text in split_map.items():
                        s_slug = safe_caller_slug(slot_name)
                        if s_slug not in st.session_state.verified_profiles:
                            st.session_state.verified_profiles[s_slug] = (
                                load_caller_backup(slot_name) or empty_profile("")
                            )
                        st.session_state.verified_profiles[s_slug]["raw_leads"] = chunk_text
                        st.session_state.verified_profiles[s_slug]["master_leads"] = chunk_text
                        st.session_state.verified_profiles[s_slug]["in_followup_queue"] = False
                        st.session_state.verified_profiles[s_slug]["lead_idx"] = 0
                        st.session_state.verified_profiles[s_slug]["lead_opened_at"] = time.time()
                        save_caller_backup(slot_name, st.session_state.verified_profiles[s_slug])
                    st.success("Leads split equally across Caller 1 to Caller 5.")
                    st.rerun()
                else:
                    st.warning("Paste valid leads first to split.")

            st.divider()
            admin_target_caller = st.selectbox(
                "Select Target Caller",
                slot_names,
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
                    t_profile["master_leads"] = parsed_up
                    t_profile["in_followup_queue"] = False
                    t_profile["lead_idx"] = 0
                    t_profile["lead_opened_at"] = time.time()
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
                t_profile["master_leads"] = admin_leads_text
                t_profile["in_followup_queue"] = False
                t_profile["lead_idx"] = 0
                t_profile["lead_opened_at"] = time.time()
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

    # Capture Call/WA click events passed via query params if triggered
    if qp_act in {"call", "wa"} and qp_act_idx.isdigit():
        act_obj = get_action_status(profile, int(qp_act_idx))
        act_obj[qp_act] = True
        save_caller_backup(caller_name, profile)
        st.query_params.pop("act", None)
        st.query_params.pop("act_idx", None)

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
                profile["master_leads"] = up_text
                profile["in_followup_queue"] = False
                profile["lead_idx"] = 0
                profile["lead_opened_at"] = time.time()
                save_caller_backup(caller_name, profile)
                st.rerun()

        raw_leads = st.text_area(
            "Assigned Leads (One per line: Name, Phone)",
            value=profile.get("raw_leads", ""),
            height=130,
            key=f"caller_batch_{current_slug}",
        )
        if raw_leads != profile.get("raw_leads", ""):
            profile["raw_leads"] = raw_leads
            if not profile.get("in_followup_queue", False):
                profile["master_leads"] = raw_leads
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
    due_followup_records = [
        r for r in profile["disposition_sheet"].values() if r.get("disposition") in FOLLOW_UP_STATUSES
    ]

    if total_leads > 0:
        mode_tag = " [Follow-Up Queue]" if profile.get("in_followup_queue", False) else ""
        st.progress(
            done_count / total_leads,
            text=f"[{caller_name} | {expected_auth['id']}{mode_tag}] Completed: {done_count} / {total_leads} leads | Current: #{profile['lead_idx'] + 1}",
        )

    current_action_state = get_action_status(profile, profile["lead_idx"])
    has_performed_action = bool(current_action_state.get("call") or current_action_state.get("wa"))

    q_col1, q_col2, q_col3, q_col4, q_col5 = st.columns([2.2, 0.9, 1.2, 1.0, 1.0])
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
            profile["lead_opened_at"] = time.time()
            save_caller_backup(caller_name, profile)
            st.rerun()

    with q_col3:
        next_clicked = st.button(
            "Save & Next ➡️",
            type="primary",
            use_container_width=True,
            disabled=total_leads == 0 or not has_performed_action,
            help="Pehle Call Now ya Send on WhatsApp par click karein, tab Save & Next unlock hoga.",
        )

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
                profile["lead_opened_at"] = time.time()
                save_caller_backup(caller_name, profile)
                st.rerun()

    with q_col5:
        if st.button("🔄 Reset Batch", use_container_width=True, disabled=total_leads == 0):
            profile["lead_idx"] = 0
            profile["done_indices"].clear()
            profile["disposition_sheet"].clear()
            profile["synced_indices"].clear()
            profile["action_log"].clear()
            profile["in_followup_queue"] = False
            profile["lead_opened_at"] = time.time()
            if profile.get("master_leads"):
                profile["raw_leads"] = profile["master_leads"]
            save_caller_backup(caller_name, profile)
            st.rerun()

    if due_followup_records or profile.get("in_followup_queue", False):
        fq_col1, fq_col2 = st.columns([1.6, 1.4])
        with fq_col1:
            if due_followup_records and st.button(
                f"🔁 Retry {len(due_followup_records)} Due Follow-Ups / Cut / Busy Leads",
                use_container_width=True,
                key=f"retry_fu_btn_{current_slug}",
            ):
                retry_lines = [
                    f"{r.get('name', 'Lead')}, {r.get('phone', '')}".strip(", ")
                    for r in due_followup_records
                ]
                if not profile.get("master_leads") and profile.get("raw_leads"):
                    profile["master_leads"] = profile["raw_leads"]
                profile["raw_leads"] = "\n".join(retry_lines)
                profile["lead_idx"] = 0
                profile["in_followup_queue"] = True
                profile["lead_opened_at"] = time.time()
                save_caller_backup(caller_name, profile)
                st.rerun()
        with fq_col2:
            if profile.get("in_followup_queue", False) and profile.get("master_leads"):
                if st.button(
                    "📋 Restore Full Lead Batch",
                    use_container_width=True,
                    key=f"restore_batch_btn_{current_slug}",
                ):
                    profile["raw_leads"] = profile["master_leads"]
                    profile["lead_idx"] = 0
                    profile["in_followup_queue"] = False
                    profile["lead_opened_at"] = time.time()
                    save_caller_backup(caller_name, profile)
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

    existing_note = profile["disposition_sheet"].get(profile["lead_idx"], {}).get("note", "")
    lead_reply = st.text_input(
        "Quick Call Note / Reply (Optional)",
        value=existing_note,
        placeholder="Example: 'Call cut', 'Busy till 6 PM', 'Requested link'",
        key=f"reply_{current_slug}_{profile['lead_idx']}",
    )

    if total_leads > 0:
        if not has_performed_action:
            st.warning("🔒 **Action Required:** Save & Next ya Quick Disposition dabane se pehle neeche **📞 Call Now** ya **📲 Send on WhatsApp** par click karein.")
        else:
            st.success(f"✅ **Action Verified:** `{format_action_label(current_action_state)}` — ab aap disposition save kar sakte hain.")

        st.caption("One-Tap Quick Save & Next:")
        q_btns = st.columns(len(QUICK_DISPOSITIONS))
        quick_chosen_disp = None
        for idx_b, (btn_label, disp_val) in enumerate(QUICK_DISPOSITIONS):
            with q_btns[idx_b]:
                if st.button(
                    btn_label,
                    use_container_width=True,
                    disabled=not has_performed_action,
                    key=f"quick_disp_{current_slug}_{profile['lead_idx']}_{idx_b}",
                ):
                    quick_chosen_disp = disp_val

        if quick_chosen_disp:
            commit_lead_disposition(
                profile_obj=profile,
                caller_display_name=caller_name,
                caller_code=expected_auth["id"],
                idx_now=profile["lead_idx"],
                name_val=lead_name,
                phone_val=lead_phone,
                disp_val=quick_chosen_disp,
                note_val=lead_reply,
                total_count=total_leads,
                leads_list=parsed_leads,
                webhook_url=gsheet_webhook,
                auto_sync_enabled=auto_sync_sheet,
            )
            st.rerun()

    analysis = analyze_lead_reply(lead_reply)
    if analysis:
        if analysis["color"] == "success":
            st.success(f"**Hint:** {analysis['status']} — {analysis['action']}")
        elif analysis["color"] == "warning":
            st.warning(f"**Hint:** {analysis['status']} — {analysis['action']}")
        elif analysis["color"] == "error":
            st.error(f"**Hint:** {analysis['status']} — {analysis['action']}")
        else:
            st.info(f"**Hint:** {analysis['status']} — {analysis['action']}")

    if next_clicked and total_leads > 0:
        commit_lead_disposition(
            profile_obj=profile,
            caller_display_name=caller_name,
            caller_code=expected_auth["id"],
            idx_now=profile["lead_idx"],
            name_val=lead_name,
            phone_val=lead_phone,
            disp_val=disp_select,
            note_val=lead_reply,
            total_count=total_leads,
            leads_list=parsed_leads,
            webhook_url=gsheet_webhook,
            auto_sync_enabled=auto_sync_sheet,
        )
        st.rerun()

    st.divider()

    clean_session_link = session_link.strip()
    link_block = f"\n🔗 *Direct Join Link:* {clean_session_link}\n" if clean_session_link else "\n"
    link_line_call = f" Link ({clean_session_link}) abhi WhatsApp par bhej raha hoon." if clean_session_link else ""

    wa_messages = {
        "Interested (Seat Confirmation & Hook)": (
            f"Namaste *{lead_name}* ji 👋\n"
            f"*{brand_name}* se baat kar raha hoon.\n\n"
            f"90% retail traders chart ke galat signals dekh kar trap hote hain — aaj ke *{topic}* ({class_time}) mein aap live dekhenge ki *Big Players / Smart Money* chart par entry aur exit kaise plan karte hain.\n\n"
            f"✅ *100% Free Practical Educational Session* (Zero tips, pure chart logic)"
            f"{link_block}"
            f"Apni seat confirm karne ke liye bas *YES* reply karein 👇"
        ),
        "Not Picked / Switched Off (Curiosity Hook)": (
            f"Hi *{lead_name}* ji 👋\n"
            f"Abhi *{brand_name}* se call kiya tha par connect nahi ho paya.\n\n"
            f"Agar aap stock market mein *bina kisi paid tip ke khud chart read karna* seekhna chahte hain, toh humne aapke liye *{topic}* ({class_time}) rakhi hai — bilkul *FREE*.\n"
            f"{link_block}"
            f"Kya main aapki seat reserve kar doon? Bas *YES* likh kar bhejein."
        ),
        "Call Cut (Short Value Hook)": (
            f"Hi *{lead_name}* ji, abhi *{brand_name}* se call connect hua tha par shayad network ki wajah se cut ho gaya.\n\n"
            f"Bas 10 second ki update: *{topic}* ({class_time}) mein hum *live market structure aur operator footprints* samjha rahe hain — *100% Free Educational Session*.\n"
            f"{link_block}"
            f"Details chahiye toh *YES* reply karein 👍"
        ),
        "Busy (Quick Evening Reminder Hook)": (
            f"Koi baat nahi *{lead_name}* ji, abhi aap busy honge 👍\n\n"
            f"Bas itna share karna tha — *{brand_name}* ki *{topic}* ({class_time}) bilkul *FREE* hai, jismein aap *support-resistance se aage badh kar real institutional price action* seekhenge.\n"
            f"{link_block}"
            f"Free hote hi *YES* reply kar dijiyega, main reminder bhej dunga."
        ),
        "Doubt: Is This Really Free? (Zero-Risk Hook)": (
            f"Bilkul *{lead_name}* ji — yeh *{topic}* *100% FREE* hai, koi hidden fee ya paid tip group nahi hai.\n\n"
            f"*{brand_name}* ka aim simple hai: pehle aap live chart par *real price action logic* dekhein aur khud decide karein ki market kaise kaam karta hai.\n"
            f"{link_block}"
            f"Aaj ({class_time}) join karne ke liye *YES* reply karein."
        ),
        "Follow-Up: Limited Seat Reminder": (
            f"Namaste *{lead_name}* ji ⏰\n"
            f"*{brand_name}* se quick reminder — aaj ke *{topic}* ({class_time}) mein kuch hi slots bachi hain.\n\n"
            f"Agar aap aaj *live chart breakdown aur smart money concepts* seekhna chahte hain toh miss mat kijiyega.\n"
            f"{link_block}"
            f"Confirm karne ke liye abhi *YES* reply karein 👇"
        ),
        "Not Interested (Polite Door-Open Close)": (
            f"Thank you *{lead_name}* ji aapke samay ke liye 🙏\n"
            f"Agar future mein kabhi *free stock market chart reading aur price action* seekhne ka mann bane, toh *{brand_name}* par zaroor message kijiyega. Have a great day!"
        ),
    }

    call_scripts = {
        "Opening (First 20 Seconds)": (
            f"\"Hello {lead_name} ji, namaste! Main {brand_name} se bol raha hoon. Aapne stock market learning ke liye enquiry ki thi — "
            f"aaj hum *{topic}* kara rahe hain jismein live chart par dikhayenge ki big players kaise move karte hain. Bilkul free educational class hai — kya aap 30 second baat kar sakte hain?\""
        ),
        "If Asked: 'I Don't Know Anything About This'": (
            f"\"Bilkul koi dikkat nahi {lead_name} ji, yeh session zero se shuru karne walon ke liye hi design kiya gaya hai — bilkul simple Hindi mein. "
            f"Main aapko {brand_name} se WhatsApp par short detail bhej deta hoon, pehle wahan dekh lijiyega.\""
        ),
        "If Asked: 'Will There Be Any Fees?'": (
            f"\"Nahi {lead_name} ji, yeh *{topic}* 100% free educational session hai, koi tip ya charge nahi hai. Main abhi aapko WhatsApp par exact details bhej raha hoon, aap khud check kar lijiyega.\""
        ),
        "Closing / WhatsApp Push": (
            f"\"Great {lead_name} ji! Main abhi aapko {brand_name} se WhatsApp par {class_time} ki detail{link_line_call} bhej raha hoon — wahan 'YES' reply kar dijiyega taaki aapki seat lock ho jaye.\""
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
        st.caption(f"Matched to disposition: **{disp_select}**")
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
            wa_col_a, wa_col_b = st.columns([2.2, 1])
            with wa_col_a:
                st.markdown(
                    f"""
                    <a href="{wa_url}" target="market_metrix_wa" rel="noopener" style="
                        display: inline-block;
                        width: 100%;
                        text-align: center;
                        background-color: #2563eb;
                        color: #ffffff;
                        font-weight: 600;
                        padding: 0.6rem 1rem;
                        border-radius: 0.5rem;
                        text-decoration: none;
                        margin-top: 0.25rem;
                    ">📲 Open WhatsApp ({display_num})</a>
                    """,
                    unsafe_allow_html=True,
                )
            with wa_col_b:
                if st.button(
                    "✅ WA Sent",
                    use_container_width=True,
                    key=f"confirm_wa_{current_slug}_{profile['lead_idx']}",
                ):
                    current_action_state["wa"] = True
                    save_caller_backup(caller_name, profile)
                    st.rerun()
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
            call_col_a, call_col_b = st.columns([2.2, 1])
            with call_col_a:
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
                    ">📞 Dial ({display_num})</a>
                    """,
                    unsafe_allow_html=True,
                )
            with call_col_b:
                if st.button(
                    "✅ Call Done",
                    use_container_width=True,
                    key=f"confirm_call_{current_slug}_{profile['lead_idx']}",
                ):
                    current_action_state["call"] = True
                    save_caller_backup(caller_name, profile)
                    st.rerun()
        else:
            st.caption("Enter a 10-digit phone number to enable the Call button.")

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
        st.info("No dispositions logged yet. Dial or send WhatsApp first, then save the disposition.")

if is_admin_verified:
    st.divider()
    st.subheader("👀 5. Admin Live Team Monitor & Quality Audit")

    total_team_interested = 0
    total_team_followup = 0
    total_team_not_int = 0
    total_team_logged = 0

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
            records = list(disp_map.values())

            c_int = sum(1 for r in records if r.get("disposition") == "Interested")
            c_fu = sum(1 for r in records if r.get("disposition") in FOLLOW_UP_STATUSES)
            c_not_int = sum(1 for r in records if r.get("disposition") == "Not Interested")

            c_call_cnt = sum(1 for r in records if "Call" in str(r.get("action_taken", "")))
            c_wa_cnt = sum(1 for r in records if "WA" in str(r.get("action_taken", "")))
            fast_skips = sum(1 for r in records if r.get("fast_skip") or float(r.get("duration_sec", 10)) < FAST_SKIP_THRESHOLD_SEC)

            durations = [float(r.get("duration_sec", 0)) for r in records if r.get("duration_sec") is not None]
            avg_sec = f"{round(sum(durations) / len(durations), 1)}s" if durations else "—"

            total_team_interested += c_int
            total_team_followup += c_fu
            total_team_not_int += c_not_int
            total_team_logged += len(records)

            last_rec = disp_map[max(disp_map.keys())] if disp_map else None
            local_team_rows.append({
                "Caller": c_name,
                "ID": auth_info["id"],
                "Progress": f"{len(prof.get('done_indices', set()))}/{p_total}",
                "🔥 Interested": c_int,
                "🔁 Follow-Up / Busy / Cut": c_fu,
                "❌ Not Int.": c_not_int,
                "📞 Calls": c_call_cnt,
                "💬 WAs": c_wa_cnt,
                "⏱️ Avg Time": avg_sec,
                "⚠️ Fast Skips (<5s)": fast_skips,
                "Current Lead": curr_lbl,
                "Last Status": last_rec["disposition"] if last_rec else "—",
            })
        else:
            local_team_rows.append({
                "Caller": c_name,
                "ID": auth_info["id"],
                "Progress": "0/0",
                "🔥 Interested": 0,
                "🔁 Follow-Up / Busy / Cut": 0,
                "❌ Not Int.": 0,
                "📞 Calls": 0,
                "💬 WAs": 0,
                "⏱️ Avg Time": "—",
                "⚠️ Fast Skips (<5s)": 0,
                "Current Lead": "Not Started",
                "Last Status": "—",
            })

    tm1, tm2, tm3, tm4 = st.columns(4)
    tm1.metric("Team Total Logged", total_team_logged)
    tm2.metric("🔥 Team Interested", total_team_interested)
    tm3.metric("🔁 Team Follow-Up / Busy / Cut", total_team_followup)
    tm4.metric("❌ Team Not Interested", total_team_not_int)

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

# 1.5s background auto-refresh loop (non-blocking)
if auto_refresh_enabled:
    now_ts = time.time()
    last_tick = st.session_state.get("_last_auto_refresh_ts", 0.0)
    if last_tick == 0.0:
        st.session_state["_last_auto_refresh_ts"] = now_ts
    elif now_ts - last_tick >= 1.5:
        st.session_state["_last_auto_refresh_ts"] = now_ts
        st.rerun()
