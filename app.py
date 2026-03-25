import os
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from supabase import create_client, Client

# ================================
# Configuração geral
# ================================
APP_TITLE = "Agendamento de Visitas"
TIMEZONE = ZoneInfo("Europe/Lisbon")
DEFAULT_PASSWORD = os.getenv("VISIT_APP_PASS", "familia2025")
ADMIN_PASSWORD = os.getenv("VISIT_APP_ADMIN_PASS", "gestao2025")
PARTY_CAPACITY = 2

# Blocos fixos disponíveis todos os dias
VISIT_BLOCKS = [
    ("morning", "Manhã", time(11, 0), time(14, 30)),
    ("afternoon", "Tarde", time(16, 0), time(19, 30)),
]

# --- Localização ---
LOCATION_TITLE = "Localização"
LOCATION_TEXT = "Centro de Reabilitação do Norte\n Area de TCE - 2o piso - Sul - Cama 279"
LOCATION_MAPS_URL = os.getenv("VISIT_LOCATION_MAPS", "")

# ================================
# Utilidades de feriados (Portugal)
# ================================

def _easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = 1 + ((h + l - 7 * m + 114) % 31)
    return date(year, month, day)


def portugal_national_holidays(year: int) -> set[date]:
    easter = _easter_date(year)
    good_friday = easter - timedelta(days=2)
    corpus_christi = easter + timedelta(days=60)
    return {
        date(year, 1, 1),
        date(year, 4, 25),
        date(year, 5, 1),
        date(year, 6, 10),
        date(year, 8, 15),
        date(year, 10, 5),
        date(year, 11, 1),
        date(year, 12, 1),
        date(year, 12, 8),
        date(year, 12, 25),
        good_friday,
        corpus_christi,
    }


@st.cache_data(show_spinner=False)
def get_holidays(years: list[int]) -> set[date]:
    hs: set[date] = set()
    for y in years:
        hs |= portugal_national_holidays(y)
    return hs


# ================================
# Supabase
# ================================

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
    except Exception:
        st.error("Configura os Secrets: SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY (ou SUPABASE_ANON_KEY).")
        st.stop()

    if not url or not key:
        st.error("SUPABASE_URL ou SUPABASE key em falta nos Secrets.")
        st.stop()

    return create_client(url, key)


def _parse_time(tval: str | time) -> time:
    if isinstance(tval, time):
        return tval
    txt = str(tval)
    if len(txt) >= 8:
        txt = txt[:8]
    fmt = "%H:%M:%S" if len(txt) == 8 else "%H:%M"
    return datetime.strptime(txt, fmt).time()


# ================================
# Bookings
# ================================

def fetch_day_bookings(sb: Client, d: date) -> list[dict]:
    res = (
        sb.table("bookings")
        .select("id, visit_date, start_time, end_time, visitor_name, phone, party_size, created_at")
        .eq("visit_date", d.isoformat())
        .order("start_time")
        .execute()
    )
    rows = res.data or []
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "visit_date": date.fromisoformat(r["visit_date"]),
                "start_time": _parse_time(r["start_time"]),
                "end_time": _parse_time(r["end_time"]),
                "visitor_name": r["visitor_name"],
                "phone": r.get("phone"),
                "party_size": int(r.get("party_size", 1)),
                "created_at": r.get("created_at"),
            }
        )
    return out


def insert_booking(sb: Client, d: date, start: time, end: time, name: str, phone: str | None, party_size: int):
    payload = {
        "visit_date": d.isoformat(),
        "start_time": start.strftime("%H:%M:%S"),
        "end_time": end.strftime("%H:%M:%S"),
        "visitor_name": name.strip(),
        "phone": (phone or "").strip(),
        "party_size": int(party_size),
    }
    sb.table("bookings").insert(payload).execute()


def delete_booking(sb: Client, booking_id: int):
    sb.table("bookings").delete().eq("id", booking_id).execute()


# ================================
# Contacto do dia
# ================================

def fetch_duty_for_date(sb: Client, d: date) -> dict:
    try:
        res = (
            sb.table("duty_contacts")
            .select("id, duty_date, period, contact_name, contact_phone, updated_at")
            .eq("duty_date", d.isoformat())
            .execute()
        )
        data = res.data or []
    except Exception:
        data = []

    duty = {"morning": None, "afternoon": None}
    for r in data:
        duty[r["period"]] = {
            "id": r["id"],
            "name": r.get("contact_name") or "",
            "phone": r.get("contact_phone") or "",
        }
    return duty


def upsert_duty(sb: Client, d: date, period: str, name: str, phone: str):
    payload = {
        "duty_date": d.isoformat(),
        "period": period,
        "contact_name": name.strip(),
        "contact_phone": phone.strip(),
    }
    sb.table("duty_contacts").upsert(payload, on_conflict="duty_date,period").execute()


# ================================
# Regras de capacidade por bloco
# ================================

def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return (datetime.combine(date.today(), a_start) < datetime.combine(date.today(), b_end)) and (
        datetime.combine(date.today(), a_end) > datetime.combine(date.today(), b_start)
    )


def capacity_remaining(existing: list[dict], new_start: time, new_end: time) -> int:
    used = 0
    for b in existing:
        if overlaps(new_start, new_end, b["start_time"], b["end_time"]):
            used += int(b.get("party_size", 1))
    return max(0, PARTY_CAPACITY - used)


def capacity_label(rem: int) -> str:
    if rem <= 0:
        return "cheio"
    if rem == 1:
        return "1 lugar restante"
    return "2 lugares restantes"


# ================================
# UI
# ================================

def require_password() -> bool:
    if "auth" not in st.session_state:
        st.session_state.auth = False
    if st.session_state.auth:
        return True

    st.markdown("### Acesso restrito")
    st.write("Introduz a senha partilhada com família e amigos para marcar visitas.")
    pwd = st.text_input("Senha", type="password")
    if st.button("Entrar"):
        if pwd == DEFAULT_PASSWORD:
            st.session_state.auth = True
            st.success("Acesso concedido.")
            st.rerun()
        else:
            st.error("Senha incorreta.")
    return False


def booking_form():
    sb = get_supabase()
    today = datetime.now(TIMEZONE).date()

    st.header("Reservar visita")
    sel_date = st.date_input("Data da visita", value=today, min_value=today)

    duty = fetch_duty_for_date(sb, sel_date)
    with st.container(border=True):
        st.markdown("**Contacto do dia**")
        morning = duty.get("morning")
        afternoon = duty.get("afternoon")
        st.markdown(
            f"**Manhã**: {morning['name']} — {morning['phone']}"
            if morning else
            "**Manhã**: Verificar mais perto da data a pessoa a contactar"
        )
        st.markdown(
            f"**Tarde**: {afternoon['name']} — {afternoon['phone']}"
            if afternoon else
            "**Tarde**: Verificar mais perto da data a pessoa a contactar"
        )

    day_bookings = fetch_day_bookings(sb, sel_date)

    with st.expander("Ver marcações deste dia", expanded=False):
        if day_bookings:
            for b in day_bookings:
                period_label = "Manhã" if b["start_time"] == time(11, 0) else "Tarde"
                st.write(
                    f"• {period_label} ({b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')}) | x{b['party_size']} — {b['visitor_name']} ({b['phone'] or 's/ contacto'})"
                )
        else:
            st.write("Sem marcações ainda.")

    party_size = st.selectbox("Nº de pessoas", options=[1, 2], index=0)
    psize = int(party_size)

    option_labels: list[str] = []
    option_map: dict[str, tuple[str, time, time]] = {}
    full_labels: list[str] = []
    insufficient_labels: list[str] = []

    for period_key, period_label, start_t, end_t in VISIT_BLOCKS:
        rem = capacity_remaining(day_bookings, start_t, end_t)
        label = f"{period_label} ({start_t.strftime('%H:%M')}–{end_t.strftime('%H:%M')}) — {capacity_label(rem)}"
        if rem >= psize and rem > 0:
            option_labels.append(label)
            option_map[label] = (period_key, start_t, end_t)
        elif rem == 0:
            full_labels.append(label)
        else:
            insufficient_labels.append(label)

    if not option_labels:
        st.warning("Não há blocos disponíveis para o nº de pessoas escolhido.")
        if insufficient_labels:
            st.caption("Indisponíveis para a seleção atual: " + ", ".join(insufficient_labels))
        if full_labels:
            st.caption("Cheios: " + ", ".join(full_labels))
        return

    selected_label = st.selectbox("Bloco de visita", options=option_labels)
    if insufficient_labels:
        st.caption(f"Indisponíveis para {psize} pessoa(s): " + ", ".join(insufficient_labels))
    if full_labels:
        st.caption("Cheios: " + ", ".join(full_labels))

    _, start_choice, end_choice = option_map[selected_label]
    visitor_name = st.text_input("Nome do visitante")
    phone = st.text_input("Contacto (opcional)")

    if st.button("Confirmar marcação", type="primary"):
        if not visitor_name.strip():
            st.error("Indica o teu nome, por favor.")
            return

        latest = fetch_day_bookings(sb, sel_date)
        if capacity_remaining(latest, start_choice, end_choice) < psize:
            st.error("Ups! Esse bloco acabou de ficar cheio. Escolhe outro, por favor.")
            st.rerun()

        insert_booking(sb, sel_date, start_choice, end_choice, visitor_name, phone, psize)
        st.success(
            f"Visita marcada para {sel_date.strftime('%d-%m-%Y')} no bloco {start_choice.strftime('%H:%M')}–{end_choice.strftime('%H:%M')} (x{psize})."
        )
        st.balloons()
        st.rerun()


def admin_panel():
    st.subheader("Gestão de marcações")
    if not st.checkbox("Mostrar painel de gestão"):
        return

    if "admin_auth" not in st.session_state:
        st.session_state.admin_auth = False

    if not st.session_state.admin_auth:
        pwd = st.text_input("Senha de gestão", type="password", key="admin_pwd")
        if st.button("Entrar (gestão)"):
            if pwd == ADMIN_PASSWORD:
                st.session_state.admin_auth = True
                st.success("Acesso de gestão concedido.")
                st.rerun()
            else:
                st.error("Senha de gestão incorreta.")
        return

    if st.button("Terminar sessão de gestão"):
        st.session_state.admin_auth = False
        st.info("Sessão de gestão terminada.")
        st.rerun()

    sb = get_supabase()
    today = datetime.now(TIMEZONE).date()
    sel_date = st.date_input("Escolher dia", value=today, key="admin_date")

    st.markdown("### Contacto do dia")
    duty = fetch_duty_for_date(sb, sel_date)

    colm, cola = st.columns(2)
    with colm:
        st.markdown("**Manhã**")
        m_name = st.text_input("Nome (manhã)", value=(duty.get("morning") or {}).get("name", ""), key="m_name")
        m_phone = st.text_input("Contacto (manhã)", value=(duty.get("morning") or {}).get("phone", ""), key="m_phone")
        if st.button("Guardar manhã"):
            upsert_duty(sb, sel_date, "morning", m_name, m_phone)
            st.success("Contacto da manhã guardado.")
            st.rerun()

    with cola:
        st.markdown("**Tarde**")
        a_name = st.text_input("Nome (tarde)", value=(duty.get("afternoon") or {}).get("name", ""), key="a_name")
        a_phone = st.text_input("Contacto (tarde)", value=(duty.get("afternoon") or {}).get("phone", ""), key="a_phone")
        if st.button("Guardar tarde"):
            upsert_duty(sb, sel_date, "afternoon", a_name, a_phone)
            st.success("Contacto da tarde guardado.")
            st.rerun()

    st.divider()

    rows = fetch_day_bookings(sb, sel_date)
    if not rows:
        st.info("Sem marcações neste dia.")
        return

    for b in rows:
        cols = st.columns([3, 3, 3, 2, 2])
        with cols[0]:
            period_label = "Manhã" if b["start_time"] == time(11, 0) else "Tarde"
            st.write(f"{period_label} ({b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')})")
        with cols[1]:
            st.write(b["visitor_name"])
        with cols[2]:
            st.write(b["phone"] or "—")
        with cols[3]:
            st.write(f"x{b['party_size']}")
        with cols[4]:
            if st.button("Apagar", key=f"del_{b['id']}"):
                delete_booking(sb, b["id"])
                st.success("Reserva apagada.")
                st.rerun()


# ================================
# Main
# ================================

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="🗓️", layout="centered")
    st.title("🗓️ Agendamento de Visitas")
    st.caption("Acesso restrito por senha partilhada entre família e amigos.")

    if not require_password():
        return

    with st.container(border=True):
        st.markdown(f"**{LOCATION_TITLE}**")
        st.markdown(LOCATION_TEXT.replace("", " "))
        if LOCATION_MAPS_URL:
            st.markdown(f"[Ver no Google Maps]({LOCATION_MAPS_URL})")

    booking_form()
    st.divider()
    admin_panel()

    st.divider()
    st.caption(
        "Persistência via Supabase. Secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY. Senhas: VISIT_APP_PASS (marcações) e VISIT_APP_ADMIN_PASS (gestão)."
    )


if __name__ == "__main__":
    main()
