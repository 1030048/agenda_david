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
DEFAULT_PASSWORD = os.getenv("VISIT_APP_PASS", "familia2025")  # Altera no Streamlit Cloud: Secrets → VISIT_APP_PASS
SLOT_STEP_MIN = 30  # granularidade base de slots (minutos)
DEFAULT_DURATION = 30  # duração sugerida (minutos)

# Janelas de visita
WEEKDAY_WINDOWS = [(time(16, 30), time(19, 30))]
WEEKEND_WINDOWS = [(time(11, 30), time(14, 0)), (time(16, 30), time(19, 30))]

# ================================
# Utilidades de feriados (Portugal)
# ================================

def _easter_date(year: int) -> date:
    """Computa a data da Páscoa (algoritmo de Meeus/Jones/Butcher)."""
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

    fixed = {
        date(year, 1, 1),   # Ano Novo
        date(year, 4, 25),  # Liberdade
        date(year, 5, 1),   # Dia do Trabalhador
        date(year, 6, 10),  # Dia de Portugal
        date(year, 8, 15),  # Assunção
        date(year, 10, 5),  # Implantação da República
        date(year, 11, 1),  # Dia de Todos os Santos
        date(year, 12, 1),  # Restauração da Independência
        date(year, 12, 8),  # Imaculada Conceição
        date(year, 12, 25), # Natal
    }
    movable = {good_friday, corpus_christi}
    return fixed | movable


@st.cache_data(show_spinner=False)
def get_holidays(years: list[int]) -> set[date]:
    hs: set[date] = set()
    for y in years:
        hs |= portugal_national_holidays(y)
    return hs

# ================================
# Supabase (persistência)
# ================================

@st.cache_resource(show_spinner=False)
def get_supabase() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or st.secrets.get("SUPABASE_ANON_KEY")
    except Exception:
        st.error("⚠️ Configura os *Secrets*: SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY (ou ANON).")
        st.stop()
    if not url or not key:
        st.error("⚠️ SUPABASE_URL/SUPABASE_*_KEY em falta nos *Secrets*.")
        st.stop()
    return create_client(url, key)


def _parse_time(tval: str | time) -> time:
    if isinstance(tval, time):
        return tval
    # Aceita "HH:MM" ou "HH:MM:SS[.ffffff]"
    txt = str(tval)
    if len(txt) >= 8:
        txt = txt[:8]
    fmt = "%H:%M:%S" if len(txt) == 8 else "%H:%M"
    return datetime.strptime(txt, fmt).time()


def fetch_day_bookings(sb: Client, d: date) -> list[dict]:
    res = sb.table("bookings").select(
        "id, visit_date, start_time, end_time, visitor_name, phone, created_at"
    ).eq("visit_date", d.isoformat()).order("start_time").execute()
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
                "created_at": r.get("created_at"),
            }
        )
    return out


def insert_booking(sb: Client, d: date, start: time, end: time, name: str, phone: str | None):
    payload = {
        "visit_date": d.isoformat(),
        "start_time": start.strftime("%H:%M:%S"),
        "end_time": end.strftime("%H:%M:%S"),
        "visitor_name": name.strip(),
        "phone": (phone or "").strip(),
    }
    try:
        sb.table("bookings").insert(payload).execute()
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "conflict" in msg:
            raise ValueError("Horário acabou de ficar ocupado.") from e
        raise


def delete_booking(sb: Client, booking_id: int):
    sb.table("bookings").delete().eq("id", booking_id).execute()


# ================================
# Lógica de horários
# ================================

def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def allowed_windows_for_date(d: date, holidays: set[date]) -> list[tuple[time, time]]:
    if is_weekend(d) or d in holidays:
        return WEEKEND_WINDOWS
    return WEEKDAY_WINDOWS


def generate_slots(start_t: time, end_t: time, step_min: int = SLOT_STEP_MIN) -> list[time]:
    slots = []
    tdt = datetime.combine(date.today(), start_t)
    enddt = datetime.combine(date.today(), end_t)
    delta = timedelta(minutes=step_min)
    while tdt <= enddt - delta:
        slots.append(tdt.time())
        tdt += delta
    return slots


def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return (datetime.combine(date.today(), a_start) < datetime.combine(date.today(), b_end)) and (
        datetime.combine(date.today(), a_end) > datetime.combine(date.today(), b_start)
    )


def find_conflict(existing: list[dict], new_start: time, new_end: time) -> dict | None:
    for b in existing:
        s = _parse_time(b["start_time"]) if isinstance(b["start_time"], str) else b["start_time"]
        e = _parse_time(b["end_time"]) if isinstance(b["end_time"], str) else b["end_time"]
        if overlaps(new_start, new_end, s, e):
            return b
    return None


# ================================
# UI / App
# ================================

def require_password():
    if "auth" not in st.session_state:
        st.session_state.auth = False
    if st.session_state.auth:
        return True

    st.markdown("""
    ### Acesso restrito
    Introduz a **senha** partilhada com família e amigos para marcar visitas.
    """)
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
    years = [today.year - 1, today.year, today.year + 1]
    holidays = get_holidays(years)

    st.header("Reservar visita")

    sel_date = st.date_input("Data da visita", value=today, min_value=today)
    windows = allowed_windows_for_date(sel_date, holidays)

    if not windows:
        st.info("Neste dia não há janelas de visita disponíveis.")
        return

    day_bookings = fetch_day_bookings(sb, sel_date)

    with st.expander("Ver marcações deste dia", expanded=False):
        if day_bookings:
            for b in day_bookings:
                st.write(f"• {b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')}: {b['visitor_name']} ({b['phone'] or 's/ contacto'})")
        else:
            st.write("Sem marcações ainda.")

    col1, col2 = st.columns(2)
    with col1:
        duration = st.selectbox("Duração (min)", options=[15, 20, 30, 45, 60], index=[15,20,30,45,60].index(DEFAULT_DURATION))
    with col2:
        pass

    # Construir lista de horas de início possíveis, respeitando duração
    start_options: list[str] = []
    for w_start, w_end in windows:
        for s in generate_slots(w_start, w_end, SLOT_STEP_MIN):
            end_candidate = (datetime.combine(sel_date, s) + timedelta(minutes=duration)).time()
            if end_candidate <= w_end:
                start_options.append(s.strftime("%H:%M"))

    # Remover as que colidem com reservas existentes
    available_starts: list[str] = []
    for s_str in start_options:
        s = datetime.strptime(s_str, "%H:%M").time()
        e = (datetime.combine(sel_date, s) + timedelta(minutes=duration)).time()
        conflict = find_conflict(day_bookings, s, e)
        if not conflict:
            available_starts.append(s_str)

    if not available_starts:
        st.warning("Não há horários livres para a duração escolhida.")
        return

    start_choice = st.selectbox("Hora de início", options=available_starts)
    visitor_name = st.text_input("Nome do visitante")
    phone = st.text_input("Contacto (opcional)")

    if st.button("Confirmar marcação", type="primary"):
        if not visitor_name.strip():
            st.error("Indica o teu nome, por favor.")
            return
        s = datetime.strptime(start_choice, "%H:%M").time()
        e = (datetime.combine(sel_date, s) + timedelta(minutes=duration)).time()
        # Double-check de conflito (evitar corrida)
        latest = fetch_day_bookings(sb, sel_date)
        if find_conflict(latest, s, e):
            st.error("Ups! Esse horário acabou de ficar ocupado. Escolhe outro, por favor.")
            st.rerun()
        try:
            insert_booking(sb, sel_date, s, e, visitor_name, phone)
        except ValueError:
            st.error("Ups! Esse horário acabou de ficar ocupado. Escolhe outro, por favor.")
            st.rerun()
        st.success(f"Visita marcada para {sel_date.strftime('%d-%m-%Y')} das {s.strftime('%H:%M')} às {e.strftime('%H:%M')}.")
        st.balloons()
        st.rerun()


def admin_panel():
    st.subheader("Gestão de marcações (mesma senha)")
    if not st.checkbox("Mostrar painel de gestão"):
        return

    sb = get_supabase()
    today = datetime.now(TIMEZONE).date()
    sel_date = st.date_input("Escolher dia", value=today, key="admin_date")
    rows = fetch_day_bookings(sb, sel_date)

    if not rows:
        st.info("Sem marcações neste dia.")
        return

    for b in rows:
        cols = st.columns([3, 3, 4, 2])
        with cols[0]:
            st.write(f"{b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')}")
        with cols[1]:
            st.write(b["visitor_name"])
        with cols[2]:
            st.write(b["phone"] or "—")
        with cols[3]:
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

    booking_form()
    st.divider()
    admin_panel()

    st.divider()
    st.caption("Persistência via Supabase. Para configurar, define SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY nos Secrets.")


if __name__ == "__main__":
    main()
