import io
import json
import re
import urllib.parse
import urllib.request
import streamlit as st

st.set_page_config(page_title="Trade Vertex - Smart Calling & WA Helper", layout="wide")

st.title("📚 Trade Vertex — Smart Lead Classifier, Calling & WA Helper")
st.caption("150 leads paste karo, disposition mark karo, aur seedha Google Sheet ya CSV mein bhejo.")


def clean_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return "91" + digits
    return digits


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
    return leads


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

    if any(k in text for k in not_interested_kw) and not any(h in text for h in ["yes", "haan", "han", "bhejo", "link"]):
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


def push_to_google_sheet(webhook_url: str, rows: list):
    payload = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url.strip(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        return resp.status, body


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
    return output.getvalue().encode("utf-8")


if "lead_idx" not in st.session_state:
    st.session_state.lead_idx = 0
if "done_indices" not in st.session_state:
    st.session_state.done_indices = set()
if "disposition_sheet" not in st.session_state:
    st.session_state.disposition_sheet = {}
if "synced_indices" not in st.session_state:
    st.session_state.synced_indices = set()

disposition_options = [
    "Interested",
    "Not Interested",
    "Doubt / Beginner",
    "Follow-Up",
    "Not Picked / Switched Off",
]

with st.sidebar:
    st.header("1. Class & Brand Settings")
    brand_name = st.text_input("Brand Name", value="Trade Vertex")
    caller_name = st.text_input("Employee / Caller Name", value="Team Member")
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
    st.header("2. Google Sheet Sync (Optional)")
    gsheet_webhook = st.text_input(
        "Apps Script Web App URL",
        placeholder="https://script.google.com/macros/s/.../exec",
    )

    st.divider()
    st.header("3. Batch Leads (Up to 150+)")
    default_leads = "Rahul, 9876543210\nAmit, 9123456789\nPriya, 9988776655"
    raw_leads = st.text_area(
        "150 tak leads paste karo (Har line: Naam, Number)",
        value=default_leads,
        height=160,
        help="Ek saath 150 lines paste kar sakte ho. Duplicate numbers apne aap skip ho jayenge.",
    )

parsed_leads = parse_leads_text(raw_leads)
total_leads = len(parsed_leads)

if total_leads > 0:
    if st.session_state.lead_idx >= total_leads:
        st.session_state.lead_idx = total_leads - 1
    current_lead = parsed_leads[st.session_state.lead_idx]
    default_name = current_lead["name"]
    default_phone = current_lead["phone"]
else:
    current_lead = None
    default_name = "Rahul"
    default_phone = ""

if total_leads > 0:
    done_count = len([i for i in st.session_state.done_indices if i < total_leads])
    st.progress(done_count / total_leads, text=f"Completed: {done_count} / {total_leads} leads | Current: #{st.session_state.lead_idx + 1}")

q_col1, q_col2, q_col3, q_col4, q_col5 = st.columns([2.4, 1, 1.2, 1.1, 1])
with q_col1:
    if total_leads > 0:
        is_done = st.session_state.lead_idx in st.session_state.done_indices
        saved_disp = st.session_state.disposition_sheet.get(st.session_state.lead_idx, {}).get("disposition", "")
        status_tag = f"✅ {saved_disp}" if saved_disp else ("✅ Done" if is_done else "📞 Pending")
        st.markdown(f"**{status_tag} — Lead {st.session_state.lead_idx + 1} of {total_leads}:** `{default_name}`")
    else:
        st.markdown("**Active Lead:** Manual Mode")

with q_col2:
    if st.button("⬅️ Prev", use_container_width=True, disabled=total_leads == 0 or st.session_state.lead_idx == 0):
        st.session_state.lead_idx -= 1
        st.rerun()

with q_col3:
    next_clicked = st.button("Save & Next ➡️", type="primary", use_container_width=True, disabled=total_leads == 0)

with q_col4:
    if total_leads > 0:
        jump_num = st.number_input(
            "Jump #",
            min_value=1,
            max_value=total_leads,
            value=st.session_state.lead_idx + 1,
            label_visibility="collapsed",
            key=f"jump_input_{st.session_state.lead_idx}",
        )
        if int(jump_num) - 1 != st.session_state.lead_idx:
            st.session_state.lead_idx = int(jump_num) - 1
            st.rerun()

with q_col5:
    if st.button("🔄 Reset", use_container_width=True, disabled=total_leads == 0):
        st.session_state.lead_idx = 0
        st.session_state.done_indices.clear()
        st.session_state.disposition_sheet.clear()
        st.session_state.synced_indices.clear()
        st.rerun()

e_col1, e_col2, e_col3 = st.columns([1.4, 1.4, 1.2])
with e_col1:
    lead_name = st.text_input("Lead ka Naam", value=default_name, key=f"name_{st.session_state.lead_idx}_{default_name}")
with e_col2:
    lead_phone = st.text_input("Lead ka Phone Number", value=default_phone, key=f"phone_{st.session_state.lead_idx}_{default_phone}")
with e_col3:
    existing_disp = st.session_state.disposition_sheet.get(st.session_state.lead_idx, {}).get("disposition")
    disp_select = st.selectbox(
        "Call Disposition",
        options=disposition_options,
        index=disposition_options.index(existing_disp) if existing_disp in disposition_options else 0,
        key=f"disp_{st.session_state.lead_idx}",
    )

st.divider()

st.subheader("🧠 1. Lead Reply Analyzer (Paste Reply / Call Note)")
existing_note = st.session_state.disposition_sheet.get(st.session_state.lead_idx, {}).get("note", "")
lead_reply = st.text_area(
    "Lead ne WhatsApp par kya likha ya phone par kya bola?",
    value=existing_note,
    placeholder="Example: 'Haan details bhejo', 'Kya ismein fees lagegi?', 'Abhi busy hoon kal call karna', 'Not interested'",
    height=75,
    key=f"reply_{st.session_state.lead_idx}",
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
    idx_now = st.session_state.lead_idx
    final_disp = disp_select if disp_select else (analysis["disposition"] if analysis else "Follow-Up")
    st.session_state.disposition_sheet[idx_now] = {
        "lead_no": idx_now + 1,
        "name": lead_name,
        "phone": lead_phone,
        "disposition": final_disp,
        "note": lead_reply.strip(),
        "caller": caller_name,
    }
    st.session_state.synced_indices.discard(idx_now)
    st.session_state.done_indices.add(idx_now)
    if idx_now < total_leads - 1:
        st.session_state.lead_idx += 1
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
    wa_type = st.selectbox("Message Type Chuno", wa_keys, index=default_wa_idx, key=f"wa_type_{st.session_state.lead_idx}")
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

    call_type = st.selectbox("Call Situation Chuno", call_keys, index=default_call_idx, key=f"call_type_{st.session_state.lead_idx}")
    st.code(call_scripts[call_type], language="text")

    if len(formatted_num) >= 10:
        tel_url = f"tel:+{formatted_num}"
        st.link_button(f"📞 Call Dialer Kholo ({display_num})", tel_url, use_container_width=True)
    else:
        st.caption("Phone number daalne par dialer kholne ka button aayega.")

st.divider()
st.subheader("📊 4. Disposition Sheet (Interested / Not Interested Tracker)")

sorted_keys = sorted(st.session_state.disposition_sheet.keys())
sheet_rows = [st.session_state.disposition_sheet[k] for k in sorted_keys]

if sheet_rows:
    int_count = sum(1 for r in sheet_rows if r["disposition"] == "Interested")
    not_int_count = sum(1 for r in sheet_rows if r["disposition"] == "Not Interested")
    fu_count = sum(1 for r in sheet_rows if r["disposition"] in ["Follow-Up", "Doubt / Beginner", "Not Picked / Switched Off"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Logged", len(sheet_rows))
    m2.metric("Interested", int_count)
    m3.metric("Not Interested", not_int_count)
    m4.metric("Follow-Up / Doubt / Other", fu_count)

    filter_disp = st.selectbox(
        "Filter Disposition",
        ["All"] + disposition_options,
        key="filter_disp_select",
    )

    filtered_rows = (
        sheet_rows
        if filter_disp == "All"
        else [r for r in sheet_rows if r["disposition"] == filter_disp]
    )

    st.dataframe(filtered_rows, use_container_width=True, hide_index=True)

    unsynced_keys = [k for k in sorted_keys if k not in st.session_state.synced_indices]
    unsynced_rows = [st.session_state.disposition_sheet[k] for k in unsynced_keys]

    s_col1, s_col2 = st.columns(2)
    with s_col1:
        if st.button(
            f"☁️ Sync to Google Sheet ({len(unsynced_rows)} new)",
            use_container_width=True,
            disabled=not gsheet_webhook.strip() or not unsynced_rows,
        ):
            try:
                status_code, _ = push_to_google_sheet(gsheet_webhook, unsynced_rows)
                if 200 <= status_code < 300:
                    st.session_state.synced_indices.update(unsynced_keys)
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
            file_name="trade_vertex_disposition_sheet.csv",
            mime="text/csv",
            use_container_width=True,
        )
else:
    st.info("Abhi koi disposition save nahi hua hai. Call/reply ke baad disposition chun kar **Save & Next ➡️** dabao — sheet yahan ban jayegi.")
