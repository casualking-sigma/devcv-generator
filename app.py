import os
import json
from datetime import datetime
import streamlit as st
from openai import OpenAI
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
import io
import stripe

# === KLUCZE – przeniesione do st.secrets (dodaj do .streamlit/secrets.toml) ===
client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]
# Font
pdfmetrics.registerFont(TTFont('DejaVuSans', 'DejaVuSans.ttf'))

# Schemat CV (stały kontrakt danych)
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
                    "links": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["full_name", "email", "phone"]
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
                        "bullets": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["role", "company", "bullets"]
                }
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
                        "details": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["school"]
                }
            },
            "skills": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["header", "summary", "experience", "education", "skills"]
    }
}

# Helpers do PDF (zawijanie tekstu)
def draw_wrapped(c, text, x, y, max_width, font="DejaVuSans", size=11, leading=14):
    c.setFont(font, size)
    words = text.split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if stringWidth(test, font, size) <= max_width:
            line = test
        else:
            c.drawString(x, y, line)
            y -= leading
            line = w
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y

def draw_bullets(c, bullets, x, y, max_width, font="DejaVuSans", size=11):
    for b in bullets:
        y = draw_wrapped(c, "• " + b, x, y, max_width, font=font, size=size)
    return y

# Płatność z URL
query_params = st.query_params
if 'paid' in query_params and query_params['paid'][0] == 'true':
    st.session_state.paid = True

if 'paid' not in st.session_state:
    st.session_state.paid = False

st.title("Generator CV – 20 zł")

if not st.session_state.paid:
    st.write("Profesjonalne CV po polsku, ATS-friendly, wygenerowane przez AI.")
    st.write("**Cena: 20 zł** – jednorazowa płatność")
    
    if st.button("TESTUJ BEZ PŁATNOŚCI (tylko lokalnie)"):
        st.session_state.paid = True
        st.rerun()
    
    if st.button("Kup teraz i wygeneruj CV"):
        with st.spinner("Przekierowanie do Stripe..."):
            session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'pln',
                        'product_data': {'name': 'Profesjonalne CV PDF'},
                        'unit_amount': 2000,
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url='http://localhost:8501/?paid=true',
                cancel_url='http://localhost:8501'
            )
            st.markdown(f'<meta http-equiv="refresh" content="0;url={session.url}">', unsafe_allow_html=True)
else:
    st.success("✅ Płatność zaakceptowana! Wypełnij dane poniżej")

    imie = st.text_input("Imię i nazwisko")
    email = st.text_input("Email")
    telefon = st.text_input("Telefon")
    doswiadczenie = st.text_area("Doświadczenie zawodowe (krótko, punktami)")
    umiejetnosci = st.text_area("Umiejętności (punktami)")
    edukacja = st.text_area("Edukacja")
    zdjecie = st.file_uploader("Zdjęcie (opcjonalne)", type=["jpg", "png"])
    styl = st.selectbox("Styl CV", ["Nowoczesny", "Klasyczny", "Kreatywny"])

    if st.button("Wygeneruj CV"):
        if not all([imie, email, telefon, doswiadczenie, umiejetnosci, edukacja]):
            st.error("Wypełnij wszystkie obowiązkowe pola")
        else:
            # === LEPSZY PROMPT – różne style tekstu ===
            style_instruction = {
                "Nowoczesny": "Styl nowoczesny: minimalistyczny, dynamiczny język, fokus na osiągnięciach z liczbami (np. 'zwiększyłem sprzedaż o 40%'), krótkie zdania, profesjonalny ton, ATS-friendly.",
                "Klasyczny": "Styl klasyczny: formalny, tradycyjny język, chronologiczna struktura, szczegółowe opisy obowiązków, bez ozdobników.",
                "Kreatywny": "Styl kreatywny: angażujący storytelling, pasja, unikalne frazy, elementy osobowości (np. 'Moja pasja do marketingu zaczęła się od...'), kreatywne sekcje."
            }[styl]

            prompt = f"""
            Jesteś ekspertem HR. Napisz profesjonalne CV po polsku w stylu: {style_instruction}
            Dane użytkownika:
            Imię i nazwisko: {imie}
            Email: {email}
            Telefon: {telefon}
            Doświadczenie: {doswiadczenie}
            Umiejętności: {umiejetnosci}
            Edukacja: {edukacja}
            Struktura: Nagłówek, Podsumowanie, Doświadczenie, Edukacja, Umiejętności.
            Użyj Markdown (## nagłówki, - listy). Tylko treść CV, bez komentarzy.
            """

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            cv_tekst = response.choices[0].message.content

            # === LEPSZY PDF – 3 wyraźne szablony ===
            buffer = io.BytesIO()
            c = canvas.Canvas(buffer, pagesize=A4)
            width, height = A4
            margin = 50
            y = height - margin

            # Zdjęcie (opcjonalnie na górze prawo)
            if zdjecie:
                img = ImageReader(zdjecie)
                c.drawImage(img, width - margin - 120, height - margin - 140, width=120, height=140, preserveAspectRatio=True)

            # Szablon zależny od stylu
            if styl == "Nowoczesny":
                # Niebieski pasek boczny
                c.setFillColorRGB(0.1, 0.4, 0.8)
                c.rect(0, 0, 150, height, fill=1)
                c.setFillColorRGB(1, 1, 1)
                c.setFont("DejaVuSans", 24)
                c.drawString(margin - 30, height - 80, imie.upper())
                c.setFont("DejaVuSans", 12)
                c.drawString(margin - 30, height - 120, f"{email} | {telefon}")
                text_x = 170
            elif styl == "Klasyczny":
                # Klasyczny centrowany
                c.setStrokeColorRGB(0, 0, 0)
                c.line(margin, height - 100, width - margin, height - 100)
                c.setFont("DejaVuSans", 20)
                c.drawCentredString(width / 2, height - 80, imie.upper())
                c.setFont("DejaVuSans", 12)
                c.drawCentredString(width / 2, height - 120, f"{email} | {telefon}")
                text_x = margin
            else:  # Kreatywny
                # Kolorowy header na górze
                c.setFillColorRGB(1, 0.3, 0.3)
                c.rect(0, height - 150, width, 150, fill=1)
                c.setFillColorRGB(1, 1, 1)
                c.setFont("DejaVuSans", 28)
                c.drawCentredString(width / 2, height - 100, imie.upper())
                text_x = margin

            # Treść CV
            c.setFillColorRGB(0, 0, 0)
            c.setFont("DejaVuSans", 11)
            y = height - 200 if styl == "Kreatywny" else height - 160
            for line in cv_tekst.split("\n"):
                line = line.strip()
                if line.startswith("## "):
                    c.setFont("DejaVuSans", 14)
                    c.drawString(text_x, y, line[3:].upper())
                    y -= 30
                    c.setFont("DejaVuSans", 11)
                elif line.startswith("- "):
                    c.drawString(text_x + 20, y, "• " + line[2:])
                    y -= 20
                elif line:
                    c.drawString(text_x, y, line)
                    y -= 20
                if y < margin:
                    c.showPage()
                    y = height - margin

            c.save()
            buffer.seek(0)

            st.download_button(
                "Pobierz gotowe CV PDF",
                buffer,
                file_name=f"CV_{imie.replace(' ', '_')}_{styl}.pdf",
                mime="application/pdf"
            )

            st.success("CV gotowe!")


