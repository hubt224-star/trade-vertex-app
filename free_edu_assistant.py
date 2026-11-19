import streamlit as st

st.set_page_config(page_title="Trade Vertex - Smart Calling & WA Helper", layout="wide")

st.title("📚 Trade Vertex — Smart Lead Classifier, Calling & WA Helper")
st.caption("Lead ka reply paste karke status pehchano aur turant sahi WhatsApp/Call response nikaalo.")

# --- SMART REPLY CLASSIFIER LOGIC ---
def analyze_lead_reply(reply_text: str):
    text = reply_text.lower().strip()
    if not text:
        return None

    not_interested_kw = ["not interested", "nhi", "nahi", "band karo", "wrong number", "mat karo", "stop", "no thanks", "timepass"]
    hot_kw = ["yes", "haan", "han", "link", "bhejo", "send", "join", "kab hai", "timing", "interested", "sikha", "seekhna", "details"]
    doubt_kw = ["free", "paisa", "paise", "fees", "charge", "kuch nhi aata", "beginner", "new", "kaise", "kya hai", "sebi", "tip"]
    followup_kw = ["baad", "kal", "sham", "busy", "abhi nahi", "later", "call back", "free hoke", "sunday", "weekend"]

    if any(k in text for k in not_interested_kw) and not any(h in text for h in ["yes", "haan", "han", "bhejo", "link"]):
        return {
            "status": "❌ Not Interested",
            "color": "error",
            "action": "Is lead par time waste mat karo. List mein Not Interested mark karke aage badho.",
            "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
            "suggested_call": "Opening (Pehle 20 Second)",
        }
    if any(k in text for k in hot_kw):
        return {
            "status": "🔥 Hot / Interested Lead",
            "color": "success",
            "action": "Turant WhatsApp par session detail/link bhejo aur aaj ki class mein join karwao.",
            "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
            "suggested_call": "Closing / WhatsApp Push",
        }
    if any(k in text for k in doubt_kw):
        return {
            "status": "🤔 Doubt / Beginner Lead (High Potential)",
            "color": "warning",
            "action": "Inka doubt clear karo ki yeh 100% free educational session hai, phir link bhejo.",
            "suggested_wa": "Doubt: Kya Yeh Sach Mein Free Hai?",
            "suggested_call": "Agar Bole: 'Paise / Fees Lagegi Kya?'",
        }
    if any(k in text for k in followup_kw):
        return {
            "status": "⏳ Call Back / Follow-Up Lead",
            "color": "info",
            "action": "Abhi force mat karo. WhatsApp par short detail chhod do aur kal/bataye time par follow-up lo.",
            "suggested_wa": "Follow-Up: Baad Mein Dekhenge / Kal Baat Karte Hain",
            "suggested_call": "Opening (Pehle 20 Second)",
        }

    return {
        "status": "🟡 Neutral / Needs Quick Clarification",
        "color": "info",
        "action": "Ek short message bhej kar pucho ki wo free live class ki detail chahte hain ya nahi.",
        "suggested_wa": "First Touch (Call ke baad / Pehela Message)",
        "suggested_call": "Opening (Pehle 20 Second)",
    }


# --- SIDEBAR INPUTS ---
with st.sidebar:
    st.header("1. Lead & Class Details")
    brand_name = st.text_input("Brand Name", value="Trade Vertex")
    lead_name = st.text_input("Lead ka Naam", value="Rahul")
    topic = st.selectbox(
        "Topic / Focus",
        [
            "Free Basics & Chart Reading Class",
            "Free Price Action & Market Structure Session",
            "Free Beginner Live Class",
        ],
    )
    class_time = st.text_input("Class Timing / Offer Line", value="Aaj sham 8 baje ki free live class")

# --- TOP SECTION: AI LEAD REPLY ANALYZER ---
st.subheader("🧠 1. Lead Reply Analyzer (Paste Reply / Call Note)")
lead_reply = st.text_area(
    "Lead ne WhatsApp par kya likha ya phone par kya bola?",
    placeholder="Example: 'Haan details bhejo', 'Kya ismein fees lagegi?', 'Abhi busy hoon kal call karna', 'Not interested'",
    height=80,
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

st.divider()

# --- TEMPLATES ---
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

# Auto-select recommended template if reply was analyzed
wa_keys = list(wa_messages.keys())
call_keys = list(call_scripts.keys())
default_wa_idx = wa_keys.index(analysis["suggested_wa"]) if analysis else 0
default_call_idx = call_keys.index(analysis["suggested_call"]) if analysis else 0

# --- BOTTOM SECTION: READY MESSAGES & SCRIPTS ---
col1, col2 = st.columns(2)

with col1:
    st.subheader("💬 2. Ready WhatsApp Message")
    wa_type = st.selectbox("Message Type Chuno", wa_keys, index=default_wa_idx)
    st.code(wa_messages[wa_type], language="text")

with col2:
    st.subheader("📞 3. Ready Calling Script")
    call_type = st.selectbox("Call Situation Chuno", call_keys, index=default_call_idx)
    st.code(call_scripts[call_type], language="text")

