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
DEFAULT_PASSWORD = os.getenv("VISIT_APP_PASS", "familia2025")  # senha para marcações
ADMIN_PASSWORD = os.getenv("VISIT_APP_ADMIN_PASS", "gestao2025")  # senha para gestão
SLOT_STEP_MIN = 30  # granularidade base de slots (minutos)
DEFAULT_DURATION = 30  # duração sugerida (minutos)
PARTY_CAPACITY = 2  # capacidade total por slot/intervalo

# Janelas de visita
WEEKDAY_WINDOWS = [(time(16, 30), time(21, 00))]
WEEKEND_WINDOWS = []

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


# -------- Bookings --------

def fetch_day_bookings(sb: Client, d: date) -> list[dict]:
    res = sb.table("bookings").select(
        "id, visit_date, start_time, end_time, visitor_name, phone, party_size, created_at"
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
    try:
        sb.table("bookings").insert(payload).execute()
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "conflict" in msg:
            raise ValueError("Horário acabou de ficar ocupado.") from e
        raise


def delete_booking(sb: Client, booking_id: int):
    sb.table("bookings").delete().eq("id", booking_id).execute()


# -------- Duty contacts (contacto do dia) --------

def fetch_duty_for_date(sb: Client, d: date) -> dict:
    try:
        res = sb.table("duty_contacts").select(
            "id, duty_date, period, contact_name, contact_phone, updated_at"
        ).eq("duty_date", d.isoformat()).execute()
        data = res.data or []
    except Exception:
        # Se a tabela não existir ou houver política a bloquear, devolve vazio (UI mostra mensagem por defeito)
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
    try:
        sb.table("duty_contacts").upsert(payload, on_conflict="duty_date,period").execute()
    except Exception:
        st.error("Não foi possível guardar o contacto do dia. Verifica se a tabela 'duty_contacts' existe e as permissões no Supabase.")
        raise


# ================================
# Lógica de horários e capacidade
# ================================

def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun


def allowed_windows_for_date(d: date, holidays: set[date]) -> list[tuple[time, time]]:
    # Novas regras: sem marcações ao fim de semana e feriados
    if is_weekend(d) or d in holidays:
        return []
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


def capacity_remaining(existing: list[dict], new_start: time, new_end: time) -> int:
    used = 0
    for b in existing:
        s = b["start_time"] if isinstance(b["start_time"], time) else _parse_time(b["start_time"])
        e = b["end_time"] if isinstance(b["end_time"], time) else _parse_time(b["end_time"])
        if overlaps(new_start, new_end, s, e):
            used += int(b.get("party_size", 1))
    rem = max(0, PARTY_CAPACITY - used)
    return rem


def capacity_label(rem: int) -> str:
    if rem <= 0:
        return "cheio"
    elif rem == 1:
        return "1 lugar restante"
    else:
        return f"{rem} lugares restantes"


def morning_applicable(d: date, holidays: set[date]) -> bool:
    return is_weekend(d) or (d in holidays)


# ================================
# UI / App
# ================================

def require_password():
    if "auth" not in st.session_state:
        st.session_state.auth = False
    if st.session_state.auth:
        return True

    st.markdown(
        """
    ### Acesso restrito
    Introduz a **senha** partilhada com família e amigos para marcar visitas.
    """
    )
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
        st.info("Neste dia não há janelas de visita disponíveis (sem marcações aos fins de semana e feriados).")
        return

    # Info de contacto do dia
    duty = fetch_duty_for_date(sb, sel_date)
    is_weekend_or_holiday = morning_applicable(sel_date, holidays)
    if is_weekend_or_holiday:
        morning_txt = (
            f"**Manhã**: {duty['morning']['name']} — {duty['morning']['phone']}"
            if duty.get("morning") and duty["morning"]
            else "**Manhã**: Verificar mais perto da data a pessoa a contactar"
        )
    else:
        morning_txt = ""
    afternoon_txt = (
        f"**Tarde**: {duty['afternoon']['name']} — {duty['afternoon']['phone']}"
        if duty.get("afternoon") and duty["afternoon"]
        else "**Tarde**: Verificar mais perto da data a pessoa a contactar"
    )
    with st.container(border=True):
        st.markdown("**Contacto do dia**")
        if morning_txt:
            st.markdown(morning_txt)
        st.markdown(afternoon_txt)

    day_bookings = fetch_day_bookings(sb, sel_date)

    with st.expander("Ver marcações deste dia", expanded=False):
        if day_bookings:
            for b in day_bookings:
                st.write(
                    f"• {b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')} | x{b['party_size']} — {b['visitor_name']} ({b['phone'] or 's/ contacto'})"
                )
        else:
            st.write("Sem marcações ainda.")

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        duration = st.selectbox(
            "Duração (min)", options=[15, 20, 30, 45, 60], index=[15, 20, 30, 45, 60].index(DEFAULT_DURATION)
        )
    with c2:
        party_size = st.selectbox("Nº de pessoas", options=[1, 2], index=0)
    with c3:
        pass

    # Construir lista de horas de início possíveis, respeitando duração e capacidade
    option_pairs: list[tuple[str, str]] = []  # (label, value)
    full_labels: list[str] = []
    insufficient_labels: list[str] = []
    label_to_time: dict[str, str] = {}
    psize = int(party_size)
    for w_start, w_end in windows:
        for s in generate_slots(w_start, w_end, SLOT_STEP_MIN):
            end_candidate = (datetime.combine(sel_date, s) + timedelta(minutes=duration)).time()
            if end_candidate <= w_end:
                rem = capacity_remaining(day_bookings, s, end_candidate)
                label = f"{s.strftime('%H:%M')} — {capacity_label(rem)}"
                if rem >= psize and rem > 0:
                    option_pairs.append((label, s.strftime('%H:%M')))
                elif rem == 0:
                    full_labels.append(label)
                else:
                    insufficient_labels.append(label)

    if not option_pairs:
        st.warning("Não há horários livres para a duração e nº de pessoas escolhidos.")
        st.caption("Dica: experimente reduzir a duração ou o nº de pessoas, ou escolher outro período.")
        # Mostrar também slots cheios/insuficientes para contexto
        if insufficient_labels:
            st.caption("Indisponíveis para a seleção atual: " + ", ".join(insufficient_labels))
        if full_labels:
            st.caption("Cheios: " + ", ".join(full_labels))
        st.stop()

    start_labels = [lbl for (lbl, _) in option_pairs]
    for lbl, val in option_pairs:
        label_to_time[lbl] = val
    start_choice_label = st.selectbox("Hora de início", options=start_labels)
    # Contexto adicional
    if insufficient_labels:
        st.caption(f"Indisponíveis para {psize} pessoa(s): " + ", ".join(insufficient_labels))
    if full_labels:
        st.caption("Cheios: " + ", ".join(full_labels))

    start_choice = label_to_time[start_choice_label]
    visitor_name = st.text_input("Nome do visitante")
    phone = st.text_input("Contacto (opcional)")

    if st.button("Confirmar marcação", type="primary"):
        if not visitor_name.strip():
            st.error("Indica o teu nome, por favor.")
            return
        s = datetime.strptime(start_choice, "%H:%M").time()
        e = (datetime.combine(sel_date, s) + timedelta(minutes=duration)).time()
        # Double-check de capacidade (evitar corridas)
        latest = fetch_day_bookings(sb, sel_date)
        if capacity_remaining(latest, s, e) < int(party_size):
            st.error("Ups! Esse horário acabou de ficar cheio. Escolhe outro, por favor.")
            st.rerun()
        try:
            insert_booking(sb, sel_date, s, e, visitor_name, phone, int(party_size))
        except ValueError:
            st.error("Ups! Esse horário acabou de ficar ocupado. Escolhe outro, por favor.")
            st.rerun()
        st.success(
            f"Visita marcada para {sel_date.strftime('%d-%m-%Y')} das {s.strftime('%H:%M')} às {e.strftime('%H:%M')} (x{party_size})."
        )
        st.balloons()
        st.rerun()


def admin_panel():
    st.subheader("Gestão de marcações")
    if not st.checkbox("Mostrar painel de gestão"):
        return

    # Autenticação específica de gestão
    if "admin_auth" not in st.session_state:
        st.session_state.admin_auth = False

    if not st.session_state.admin_auth:
        pwd = st.text_input("Senha de gestão", type="password", key="admin_pwd")
        colg1, colg2 = st.columns([1, 3])
        with colg1:
            if st.button("Entrar (gestão)"):
                if pwd == ADMIN_PASSWORD:
                    st.session_state.admin_auth = True
                    st.success("Acesso de gestão concedido.")
                    st.rerun()
                else:
                    st.error("Senha de gestão incorreta.")
        return

    # Botão para terminar sessão de gestão
    if st.button("Terminar sessão de gestão"):
        st.session_state.admin_auth = False
        st.info("Sessão de gestão terminada.")
        st.rerun()

    sb = get_supabase()
    today = datetime.now(TIMEZONE).date()
    years = [today.year - 1, today.year, today.year + 1]
    holidays = get_holidays(years)

    sel_date = st.date_input("Escolher dia", value=today, key="admin_date")

    # Gestão de contacto do dia
    st.markdown("### Contacto do dia")
    duty = fetch_duty_for_date(sb, sel_date)
    is_weekend_or_holiday = morning_applicable(sel_date, holidays)

    colm, cola = st.columns(2)
    with colm:
        st.markdown("**Manhã**")
        if is_weekend_or_holiday:
            m_name = st.text_input("Nome (manhã)", value=(duty.get("morning") or {}).get("name", ""), key="m_name")
            m_phone = st.text_input("Contacto (manhã)", value=(duty.get("morning") or {}).get("phone", ""), key="m_phone")
            if st.button("Guardar manhã"):
                upsert_duty(sb, sel_date, "morning", m_name, m_phone)
                st.success("Contacto da manhã guardado.")
                st.rerun()
        else:
            st.caption("(Não aplicável em dias úteis)")
    with cola:
        st.markdown("**Tarde**")
        a_name = st.text_input("Nome (tarde)", value=(duty.get("afternoon") or {}).get("name", ""), key="a_name")
        a_phone = st.text_input("Contacto (tarde)", value=(duty.get("afternoon") or {}).get("phone", ""), key="a_phone")
        if st.button("Guardar tarde"):
            upsert_duty(sb, sel_date, "afternoon", a_name, a_phone)
            st.success("Contacto da tarde guardado.")
            st.rerun()

    st.divider()

    # Lista e gestão de marcações
    rows = fetch_day_bookings(sb, sel_date)

    if not rows:
        st.info("Sem marcações neste dia.")
        return

    for b in rows:
        cols = st.columns([3, 3, 3, 3, 2])
        with cols[0]:
            st.write(f"{b['start_time'].strftime('%H:%M')}–{b['end_time'].strftime('%H:%M')}")
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


# --- Localização ---
LOCATION_TITLE = "Localização"
LOCATION_TEXT = "Centro de Reabilitação do Norte \n Area de TCE - 2o piso - Sul - Cama 279"
# Opcional: define VISIT_LOCATION_MAPS nos Secrets para mostrar link/botão do Google Maps
LOCATION_MAPS_URL = os.getenv("VISIT_LOCATION_MAPS", "")


# ================================
# Main
# ================================

def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="🗓️", layout="centered")
    st.title("🗓️ Agendamento de Visitas ao David")
    st.caption("Acesso restrito por senha partilhada entre família e amigos.")

    if not require_password():
        return

    # Bloco de localização (após login)
    with st.container(border=True):
    st.markdown(f"**{LOCATION_TITLE}**")
    st.markdown(LOCATION_TEXT.replace("    ", "    "))
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
