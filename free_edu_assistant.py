from datetime import datetime
import io
import json
import os
import re
import urllib.parse
import urllib.request
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Trade Vertex - Smart Calling & WA Helper", layout="wide")

st.title("📚 Trade Vertex — Smart Lead Classifier, Calling & WA Helper")
st.caption("Leads assign/paste karo, call & WhatsApp karo, disposition save karo, aur 5on callers ka live status track karo.")

ADMIN_PIN = "1234"  # Deploy karne se pehle apna secret Admin PIN set kar lena

CALLER_AUTH_MAP = {
    "Caller 1": {"id": "TV-C1", "pin": "1101"},
    "Caller 2": {"id": "TV-C2", "pin": "1102"},
    "Caller 3": {"id": "TV-C3", "pin": "1103"},
    "Caller 4": {"id": "TV-C4", "pin": "1104"},
    "Caller 5": {"id": "TV-C5", "pin": "1105"},
}

DISPOSITION_OPTIONS = [
    "Interested",
    "Not Interested",
    "Doubt / Beginner",
    "Follow-Up",
    "Not Picked / Switched Off",
]

DEFAULT_LEADS = "Rahul, 9876543210\nAmit, 9123456789\nPriya, 9988776655"


def clean_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return "91" + digits
    return digits


def safe_caller_slug(caller_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (caller_name or "team_member").strip().lower())
    cleaned = cleaned.strip("_")
    return cleaned or "team_member"


def get_backup_file(caller_name: str) -> str:
    return f"trade_vertex_backup_{safe_caller_slug(caller_name)}.json"


def parse_leads_text(raw_text: str):
    leads = []
    seen_phones = set()
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue

        parts = [p.strip() for p in re.split(r"[,|\t]+", line) if p.strip()]
        if len(parts) >= 2:
            name, phone = parts[0], parts[1]
        else:
            digits_only = re.sub(r"\D", "", line)
            if len(digits_only) >= 10:
                name = "Lead"
                phone = line
            else:
                name = line
                phone = ""

        norm = clean_phone(phone)
        if norm and norm in seen_phones:
            continue
        if norm:
            seen_phones.add(norm)

        leads.append({"name": name, "phone": phone})
    return leads[:150]


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
    hot_kw = [
        "yes", "haan", "han", "link", "bhejo", "send", "join",
        "kab hai", "timing", "interested", "sikha", "seekhna", "details"
    ]
    doubt_kw = [
        "free", "paisa", "paise", "fees", "charge", "kuch nhi aata",
        "beginner", "new", "kaise", "kya hai", "sebi", "tip"
    ]
    followup_kw = [
        "baad", "kal", "sham", "busy", "abhi nahi", "later",
        "call back", "free hoke", "sunday", "weekend"
    ]

    if any(k in text for k in not_interested_kw) and not any(
        h in text for h in ["yes", "haan", "han", "bhejo", "link"]
    ):
        return {
            "status": "❌ Not Interested",
            "disposition": "Not Interested",
            "color": "error",
            "action": "Is lead par time waste mat karo. Next Lead par aage badho.",
            "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
            "suggested_call": "Opening (Pehle 20 Second)",
        }
    if any(k in text for k in hot_kw):
        return {
            "status": "🔥 Hot / Interested Lead",
            "disposition": "Interested",
            "color": "success",
            "action": "Turant WhatsApp par session detail/link bhejo aur aaj ki class mein join karwao.",
            "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
            "suggested_call": "Closing / WhatsApp Push",
        }
    if any(k in text for k in doubt_kw):
        return {
            "status": "🤔 Doubt / Beginner Lead (High Potential)",
            "disposition": "Doubt / Beginner",
            "color": "warning",
            "action": "Inka doubt clear karo ki yeh 100% free educational session hai, phir link bhejo.",
            "suggested_wa": "Doubt: Kya Yeh Sach Mein Free Hai?",
            "suggested_call": "Agar Bole: 'Paise / Fees Lagegi Kya?'",
        }
    if any(k in text for k in followup_kw):
        return {
            "status": "⏳ Call Back / Follow-Up Lead",
            "disposition": "Follow-Up",
            "color": "info",
            "action": "Abhi force mat karo. WhatsApp par short detail chhod do aur kal/bataye time par follow-up lo.",
            "suggested_wa": "Follow-Up: Baad Mein Dekhenge / Kal Baat Karte Hain",
            "suggested_call": "Opening (Pehle 20 Second)",
        }

    return {
        "status": "🟡 Neutral / Needs Quick Clarification",
        "disposition": "Follow-Up",
        "color": "info",
        "action": "Ek short message bhej kar pucho ki wo free live class ki detail chahte hain ya nahi.",
        "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
        "suggested_call": "Opening (Pehle 20 Second)",
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
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, body


def fetch_live_sheet_data(webhook_url: str):
    url = webhook_url.strip()
    sep = "&" if "?" in url else "?"
    full_url = f"{url}{sep}action=live_view"
    req = urllib.request.Request(full_url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return json.loads(body)


def build_csv(records):
    output = io.StringIO()
    headers = ["Lead #", "Name", "Phone", "Disposition", "Reply / Note", "Caller"]
    output.write(",".join(headers) + "\n")

    def esc(val):
        s = str(val or "").replace('"', '""')
        return f'"{s}"'

    for r in records:
        row = [
            esc(r.get("lead_no", "")),
            esc(r.get("name", "")),
            esc(r.get("phone", "")),
            esc(r.get("disposition", "")),
            esc(r.get("note", "")),
            esc(r.get("caller", "")),
        ]
        output.write(",".join(row) + "\n")
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
            json.dump(data, f, ensure_ascii=False, indent=2)
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


if "verified_profiles" not in st.session_state:
    st.session_state.verified_profiles = {}

# Sidebar: Caller Login, Class Settings, Sheet Sync & Admin Lead Assignment
with st.sidebar:
    st.header("1. Caller Login & Class Settings")
    brand_name = st.text_input("Brand Name", value="Trade Vertex")

    selected_preset = st.selectbox(
        "Caller Slot",
        list(CALLER_AUTH_MAP.keys()),
        index=0,
    )
    entered_caller_id = st.text_input(
        "Apna Caller ID Daalo",
        placeholder="Jaise: TV-C1",
        key=f"cid_input_{selected_preset}",
    )
    entered_caller_pin = st.text_input(
        "Apna PIN Daalo",
        type="password",
        placeholder="4-digit PIN",
        key=f"cpin_input_{selected_preset}",
    )

    expected_auth = CALLER_AUTH_MAP[selected_preset]
    is_caller_verified = (
        entered_caller_id.strip().upper() == expected_auth["id"]
        and entered_caller_pin.strip() == expected_auth["pin"]
    )

    topic = st.selectbox(
        "Topic / Focus",
        [
            "Free Basics & Chart Reading Class",
            "Free Price Action & Market Structure Session",
            "Free Beginner Live Class",
        ],
    )
    class_time = st.text_input("Class Timing / Offer Line", value="Aaj sham 8 baje ki free live class")

    st.divider()
    st.header("2. Google Sheet Sync")
    gsheet_webhook = st.text_input(
        "Apps Script Web App URL",
        placeholder="https://script.google.com/macros/s/.../exec",
    )
    auto_sync_sheet = st.checkbox(
        "Save & Next par auto Google Sheet sync + live activity",
        value=True,
    )

    st.divider()
    st.header("3. Admin: 5on Callers Ko Leads Do")
    admin_pin_input = st.text_input("Admin PIN", type="password", placeholder="Admin PIN", key="admin_pin_box")
    is_admin_verified = admin_pin_input.strip() == ADMIN_PIN

    if is_admin_verified:
        st.success("Admin Unlocked: Kisi bhi caller ko alag leads assign karo.")
        admin_target_caller = st.selectbox(
            "Target Caller Chuno",
            list(CALLER_AUTH_MAP.keys()),
            key="admin_target_caller_select",
        )
        t_slug = safe_caller_slug(admin_target_caller)
        if t_slug not in st.session_state.verified_profiles:
            st.session_state.verified_profiles[t_slug] = (
                load_caller_backup(admin_target_caller) or empty_profile("")
            )
        t_profile = st.session_state.verified_profiles[t_slug]

        admin_uploaded = st.file_uploader(
            f"{admin_target_caller} ke liye CSV / TXT Upload",
            type=["csv", "txt"],
            key=f"admin_upload_{t_slug}",
        )
        if admin_uploaded is not None:
            parsed_up = parse_uploaded_leads_file(admin_uploaded)
            if parsed_up:
                t_profile["raw_leads"] = parsed_up
                t_profile["lead_idx"] = 0
                save_caller_backup(admin_target_caller, t_profile)
                st.success(f"{admin_target_caller} ki uploaded file save ho gayi!")

        admin_leads_text = st.text_area(
            f"{admin_target_caller} ki Leads Paste Karo (Har line: Naam, Number)",
            value=t_profile.get("raw_leads", ""),
            height=130,
            key=f"admin_paste_{t_slug}",
        )
        if st.button(f"Save {admin_target_caller} Leads", use_container_width=True, key=f"admin_save_{t_slug}"):
            t_profile["raw_leads"] = admin_leads_text
            t_profile["lead_idx"] = 0
            save_caller_backup(admin_target_caller, t_profile)
            st.success(f"{admin_target_caller} ke liye leads save ho gayin!")
            st.rerun()
    elif admin_pin_input.strip():
        st.caption("Galat Admin PIN.")

# Strict Caller Lock Check
if not is_caller_verified:
    st.warning(
        f"🔒 **{selected_preset} Locked:** Apni leads, dialer aur disposition sheet dekhne ke liye left sidebar mein sahi **Caller ID** aur **PIN** daalein."
    )
    st.info("Har employee ko sirf uska apna ID + PIN hi dein taaki koi kisi aur ka lead data na dekh sake.")
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
            "CSV / TXT File Upload Karo",
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
            "150 tak leads (Har line: Naam, Number)",
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

    done_count = len([i for i in profile["done_indices"] if i < total_leads]) if total_leads > 0 else 0

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
            "Lead ka Naam",
            value=default_name,
            key=f"name_{current_slug}_{profile['lead_idx']}_{default_name}",
        )
    with e_col2:
        lead_phone = st.text_input(
            "Lead ka Phone Number",
            value=default_phone,
            key=f"phone_{current_slug}_{profile['lead_idx']}_{default_phone}",
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

    st.subheader("🧠 1. Lead Reply Analyzer (Paste Reply / Call Note)")
    existing_note = profile["disposition_sheet"].get(profile["lead_idx"], {}).get("note", "")
    lead_reply = st.text_area(
        "Lead ne WhatsApp par kya likha ya phone par kya bola?",
        value=existing_note,
        placeholder="Example: 'Haan details bhejo', 'Kya ismein fees lagegi?', 'Abhi busy hoon kal call karna', 'Not interested'",
        height=75,
        key=f"reply_{current_slug}_{profile['lead_idx']}",
    )

    analysis = analyze_lead_reply(lead_reply)
    if analysis:
        if analysis["color"] == "success":
            st.success(f"**Status:** {analysis['status']}  \n**Next Action:** {analysis['action']}")
        elif analysis["color"] == "warning":
            st.warning(f"**Status:** {analysis['status']}  \n**Next Action:** {analysis['action']}")
        elif analysis["color"] == "error":
            st.error(f"**Status:** {analysis['status']}  \n**Next Action:** {analysis['action']}")
        else:
            st.info(f"**Status:** {analysis['status']}  \n**Next Action:** {analysis['action']}")

    if next_clicked and total_leads > 0:
        idx_now = profile["lead_idx"]
        final_disp = disp_select if disp_select else (analysis["disposition"] if analysis else "Follow-Up")
        saved_record = {
            "lead_no": idx_now + 1,
            "name": lead_name,
            "phone": lead_phone,
            "disposition": final_disp,
            "note": lead_reply.strip(),
            "caller": f"{caller_name} ({expected_auth['id']})",
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
                "caller": f"{caller_name} ({expected_auth['id']})",
                "current_lead": f"#{next_idx + 1} - {parsed_leads[next_idx]['name']}" if total_leads else lead_name,
                "completed": f"{len(profile['done_indices'])}/{total_leads}",
                "last_disposition": final_disp,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                status_code, _ = push_to_google_sheet(gsheet_webhook, [saved_record], live_activity=live_info)
                if 200 <= status_code < 300:
                    profile["synced_indices"].add(idx_now)
                    save_caller_backup(caller_name, profile)
            except Exception:
                pass

        st.rerun()

    st.divider()

    wa_messages = {
        "First Touch (Call ke baad / Pehela Message)": (
            f"Namaste {lead_name} ji 👋\n"
            f"{brand_name} se baat kar raha hoon.\n\n"
            f"Humne aapke liye *{topic}* rakhi hai ({class_time}) — yeh *100% free educational session* hai (koi tip ya paid signal nahi).\n\n"
            f"Agar aap seekhna chahte hain toh *YES* likh kar bhejein, main join karne ki detail bhej deta hoon."
        ),
        "Call Nahi Uthaya (Missed / Not Picked)": (
            f"Hi {lead_name} ji, abhi {brand_name} se call kiya tha par connect nahi ho paya.\n\n"
            f"Hum *{topic}* ke liye free educational guidance de rahe hain.\n"
            f"Jab free hon toh batayein, kya main aapko {class_time} ki detail yahan bhej dun?"
        ),
        "Doubt: Kya Yeh Sach Mein Free Hai?": (
            f"Bilkul {lead_name} ji, yeh *{topic}* learning & awareness ke liye *100% free* hai.\n\n"
            f"Hamara focus sirf concepts samjhana hai taaki aap khud market ko better samajh sakein.\n"
            f"Kya main aapko session ka link/timing bhej dun?"
        ),
        "Follow-Up: Baad Mein Dekhenge / Kal Baat Karte Hain": (
            f"Koi baat nahi {lead_name} ji 👍\n"
            f"Jab aapko waqt mile tab zaroor batana. *{topic}* ke basic notes/details main yahan rakh deta hoon taaki aaram se dekh sakein."
        ),
    }

    call_scripts = {
        "Opening (Pehle 20 Second)": (
            f"\"Hello {lead_name} ji, namaste! Main {brand_name} se bol raha hoon. Aapne market learning ke liye enquiry ki thi — "
            f"hum *{topic}* kara rahe hain jo bilkul free educational class hai. Kya aap abhi 30 second baat kar sakte hain?\""
        ),
        "Agar Bole: 'Mujhe Kuch Nahi Aata'": (
            f"\"Bilkul koi dikkat nahi {lead_name} ji, yeh session shuruat se seekhne walon ke liye hi hai — bilkul simple bhasha mein. "
            f"Main aapko WhatsApp par short detail bhej deta hoon, pehle wahan dekh lijiyega.\""
        ),
        "Agar Bole: 'Paise / Fees Lagegi Kya?'": (
            f"\"Nahi {lead_name} ji, yeh *{topic}* learning ke liye free hai. Main abhi aapko WhatsApp par exact details bhej raha hoon, aap khud check kar lijiyega.\""
        ),
        "Closing / WhatsApp Push": (
            f"\"Great {lead_name} ji! Main abhi aapko WhatsApp par {class_time} ki detail bhej raha hoon — wahan 'YES' bhej dijiyega taaki aapko reminder mil jaye.\""
        ),
    }

    wa_keys = list(wa_messages.keys())
    call_keys = list(call_scripts.keys())
    default_wa_idx = wa_keys.index(analysis["suggested_wa"]) if analysis else 0
    default_call_idx = call_keys.index(analysis["suggested_call"]) if analysis else 0

    col1, col2 = st.columns(2)
    formatted_num = clean_phone(lead_phone)
    display_num = f"+{formatted_num}" if len(formatted_num) >= 10 else (lead_phone or "No Number")

    with col1:
        st.subheader("💬 2. Ready WhatsApp Message")
        wa_type = st.selectbox(
            "Message Type Chuno",
            wa_keys,
            index=default_wa_idx,
            key=f"wa_type_{current_slug}_{profile['lead_idx']}",
        )
        selected_wa_text = wa_messages[wa_type]
        st.code(selected_wa_text, language="text")

        if len(formatted_num) >= 10:
            encoded_msg = urllib.parse.quote(selected_wa_text)
            wa_url = f"https://wa.me/{formatted_num}?text={encoded_msg}"
            st.link_button(f"📲 WhatsApp Par Bhejo ({display_num})", wa_url, use_container_width=True)
        else:
            st.caption("10-digit number daalne par direct WhatsApp link ban jayega.")

    with col2:
        st.subheader("📞 3. Ready Calling Script")
        st.markdown("**Dial Number (Copy / Call):**")
        st.code(display_num, language="text")

        call_type = st.selectbox(
            "Call Situation Chuno",
            call_keys,
            index=default_call_idx,
            key=f"call_type_{current_slug}_{profile['lead_idx']}",
        )
        st.code(call_scripts[call_type], language="text")

        if len(formatted_num) >= 10:
            tel_url = f"tel:+{formatted_num}"
            st.link_button(f"📞 Call Dialer Kholo ({display_num})", tel_url, use_container_width=True)
        else:
            st.caption("Phone number daalne par dialer kholne ka button aayega.")

    st.divider()
    st.subheader("📊 4. Disposition Sheet (Interested / Not Interested Tracker)")

    sorted_keys = sorted(profile["disposition_sheet"].keys())
    sheet_rows = [profile["disposition_sheet"][k] for k in sorted_keys]

    if sheet_rows:
        int_count = sum(1 for r in sheet_rows if r["disposition"] == "Interested")
        not_int_count = sum(1 for r in sheet_rows if r["disposition"] == "Not Interested")
        fu_count = sum(
            1
            for r in sheet_rows
            if r["disposition"] in ["Follow-Up", "Doubt / Beginner", "Not Picked / Switched Off"]
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Logged", len(sheet_rows))
        m2.metric("Interested", int_count)
        m3.metric("Not Interested", not_int_count)
        m4.metric("Follow-Up / Doubt / Other", fu_count)

        filter_disp = st.selectbox(
            "Filter Disposition",
            ["All"] + DISPOSITION_OPTIONS,
            key=f"filter_disp_select_{current_slug}",
        )

        filtered_rows = (
            sheet_rows
            if filter_disp == "All"
            else [r for r in sheet_rows if r["disposition"] == filter_disp]
        )

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
                live_info = {
                    "caller": f"{caller_name} ({expected_auth['id']})",
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
                        st.success(f"{len(unsynced_rows)} rows Google Sheet mein sync ho gayi!")
                        st.rerun()
                    else:
                        st.error(f"Google Sheet response status: {status_code}")
                except Exception as e:
                    st.error(f"Sync error: {e}")

        with s_col2:
            st.download_button(
                label="⬇️ Download Disposition Sheet (CSV)",
                data=build_csv(sheet_rows),
                file_name=f"trade_vertex_disposition_{current_slug}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"dl_btn_{current_slug}",
            )
    else:
        st.info("Abhi koi disposition save nahi hua hai. Call/reply ke baad disposition chun kar **Save & Next ➡️** dabao — sheet yahan ban jayegi.")

# Admin Live Team Monitor (Only visible when Admin PIN matches)
if is_admin_verified:
    st.divider()
    st.subheader("👀 5. Admin Live Team Monitor (5on Callers Kya Kar Rahe Hain)")

    local_team_rows = []
    for c_name, auth_info in CALLER_AUTH_MAP.items():
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
                "caller": c_name,
                "caller_id": auth_info["id"],
                "current_lead": curr_lbl,
                "completed": f"{len(prof.get('done_indices', set()))}/{p_total}",
                "logged_count": len(disp_map),
                "last_disposition": last_rec["disposition"] if last_rec else "—",
            })
        else:
            local_team_rows.append({
                "caller": c_name,
                "caller_id": auth_info["id"],
                "current_lead": "Not Started",
                "completed": "0/0",
                "logged_count": 0,
                "last_disposition": "—",
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
                "Google Sheet live view ke liye Apps Script mein `doGet(e)` bhi hona chahiye aur deploy update hona chahiye."
            )

    cached_sheet_rows = st.session_state.get("live_sheet_cache", [])
    if cached_sheet_rows:
        df_remote = pd.DataFrame(cached_sheet_rows)
        remote_csv = df_remote.to_csv(index=False, header=False).encode("utf-8-sig")
        st.download_button(
            label="⬇️ Download All Callers Sheet from Google Sheet (CSV)",
            data=remote_csv,
            file_name="trade_vertex_all_callers_live.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_all_callers_remote_csv",
        )
