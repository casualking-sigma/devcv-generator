import io
import json
import logging
import re
from typing import Optional

import streamlit as st
import stripe
from openai import OpenAI

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader

from jsonschema import validate as js_validate
from jsonschema.exceptions import ValidationError


# ----------------------------
# Page config (must be first)
# ----------------------------
st.set_page_config(page_title="DevCV", page_icon="📄", layout="centered")


# ----------------------------
# UI styling
# ----------------------------
def inject_css(background_image_url: Optional[str] = None):
    if background_image_url:
        app_bg = f"""
        background-image:
          radial-gradient(1200px 600px at 20% 10%, rgba(124,58,237,0.35), transparent 60%),
          radial-gradient(900px 500px at 80% 30%, rgba(59,130,246,0.25), transparent 55%),
          linear-gradient(180deg, rgba(0,0,0,0.75), rgba(0,0,0,0.85)),
          url('{background_image_url}');
        background-size: cover;
        background-position: center;
        """
    else:
        app_bg = """
        background:
          radial-gradient(1200px 600px at 20% 10%, rgba(124,58,237,0.35), transparent 60%),
          radial-gradient(900px 500px at 80% 30%, rgba(59,130,246,0.25), transparent 55%),
          linear-gradient(180deg, #050612, #070A12);
        """

    st.markdown(
        f"""
        <style>
        #MainMenu {{visibility: hidden;}}
        footer {{visibility: hidden;}}
        header {{visibility: hidden;}}

        .stApp {{
            {app_bg}
        }}

        section.main > div {{ max-width: 920px; padding-top: 40px; }}

        .glass {{
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.10);
            box-shadow: 0 20px 60px rgba(0,0,0,0.45);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border-radius: 20px;
            padding: 28px;
        }}

        div.stButton > button {{
            border-radius: 14px !important;
            padding: 10px 14px !important;
            font-weight: 700 !important;
            border: 1px solid rgba(255,255,255,0.14) !important;
            background: rgba(255,255,255,0.06) !important;
        }}
        div.stButton > button:hover {{
            border-color: rgba(124,58,237,0.55) !important;
            box-shadow: 0 0 0 6px rgba(124,58,237,0.15) !important;
        }}

        .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div {{
            border-radius: 14px !important;
            background: rgba(255,255,255,0.06) !important;
            border: 1px solid rgba(255,255,255,0.14) !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_css()


# ----------------------------
# Secrets / clients
# ----------------------------
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]

logging.basicConfig(level=logging.INFO)

APP_URL = st.secrets.get("APP_URL", "http://localhost:8501").rstrip("/")
PRICE_AMOUNT = 2000
CURRENCY = "pln"


# ----------------------------
# Stripe helpers
# ----------------------------
def get_qparam(name: str) -> Optional[str]:
    v = st.query_params.get(name)
    if v is None:
        return None
    if isinstance(v, list):
        return v[0] if v else None
    return v


def verify_stripe_session(session_id: str) -> bool:
    try:
        s = stripe.checkout.Session.retrieve(session_id)
        return (
            s.get("mode") == "payment"
            and s.get("payment_status") == "paid"
            and s.get("amount_total") == PRICE_AMOUNT
            and s.get("currency") == CURRENCY
        )
    except Exception:
        logging.exception("Stripe verification failed")
        return False


# ----------------------------
# Font (PL chars)
# ----------------------------
pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))


# ----------------------------
# CV_SCHEMA (data contract)
# ----------------------------
CV_SCHEMA = {
    "name": "cv_pl_v1",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "header": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "full_name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "location": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["full_name", "email", "phone"],
            },
            "summary": {"type": "string"},
            "experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "role": {"type": "string"},
                        "company": {"type": "string"},
                        "location": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["role", "company", "bullets"],
                },
            },
            "education": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "school": {"type": "string"},
                        "degree": {"type": "string"},
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                        "details": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["school"],
                },
            },
            "skills": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["header", "summary", "experience", "education", "skills"],
    },
}


# ----------------------------
# LLM: JSON-only generation
# ----------------------------
SYSTEM_PROMPT_JSON = """
Jesteś ekspertem HR specjalizującym się w CV dla programistów.
Twoim jedynym zadaniem jest wygenerowanie CV jako CZYSTY JSON zgodny z podanym CV_SCHEMA.
Nie używaj Markdown ani HTML. Nie dodawaj komentarzy ani dodatkowego tekstu poza JSON.

ATS zasady (MUSZĄ być spełnione w treści):
- Sekcje: header, summary, skills, experience, education.
- Summary: 3–5 zdań, konkretnie.
- Experience bullets: osiągnięcia (czasownik + co + jak + efekt/liczba jeśli możliwe), max 160 znaków na bullet.
- Bez dziwnych znaków i ozdobników.
""".strip()


def clamp_text(s: str, max_chars: int) -> str:
    return (s or "").strip()[:max_chars]


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    if not text.startswith("{"):
        i = text.find("{")
        j = text.rfind("}")
        if i != -1 and j != -1 and j > i:
            text = text[i : j + 1]
    return json.loads(text)


def validate_cv_json(data: dict) -> None:
    js_validate(instance=data, schema=CV_SCHEMA["schema"])


def call_llm_json(messages, model="gpt-4o-mini", temperature=0.3, max_tokens=1400) -> str:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=messages,
    )
    return resp.choices[0].message.content


def generate_cv_data(style: str, user_inputs: dict, max_retries: int = 2) -> dict:
    style_instruction = {
        "Nowoczesny": "Język nowoczesny: zwięźle, konkretnie, metryki/liczby jeśli możliwe.",
        "Klasyczny": "Język klasyczny: formalnie, rzeczowo, bez ozdobników.",
        "Kreatywny": "Język kreatywny: lekko bardziej narracyjnie, ale nadal profesjonalnie i ATS-friendly.",
    }[style]

    user_prompt = f"""
Styl: {style_instruction}

Dane użytkownika:
full_name: {user_inputs["full_name"]}
email: {user_inputs["email"]}
phone: {user_inputs["phone"]}
location: {user_inputs.get("location","")}
links: {user_inputs.get("links","")}
experience_raw: {user_inputs["experience_raw"]}
skills_raw: {user_inputs["skills_raw"]}
education_raw: {user_inputs["education_raw"]}

Wymagania:
- Zwróć WYŁĄCZNIE poprawny JSON zgodny z CV_SCHEMA (poniżej).
- Dla experience: jeśli użytkownik podał wiele ról/firm, rozdziel logicznie na pozycje. 4–6 bulletów na rolę.
- Każdy bullet max 160 znaków.

CV_SCHEMA:
{json.dumps(CV_SCHEMA["schema"], ensure_ascii=False)}
""".strip()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_JSON},
        {"role": "user", "content": user_prompt},
    ]

    last_text = ""
    for attempt in range(max_retries + 1):
        text = call_llm_json(messages)
        last_text = text
        try:
            data = extract_json(text)
            validate_cv_json(data)
            return data
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt >= max_retries:
                raise RuntimeError(f"LLM JSON validation failed: {e}\nRaw:\n{last_text}")
            fix_prompt = f"""
Poprzednia odpowiedź nie przeszła walidacji JSON/schema.
Błąd: {str(e)}

Poprzednia odpowiedź:
{last_text}

Napraw i zwróć WYŁĄCZNIE poprawny JSON zgodny z CV_SCHEMA. Bez komentarzy.
""".strip()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_JSON},
                {"role": "user", "content": fix_prompt},
            ]

    raise RuntimeError("Unexpected JSON generation failure")


# ----------------------------
# Deterministic preview (Markdown) from JSON
# ----------------------------
def cv_to_markdown(cv: dict) -> str:
    h = cv["header"]
    lines = []
    lines.append(f"# {h['full_name']}")
    contact = [h.get("email", ""), h.get("phone", ""), h.get("location", "")]
    contact = [x for x in contact if x]
    if contact:
        lines.append(" | ".join(contact))
    if h.get("links"):
        lines.append(" | ".join(h["links"]))
    lines.append("")

    lines.append("## Podsumowanie")
    lines.append(cv["summary"].strip())
    lines.append("")

    lines.append("## Umiejętności")
    for s in cv["skills"]:
        lines.append(f"- {s}")
    lines.append("")

    lines.append("## Doświadczenie")
    for exp in cv["experience"]:
        title = f"**{exp['role']} – {exp['company']}**"
        meta = []
        if exp.get("location"):
            meta.append(exp["location"])
        if exp.get("date_from") or exp.get("date_to"):
            meta.append(f"{exp.get('date_from','')} – {exp.get('date_to','')}".strip())
        if meta:
            title += "  \n" + " · ".join([m for m in meta if m])
        lines.append(title)
        for b in exp["bullets"]:
            lines.append(f"- {b}")
        lines.append("")

    lines.append("## Edukacja")
    for edu in cv["education"]:
        title = f"**{edu['school']}**"
        if edu.get("degree"):
            title += f" — {edu['degree']}"
        dates = " · ".join([x for x in [edu.get("date_from", ""), edu.get("date_to", "")] if x])
        if dates:
            title += f"  \n{dates}"
        lines.append(title)
        for d in edu.get("details", []):
            lines.append(f"- {d}")
        lines.append("")

    return "\n".join(lines).strip()


# ----------------------------
# PDF rendering (ATS-safe, 1 column) from JSON
# ----------------------------
def _set_style(style: str):
    # minimal typographic differences only
    if style == "Nowoczesny":
        return {"h1": 18, "h2": 12, "body": 10, "leading": 13}
    if style == "Klasyczny":
        return {"h1": 17, "h2": 12, "body": 10, "leading": 14}
    return {"h1": 18, "h2": 12, "body": 10, "leading": 13}  # Kreatywny


def draw_wrapped(c, text, x, y, max_width, font="DejaVuSans", size=10, leading=13):
    c.setFont(font, size)
    words = (text or "").split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if stringWidth(test, font, size) <= max_width:
            line = test
        else:
            if line:
                c.drawString(x, y, line)
                y -= leading
            line = w
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def ensure_space(c, y, needed, margin, height, style_conf):
    if y - needed < margin:
        c.showPage()
        c.setFont("DejaVuSans", style_conf["body"])
        return height - margin
    return y


def render_pdf_from_cv(cv: dict, style: str, photo_file) -> io.BytesIO:
    style_conf = _set_style(style)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 50
    max_width = width - 2 * margin

    y = height - margin

    # Header
    h = cv["header"]
    c.setFont("DejaVuSans", style_conf["h1"])
    y = ensure_space(c, y, 30, margin, height, style_conf)
    c.drawString(margin, y, h["full_name"])
    y -= 22

    c.setFont("DejaVuSans", style_conf["body"])
    contact = [h.get("email", ""), h.get("phone", ""), h.get("location", "")]
    contact = [x for x in contact if x]
    if contact:
        y = draw_wrapped(c, " | ".join(contact), margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
    if h.get("links"):
        y = draw_wrapped(c, " | ".join(h["links"]), margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
    y -= 6

    # Optional photo (top-right) - ATS safer if small and not pushing layout; keep optional
    if photo_file:
        try:
            img = ImageReader(photo_file)
            img_w, img_h = 90, 110
            c.drawImage(img, width - margin - img_w, height - margin - img_h, width=img_w, height=img_h, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    def section(title: str, y: float) -> float:
        y = ensure_space(c, y, 24, margin, height, style_conf)
        c.setFont("DejaVuSans", style_conf["h2"])
        c.drawString(margin, y, title.upper())
        y -= 16
        c.setFont("DejaVuSans", style_conf["body"])
        return y

    # Summary
    y = section("Podsumowanie", y)
    y = ensure_space(c, y, 60, margin, height, style_conf)
    y = draw_wrapped(c, cv["summary"], margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
    y -= 6

    # Skills
    y = section("Umiejętności", y)
    for s in cv["skills"]:
        y = ensure_space(c, y, style_conf["leading"] + 2, margin, height, style_conf)
        y = draw_wrapped(c, f"• {s}", margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
    y -= 6

    # Experience
    y = section("Doświadczenie", y)
    for exp in cv["experience"]:
        y = ensure_space(c, y, 40, margin, height, style_conf)
        c.setFont("DejaVuSans", style_conf["body"])
        header_line = f"{exp['role']} — {exp['company']}"
        y = draw_wrapped(c, header_line, margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
        meta = []
        if exp.get("location"):
            meta.append(exp["location"])
        if exp.get("date_from") or exp.get("date_to"):
            meta.append(f"{exp.get('date_from','')} – {exp.get('date_to','')}".strip())
        if meta:
            y = draw_wrapped(c, " · ".join([m for m in meta if m]), margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])

        for b in exp["bullets"]:
            y = ensure_space(c, y, style_conf["leading"] + 2, margin, height, style_conf)
            y = draw_wrapped(c, f"• {b}", margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
        y -= 6

    # Education
    y = section("Edukacja", y)
    for edu in cv["education"]:
        y = ensure_space(c, y, 34, margin, height, style_conf)
        title = edu["school"]
        if edu.get("degree"):
            title += f" — {edu['degree']}"
        y = draw_wrapped(c, title, margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
        dates = " · ".join([x for x in [edu.get("date_from", ""), edu.get("date_to", "")] if x])
        if dates:
            y = draw_wrapped(c, dates, margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
        for d in edu.get("details", []):
            y = ensure_space(c, y, style_conf["leading"] + 2, margin, height, style_conf)
            y = draw_wrapped(c, f"• {d}", margin, y, max_width, size=style_conf["body"], leading=style_conf["leading"])
        y -= 6

    c.save()
    buffer.seek(0)
    return buffer


# ----------------------------
# Stripe gate (secure unlock)
# ----------------------------
if "paid" not in st.session_state:
    st.session_state.paid = False

session_id = get_qparam("session_id")
if not st.session_state.paid and session_id:
    if verify_stripe_session(session_id):
        st.session_state.paid = True
        st.session_state.paid_session_id = session_id
        try:
            del st.query_params["session_id"]
        except Exception:
            pass
        st.rerun()


# ----------------------------
# UI
# ----------------------------
st.title("Generator CV – 20 zł")

if not st.session_state.paid:
    st.write("Profesjonalne CV po polsku, ATS-friendly, wygenerowane przez AI.")
    st.write("**Cena: 20 zł** – jednorazowa płatność")

    if st.secrets.get("ALLOW_TEST_BYPASS", False):
        if st.button("TESTUJ BEZ PŁATNOŚCI (tylko lokalnie)"):
            st.session_state.paid = True
            st.rerun()

    if st.button("Kup teraz i wygeneruj CV"):
        with st.spinner("Przekierowanie do Stripe..."):
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price_data": {
                            "currency": "pln",
                            "product_data": {"name": "Profesjonalne CV PDF"},
                            "unit_amount": PRICE_AMOUNT,
                        },
                        "quantity": 1,
                    }
                ],
                mode="payment",
                success_url=f"{APP_URL}/?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{APP_URL}/",
            )
            st.markdown(
                f'<meta http-equiv="refresh" content="0;url={session.url}">',
                unsafe_allow_html=True,
            )
else:
    st.success("✅ Płatność zaakceptowana! Wypełnij dane poniżej")

    imie = st.text_input("Imię i nazwisko")
    email = st.text_input("Email")
    telefon = st.text_input("Telefon")
    location = st.text_input("Lokalizacja (opcjonalnie)", "")
    links = st.text_input("Linki (LinkedIn/GitHub) – opcjonalnie, oddziel przecinkami", "")

    doswiadczenie = st.text_area("Doświadczenie zawodowe (krótko, punktami)")
    umiejetnosci = st.text_area("Umiejętności (punktami)")
    edukacja = st.text_area("Edukacja")
    zdjecie = st.file_uploader("Zdjęcie (opcjonalne)", type=["jpg", "png"])
    styl = st.selectbox("Styl CV", ["Nowoczesny", "Klasyczny", "Kreatywny"])

    if st.button("Wygeneruj CV"):
        if not all([imie, email, telefon, doswiadczenie, umiejetnosci, edukacja]):
            st.error("Wypełnij wszystkie obowiązkowe pola")
            st.stop()

        user_inputs = {
            "full_name": imie,
            "email": email,
            "phone": telefon,
            "location": location.strip(),
            "links": [x.strip() for x in links.split(",") if x.strip()],
            "experience_raw": clamp_text(doswiadczenie, 3000),
            "skills_raw": clamp_text(umiejetnosci, 2000),
            "education_raw": clamp_text(edukacja, 2000),
        }

        with st.spinner("Generowanie treści CV (JSON)..."):
            try:
                cv_data = generate_cv_data(styl, user_inputs)
            except Exception as e:
                st.error("Nie udało się wygenerować poprawnego CV. Spróbuj ponownie.")
                st.exception(e)
                st.stop()

        st.subheader("Podgląd CV")
        st.markdown(cv_to_markdown(cv_data))

        with st.spinner("Renderowanie PDF..."):
            pdf_buf = render_pdf_from_cv(cv_data, styl, zdjecie)

        st.download_button(
            "Pobierz gotowe CV PDF",
            pdf_buf,
            file_name=f"CV_{imie.replace(' ', '_')}_{styl}.pdf",
            mime="application/pdf",
        )
        st.success("CV gotowe!")

# Notes:
# - Ten PDF jest ATS-safe: 1 kolumna, brak tabel/ikon/kolorowych bloków.
# - Jeśli potrzebujesz twardego “zapisu płatności” bez DB, kolejnym krokiem jest receipt_code (HMAC).
