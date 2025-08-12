import os
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

# ================================
# Configuração geral
# ================================
APP_TITLE = "Agendamento de Visitas"
TIMEZONE = ZoneInfo("Europe/Lisbon")
DEFAULT_PASSWORD = os.getenv("VISIT_APP_PASS", "familia2025")  # Altera no Streamlit Cloud: Secrets → VISIT_APP_PASS
DB_PATH = os.getenv("VISIT_APP_DB", "data/bookings.db")
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
    """Devolve conjunto de feriados nacionais (PT) para o ano dado.
    Nota: Carnaval não é feriado nacional obrigatório; incluímos apenas nacionais.
    """
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
# Base de dados
# ================================

def ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visit_date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            visitor_name TEXT NOT NULL,
            phone TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(visit_date)"
    )
    conn.commit()
    return conn


@st.cache_resource(show_spinner=False)
def get_conn():
    # Nota: no Streamlit Cloud podem existir múltiplas threads.
    # Usamos check_same_thread=False na conexão (ver ensure_db) e
    # garantimos que devolvemos a mesma ligação por processo.
    return ensure_db()


def fetch_day_bookings(conn, d: date) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT id, visit_date, start_time, end_time, visitor_name, phone, created_at FROM bookings WHERE visit_date = ? ORDER BY start_time",
        (d.isoformat(),),
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "visit_date": r[1] if isinstance(r[1], date) else date.fromisoformat(r[1]),
                "start_time": r[2] if isinstance(r[2], str) else r[2],
                "end_time": r[3] if isinstance(r[3], str) else r[3],
                "visitor_name": r[4],
                "phone": r[5],
                "created_at": r[6],
            }
        )
    return out


def insert_booking(conn, d: date, start: time, end: time, name: str, phone: str | None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bookings (visit_date, start_time, end_time, visitor_name, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            d.isoformat(),
            start.strftime("%H:%M"),
            end.strftime("%H:%M"),
            name.strip(),
            (phone or "").strip(),
            datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()


def delete_booking(conn, booking_id: int):
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    conn.commit()


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
        s = datetime.strptime(b["start_time"], "%H:%M").time() if isinstance(b["start_time"], str) else b["start_time"]
        e = datetime.strptime(b["end_time"], "%H:%M").time() if isinstance(b["end_time"], str) else b["end_time"]
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
    Introduz a **senha** partilhada com família/amigos para marcar visitas.
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
    conn = get_conn()

    today = datetime.now(TIMEZONE).date()
    years = [today.year - 1, today.year, today.year + 1]
    holidays = get_holidays(years)

    st.header("Reservar visita")

    sel_date = st.date_input("Data da visita", value=today, min_value=today)
    windows = allowed_windows_for_date(sel_date, holidays)

    if not windows:
        st.info("Neste dia não há janelas de visita disponíveis.")
        return

    day_bookings = fetch_day_bookings(conn, sel_date)

    with st.expander("Ver marcações deste dia", expanded=False):
        if day_bookings:
            for b in day_bookings:
                st.write(f"• {b['start_time']}–{b['end_time']}: {b['visitor_name']} ({b['phone'] or 's/ contacto'})")
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
        latest = fetch_day_bookings(conn, sel_date)
        if find_conflict(latest, s, e):
            st.error("Ups! Esse horário acabou de ficar ocupado. Escolhe outro, por favor.")
            st.rerun()
        insert_booking(conn, sel_date, s, e, visitor_name, phone)
        st.success(f"Visita marcada para {sel_date.strftime('%d-%m-%Y')} das {s.strftime('%H:%M')} às {e.strftime('%H:%M')}.")
        st.balloons()
        st.rerun()


def admin_panel():
    st.subheader("Gestão de marcações (mesma senha)")
    if not st.checkbox("Mostrar painel de gestão"):
        return

    conn = get_conn()
    today = datetime.now(TIMEZONE).date()
    sel_date = st.date_input("Escolher dia", value=today, key="admin_date")
    rows = fetch_day_bookings(conn, sel_date)

    if not rows:
        st.info("Sem marcações neste dia.")
        return

    for b in rows:
        cols = st.columns([3, 3, 4, 2])
        with cols[0]:
            st.write(f"{b['start_time']}–{b['end_time']}")
        with cols[1]:
            st.write(b["visitor_name"]) 
        with cols[2]:
            st.write(b["phone"] or "—")
        with cols[3]:
            if st.button("Apagar", key=f"del_{b['id']}"):
                delete_booking(conn, b["id"])
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
    st.caption("Dica: para alterar a senha, define o secret VISIT_APP_PASS. Para limpar dados, remove o ficheiro data/bookings.db.")


if __name__ == "__main__":
    main()
