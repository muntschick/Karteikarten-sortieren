import csv
import io
import json
import math
import random
import tempfile
import uuid
import hmac
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
import streamlit.components.v1 as components


LEVELS = ["Pre-A1", "A1", "A2", "B1", "B2"]
UNASSIGNED = "Noch nicht zugeordnet"

# Diese Variablen werden nach dem Login aus Supabase befüllt.
# An den Browser werden für die Teilnehmeransicht nur card_id + card_text gegeben.
CARDS = {}
CARD_METADATA = {}
LABEL_TO_ID = {}
ID_TO_LABEL = {}


# -------------------------------------------------------------------
# Board / Daten
# -------------------------------------------------------------------

def get_random_card_order():
    """
    Mischt die Karten pro Browser-Sitzung genau einmal.
    Die Reihenfolge bleibt danach beim Sortieren und bei Reruns stabil.
    """
    current_ids = set(CARDS)
    stored_order = st.session_state.get("card_order")

    if (
        not stored_order
        or set(stored_order) != current_ids
        or len(stored_order) != len(CARDS)
    ):
        card_ids = list(CARDS.keys())
        random.SystemRandom().shuffle(card_ids)
        st.session_state.card_order = card_ids

    return st.session_state.card_order


def initial_board():
    card_order = get_random_card_order()

    return [
        {
            "header": UNASSIGNED,
            "items": [
                ID_TO_LABEL[card_id]
                for card_id in card_order
            ],
        },
        *[
            {
                "header": level,
                "items": [],
            }
            for level in LEVELS
        ],
    ]


def board_to_assignments(board):
    assignments = {}

    for container in board:
        level = container["header"]

        if level == UNASSIGNED:
            continue

        for label in container["items"]:
            card_id = LABEL_TO_ID.get(label)

            if card_id:
                assignments[card_id] = level

    return assignments


def validate_board(board):
    expected_headers = [UNASSIGNED, *LEVELS]

    if not isinstance(board, list):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    if len(board) != len(expected_headers):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    headers = []
    all_items = []
    unassigned_count = 0

    try:
        for container in board:
            header = container["header"]
            items = list(container["items"])

            headers.append(header)
            all_items.extend(items)

            if header == UNASSIGNED:
                unassigned_count = len(items)

    except (KeyError, TypeError):
        return {
            "unassigned_count": len(CARDS),
            "duplicate_count": 0,
            "missing_count": len(CARDS),
            "unknown_count": 0,
            "is_valid": False,
        }

    expected_items = set(LABEL_TO_ID)
    actual_items = set(all_items)

    duplicate_count = len(all_items) - len(actual_items)
    missing = expected_items - actual_items
    unknown = actual_items - expected_items

    structure_ok = (
        headers == expected_headers
        and len(all_items) == len(CARDS)
        and duplicate_count == 0
        and not missing
        and not unknown
    )

    return {
        "unassigned_count": unassigned_count,
        "duplicate_count": duplicate_count,
        "missing_count": len(missing),
        "unknown_count": len(unknown),
        "is_valid": structure_ok and unassigned_count == 0,
    }


def is_safe_board_payload(board):
    """
    Prüft, ob der Browser einen plausiblen Board-Zustand zurückgegeben hat.
    """
    if not isinstance(board, list):
        return False

    if len(board) != 1 + len(LEVELS):
        return False

    expected_headers = [UNASSIGNED, *LEVELS]

    try:
        headers = [
            container["header"]
            for container in board
        ]

        all_items = [
            item
            for container in board
            for item in container["items"]
        ]

    except (KeyError, TypeError):
        return False

    return (
        headers == expected_headers
        and len(all_items) == len(CARDS)
        and len(set(all_items)) == len(CARDS)
        and set(all_items) == set(LABEL_TO_ID)
    )


# -------------------------------------------------------------------
# Datenbank: Supabase / PostgreSQL
# -------------------------------------------------------------------

def get_connection():
    return psycopg.connect(
        str(st.secrets["DATABASE_URL"]),
        sslmode="require",
        connect_timeout=10,
    )


@st.cache_resource
def init_db():
    """
    Prüft die Grundstruktur und migriert bei Bedarf
    assignments.category -> assignments.level.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.submissions (
                    submission_id UUID PRIMARY KEY,
                    participant_id TEXT NOT NULL UNIQUE,
                    submitted_at_utc TIMESTAMPTZ NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.assignments (
                    submission_id UUID NOT NULL
                        REFERENCES public.submissions(submission_id)
                        ON DELETE CASCADE,
                    card_id TEXT NOT NULL,
                    level TEXT NOT NULL
                        CHECK (level IN ('Pre-A1', 'A1', 'A2', 'B1', 'B2')),
                    PRIMARY KEY (submission_id, card_id)
                )
                """
            )

            # Bestehende Installation automatisch von "category" auf "level" migrieren.
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'assignments'
                  AND column_name IN ('category', 'level')
                """
            )
            assignment_columns = {
                row[0]
                for row in cur.fetchall()
            }

            if (
                "category" in assignment_columns
                and "level" not in assignment_columns
            ):
                cur.execute(
                    """
                    ALTER TABLE public.assignments
                    RENAME COLUMN category TO level
                    """
                )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.cards (
                    card_id TEXT PRIMARY KEY,
                    card_text TEXT NOT NULL,
                    reference_level TEXT NOT NULL
                        CHECK (reference_level IN ('Pre-A1', 'A1', 'A2', 'B1', 'B2')),
                    item_category TEXT NOT NULL,
                    category_number SMALLINT NOT NULL,
                    level_number SMALLINT NOT NULL,
                    sort_order SMALLINT NOT NULL UNIQUE
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE public.submissions
                ENABLE ROW LEVEL SECURITY
                """
            )

            cur.execute(
                """
                ALTER TABLE public.assignments
                ENABLE ROW LEVEL SECURITY
                """
            )

            cur.execute(
                """
                ALTER TABLE public.cards
                ENABLE ROW LEVEL SECURITY
                """
            )

    return True


@st.cache_data(ttl=300)
def load_cards():
    """
    Lädt die Stammdaten in Ihrer fachlichen sort_order.
    Für die Teilnehmeransicht werden später nur ID + Text verwendet.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    card_id,
                    card_text,
                    reference_level,
                    item_category,
                    category_number,
                    level_number,
                    sort_order
                FROM public.cards
                ORDER BY sort_order
                """
            )
            rows = cur.fetchall()

    cards = {}
    metadata = {}

    for (
        card_id,
        card_text,
        reference_level,
        item_category,
        category_number,
        level_number,
        sort_order,
    ) in rows:
        cards[card_id] = card_text
        metadata[card_id] = {
            "card_text": card_text,
            "reference_level": reference_level,
            "item_category": item_category,
            "category_number": int(category_number),
            "level_number": int(level_number),
            "sort_order": int(sort_order),
        }

    return cards, metadata


def save_submission(participant_id, assignments):
    """
    Speichert eine vollständige Abgabe in einer Transaktion.
    """
    submission_id = uuid.uuid4()
    submitted_at = datetime.now(timezone.utc)

    rows = [
        (
            submission_id,
            card_id,
            level,
        )
        for card_id, level
        in sorted(assignments.items())
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.submissions (
                    submission_id,
                    participant_id,
                    submitted_at_utc
                )
                VALUES (%s, %s, %s)
                """,
                (
                    submission_id,
                    participant_id,
                    submitted_at,
                ),
            )

            cur.executemany(
                """
                INSERT INTO public.assignments (
                    submission_id,
                    card_id,
                    level
                )
                VALUES (%s, %s, %s)
                """,
                rows,
            )

    return str(submission_id)


def load_results():
    """
    Lädt die Auswertungsdaten bereits mit den Karten-Stammdaten zusammengeführt.
    Nur Karten, die aktuell in public.cards vorhanden sind, werden ausgewertet.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.participant_id,
                    s.submitted_at_utc,
                    a.card_id,
                    a.level AS participant_level,
                    c.card_text,
                    c.reference_level,
                    c.item_category,
                    c.category_number,
                    c.level_number,
                    c.sort_order
                FROM public.submissions AS s
                JOIN public.assignments AS a
                    ON a.submission_id = s.submission_id
                JOIN public.cards AS c
                    ON c.card_id = a.card_id
                ORDER BY
                    s.submitted_at_utc,
                    s.participant_id,
                    c.sort_order
                """
            )
            return cur.fetchall()


def create_results_csv(rows):
    """
    Rohdatenexport inklusive Referenzniveau und Kartenkategorie.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "participant_id",
            "submitted_at_utc",
            "card_id",
            "participant_level",
            "card_text",
            "reference_level",
            "item_category",
            "category_number",
            "level_number",
            "sort_order",
        ]
    )

    for row in rows:
        row = list(row)
        if hasattr(row[1], "isoformat"):
            row[1] = row[1].isoformat()
        writer.writerow(row)

    return output.getvalue().encode("utf-8-sig")


def results_to_dataframe(rows):
    df = pd.DataFrame(
        rows,
        columns=[
            "participant_id",
            "submitted_at_utc",
            "card_id",
            "participant_level",
            "card_text",
            "reference_level",
            "item_category",
            "category_number",
            "level_number",
            "sort_order",
        ],
    )

    if not df.empty:
        df["submitted_at_utc"] = pd.to_datetime(
            df["submitted_at_utc"],
            utc=True,
        )

    return df


def build_submission_overview(raw_df):
    if raw_df.empty:
        return pd.DataFrame(
            columns=[
                "Teilnehmer-ID",
                "Zeitpunkt (UTC)",
                "Karten",
            ]
        )

    grouped = (
        raw_df
        .groupby(
            [
                "participant_id",
                "submitted_at_utc",
            ],
            as_index=False,
        )
        .agg(
            Karten=("card_id", "nunique")
        )
        .sort_values(
            "submitted_at_utc",
            ascending=False,
        )
    )

    grouped["Zeitpunkt (UTC)"] = (
        grouped["submitted_at_utc"]
        .dt.strftime("%d.%m.%Y %H:%M:%S UTC")
    )

    grouped = grouped.rename(
        columns={
            "participant_id": "Teilnehmer-ID",
        }
    )

    return grouped[
        [
            "Teilnehmer-ID",
            "Zeitpunkt (UTC)",
            "Karten",
        ]
    ]


def build_card_analysis(raw_df):
    """
    Berechnet pro Karte sowohl Interrater-Konsens als auch
    den Vergleich mit dem hinterlegten Referenzniveau.
    """
    result_rows = []
    level_positions = {
        level: index
        for index, level
        in enumerate(LEVELS)
    }

    ordered_card_ids = sorted(
        CARDS,
        key=lambda card_id: CARD_METADATA[card_id]["sort_order"],
    )

    for card_id in ordered_card_ids:
        meta = CARD_METADATA[card_id]
        card_rows = raw_df[
            raw_df["card_id"] == card_id
        ]

        counts_series = (
            card_rows["participant_level"]
            .value_counts()
            .reindex(
                LEVELS,
                fill_value=0,
            )
        )

        counts = {
            level: int(counts_series[level])
            for level in LEVELS
        }
        number_ratings = int(counts_series.sum())
        reference_level = meta["reference_level"]

        if number_ratings > 0:
            max_count = int(counts_series.max())
            modal_levels = [
                level
                for level in LEVELS
                if counts[level] == max_count
            ]
            majority = " / ".join(modal_levels)
            agreement = max_count / number_ratings * 100
            dissent = 100 - agreement

            used_positions = [
                level_positions[level]
                for level in LEVELS
                if counts[level] > 0
            ]
            spread = max(used_positions) - min(used_positions)

            probabilities = [
                counts[level] / number_ratings
                for level in LEVELS
                if counts[level] > 0
            ]
            entropy = (
                -sum(
                    p * math.log(p)
                    for p in probabilities
                )
                / math.log(len(LEVELS))
                * 100
            )

            reference_match = (
                counts[reference_level]
                / number_ratings
                * 100
            )

            reference_position = level_positions[reference_level]
            mean_abs_deviation = (
                sum(
                    counts[level]
                    * abs(level_positions[level] - reference_position)
                    for level in LEVELS
                )
                / number_ratings
            )

            majority_matches_reference = (
                reference_level in modal_levels
            )
        else:
            majority = "—"
            agreement = 0.0
            dissent = 0.0
            spread = 0
            entropy = 0.0
            reference_match = 0.0
            mean_abs_deviation = 0.0
            majority_matches_reference = False

        result_rows.append(
            {
                "Reihenfolge": meta["sort_order"],
                "Karte": card_id,
                "Text": meta["card_text"],
                "Kategorie": meta["item_category"],
                "Kategorie-Nr": meta["category_number"],
                "Referenzniveau": reference_level,
                "Level-Nr": meta["level_number"],
                "Bewertungen": number_ratings,
                "Pre-A1": counts["Pre-A1"],
                "A1": counts["A1"],
                "A2": counts["A2"],
                "B1": counts["B1"],
                "B2": counts["B2"],
                "Mehrheit": majority,
                "Mehrheit = Referenz": (
                    "Ja" if majority_matches_reference else "Nein"
                ),
                "Übereinstimmung %": round(agreement, 1),
                "Dissens %": round(dissent, 1),
                "Referenz-Treffer %": round(reference_match, 1),
                "Ø Abweichung zur Referenz": round(mean_abs_deviation, 2),
                "Spannweite": spread,
                "Entropie %": round(entropy, 1),
            }
        )

    return pd.DataFrame(result_rows)


def full_table_height(df):
    """Genug Höhe, damit nur die Browserseite vertikal scrollt."""
    return 48 + len(df) * 35


def build_group_summary(card_analysis_df, group_column, order_column=None):
    if card_analysis_df.empty:
        return pd.DataFrame()

    grouped = (
        card_analysis_df
        .groupby(group_column, as_index=False)
        .agg(
            Karten=("Karte", "count"),
            **{
                "Ø Übereinstimmung %": ("Übereinstimmung %", "mean"),
                "Ø Referenz-Treffer %": ("Referenz-Treffer %", "mean"),
                "Ø Abweichung": ("Ø Abweichung zur Referenz", "mean"),
            },
        )
    )

    if order_column is not None:
        order_map = (
            card_analysis_df[[group_column, order_column]]
            .drop_duplicates()
            .set_index(group_column)[order_column]
            .to_dict()
        )
        grouped["__order"] = grouped[group_column].map(order_map)
        grouped = grouped.sort_values("__order").drop(columns="__order")

    for column in [
        "Ø Übereinstimmung %",
        "Ø Referenz-Treffer %",
        "Ø Abweichung",
    ]:
        grouped[column] = grouped[column].round(1 if "%" in column else 2)

    return grouped


def render_admin_dashboard(rows):
    raw_df = results_to_dataframe(rows)
    submissions_df = build_submission_overview(raw_df)
    card_analysis_df = build_card_analysis(raw_df)

    number_submissions = len(submissions_df)
    number_assignments = len(raw_df)
    perfect_consensus_count = int(
        (
            (card_analysis_df["Übereinstimmung %"] == 100.0)
            & (card_analysis_df["Bewertungen"] > 0)
        ).sum()
    )
    low_agreement_count = int(
        (
            (card_analysis_df["Übereinstimmung %"] < 50.0)
            & (card_analysis_df["Bewertungen"] > 0)
        ).sum()
    )

    if submissions_df.empty:
        last_submission = "—"
    else:
        last_submission = submissions_df.iloc[0]["Zeitpunkt (UTC)"]

    st.title("📊 Admin-Dashboard")

    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.caption(
            "Live-Auswertung aus Supabase/PostgreSQL. "
            "Die Teilnehmeransicht erhält die versteckten Stammdaten nicht."
        )
    with top_right:
        if st.button("← Zur Umfrage", use_container_width=True):
            st.session_state["admin_dashboard_open"] = False
            st.rerun()

    (
        tab_overview,
        tab_submissions,
        tab_cards,
        tab_consensus,
        tab_dissent,
    ) = st.tabs(
        [
            "📌 Überblick",
            "🧾 Abgaben",
            "🗂️ Kartenanalyse",
            "✅ TOP Konsens",
            "⚠️ Problemfälle",
        ]
    )

    # 1. Überblick
    with tab_overview:
        metric_1, metric_2, metric_3, metric_4 = st.columns(4)
        metric_1.metric("Abgaben", number_submissions)
        metric_2.metric("Zuordnungen", number_assignments)
        metric_3.metric("100 % Konsens", perfect_consensus_count)
        metric_4.metric("< 50 % Übereinstimmung", low_agreement_count)

        st.write(f"**Letzte Abgabe:** {last_submission}")
        st.write(f"**Aktive Karten in der Erhebung:** {len(CARDS)}")

        st.divider()
        st.subheader("Auswertung nach Kategorie und Referenzniveau")

        category_summary = build_group_summary(
            card_analysis_df,
            "Kategorie",
            "Kategorie-Nr",
        )
        level_summary = build_group_summary(
            card_analysis_df,
            "Referenzniveau",
            "Level-Nr",
        )

        summary_left, summary_right = st.columns(2)
        with summary_left:
            st.markdown("**Nach inhaltlicher Kategorie**")
            st.dataframe(
                category_summary,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(category_summary),
            )
        with summary_right:
            st.markdown("**Nach Referenzniveau**")
            st.dataframe(
                level_summary,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(level_summary),
            )

        st.divider()
        st.subheader("Downloads")

        raw_csv = create_results_csv(rows)
        analysis_csv = (
            card_analysis_df
            .to_csv(index=False)
            .encode("utf-8-sig")
        )

        download_1, download_2 = st.columns(2)
        with download_1:
            st.download_button(
                "📥 Rohdaten als CSV",
                data=raw_csv,
                file_name="ergebnisse_rohdaten.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with download_2:
            st.download_button(
                "📥 Kartenanalyse als CSV",
                data=analysis_csv,
                file_name="kartenanalyse.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.caption(
            "Übereinstimmung = Anteil der häufigsten Einstufung. "
            "Referenz-Treffer = Anteil der Teilnehmenden, die genau das "
            "hinterlegte Referenzniveau wählen. Ø Abweichung misst den "
            "mittleren ordinalen Abstand zum Referenzniveau (0 = identisch, "
            "1 = eine Stufe, usw.)."
        )

    # 2. Abgaben
    with tab_submissions:
        st.subheader("Alle Abgaben")
        if submissions_df.empty:
            st.info("Noch keine Abgaben vorhanden.")
        else:
            st.dataframe(
                submissions_df,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(submissions_df),
            )

    # 3. Kartenanalyse
    with tab_cards:
        st.subheader("Alle Karteikarten")
        st.caption(
            "Die Tabelle ist in Ihrer hinterlegten sort_order sortiert. "
            "Diese Reihenfolge wird den Teilnehmenden nicht gezeigt."
        )
        st.dataframe(
            card_analysis_df,
            hide_index=True,
            use_container_width=True,
            height=full_table_height(card_analysis_df),
            column_config={
                "Übereinstimmung %": st.column_config.ProgressColumn(
                    "Übereinstimmung %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                ),
                "Referenz-Treffer %": st.column_config.ProgressColumn(
                    "Referenz-Treffer %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                ),
                "Dissens %": st.column_config.NumberColumn(
                    "Dissens %",
                    format="%.1f %%",
                ),
                "Entropie %": st.column_config.NumberColumn(
                    "Entropie %",
                    format="%.1f %%",
                ),
            },
        )

    # 4. TOP 10 vollständiger Konsens
    with tab_consensus:
        st.subheader("TOP 10 – vollständiger Konsens")
        st.write(
            "Hier erscheinen Karten, bei denen **100 % der Teilnehmenden "
            "dieselbe Einstufung gewählt haben**. Zusätzlich sehen Sie, "
            "ob dieser Konsens dem Referenzniveau entspricht."
        )

        perfect_df = (
            card_analysis_df[
                (card_analysis_df["Übereinstimmung %"] == 100.0)
                & (card_analysis_df["Bewertungen"] > 0)
            ]
            .sort_values(
                ["Bewertungen", "Reihenfolge"],
                ascending=[False, True],
            )
            .head(10)
        )

        if perfect_df.empty:
            st.info("Aktuell gibt es keine Karte mit 100 % Übereinstimmung.")
        else:
            perfect_view = perfect_df[
                [
                    "Karte",
                    "Text",
                    "Kategorie",
                    "Referenzniveau",
                    "Bewertungen",
                    "Mehrheit",
                    "Mehrheit = Referenz",
                    "Übereinstimmung %",
                ]
            ]
            st.dataframe(
                perfect_view,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(perfect_view),
            )

    # 5. Problemfälle
    with tab_dissent:
        st.subheader("Besonders problematische Karten")
        st.write(
            "Sortierung: zuerst geringe Übereinstimmung, bei Gleichstand "
            "größere Spannweite und danach höhere Entropie."
        )

        problematic_df = (
            card_analysis_df[
                card_analysis_df["Bewertungen"] > 0
            ]
            .sort_values(
                [
                    "Übereinstimmung %",
                    "Spannweite",
                    "Entropie %",
                ],
                ascending=[True, False, False],
            )
            .head(10)
        )

        if problematic_df.empty:
            st.info("Noch keine bewerteten Karten vorhanden.")
        else:
            problem_view = problematic_df[
                [
                    "Karte",
                    "Text",
                    "Kategorie",
                    "Referenzniveau",
                    "Bewertungen",
                    "Mehrheit",
                    "Übereinstimmung %",
                    "Dissens %",
                    "Referenz-Treffer %",
                    "Ø Abweichung zur Referenz",
                    "Spannweite",
                    "Entropie %",
                ]
            ]
            st.dataframe(
                problem_view,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(problem_view),
                column_config={
                    "Übereinstimmung %": st.column_config.ProgressColumn(
                        "Übereinstimmung %",
                        min_value=0,
                        max_value=100,
                        format="%.1f %%",
                    ),
                    "Referenz-Treffer %": st.column_config.ProgressColumn(
                        "Referenz-Treffer %",
                        min_value=0,
                        max_value=100,
                        format="%.1f %%",
                    ),
                    "Dissens %": st.column_config.NumberColumn(
                        "Dissens %",
                        format="%.1f %%",
                    ),
                    "Entropie %": st.column_config.NumberColumn(
                        "Entropie %",
                        format="%.1f %%",
                    ),
                },
            )


def render_admin_area():
    with st.sidebar:
        with st.expander("🔒 Admin-Bereich", expanded=False):
            if not st.session_state.get("admin_granted", False):
                with st.form("admin_login_form"):
                    entered_admin_code = st.text_input(
                        "Admin-Code",
                        type="password",
                    )
                    login = st.form_submit_button("Admin öffnen")

                if login:
                    try:
                        correct_admin_code = str(st.secrets["ADMIN_CODE"])
                    except Exception:
                        st.error(
                            "ADMIN_CODE wurde noch nicht in den "
                            "Streamlit-Secrets konfiguriert."
                        )
                        return

                    if hmac.compare_digest(
                        entered_admin_code.strip(),
                        correct_admin_code,
                    ):
                        st.session_state["admin_granted"] = True
                        st.session_state["admin_dashboard_open"] = True
                        st.rerun()
                    else:
                        st.error("Admin-Code nicht korrekt.")

                return

            st.success("Admin angemeldet")

            if st.button("📊 Dashboard öffnen", use_container_width=True):
                st.session_state["admin_dashboard_open"] = True
                st.rerun()

            if st.button("Admin abmelden", use_container_width=True):
                st.session_state["admin_granted"] = False
                st.session_state["admin_dashboard_open"] = False
                st.rerun()

    if not st.session_state.get("admin_granted", False):
        return

    if not st.session_state.get("admin_dashboard_open", False):
        return

    try:
        rows = load_results()
    except psycopg.Error as exc:
        print("Fehler beim Laden der Admin-Daten:", repr(exc))
        st.error("Die Ergebnisse konnten nicht geladen werden.")
        st.stop()

    render_admin_dashboard(rows)
    st.stop()


# -------------------------------------------------------------------
# Eigene Drag&Drop-Komponente
# -------------------------------------------------------------------

COMPONENT_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>
    * {
        box-sizing: border-box;
    }

    html,
    body {
        margin: 0;
        padding: 0;

        width: 100%;

        background: transparent;

        font-family:
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            Roboto,
            Helvetica,
            Arial,
            sans-serif;
    }

    body {
        overflow: visible;
        padding: 2px 1px 8px 1px;
    }

    /*
    Sechs Spalten über die verfügbare Breite.

    Die erste Spalte erhält etwas mehr Raum,
    weil dort anfangs alle Karten liegen.
    */
    #board {
        display: grid;

        grid-template-columns:
            minmax(220px, 1.45fr)
            repeat(5, minmax(125px, 1fr));

        gap: 10px;

        width: 100%;

        align-items: start;
    }

    .column {
        min-width: 0;
        min-height: 110px;

        padding: 8px;

        border:
            1px solid
            rgba(128, 128, 128, 0.35);

        border-radius: 10px;

        background:
            rgba(128, 128, 128, 0.025);
    }

    .header {
        padding: 10px 6px;
        margin-bottom: 8px;

        border-radius: 7px;

        background:
            rgba(128, 128, 128, 0.16);

        font-weight: 700;
        text-align: center;

        line-height: 1.2rem;
    }

    /*
    Keine Scrollbox:
    Die Kategorie wächst mit ihrem Inhalt.
    */
    .dropzone {
        min-height: 70px;

        padding: 1px;

        border-radius: 7px;

        transition:
            background 0.10s ease,
            outline 0.10s ease;
    }

    .dropzone.drag-over {
        background:
            rgba(49, 130, 206, 0.06);

        outline:
            2px dashed
            rgba(49, 130, 206, 0.30);
    }

    /*
    Wichtig:
    Keine feste Kartenhöhe.
    Die Höhe ergibt sich ausschließlich
    aus der jeweiligen Textmenge.
    */
    .card {
        display: block;

        width: 100%;
        height: auto;
        min-height: 0;

        padding: 8px 10px;
        margin: 6px 0;

        border:
            1px solid
            rgba(128, 128, 128, 0.35);

        border-radius: 8px;

        background:
            rgba(128, 128, 128, 0.08);

        line-height: 1.3rem;

        white-space: normal;
        overflow-wrap: anywhere;
        word-break: normal;

        cursor: grab;

        user-select: none;

        transition:
            background 0.10s ease,
            opacity 0.10s ease,
            transform 0.08s ease;
    }

    .card:hover {
        background:
            rgba(128, 128, 128, 0.14);
    }

    .card:active {
        cursor: grabbing;
    }

    .card.dragging {
        opacity: 0.35;
    }

    /*
    Auf kleineren Bildschirmen darf die
    Komponente horizontal scrollen.
    Auf einem normalen Desktop werden alle
    sechs Spalten nebeneinander gezeigt.
    */
    @media (max-width: 1050px) {
        body {
            overflow-x: auto;
        }

        #board {
            min-width: 1000px;
        }
    }
</style>
</head>

<body>

<div id="board"></div>

<script>
    // -------------------------------------------------------------
    // Streamlit-Komponentenprotokoll
    // -------------------------------------------------------------

    function sendMessageToStreamlitClient(
        type,
        data = {}
    ) {
        const message = Object.assign(
            {
                isStreamlitMessage: true,
                type: type
            },
            data
        );

        window.parent.postMessage(
            message,
            "*"
        );
    }


    function componentReady() {
        sendMessageToStreamlitClient(
            "streamlit:componentReady",
            {
                apiVersion: 1
            }
        );
    }


    function setFrameHeight(height) {
        sendMessageToStreamlitClient(
            "streamlit:setFrameHeight",
            {
                height: height
            }
        );
    }


    function setComponentValue(value) {
        sendMessageToStreamlitClient(
            "streamlit:setComponentValue",
            {
                value: value
            }
        );
    }


    // -------------------------------------------------------------
    // Lokaler Zustand
    // -------------------------------------------------------------

    const boardElement =
        document.getElementById("board");

    let draggedCard = null;

    let originalParent = null;
    let originalNextSibling = null;

    let dropAccepted = false;

    let currentBoardJson = "";
    let lastReportedHeight = 0;


    // -------------------------------------------------------------
    // Iframe-Höhe an den tatsächlichen Inhalt anpassen
    // -------------------------------------------------------------

    function updateFrameHeight() {
    window.requestAnimationFrame(
        () => {
            /*
            Wichtig:
            Nicht document.scrollHeight messen,
            sondern nur die tatsächliche Höhe
            des Karten-Boards.

            Dadurch kann das Iframe nicht nur
            größer, sondern auch wieder kleiner
            werden.
            */
            const boardHeight =
                boardElement.getBoundingClientRect().height;

            const bodyStyle =
                window.getComputedStyle(
                    document.body
                );

            const paddingTop =
                parseFloat(
                    bodyStyle.paddingTop
                ) || 0;

            const paddingBottom =
                parseFloat(
                    bodyStyle.paddingBottom
                ) || 0;

            const height =
                Math.ceil(
                    boardHeight
                    + paddingTop
                    + paddingBottom
                    + 4
                );

            if (
                Math.abs(
                    height - lastReportedHeight
                ) > 1
            ) {
                lastReportedHeight =
                    height;

                setFrameHeight(
                    height
                );
            }
        }
    );
}


    // -------------------------------------------------------------
    // Aktuellen Board-Zustand aus dem DOM lesen
    // -------------------------------------------------------------

    function readBoard() {
        const result = [];

        document
            .querySelectorAll(".column")
            .forEach(
                column => {
                    const items = Array
                        .from(
                            column.querySelectorAll(
                                ".card"
                            )
                        )
                        .map(
                            card =>
                                card.dataset.value
                        );

                    result.push(
                        {
                            header:
                                column.dataset.header,

                            items:
                                items
                        }
                    );
                }
            );

        return result;
    }


    // -------------------------------------------------------------
    // Board zeichnen
    // -------------------------------------------------------------

    function renderBoard(board) {
        boardElement.innerHTML = "";

        board.forEach(
            container => {
                const column =
                    document.createElement(
                        "div"
                    );

                column.className = "column";

                column.dataset.header =
                    container.header;


                const header =
                    document.createElement(
                        "div"
                    );

                header.className = "header";

                header.textContent =
                    container.header;


                const zone =
                    document.createElement(
                        "div"
                    );

                zone.className = "dropzone";

                container.items.forEach(
                    value => {
                        const card =
                            document.createElement(
                                "div"
                            );

                        card.className =
                            "card";

                        card.draggable =
                            true;

                        card.dataset.value =
                            value;

                        /*
                        textContent ist absichtlich
                        kein innerHTML.
                        */
                        card.textContent =
                            value;

                        zone.appendChild(
                            card
                        );
                    }
                );

                column.appendChild(
                    header
                );

                column.appendChild(
                    zone
                );

                boardElement.appendChild(
                    column
                );
            }
        );

        currentBoardJson =
            JSON.stringify(board);

        updateFrameHeight();
    }


    // -------------------------------------------------------------
    // Position bestimmen, an der eine Karte eingefügt wird
    // -------------------------------------------------------------

    function getCardAfterPointer(
        zone,
        mouseY
    ) {
        const cards = [
            ...zone.querySelectorAll(
                ".card:not(.dragging)"
            )
        ];

        let closest = {
            offset:
                Number.NEGATIVE_INFINITY,

            element:
                null
        };

        cards.forEach(
            card => {
                const box =
                    card.getBoundingClientRect();

                const offset =
                    mouseY
                    - box.top
                    - box.height / 2;

                if (
                    offset < 0
                    && offset
                        > closest.offset
                ) {
                    closest = {
                        offset:
                            offset,

                        element:
                            card
                    };
                }
            }
        );

        return closest.element;
    }


    // -------------------------------------------------------------
    // Drag starten
    // -------------------------------------------------------------

    document.addEventListener(
        "dragstart",
        event => {
            const card =
                event.target.closest(
                    ".card"
                );

            if (!card) {
                return;
            }

            draggedCard =
                card;

            originalParent =
                card.parentElement;

            originalNextSibling =
                card.nextSibling;

            dropAccepted =
                false;

            event.dataTransfer.effectAllowed =
                "move";

            event.dataTransfer.setData(
                "text/plain",
                card.dataset.value
            );

            window.requestAnimationFrame(
                () => {
                    card.classList.add(
                        "dragging"
                    );
                }
            );
        }
    );


    // -------------------------------------------------------------
    // Karte während des Ziehens verschieben
    // -------------------------------------------------------------

    document.addEventListener(
        "dragover",
        event => {
            const zone =
                event.target.closest(
                    ".dropzone"
                );

            if (
                !zone
                || !draggedCard
            ) {
                return;
            }

            event.preventDefault();

            event.dataTransfer.dropEffect =
                "move";

            document
                .querySelectorAll(
                    ".dropzone"
                )
                .forEach(
                    z =>
                        z.classList.remove(
                            "drag-over"
                        )
                );

            zone.classList.add(
                "drag-over"
            );

            const afterElement =
                getCardAfterPointer(
                    zone,
                    event.clientY
                );

            if (
                afterElement === null
            ) {
                zone.appendChild(
                    draggedCard
                );
            } else {
                zone.insertBefore(
                    draggedCard,
                    afterElement
                );
            }
        }
    );


    // -------------------------------------------------------------
    // Karte erfolgreich ablegen
    // -------------------------------------------------------------

    document.addEventListener(
        "drop",
        event => {
            const zone =
                event.target.closest(
                    ".dropzone"
                );

            if (
                !zone
                || !draggedCard
            ) {
                return;
            }

            event.preventDefault();

            dropAccepted =
                true;
        }
    );


    // -------------------------------------------------------------
    // Drag abgeschlossen
    // -------------------------------------------------------------

    document.addEventListener(
        "dragend",
        () => {
            if (!draggedCard) {
                return;
            }

            draggedCard.classList.remove(
                "dragging"
            );

            document
                .querySelectorAll(
                    ".dropzone"
                )
                .forEach(
                    z =>
                        z.classList.remove(
                            "drag-over"
                        )
                );


            if (!dropAccepted) {
                /*
                Außerhalb einer Dropzone
                losgelassen:
                Karte an die alte Position
                zurücksetzen.
                */
                if (originalNextSibling) {
                    originalParent.insertBefore(
                        draggedCard,
                        originalNextSibling
                    );
                } else {
                    originalParent.appendChild(
                        draggedCard
                    );
                }
            } else {
                const newBoard =
                    readBoard();

                const newBoardJson =
                    JSON.stringify(
                        newBoard
                    );

                /*
                Nur dann an Python schicken,
                wenn sich tatsächlich etwas
                geändert hat.
                */
                if (
                    newBoardJson
                    !== currentBoardJson
                ) {
                    currentBoardJson =
                        newBoardJson;

                    setComponentValue(
                        newBoard
                    );
                }
            }


            draggedCard =
                null;

            originalParent =
                null;

            originalNextSibling =
                null;

            dropAccepted =
                false;


            updateFrameHeight();
        }
    );


    // -------------------------------------------------------------
    // Streamlit schickt Python-Argumente an die Komponente
    // -------------------------------------------------------------

    function onDataFromPython(event) {
        if (
            !event.data
            || event.data.type
                !== "streamlit:render"
        ) {
            return;
        }

        const args =
            event.data.args || {};

        const board =
            args.board || [];

        const incomingJson =
            JSON.stringify(
                board
            );

        /*
        Bei einem normalen Streamlit-Rerun
        wird nicht unnötig neu gerendert.
        */
        if (
            incomingJson
            !== currentBoardJson
        ) {
            renderBoard(
                board
            );
        } else {
            updateFrameHeight();
        }
    }


    window.addEventListener(
        "message",
        onDataFromPython
    );


    window.addEventListener(
        "resize",
        () => {
            updateFrameHeight();
        }
    );


    componentReady();
</script>

</body>
</html>
"""


def prepare_component():
    """
    Die Custom Component besteht aus einer einzigen HTML-Datei.
    Sie wird beim Start automatisch in das temporäre Verzeichnis
    geschrieben. Es ist daher keine zusätzliche Projektdatei nötig.
    """
    component_dir = (
        Path(tempfile.gettempdir())
        / "karteikarten_dragdrop_component_v1"
    )

    component_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    index_file = (
        component_dir
        / "index.html"
    )

    if (
        not index_file.exists()
        or index_file.read_text(
            encoding="utf-8"
        ) != COMPONENT_HTML
    ):
        index_file.write_text(
            COMPONENT_HTML,
            encoding="utf-8",
        )

    return component_dir


COMPONENT_DIR = prepare_component()

_card_sorter_component = (
    components.declare_component(
        "karteikarten_dragdrop_v1",
        path=str(COMPONENT_DIR),
    )
)


def card_sorter(board, key):
    return _card_sorter_component(
        board=board,
        key=key,
        default=board,
    )

# -------------------------------------------------------------------
# Streamlit-App
# -------------------------------------------------------------------

st.set_page_config(
    page_title="Karteikarten-Zuordnung",
    page_icon="🗂️",
    layout="wide",
)

# Möglichst viel Bildschirmbreite verwenden.
st.markdown(
    """
    <style>
        .block-container {
            max-width: 100% !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def require_access_code():
    if st.session_state.get("access_granted", False):
        return

    st.title("🔐 Zugang zur Umfrage")
    st.write(
        "Bitte geben Sie den Zugangscode ein, "
        "um zu beginnen."
    )

    with st.form("access_form"):
        entered_code = st.text_input(
            "Zugangscode",
            type="password",
        )
        submitted = st.form_submit_button(
            "Weiter",
            type="primary",
        )

    if submitted:
        try:
            correct_code = str(st.secrets["ACCESS_CODE"])
        except Exception:
            st.error("Es wurde noch kein Zugangscode konfiguriert.")
            st.stop()

        if hmac.compare_digest(
            entered_code.strip(),
            correct_code,
        ):
            st.session_state.access_granted = True
            st.rerun()
        else:
            st.error("Der Zugangscode ist nicht korrekt.")

    st.stop()


require_access_code()
init_db()

# Kartenstammdaten aus Supabase laden.
CARDS, CARD_METADATA = load_cards()

if not CARDS:
    st.error(
        "In public.cards wurden noch keine Karteikarten gefunden. "
        "Bitte zuerst die Stammdaten in Supabase importieren."
    )
    st.stop()

LABEL_TO_ID = {
    f"{card_id} · {text}": card_id
    for card_id, text in CARDS.items()
}

ID_TO_LABEL = {
    card_id: label
    for label, card_id in LABEL_TO_ID.items()
}

render_admin_area()

if "sorter_generation" not in st.session_state:
    st.session_state.sorter_generation = 0

# Bei einem Deployment mit geänderten Karten wird ein alter Browserzustand
# automatisch verworfen, damit keine alten Labels in der Erhebung bleiben.
if (
    "board" not in st.session_state
    or not is_safe_board_payload(st.session_state.board)
):
    st.session_state.pop("card_order", None)
    st.session_state.board = initial_board()
    st.session_state.sorter_generation += 1

if "submitted" not in st.session_state:
    st.session_state.submitted = False


def start_new_entry():
    # Neue Person am gleichen Browser -> neue zufällige Kartenreihenfolge.
    st.session_state.pop("card_order", None)
    st.session_state.board = initial_board()
    st.session_state.sorter_generation += 1
    st.session_state.submitted = False
    st.session_state.participant_id = ""


st.title("🗂️ Karteikarten-Zuordnung")
st.write(
    "Ordnen Sie die Deskriptoren dem Kompetenzniveau zu, "
    "in dem eine minimal kompetente Leserin bzw. ein minimal kompetenter Leser "
    "die beschriebene Leseleistung ohne Unterstützung zuverlässig erbringt. "
    f"Anzahl der Karten: **{len(CARDS)} Karten**, "
    "Kategorien: **Pre-A1, A1, A2, B1 oder B2**."
)

participant_id = st.text_input(
    "Teilnehmer-ID",
    key="participant_id",
    placeholder="z. B. P001",
    help=(
        "Bitte keine Namen eingeben, "
        "wenn eine pseudonyme ID ausreicht."
    ),
    disabled=st.session_state.submitted,
)

if st.session_state.submitted:
    st.success(
        "Vielen Dank! Ihre Zuordnung wurde gespeichert. "
        "Sie können die Seite jetzt schließen."
    )
    st.button(
        "Neue Eingabe starten",
        type="secondary",
        on_click=start_new_entry,
    )
    st.stop()

st.caption(
    "Ziehen Sie die Karten in die gewünschte Kategorie. "
    "Die Kartenreihenfolge wurde für diese Sitzung zufällig gemischt."
    "Die Nummerierung der Karten (K1-K48)"
    " entspricht nicht der tatsächlichen Sortierung."
)

component_result = card_sorter(
    st.session_state.board,
    key=(
        "card_sorter_"
        f"{st.session_state.sorter_generation}"
    ),
)

if component_result != st.session_state.board:
    if is_safe_board_payload(component_result):
        st.session_state.board = component_result
        st.rerun()

status = validate_board(st.session_state.board)
assigned = len(CARDS) - status["unassigned_count"]

st.progress(assigned / len(CARDS))
st.write(
    f"**{assigned} von {len(CARDS)} Karten zugeordnet.**"
)

if status["unassigned_count"] > 0:
    st.info(
        f"Noch {status['unassigned_count']} Karte(n) nicht zugeordnet."
    )
elif status["is_valid"]:
    st.success(
        f"Alle {len(CARDS)} Karten sind vollständig und eindeutig zugeordnet."
    )
else:
    st.error(
        "Die Kartenstruktur ist inkonsistent. "
        "Bitte setzen Sie die Zuordnung zurück."
    )

button_col, reset_col = st.columns([2, 1])

with button_col:
    submit = st.button(
        "Ergebnis absenden",
        type="primary",
        use_container_width=True,
        disabled=not status["is_valid"],
    )

with reset_col:
    reset = st.button(
        "Zuordnung zurücksetzen",
        use_container_width=True,
    )

if reset:
    # Beim Zurücksetzen derselben Person bleibt die zufällige Ausgangsreihenfolge gleich.
    st.session_state.board = initial_board()
    st.session_state.sorter_generation += 1
    st.rerun()

if submit:
    clean_participant_id = participant_id.strip()

    if not clean_participant_id:
        st.error("Bitte geben Sie zuerst eine Teilnehmer-ID ein.")
    elif len(clean_participant_id) > 100:
        st.error("Die Teilnehmer-ID darf höchstens 100 Zeichen lang sein.")
    else:
        assignments = board_to_assignments(st.session_state.board)

        if len(assignments) != len(CARDS):
            st.error(
                f"Es konnten nicht alle {len(CARDS)} Zuordnungen gelesen werden. "
                "Bitte prüfen Sie die Karten."
            )
        else:
            try:
                save_submission(
                    clean_participant_id,
                    assignments,
                )
            except psycopg.errors.UniqueViolation:
                st.error(
                    "Diese Teilnehmer-ID wurde bereits verwendet. "
                    "Bitte prüfen Sie die ID oder verwenden Sie eine andere."
                )
            except psycopg.Error as exc:
                print("PostgreSQL-Datenbankfehler:", repr(exc))
                st.error(
                    "Beim Speichern ist ein Datenbankfehler aufgetreten. "
                    "Bitte versuchen Sie es erneut."
                )
            else:
                st.session_state.submitted = True
                st.rerun()
