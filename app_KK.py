import csv
import io
import json
import math
import random
import tempfile
import uuid
import hmac
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
import streamlit.components.v1 as components


LEVELS = ["vor A1", "A1", "A2", "B1", "B2", "C1", "C2"]
# Historische Daten bleiben in Supabase und in Rohdatenexporten als "Pre-A1" erhalten.
# Nur bei numerischen Niveauvergleichen gilt die alte Bezeichnung als Stufe 0.
ANALYSIS_POSITIONS = {**{level: i for i, level in enumerate(LEVELS)}, "Pre-A1": 0}

UNASSIGNED = "Noch nicht zugeordnet"

# Die Fertigkeit wird erst nach der Zugangskontrolle ausgewählt.
# Der aktive Wert wird pro Streamlit-Sitzung gesetzt, nicht global in Secrets.
APP_PROFICIENCY = st.session_state.get("selected_proficiency")

# Diese Variablen werden nach dem Login aus Supabase befüllt.
# An den Browser werden für die Teilnehmer:innenansicht nur card_id + card_text gegeben.
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
    """Nur lesende Prüfung des Schemas; keine Änderungen an Supabase."""
    required = {
        "submissions": {"submission_id", "participant_id", "submitted_at_utc", "proficiency"},
        "assignments": {"submission_id", "card_id", "level", "confidence"},
        "cards": {"card_id", "card_text", "reference_level", "item_category",
                  "category_number", "level_number", "sort_order", "proficiency"},
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN ('submissions', 'assignments', 'cards')
                """
            )
            found = {}
            for table, column in cur.fetchall():
                found.setdefault(table, set()).add(column)
    missing = {
        table: sorted(columns - found.get(table, set()))
        for table, columns in required.items()
        if columns - found.get(table, set())
    }
    if missing:
        raise RuntimeError(
            "Datenbankschema unvollständig (bitte separat vorbereiten): "
            + str(missing)
        )
    return True


@st.cache_data(ttl=300)
def load_cards(proficiency):
    """
    Lädt ausschließlich Karten der ausgewählten Kompetenz.
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
                WHERE proficiency = %s
                ORDER BY sort_order
                """,
                (proficiency,)
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
            "category_number": (
                int(category_number)
                if category_number is not None
                else None
            ),
            "level_number": int(level_number),
            "sort_order": int(sort_order),
        }

    return cards, metadata


def save_submission(participant_id, assignments, confidence):
    """
    Speichert eine vollständige Abgabe
    einschließlich der Kompetenz.
    """
    submission_id = uuid.uuid4()
    submitted_at = datetime.now(timezone.utc)

    rows = [
        (
            submission_id,
            card_id,
            level,
            confidence.get(card_id),
        )
        for card_id, level
        in sorted(assignments.items())
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Sicherstellen, dass die Karten
            # zur aktuellen App gehören.
            card_ids = list(assignments.keys())

            cur.execute(
                """
                SELECT card_id
                FROM public.cards
                WHERE proficiency = %s
                """,
                (APP_PROFICIENCY,)
            )
            valid_ids = {
                row[0] for row in cur.fetchall()
            }

            if (
                set(card_ids) != valid_ids
                or len(card_ids) != len(valid_ids)
            ):
                raise ValueError(
                    "Die Karten stimmen nicht mit "
                    "der aktuellen Kompetenz überein."
                )

            cur.execute(
                """
                INSERT INTO public.submissions (
                    submission_id,
                    participant_id,
                    submitted_at_utc,
                    proficiency
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    submission_id,
                    participant_id,
                    submitted_at,
                    APP_PROFICIENCY,
                ),
            )

            cur.executemany(
                """
                INSERT INTO public.assignments (
                    submission_id,
                    card_id,
                    level,
                    confidence
                )
                VALUES (%s, %s, %s, %s)
                """,
                rows,
            )

    return str(submission_id)


def load_results():
    """
    Lädt ausschließlich Ergebnisse der
    aktuellen Kompetenz.
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
                    c.sort_order,
                    s.proficiency,
                    a.confidence
                FROM public.submissions AS s
                JOIN public.assignments AS a
                    ON a.submission_id = s.submission_id
                JOIN public.cards AS c
                    ON c.card_id = a.card_id
                WHERE
                    s.proficiency = %s
                    AND c.proficiency = %s
                ORDER BY
                    s.submitted_at_utc,
                    s.participant_id,
                    c.sort_order
                """,
                (
                    APP_PROFICIENCY,
                    APP_PROFICIENCY,
                )
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
            "proficiency",
            "confidence",
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
            "proficiency",
            "confidence",
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
                "Teilnehmer:innen-ID",
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
            "participant_id": "Teilnehmer:innen-ID",
        }
    )

    return grouped[
        [
            "Teilnehmer:innen-ID",
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
        key=lambda card_id: int(card_id[-2:])
    )

    for card_id in ordered_card_ids:
        meta = CARD_METADATA[card_id]
        card_rows = raw_df[
            raw_df["card_id"] == card_id
        ]

        counts_series = (
            card_rows["participant_level"]
            .replace({"Pre-A1": "vor A1"})
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
                counts["vor A1" if reference_level == "Pre-A1" else reference_level]
                / number_ratings
                * 100
            )

            reference_position = ANALYSIS_POSITIONS[reference_level]
            mean_abs_deviation = (
                sum(
                    counts[level]
                    * abs(level_positions[level] - reference_position)
                    for level in LEVELS
                )
                / number_ratings
            )

            majority_matches_reference = (
                ("vor A1" if reference_level == "Pre-A1" else reference_level) in modal_levels
            )
        else:
            majority = "—"
            agreement = 0.0
            spread = 0
            entropy = 0.0
            reference_match = 0.0
            mean_abs_deviation = 0.0
            majority_matches_reference = False

        pair_count = number_ratings * (number_ratings - 1) / 2
        pair_distance = (
            sum(
                counts[LEVELS[i]] * counts[LEVELS[j]] * abs(j - i)
                for i in range(len(LEVELS))
                for j in range(i + 1, len(LEVELS))
            ) / pair_count
            if pair_count else None
        )

        secure_count = int((card_rows["confidence"] == True).sum())
        unsure_count = int((card_rows["confidence"] == False).sum())
        no_answer_count = number_ratings - secure_count - unsure_count
        answered_count = secure_count + unsure_count
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
                **{level: counts[level] for level in LEVELS},
                "Modalniveau": majority,
                "Modalanteil %": round(agreement, 1),
                "Spannweite (Stufen)": spread,
                "Ø paarweiser Niveauabstand": round(pair_distance, 2) if pair_distance is not None else None,
                "Entropie %": round(entropy, 1),
                "Referenzübereinstimmung %": round(reference_match, 1),
                "Ø absolute Referenzabweichung (Stufen)": round(mean_abs_deviation, 2),
                "Modal-Referenz (PP)": (
                    round(agreement - reference_match, 1)
                    if number_ratings > 0 else None
                ),
                "Sicher (!)": secure_count,
                "Unsicher (?)": unsure_count,
                "Ohne Angabe": no_answer_count,
                "Sicher %": round(100 * secure_count / answered_count, 1) if answered_count else None,
                "Unsicher %": round(100 * unsure_count / answered_count, 1) if answered_count else None,
            }
        )

    return pd.DataFrame(result_rows)


def full_table_height(df):
    """Genug Höhe, damit nur die Browserseite vertikal scrollt."""
    return 48 + len(df) * 35


def build_rater_level_analysis(raw_df, participant_id):
    """
    Trefferquote einer Person getrennt
    nach Referenzniveau.
    """
    df = raw_df[
        raw_df["participant_id"] == participant_id
    ].copy()

    columns = [
        "Referenzniveau",
        "Karten",
        "Referenzgleiche Zuordnungen",
        "Referenzabweichungen",
        "Referenzübereinstimmung %",
        "Ø absolute Referenzabweichung (Stufen)",
    ]

    if df.empty:
        return pd.DataFrame(columns=columns)

    level_positions = ANALYSIS_POSITIONS

    df["abweichung"] = (
        df["participant_level"].map(level_positions)
        - df["reference_level"].map(level_positions)
    )

    df["treffer"] = df["abweichung"] == 0
    df["absolute_abweichung"] = df["abweichung"].abs()

    result = (
        df.groupby("reference_level", as_index=False)
        .agg(
            Karten=("card_id", "nunique"),
            **{"Referenzgleiche Zuordnungen": ("treffer", "sum")},
            **{
                "Ø absolute Referenzabweichung (Stufen)": (
                    "absolute_abweichung", "mean"
                ),
            },
        )
    )

    result["Referenzabweichungen"] = (
        result["Karten"] - result["Referenzgleiche Zuordnungen"]
    )

    result["Referenzübereinstimmung %"] = (
        result["Referenzgleiche Zuordnungen"]
        / result["Karten"]
        * 100
    ).round(1)

    result["Ø absolute Referenzabweichung (Stufen)"] = (
        result["Ø absolute Referenzabweichung (Stufen)"].round(2)
    )

    result = result.rename(
        columns={
            "reference_level": "Referenzniveau"
        }
    )

    result["Referenzniveau"] = pd.Categorical(
        result["Referenzniveau"],
        categories=["Pre-A1", *LEVELS],
        ordered=True,
    )

    result = result.sort_values("Referenzniveau")

    return result[columns].reset_index(drop=True)

def build_confidence_accuracy_analysis(raw_df):
    """Referenzübereinstimmung je Sicherheitszustand (nur explizite Antworten)."""
    columns = ["Sicherheit", "Bewertungen", "Referenzgleiche Zuordnungen", "Referenzübereinstimmung %"]
    if raw_df.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for confidence_value, label in [(True, "Sicher (!)"), (False, "Unsicher (?)")]:
        subset = raw_df[raw_df["confidence"].eq(confidence_value)]
        count = len(subset)
        hits = int((subset["participant_level"] == subset["reference_level"]).sum())
        rows.append({
            "Sicherheit": label,
            "Bewertungen": count,
            "Referenzgleiche Zuordnungen": hits,
            "Referenzübereinstimmung %": round(100 * hits / count, 1) if count else None,
        })
    return pd.DataFrame(rows, columns=columns)


def build_confidence_level_analysis(raw_df):
    """Sicherheitsverteilung nach Referenzniveau; Quoten unter expliziten Antworten."""
    columns = ["Referenzniveau", "Bewertungen", "Mit Angabe", "Sicher (!)",
               "Unsicher (?)", "Ohne Angabe", "Sicher %", "Unsicher %"]
    rows = []
    reference_levels = (["Pre-A1"] if raw_df["reference_level"].eq("Pre-A1").any() else []) + LEVELS
    for level in reference_levels:
        subset = raw_df[raw_df["reference_level"] == level]
        total = len(subset)
        secure = int(subset["confidence"].eq(True).sum())
        unsure = int(subset["confidence"].eq(False).sum())
        answered = secure + unsure
        rows.append({
            "Referenzniveau": level,
            "Bewertungen": total,
            "Mit Angabe": answered,
            "Sicher (!)": secure,
            "Unsicher (?)": unsure,
            "Ohne Angabe": total - answered,
            "Sicher %": round(100 * secure / answered, 1) if answered else None,
            "Unsicher %": round(100 * unsure / answered, 1) if answered else None,
        })
    return pd.DataFrame(rows, columns=columns)


def _reliability_from_counts(counts_by_card):
    """Punktschätzungen für Krippendorffs Alpha (ordinal) und Fleiss-Kappa (nominal)."""
    n_categories = len(LEVELS)
    coincidence = [[0.0] * n_categories for _ in LEVELS]
    for counts in counts_by_card:
        m = sum(counts)
        if m < 2:
            continue
        for i in range(n_categories):
            for j in range(n_categories):
                coincidence[i][j] += counts[i] * (counts[j] - (i == j)) / (m - 1)

    marginals = [sum(row) for row in coincidence]
    total = sum(marginals)
    alpha = None
    if total > 1:
        distances = [[0.0] * n_categories for _ in LEVELS]
        for i in range(n_categories):
            for j in range(n_categories):
                if i != j:
                    lo, hi = sorted((i, j))
                    distances[i][j] = (
                        sum(marginals[lo:hi + 1])
                        - (marginals[lo] + marginals[hi]) / 2
                    ) ** 2
        observed = sum(
            coincidence[i][j] * distances[i][j]
            for i in range(n_categories) for j in range(n_categories)
        ) / total
        expected = sum(
            marginals[i] * marginals[j] * distances[i][j]
            for i in range(n_categories) for j in range(n_categories)
        ) / (total * (total - 1))
        if expected > 0:
            alpha = 1 - observed / expected

    fleiss = None
    if counts_by_card and len({sum(row) for row in counts_by_card}) == 1:
        n = sum(counts_by_card[0])
        if n >= 2:
            n_items = len(counts_by_card)
            observed_agreement = sum(
                sum(v * (v - 1) for v in counts) / (n * (n - 1))
                for counts in counts_by_card
            ) / n_items
            proportions = [
                sum(counts[i] for counts in counts_by_card) / (n_items * n)
                for i in range(n_categories)
            ]
            expected_agreement = sum(x * x for x in proportions)
            if expected_agreement < 1:
                fleiss = (observed_agreement - expected_agreement) / (1 - expected_agreement)
    return alpha, fleiss


def build_reliability_analysis(raw_df, bootstrap_samples=500):
    """95-%-Perzentil-Bootstrap-KI durch Resampling ganzer Karten (mit fester Seed)."""
    columns = ["Kennzahl", "Wert", "95-%-KI Untergrenze", "95-%-KI Obergrenze"]
    labels = ["Krippendorffs α (ordinal)", "Fleiss-κ (nominal)"]
    data = raw_df.dropna(subset=["participant_level", "card_id"])
    counts_by_card = []
    for _, group in data.groupby("card_id"):
        if len(group) < 2:
            continue
        counts = group["participant_level"].replace({"Pre-A1": "vor A1"}).value_counts()
        counts_by_card.append([int(counts.get(level, 0)) for level in LEVELS])

    if not counts_by_card:
        return pd.DataFrame([dict(zip(columns, [label, None, None, None])) for label in labels])

    estimates = _reliability_from_counts(counts_by_card)
    intervals = [(None, None), (None, None)]
    # Bei zu wenigen unterschiedlichen Karten ist das KI nicht sinnvoll interpretierbar.
    if len(counts_by_card) >= 5:
        generator = random.Random(20261016)
        distribution = [[], []]
        n_items = len(counts_by_card)
        for _ in range(bootstrap_samples):
            sample = [counts_by_card[generator.randrange(n_items)] for _ in range(n_items)]
            values = _reliability_from_counts(sample)
            for i, value in enumerate(values):
                if value is not None and math.isfinite(value):
                    distribution[i].append(value)
        for i, values in enumerate(distribution):
            # Bei häufig undefinierten Wiederholungen lieber kein instabiles KI zeigen.
            if len(values) >= 0.9 * bootstrap_samples:
                values.sort()
                lo = values[int(0.025 * (len(values) - 1))]
                hi = values[int(0.975 * (len(values) - 1))]
                intervals[i] = (lo, hi)

    return pd.DataFrame([
        {
            "Kennzahl": label,
            "Wert": round(value, 3) if value is not None else None,
            "95-%-KI Untergrenze": round(intervals[i][0], 3) if intervals[i][0] is not None else None,
            "95-%-KI Obergrenze": round(intervals[i][1], 3) if intervals[i][1] is not None else None,
        }
        for i, (label, value) in enumerate(zip(labels, estimates))
    ], columns=columns)


def build_spearman_matrix(raw_df):
    """Paarweise Rangkorrelation über gemeinsame Karten; konstante Reihen undefiniert."""
    if raw_df.empty:
        return pd.DataFrame()
    order = ANALYSIS_POSITIONS
    pivot = raw_df.assign(level_index=raw_df["participant_level"].map(order)).pivot_table(
        index="card_id", columns="participant_id", values="level_index", aggfunc="first"
    )
    return pivot.corr(method="spearman", min_periods=3).round(3)


def build_reference_summary(raw_df):
    if raw_df.empty:
        return {"Referenzübereinstimmung %": None, "Ø Referenzabweichung (Stufen)": None}
    order = ANALYSIS_POSITIONS
    correct = raw_df["participant_level"].eq(raw_df["reference_level"])
    distance = (raw_df["participant_level"].map(order) - raw_df["reference_level"].map(order)).abs()
    return {"Referenzübereinstimmung %": round(100 * correct.mean(), 1),
            "Ø Referenzabweichung (Stufen)": round(distance.mean(), 2)}


def render_admin_dashboard(rows):
    raw_df = results_to_dataframe(rows)
    submissions_df = build_submission_overview(raw_df)
    card_analysis_df = build_card_analysis(raw_df)
    rater_analysis_df = build_rater_analysis(raw_df)
    confidence_accuracy_df = build_confidence_accuracy_analysis(raw_df)
    confidence_level_df = build_confidence_level_analysis(raw_df)
    reliability_df = build_reliability_analysis(raw_df)
    spearman_df = build_spearman_matrix(raw_df)
    reference_summary = build_reference_summary(raw_df)

    number_submissions = len(submissions_df)
    number_assignments = len(raw_df)
    if submissions_df.empty:
        last_submission = "—"
    else:
        last_submission = submissions_df.iloc[0]["Zeitpunkt (UTC)"]

    st.title(f"Admin-Dashboard {APP_PROFICIENCY}")

    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.caption(
            "Live-Auswertung aus Supabase/PostgreSQL."
        )
    with top_right:
        if st.button("← Zur Umfrage", use_container_width=True):
            st.session_state["admin_dashboard_open"] = False
            st.rerun()

    (
        tab_overview,
        tab_submissions,
        tab_cards,
        tab_individual,
        tab_raters,
        tab_reliability,
        tab_confidence,
    ) = st.tabs(
        [
            "📌 Überblick",
            "🧾 Abgaben",
            "🗂️ Kartenanalyse",
            "📊 Einzelanalyse",
            "👥 Bewerter:innen",
            "📐 Reliabilität",
            "❗ Sicherheitsanalyse",
        ]
    )

    # 1. Überblick
    with tab_overview:

        if st.button("Daten neu laden"):
            load_cards.clear()
            st.rerun()
        metric_1, metric_2 = st.columns(2)
        metric_1.metric("Abgaben", number_submissions)
        metric_2.metric("Zuordnungen", number_assignments)

        st.write(f"**Letzte Abgabe:** {last_submission}")
        st.write(f"**Aktive Karten in der Erhebung:** {len(CARDS)}")
        if number_assignments:
            x, y = st.columns(2)
            x.metric("Referenzübereinstimmung", f"{reference_summary['Referenzübereinstimmung %']:.1f} %")
            y.metric("Ø Referenzabweichung", f"{reference_summary['Ø Referenzabweichung (Stufen)']:.2f} Stufen")
        st.caption("Referenzübereinstimmung: Anteil der Zuordnungen, die exakt dem hinterlegten Referenzniveau entsprechen. "
                   "Ø Referenzabweichung: durchschnittlicher absoluter Abstand zwischen vergebenem und Referenzniveau; "
                   "eine Stufe entspricht einem Schritt in vor A1 → A1 → A2 → B1 → B2 → C1 → C2.")

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

        st.markdown("**Sicherheitsanalyse**")
        download_3, download_4 = st.columns(2)
        with download_3:
            st.download_button(
                "📥 Sicherheit und Treffergenauigkeit als CSV",
                data=confidence_accuracy_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="sicherheit_treffergenauigkeit.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with download_4:
            st.download_button(
                "📥 Sicherheit nach Referenzniveau als CSV",
                data=confidence_level_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="sicherheit_nach_referenzniveau.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.markdown("**Bewerter:innen**")
        st.download_button(
            "📥 Bewerter:innen als CSV",
            data=rater_analysis_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"bewerter_{APP_PROFICIENCY}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        if not rater_analysis_df.empty:
            export_participant = st.selectbox(
                "Bewerter:in für den Niveauanalyse-Export auswählen",
                options=sorted(raw_df["participant_id"].dropna().unique().tolist()),
                key="export_rater",
            )
            export_level_df = build_rater_level_analysis(raw_df, export_participant)
            st.download_button(
                "📥 Niveauanalyse als CSV",
                data=export_level_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"niveauanalyse_{export_participant}_{APP_PROFICIENCY}.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.markdown("**Reliabilität**")
        a, b = st.columns(2)
        with a:
            st.download_button("📥 Reliabilitätskennzahlen als CSV",
                               data=reliability_df.to_csv(index=False).encode("utf-8-sig"),
                               file_name="reliabilitaet.csv", mime="text/csv", use_container_width=True)
        with b:
            st.download_button("📥 Spearman-Matrix als CSV",
                               data=spearman_df.to_csv(index=True).encode("utf-8-sig"),
                               file_name="spearman_matrix.csv", mime="text/csv", use_container_width=True)

        st.caption(
            "Die Downloads enthalten Rohdaten und aggregierte Ergebnisse. "
            "Das Modalniveau ist die häufigste Einstufung; die Referenzübereinstimmung "
            "ist die exakte Gleichheit mit dem hinterlegten Referenzniveau."
        )

        # Glossare stehen direkt unter den jeweiligen Auswertungstabellen.
        # Alttext im Überblick entfällt.

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
        sort_choice = st.selectbox(
            "Karten sortieren nach",
            ["Hinterlegter Reihenfolge", "Modalniveau (aufsteigend)",
             "Modalniveau (absteigend)", "Geringstem Modalanteil",
             "Größtem Ø paarweisen Niveauabstand"],
            key="card_analysis_sort",
        )
        sorted_cards = card_analysis_df.copy()
        if sort_choice.startswith("Modalniveau"):
            rank = {level: i for i, level in enumerate(LEVELS)}
            sorted_cards["_modal_sort"] = sorted_cards["Modalniveau"].map(rank)
            sorted_cards = sorted_cards.sort_values(
                ["_modal_sort", "Reihenfolge"],
                ascending=[sort_choice.endswith("(aufsteigend)"), True],
                na_position="last",
            ).drop(columns="_modal_sort")
        elif sort_choice == "Geringstem Modalanteil":
            sorted_cards = sorted_cards.sort_values("Modalanteil %")
        elif sort_choice == "Größtem Ø paarweisen Niveauabstand":
            sorted_cards = sorted_cards.sort_values(
                "Ø paarweiser Niveauabstand", ascending=False, na_position="last"
            )
        card_analysis_view = sorted_cards.drop(
            columns=[
                "Kategorie-Nr",
                "Level-Nr",
            ],
            errors="ignore",
        )
        st.dataframe(
            card_analysis_view,
            hide_index=True,
            use_container_width=True,
            height=full_table_height(card_analysis_view),
            column_config={
                "Reihenfolge": st.column_config.NumberColumn("Reihenfolge", help="Interne Sortierreihenfolge der Karte in den Stammdaten."),
                "Karte": st.column_config.TextColumn("Karte", help="Kennung der Karte."),
                "Text": st.column_config.TextColumn("Text", help="Wortlaut des Deskriptors."),
                "Kategorie": st.column_config.TextColumn("Kategorie", help="Inhaltliche Kartenkategorie aus den Stammdaten."),
                "Referenzniveau": st.column_config.TextColumn("Referenzniveau", help="Vorab hinterlegtes Niveau zum Vergleich mit den Bewertungen."),
                "Bewertungen": st.column_config.NumberColumn("Bewertungen", help="Anzahl vorliegender Niveaueinstufungen für diese Karte."),
                **{level: st.column_config.NumberColumn(level, help=f"Anzahl der Zuordnungen zur Niveaustufe {level}.") for level in LEVELS},
                "Modalniveau": st.column_config.TextColumn("Modalniveau", help="Am häufigsten gewähltes Niveau; bei Gleichstand werden mehrere Niveaus angezeigt."),
                "Sicher (!)": st.column_config.NumberColumn("Sicher (!)", help="Anzahl ausdrücklich als sicher markierter Zuordnungen."),
                "Unsicher (?)": st.column_config.NumberColumn("Unsicher (?)", help="Anzahl ausdrücklich als unsicher markierter Zuordnungen."),
                "Ohne Angabe": st.column_config.NumberColumn("Ohne Angabe", help="Zuordnungen ohne Sicherheitsmarkierung; einschließlich älterer Abgaben ohne diese Funktion."),
                "Sicher %": st.column_config.ProgressColumn("Sicher %", min_value=0, max_value=100, format="%.1f %%", help="Anteil sicherer Einstufungen an allen Einstufungen mit expliziter Sicherheitsangabe (! oder ?)."),
                "Unsicher %": st.column_config.ProgressColumn("Unsicher %", min_value=0, max_value=100, format="%.1f %%", help="Anteil unsicherer Einstufungen an allen Einstufungen mit expliziter Sicherheitsangabe (! oder ?)."),
                "Modalanteil %": st.column_config.ProgressColumn(
                    "Modalanteil %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help=(
                        "Anteil der häufigsten Einstufung "
                        "an allen Bewertungen einer Karte; "
                        "zeigt die Konzentration, "
                        "nicht die zufallskorrigierte Reliabilität."
                    ),
                ),
                "Referenzübereinstimmung %": st.column_config.ProgressColumn(
                    "Referenzübereinstimmung %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help=(
                        "Anteil exakt referenzgleicher "
                        "Zuordnungen."
                    ),
                ),

                "Modal-Referenz (PP)": st.column_config.NumberColumn(
                    "Modal-Referenz (PP)",
                    format="%.1f",
                    help=(
                        "Modalanteil minus Referenzübereinstimmung "
                        "in Prozentpunkten (nie negativ). "
                        "0 ist auch bei mehreren gleich häufigen Modalniveaus möglich."
                    ),
                ),

                "Spannweite (Stufen)": st.column_config.NumberColumn(
                    "Spannweite (Stufen)",
                    format="%d",
                    help=(
                        "Abstand zwischen dem niedrigsten und höchsten "
                        "vergebenen Niveau (0 bis 6 Stufen)."
                    ),
                ),
                
                "Ø absolute Referenzabweichung (Stufen)": st.column_config.NumberColumn(
                    "Ø absolute Referenzabweichung (Stufen)",
                    format="%.2f",
                    help=(
                        "Durchschnittlicher absoluter Abstand der Einstufungen "
                        "zum Referenzniveau (0 = vollständige Übereinstimmung)."
                    ),
                ),

                "Ø paarweiser Niveauabstand": st.column_config.NumberColumn(
                    "Ø paarweiser Niveauabstand",
                    format="%.2f",
                    help=(
                        "Durchschnittlicher Abstand zwischen den Einstufungen "
                        "aller möglichen Rater:innen-Paare für dieselbe Karte. "
                        "0 bedeutet vollständige Übereinstimmung. "
                        "Je höher der Wert, desto stärker unterscheiden sich "
                        "die vergebenen Niveaus (maximal 6 Stufen)."
                    ),
                ),

                
                "Entropie %": st.column_config.NumberColumn(
                    "Entropie %",
                    format="%.1f %%",
                    help=(
                        "Verteilung der Einstufungen "
                        "über die sieben Niveaus; höhere "
                        "Werte bedeuten eine "
                        "gleichmäßigere Streuung."
                    ),
                ),
            },
        )

        st.markdown("**Erläuterung der Kennzahlen**")
        st.caption("""
        **Modalniveau:** am häufigsten gewähltes Niveau (bei Gleichstand mehrere Niveaus).  
        
        **Modalanteil %:** Anteil der häufigsten Einstufung an allen Bewertungen einer Karte; zeigt die Konzentration, nicht die zufallskorrigierte Reliabilität.  
        
        **Referenzübereinstimmung %:** Anteil exakt referenzgleicher Zuordnungen.  
        
        **Modal-Referenz (PP):** Modalanteil minus Referenzübereinstimmung, in Prozentpunkten (mindestens 0). Bei 0 kann auch ein Gleichstand mehrerer Modalniveaus vorliegen.  
        
        **Ø absolute Referenzabweichung (Stufen):** mittlerer absoluter Niveauabstand zur Referenz.  
        
        **Spannweite (Stufen):** Abstand zwischen der niedrigsten und höchsten vergebenen Stufe.  
        
        **Ø paarweiser Niveauabstand:** durchschnittlicher Abstand aller Rater-Paare auf derselben Karte (0 = identische Niveaus; maximal 6 Stufen).  
        
        **Entropie %:** Verteilung der Einstufungen über die sieben Niveaus; höhere Werte bedeuten eine gleichmäßigere Streuung.  
        
        **Sicher/Unsicher/Ohne Angabe:** Häufigkeiten der Sicherheitseinschätzung; Sicher % und Unsicher % beziehen sich nur auf Bewertungen mit ausdrücklicher Sicherheitsangabe (! oder ?). Ohne Angabe wird separat gezählt; ohne explizite Angaben bleiben die Quoten leer.
        """)
    # 4. Einzelanalyse
    with tab_individual:

        st.subheader("Einzelanalyse der Karten")

        st.caption(
            "Verteilung der abgegebenen Niveaueinstufungen "
            "für jede einzelne Karte. "
            "Dargestellt werden ausschließlich die Bewertungen "
            "der Teilnehmenden."
        )

        ordered_card_ids = sorted(
            CARDS,
            key=lambda card_id: int(card_id[-2:])
        )

        for card_id in ordered_card_ids:

            card_text = CARDS[card_id]

            # Bewertungen für diese Karte
            card_ratings = raw_df.loc[
                raw_df["card_id"] == card_id,
                "participant_level"
            ]

            # Stimmen pro Niveau zählen
            counts = (
                card_ratings.replace({"Pre-A1": "vor A1"})
                .value_counts()
                .reindex(LEVELS, fill_value=0)
            )

            total_votes = int(counts.sum())

            # Diagrammdaten
            chart_df = pd.DataFrame({
                "Niveau": LEVELS,
                "Stimmen": [
                    int(counts[level])
                    for level in LEVELS
                ]
            })

            # Einzelkarte darstellen
            with st.container(border=True):

                st.markdown(f"### {card_id}")

                st.write(card_text)

                st.caption(
                    f"Abgegebene Bewertungen: {total_votes}"
                )

                if total_votes == 0:
                    st.info(
                        "Für diese Karte liegen noch keine Bewertungen vor."
                    )

                else:
                    st.bar_chart(
                        chart_df,
                        x="Niveau",
                        y="Stimmen",
                        sort=False,
                        height=250,
                        use_container_width=True,
                        x_label="Sprachniveau",
                        y_label="Anzahl der Stimmen"
                    )
                    
    # Gesamtreliabilität
    with tab_reliability:
        st.subheader("Übereinstimmung zwischen den Rater:innen")
        st.dataframe(
            reliability_df,
            hide_index=True,
            use_container_width=True,
            height=full_table_height(reliability_df),
        )
        st.caption("""
        **Krippendorffs α (ordinal):** zufallskorrigierte Übereinstimmung für geordnete Niveaus. Ein höherer Wert bedeutet höhere Reliabilität; 1 steht für perfekte Übereinstimmung.
        
        **Fleiss-κ (nominal):** zufallskorrigierte exakte Übereinstimmung, ohne die Größe der Niveauabstände zu berücksichtigen. 0 entspricht der modellbedingt erwarteten Übereinstimmung.
        
        **95-%-Konfidenzintervalle:** Perzentil-Bootstrap mit 500 Wiederholungen; dabei werden ganze Karten einschließlich aller zugehörigen Ratings wiederholt gezogen. Das Intervall beschreibt die Unsicherheit der Schätzung bei Variation der Kartenstichprobe (nicht der Rater:innen). Es wird bei weniger als fünf mehrfach bewerteten Karten oder zu vielen undefinierten Bootstrap-Werten nicht angezeigt.

        Beide Kennzahlen können bei unzureichenden oder konstanten Daten undefiniert sein; Fleiss-κ wird nur bei gleicher Anzahl an Bewertungen pro Karte berechnet.
        """)

        st.divider()
        st.subheader("Rangähnlichkeit der Rater:innen (Spearman-ρ)")
        if spearman_df.empty or len(spearman_df.columns) < 2:
            st.info("Für eine paarweise Rangkorrelation sind mindestens zwei Rater:innen erforderlich.")
        else:
            st.dataframe(spearman_df, use_container_width=True, height=full_table_height(spearman_df),)
        st.caption("**Spearman-ρ:** paarweise Rangkorrelation der vergebenen Niveaus über dieselben Karten "
                   "(−1 bis +1). Ein hoher Wert bedeutet ähnliche Rangordnung, "
                   "nicht notwendigerweise dieselben Niveaus. "
                   "Leere Felder: weniger als drei gemeinsame Karten oder konstante Bewertungen.")

    # Sicherheitsanalyse: Genauigkeit und Verteilung nach Referenzniveau
    with tab_confidence:
        st.subheader("Subjektive Sicherheit und Referenzübereinstimmung")
        st.caption(
            "Vergleich der exakten Übereinstimmung mit dem Referenzniveau "
            "für ausdrücklich als sicher bzw. unsicher markierte Karten. "
            "Bewertungen ohne Sicherheitsangabe werden hier nicht berücksichtigt."
        )
        if raw_df.empty or not raw_df["confidence"].notna().any():
            st.info("Noch keine Bewertungen mit Sicherheitsangabe vorhanden.")
        else:
            st.dataframe(
                confidence_accuracy_df,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(confidence_accuracy_df),
                column_config={
                    "Sicherheit": st.column_config.TextColumn("Sicherheit", help="Explizite Sicherheitsmarkierung der Karten: ! sicher, ? unsicher."),
                    "Bewertungen": st.column_config.NumberColumn("Bewertungen", help="Anzahl Zuordnungen in dieser Sicherheitsgruppe; fehlende Angaben ausgeschlossen."),
                    "Referenzgleiche Zuordnungen": st.column_config.NumberColumn("Referenzgleiche Zuordnungen", help="Anzahl exakt mit dem Referenzniveau übereinstimmender Zuordnungen."),
                    "Referenzübereinstimmung %": st.column_config.ProgressColumn(
                        "Referenzübereinstimmung %", min_value=0, max_value=100,
                        format="%.1f %%",
                        help=(
                            "Referenzgleiche Zuordnungen geteilt durch die Bewertungen "
                            "dieser Sicherheitsgruppe. Bewertungen "
                            "ohne Sicherheitsangabe bleiben aus "
                            "diesem Vergleich ausgeschlossen."                 
                        ),
                    ),
                },
            )
            chart_data = confidence_accuracy_df.dropna(
                subset=["Referenzübereinstimmung %"]
            ).set_index("Sicherheit")
            if not chart_data.empty:
                st.bar_chart(
                    chart_data[["Referenzübereinstimmung %"]],
                    y="Referenzübereinstimmung %",
                    x_label="Sicherheitseinschätzung",
                    y_label="Referenzübereinstimmung in %",
                )

        
        st.caption("""
        **Bewertungen:** Zahl der Zuordnungen mit der jeweiligen expliziten Sicherheitsmarkierung.
        
        **Referenzgleiche Zuordnungen:** Anzahl der mit dem Referenzniveau übereinstimmenden Zuordnungen.
        
        **Referenzübereinstimmung %:** Referenzgleiche Zuordnungen geteilt durch die Bewertungen dieser Sicherheitsgruppe.
        
        Bewertungen ohne Sicherheitsangabe bleiben aus diesem Vergleich ausgeschlossen.
        """)

        st.divider()
        st.subheader("Subjektive Sicherheit nach Referenzniveau")
        st.caption(
            "Die Prozentwerte für Sicher und Unsicher beziehen sich auf "
            "Bewertungen mit ausdrücklicher Sicherheitsangabe innerhalb "
            "des jeweiligen Referenzniveaus. Ohne Angabe wird getrennt gezeigt."
        )
        st.dataframe(
            confidence_level_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Referenzniveau": st.column_config.TextColumn("Referenzniveau", help="Hinterlegtes Referenzniveau der Karte."),
                "Bewertungen": st.column_config.NumberColumn("Bewertungen", help="Alle Bewertungen auf diesem Referenzniveau."),
                "Mit Angabe": st.column_config.NumberColumn("Mit Angabe", help="Bewertungen mit explizitem ! oder ?."),
                "Sicher (!)": st.column_config.NumberColumn("Sicher (!)", help="Ausdrücklich als sicher eingestufte Bewertungen."),
                "Unsicher (?)": st.column_config.NumberColumn("Unsicher (?)", help="Ausdrücklich als unsicher eingestufte Bewertungen."),
                "Ohne Angabe": st.column_config.NumberColumn("Ohne Angabe", help="Bewertungen ohne Markierung, einschließlich älterer Abgaben ohne Sicherheitsfunktion."),
                "Sicher %": st.column_config.ProgressColumn(
                    "Sicher %", min_value=0, max_value=100, format="%.1f %%",
                    help="Anteil sicherer Zuordnungen unter den ausdrücklich markierten Bewertungen dieses Referenzniveaus."
                ),
                "Unsicher %": st.column_config.ProgressColumn(
                    "Unsicher %", min_value=0, max_value=100, format="%.1f %%",
                    help="Anteil unsicherer Zuordnungen unter den ausdrücklich markierten Bewertungen dieses Referenzniveaus."
                ),
            },
        )

        st.caption("""
        **Mit Angabe:** Zahl der bewusst als sicher oder unsicher markierten Einstufungen.
        
        **Sicher % / Unsicher %:** jeweiliger Anteil nur unter Bewertungen mit ausdrücklicher Angabe des betreffenden Referenzniveaus.
        
        **Ohne Angabe:** fehlende Sicherheitsmarkierung, nicht automatisch Unsicherheit.
        """)

        st.caption(
            "Hinweis zu älteren Abgaben: Die Datenbank enthält für frühere "
            "Bewertungen ohne Sicherheitsfunktion ebenfalls NULL. Diese "
            "lassen sich allein anhand der confidence-Spalte nicht von "
            "heute freiwillig ausgelassenen Angaben unterscheiden. Daher "
            "werden sie nicht als unsicher interpretiert."
        )
    # Bewerter:innen
    with tab_raters:
        st.subheader("Auswertung nach Bewerter:innen")

        st.caption(
            f"Einzelne Abgaben für die Kompetenz "
            f"{APP_PROFICIENCY}. "
            "Referenzübereinstimmung = exakte Übereinstimmung "
            "mit dem hinterlegten Referenzniveau."
        )

        if rater_analysis_df.empty:
            st.info("Noch keine Abgaben vorhanden.")
        else:
            st.dataframe(
                rater_analysis_df,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(
                    rater_analysis_df
                ),
            
            column_config={
                "Referenzübereinstimmung %": st.column_config.ProgressColumn(
                    "Referenzübereinstimmung %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help="Anteil der Einstufungen, die exakt dem Referenzniveau entsprechen.",
                ),
            
                "Peer-Übereinstimmung %": st.column_config.ProgressColumn(
                    "Peer-Übereinstimmung %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help=(
                        "Durchschnittlicher Anteil anderer Rater:innen, "
                        "die bei denselben Karten dasselbe Niveau vergeben haben."
                    ),
                ),
            
                "Peer-Vergleiche (Karten)": st.column_config.NumberColumn(
                    "Peer-Vergleiche (Karten)",
                    format="%d",
                    help="Anzahl der Karten, für die mindestens eine weitere Bewertung vorliegt.",
                ),
            
                "Referenzabweichungen": st.column_config.NumberColumn(
                    "Referenzabweichungen",
                    format="%d",
                    help="Anzahl der Einstufungen, die nicht dem Referenzniveau entsprechen.",
                ),
            
                "Referenzabweichungen %": st.column_config.ProgressColumn(
                    "Referenzabweichungen %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help="Prozentualer Anteil der nicht referenzgleichen Einstufungen.",
                ),
            
                "Ø absolute Referenzabweichung (Stufen)": st.column_config.NumberColumn(
                    "Ø absolute Referenzabweichung (Stufen)",
                    format="%.2f",
                    help="Durchschnittlicher absoluter Abstand zum Referenzniveau in Stufen.",
                ),
            
                "Ø gerichtete Referenzabweichung (Stufen)": st.column_config.NumberColumn(
                    "Ø gerichtete Referenzabweichung (Stufen)",
                    format="%.2f",
                    help=(
                        "Mittlere Abweichung vom Referenzniveau. "
                        "Positive Werte bedeuten eher höhere, "
                        "negative Werte eher niedrigere Einstufungen."
                    ),
                ),
            
                "Oberhalb der Referenz": st.column_config.NumberColumn(
                    "Oberhalb der Referenz",
                    format="%d",
                    help="Anzahl der Einstufungen oberhalb des Referenzniveaus.",
                ),
            
                "Unterhalb der Referenz": st.column_config.NumberColumn(
                    "Unterhalb der Referenz",
                    format="%d",
                    help="Anzahl der Einstufungen unterhalb des Referenzniveaus.",
                ),
            
                "Abweichung ≥ 2": st.column_config.NumberColumn(
                    "Abweichung ≥ 2",
                    format="%d",
                    help="Anzahl der Einstufungen mit mindestens zwei Niveaustufen Abstand zur Referenz.",
                ),
            
                "Sicher (!)": st.column_config.NumberColumn(
                    "Sicher (!)",
                    format="%d",
                    help="Anzahl der ausdrücklich als sicher markierten Einstufungen.",
                ),
            
                "Unsicher (?)": st.column_config.NumberColumn(
                    "Unsicher (?)",
                    format="%d",
                    help="Anzahl der ausdrücklich als unsicher markierten Einstufungen.",
                ),
            
                "Ohne Angabe": st.column_config.NumberColumn(
                    "Ohne Angabe",
                    format="%d",
                    help="Anzahl der Einstufungen ohne Sicherheitsangabe.",
                ),
            
                "Referenzübereinstimmung sicher %": st.column_config.ProgressColumn(
                    "Referenzübereinst. sicher %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help="Referenzübereinstimmung nur bei ausdrücklich sicher bewerteten Karten.",
                ),
            
                "Referenzübereinstimmung unsicher %": st.column_config.ProgressColumn(
                    "Referenzübereinst. unsicher %",
                    min_value=0,
                    max_value=100,
                    format="%.1f %%",
                    help="Referenzübereinstimmung nur bei ausdrücklich unsicher bewerteten Karten.",
                ),
            },

            )

        
        st.caption("""
        **Referenzübereinstimmung %:** Anteil der Einstufungen einer Person, die dem Referenzniveau entsprechen.
        
        **Peer-Übereinstimmung %:** durchschnittlicher Anteil anderer Rater:innen, die bei denselben Karten dasselbe Niveau vergeben haben (ohne eigene Bewertung).
        
        **Peer-Vergleiche (Karten):** Karten, für die mindestens eine weitere Bewertung vorliegt.
        
        **Referenzübereinstimmung sicher/unsicher %:** Referenzübereinstimmung jeweils nur bei entsprechend gekennzeichneten Karten; fehlende Sicherheitsangaben werden gesondert gezählt.
        """)
        
        st.caption("""
        **Ø absolute Referenzabweichung (Stufen):** mittlerer absoluter Abstand zum Referenzniveau.
        
        **Ø gerichtete Referenzabweichung (Stufen):** positive Werte zeigen eine Tendenz zu höheren, negative Werte zu niedrigeren Einstufungen.
        
        **Abweichung ≥ 2:** mindestens zwei Niveaustufen Abstand.
        """)


        st.divider()
        st.subheader("Detailanalyse nach Referenzniveau")

        if not rater_analysis_df.empty:
            participant_ids = sorted(
                raw_df["participant_id"]
                .dropna()
                .unique()
                .tolist()
            )

            selected_participant = st.selectbox(
                "Bewerter:in auswählen",
                options=participant_ids,
                key="selected_rater",
            )

            level_analysis_df = (
                build_rater_level_analysis(
                    raw_df,
                    selected_participant,
                )
            )

            st.write(
                f"**Referenzübereinstimmung nach Referenzniveau "
                f"für {selected_participant}**"
            )

            st.dataframe(
                level_analysis_df,
                hide_index=True,
                use_container_width=True,
                height=full_table_height(
                    level_analysis_df
                ),
                column_config={
                    "Referenzübereinstimmung %":
                        st.column_config.ProgressColumn(
                            "Referenzübereinstimmung %",
                            min_value=0,
                            max_value=100,
                            format="%.1f %%",
                        ),
                },
            )

            chart_df = (
                level_analysis_df[
                    ["Referenzniveau", "Referenzübereinstimmung %"]
                ]
                .set_index("Referenzniveau")
            )

            st.bar_chart(
                chart_df,
                y="Referenzübereinstimmung %",
                y_label="Referenzübereinstimmung in %",
                x_label="Referenzniveau",
            )

            st.caption("""
            **Referenzgleiche Zuordnungen:** exakt referenzgleiche Einstufungen dieser Person.
            
            **Referenzabweichungen:** nicht referenzgleiche Einstufungen.
            
            **Referenzübereinstimmung %:** Referenzgleiche Zuordnungen geteilt durch die Zahl der Karten auf dem Referenzniveau.
            
            **Ø absolute Referenzabweichung (Stufen):** mittlerer absoluter Abstand zum Referenzniveau.
            """)



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

            if st.button(
                "📊 Dashboard öffnen",
                use_container_width=True,
            ):
                st.session_state["admin_granted"] = True
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

def build_rater_analysis(raw_df):
    """
    Erstellt eine Auswertung pro Abgabe.
    Eine Stufe Abstand entspricht einer Position
    in LEVELS, z. B. A1 -> A2.
    """
    columns = [
        "Teilnehmer:innen-ID", "Karten",
        "Peer-Übereinstimmung %", "Peer-Vergleiche (Karten)",
        "Referenzübereinstimmung %", "Referenzabweichungen", "Referenzabweichungen %",
        "Ø absolute Referenzabweichung (Stufen)",
        "Ø gerichtete Referenzabweichung (Stufen)",
        "Oberhalb der Referenz", "Unterhalb der Referenz", "Abweichung ≥ 2",
        "Sicher (!)", "Unsicher (?)", "Ohne Angabe",
        "Referenzübereinstimmung sicher %", "Referenzübereinstimmung unsicher %",
    ]

    if raw_df.empty:
        return pd.DataFrame(columns=columns)

    level_positions = ANALYSIS_POSITIONS

    df = raw_df.copy()

    df["sicher"] = df["confidence"].eq(True)
    df["unsicher"] = df["confidence"].eq(False)
    df["ohne_angabe"] = df["confidence"].isna()

    df["bewertungs_position"] = (
        df["participant_level"].map(level_positions)
    )
    df["referenz_position"] = (
        df["reference_level"].map(level_positions)
    )

    df["abweichung"] = (
        df["bewertungs_position"]
        - df["referenz_position"]
    )

    df["absolute_abweichung"] = df["abweichung"].abs()
    df["treffer"] = df["abweichung"] == 0
    df["zu_hoch"] = df["abweichung"] > 0
    df["zu_niedrig"] = df["abweichung"] < 0
    df["starke_abweichung"] = (
        df["absolute_abweichung"] >= 2
    )

    # Anzahl aller Bewertungen pro Karte
    df["anzahl_pro_karte"] = (
        df.groupby("card_id")["card_id"]
        .transform("size")
    )

    # Anzahl identischer Einstufungen pro Karte
    df["gleiche_bewertungen"] = (
        df.groupby(
            ["card_id", "participant_level"]
        )["card_id"]
        .transform("size")
    )
    
    # Eigene Bewertung aus dem Vergleich entfernen
    df["peer_uebereinstimmung"] = (
        (df["gleiche_bewertungen"] - 1)
        / (
            df["anzahl_pro_karte"] - 1
        ).replace(0, float("nan"))
    )

    # Karten mit mindestens einer weiteren Bewertung
    df["peer_vergleichbar"] = (
        df["anzahl_pro_karte"] > 1
    )

    result = (
        df.groupby(
            ["participant_id", "submitted_at_utc"],
            as_index=False,
        )
        .agg(
            Karten=("card_id", "nunique"),
            **{"Referenzgleiche Zuordnungen": ("treffer", "sum")},
            **{
                "Ø absolute Referenzabweichung (Stufen)": (
                    "absolute_abweichung", "mean"
                ),
                "Ø gerichtete Referenzabweichung (Stufen)": (
                    "abweichung", "mean"
                ),
                "Oberhalb der Referenz": ("zu_hoch", "sum"),
                "Unterhalb der Referenz": ("zu_niedrig", "sum"),
                "Abweichung ≥ 2": (
                    "starke_abweichung", "sum"
                ),
                "Sicher (!)": ("sicher", "sum"),
                "Unsicher (?)": ("unsicher", "sum"),
                "Ohne Angabe": ("ohne_angabe", "sum"),
                "Peer-Übereinstimmung %": (
                    "peer_uebereinstimmung", "mean"
                ),
                "Peer-Vergleiche (Karten)": (
                    "peer_vergleichbar", "sum"
                ),
            },
        )
    )


    hit_by_confidence = (
        df.groupby(["participant_id", "submitted_at_utc", "confidence"], dropna=False)
        .agg(treffer=("treffer", "sum"), anzahl=("treffer", "size"))
        .reset_index()
    )
    for confidence_value, label in [(True, "Referenzübereinstimmung sicher %"), (False, "Referenzübereinstimmung unsicher %")]:
        subset = hit_by_confidence[hit_by_confidence["confidence"] == confidence_value].copy()
        subset[label] = (100 * subset["treffer"] / subset["anzahl"]).round(1)
        result = result.merge(subset[["participant_id", "submitted_at_utc", label]],
                              on=["participant_id", "submitted_at_utc"], how="left")

    result["Referenzübereinstimmung %"] = (
        100 * result["Referenzgleiche Zuordnungen"] / result["Karten"]
    ).round(1)

    result["Referenzabweichungen"] = (
        result["Karten"] - result["Referenzgleiche Zuordnungen"]
    )

    result["Referenzabweichungen %"] = (
        100 * result["Referenzabweichungen"] / result["Karten"]
    ).round(1)

    result["Peer-Übereinstimmung %"] = (
        result["Peer-Übereinstimmung %"] * 100
    ).round(1)

    result["Peer-Vergleiche (Karten)"] = (
        result["Peer-Vergleiche (Karten)"].astype(int)
    )
    
    result["Ø absolute Referenzabweichung (Stufen)"] = (
        result["Ø absolute Referenzabweichung (Stufen)"].round(2)
    )
    result["Ø gerichtete Referenzabweichung (Stufen)"] = (
        result["Ø gerichtete Referenzabweichung (Stufen)"].round(2)
    )

    result = result.rename(
        columns={"participant_id": "Teilnehmer:innen-ID"}
    )

    return (
        result[columns]
        .sort_values("Teilnehmer:innen-ID")
        .reset_index(drop=True)
    )



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
    Acht Spalten über die verfügbare Breite.

    Die erste Spalte erhält etwas mehr Raum,
    weil dort anfangs alle Karten liegen.
    */
    #board {
        display: grid;

        grid-template-columns:
            minmax(220px, 1.45fr)
            repeat(7, minmax(125px, 1fr));

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

    .card-text { padding-right: 43px; }
    .card { position: relative; }
    .confidence-controls {
        position: absolute; top: 5px; right: 5px;
        display: flex; gap: 3px;
    }
    .confidence-btn {
        width: 18px; height: 18px; padding: 0;
        border-radius: 5px; border: 1px solid currentColor;
        background: transparent; font-size: 12px; font-weight: 700;
        line-height: 16px; cursor: pointer; opacity: .65;
    }
    .confidence-btn.secure { color: #198754; }
    .confidence-btn.unsure { color: #c73535; }
    .confidence-btn.selected.secure { background: #198754; color: white; opacity: 1; }
    .confidence-btn.selected.unsure { background: #c73535; color: white; opacity: 1; }
    .confidence-btn:disabled { opacity: .22; cursor: default; }

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
    acht Spalten nebeneinander gezeigt.
    */
    @media (max-width: 1380px) {
        body {
            overflow-x: auto;
        }

        #board {
            min-width: 1330px;
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
    let autosaveKey = "";
    let autosaveReady = false;
    let lastParticipantId = "";
    let lastStorageAction = "";
    let firstRender = true;
    let shouldSaveInitial = false;
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
        return Array.from(document.querySelectorAll(".column"), column => ({
            header: column.dataset.header,
            items: Array.from(column.querySelectorAll(".card"), card => card.dataset.value)
        }));
    }

    // Sicherheit ist unabhängig von der Kartenposition und optional.
    let confidenceValues = {};
    let currentConfidenceJson = "";

    // Einzige Speicherquelle: sessionStorage der Hauptseite.
    // Übersteht ein Neuladen, wird beim normalen Schließen des Tabs verworfen.
    let storageRequestNumber = 0;
    const pendingStorageRequests = new Map();

    function requestTabStorage(action, key, value = null) {
        return new Promise((resolve) => {
            const requestId = ++storageRequestNumber;
            const timeout = window.setTimeout(() => {
                pendingStorageRequests.delete(requestId);
                resolve({success: false, value: null});
            }, 2000);
            pendingStorageRequests.set(requestId, (result) => {
                window.clearTimeout(timeout);
                resolve(result);
            });
            window.parent.postMessage({
                type: "kompchin:tab-storage", action, key, value, requestId
            }, "*");
        });
    }

    window.addEventListener("message", (event) => {
        if (event.source !== window.parent || !event.data ||
            event.data.type !== "kompchin:tab-storage-response") return;
        const done = pendingStorageRequests.get(event.data.requestId);
        if (!done) return;
        pendingStorageRequests.delete(event.data.requestId);
        done(event.data);
    });

    async function loadStoredDraft(key) {
        // Der Listener der Hauptseite kann nach dem iframe starten.
        // Vor der Antwort keinesfalls den leeren Anfangszustand speichern.
        for (let attempt = 0; attempt < 5; attempt++) {
            const result = await requestTabStorage("get", key);
            if (result.success) return result.value;
            await new Promise(resolve => window.setTimeout(resolve, 300));
        }
        console.warn("Autosave: Sitzungsspeicher der Hauptseite nicht erreichbar");
        return null;
    }

    function clearStoredDraft(key) {
        requestTabStorage("remove", key);
    }

    function saveDraft() {
        if (!autosaveReady || !autosaveKey) return;
        requestTabStorage("set", autosaveKey, JSON.stringify({
            version: 1,
            board: readBoard(),
            confidence: confidenceValues,
            participant_id: lastParticipantId
        }));
    }

    function reportState() {
        currentBoardJson = JSON.stringify(readBoard());
        currentConfidenceJson = JSON.stringify(confidenceValues);
        saveDraft();
        setComponentValue({board: readBoard(), confidence: confidenceValues});
    }

    function updateButtons() {
        document.querySelectorAll(".column").forEach(column => {
            const unassigned = column.dataset.header === "Noch nicht zugeordnet";
            column.querySelectorAll(".card").forEach(card => {
                const value = confidenceValues[card.dataset.value];
                card.querySelectorAll(".confidence-btn").forEach(btn => {
                    btn.disabled = unassigned;
                    btn.classList.toggle("selected", value === (btn.dataset.confidence === "true"));
                });
            });
        });
    }

    function renderBoard(board) {
        boardElement.innerHTML = "";
        board.forEach(container => {
            const column = document.createElement("div");
            column.className = "column";
            column.dataset.header = container.header;
            const header = document.createElement("div");
            header.className = "header";
            header.textContent = container.header;
            const zone = document.createElement("div");
            zone.className = "dropzone";
            container.items.forEach(value => {
                const card = document.createElement("div");
                card.className = "card";
                card.draggable = true;
                card.dataset.value = value;
                const text = document.createElement("div");
                text.className = "card-text";
                text.textContent = value;
                card.appendChild(text);
                const controls = document.createElement("div");
                controls.className = "confidence-controls";
                [["!", "true", "Sicher"], ["?", "false", "Unsicher"]].forEach(([symbol, bool, title]) => {
                    const btn = document.createElement("button");
                    btn.type = "button";
                    btn.className = "confidence-btn " + (bool === "true" ? "secure" : "unsure");
                    btn.dataset.confidence = bool;
                    btn.textContent = symbol;
                    btn.title = title + " (erneut klicken zum Abwählen)";
                    btn.setAttribute("aria-label", title);
                    btn.draggable = false;
                    btn.addEventListener("mousedown", event => event.stopPropagation());
                    btn.addEventListener("dragstart", event => event.preventDefault());
                    btn.addEventListener("click", event => {
                        event.stopPropagation();
                        const next = bool === "true";
                        if (confidenceValues[value] === next) {
                            delete confidenceValues[value];
                        } else {
                            confidenceValues[value] = next;
                        }
                        updateButtons();
                        reportState();
                    });
                    controls.appendChild(btn);
                });
                card.appendChild(controls);
                zone.appendChild(card);
            });
            column.appendChild(header);
            column.appendChild(zone);
            boardElement.appendChild(column);
        });
        currentBoardJson = JSON.stringify(board);
        updateButtons();
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
                updateButtons();
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

                    reportState();
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

    async function onDataFromPython(event) {
        if (
            !event.data
            || event.data.type
                !== "streamlit:render"
        ) {
            return;
        }

        const args =
            event.data.args || {};

        const key = args.storage_key || "";
        const action = args.storage_action || "";
        const actionId = args.storage_action_id || "";
        const participantId = args.participant_id || "";
        if (args.hidden) document.body.style.display = "none";

        // Beim ersten Render zuerst aus dem persistenten Speicher lesen.
        // Python darf den Entwurf vor der Wiederherstellung nicht überschreiben.
        if (firstRender || key !== autosaveKey) {
            firstRender = false;
            autosaveKey = key;
            lastParticipantId = participantId;
            lastStorageAction = actionId;
            autosaveReady = false;
            shouldSaveInitial = true;

            if (action === "clear" || action === "reset") {
                clearStoredDraft(key);
            } else if (key) {
                const raw = await loadStoredDraft(key);
                if (raw) {
                    try {
                        const saved = JSON.parse(raw);
                        if (saved.version === 1 && Array.isArray(saved.board)
                            && saved.confidence && typeof saved.confidence === "object") {
                            autosaveReady = true;
                            shouldSaveInitial = false;
                            // Gespeicherte Karten sofort anzeigen; Python
                            // übernimmt den Zustand im folgenden Rerun.
                            confidenceValues = {...saved.confidence};
                            currentConfidenceJson = JSON.stringify(confidenceValues);
                            renderBoard(saved.board);
                            setComponentValue({
                                board: saved.board,
                                confidence: saved.confidence,
                                participant_id: typeof saved.participant_id === "string"
                                    ? saved.participant_id : "",
                                recovered: true
                            });
                            return;
                        }
                    } catch (error) {
                        console.warn("Zwischenstand kann nicht gelesen werden", error);
                    }
                }
            }
            autosaveReady = true;
        } else if (actionId && actionId !== lastStorageAction) {
            lastStorageAction = actionId;
            if (action === "clear" || action === "reset") {
                clearStoredDraft(key);
            }
        }

        if (action === "clear") {
            boardElement.innerHTML = "";
            setFrameHeight(0);
            return;
        }

        if (lastParticipantId !== participantId) {
            lastParticipantId = participantId;
            // Das Eingabefeld wird unabhängig vom Karten-Board gesichert.
        }

        const board =
            args.board || [];
        const incomingConfidence = args.confidence || {};
        if (JSON.stringify(incomingConfidence) !== currentConfidenceJson) {
            confidenceValues = {...incomingConfidence};
            currentConfidenceJson = JSON.stringify(confidenceValues);
            updateButtons();
        }

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
        // Auch ohne verschobene Karten einen Entwurf für die ID anlegen.
        if (shouldSaveInitial) {
            shouldSaveInitial = false;
            saveDraft();
        }
    }


    // Streamlit kann unmittelbar nach dem Laden mehrfach render senden.
    // Sequenzielle Verarbeitung verhindert, dass ein neuer Render die
    // noch laufende Wiederherstellung überholt.
    let renderQueue = Promise.resolve();
    window.addEventListener("message", (event) => {
        if (!event.data || event.data.type !== "streamlit:render") return;
        renderQueue = renderQueue.then(() => onDataFromPython(event)).catch(error => {
            console.error("Fehler beim Wiederherstellen des Kartenstands", error);
        });
    });


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


def render_autosave_storage_bridge():
    """Tabgebundener Autosave über sessionStorage der Streamlit-Hauptseite."""
    st.html(r"""
<script>
(() => {
    const key = __DRAFT_KEY__;
    // Beim Fertigkeitswechsel den alten Listener entfernen, nicht stapeln.
    if (window.__kompchinTabStorageKey === key) return;
    if (window.__kompchinTabStorageAbort) window.__kompchinTabStorageAbort.abort();
    const controller = new AbortController();
    window.__kompchinTabStorageAbort = controller;
    window.__kompchinTabStorageKey = key;

    // Änderungen an der KompChin-ID unmittelbar sichern, nicht erst bei Enter.
    document.addEventListener("input", (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement) ||
            input.getAttribute("aria-label") !== "KompChin-ID") return;
        try {
            const raw = sessionStorage.getItem(key);
            if (!raw) return;
            const draft = JSON.parse(raw);
            if (draft.version !== 1) return;
            draft.participant_id = input.value;
            sessionStorage.setItem(key, JSON.stringify(draft));
        } catch (error) { console.warn("ID-Zwischenspeicherung fehlgeschlagen", error); }
    }, {capture: true, signal: controller.signal});

    window.addEventListener("message", (event) => {
        const data = event.data;
        if (!data || data.type !== "kompchin:tab-storage" || data.key !== key ||
            !event.source || event.source === window) return;
        const isChild = Array.from(document.querySelectorAll("iframe"))
            .some(frame => frame.contentWindow === event.source);
        if (!isChild) return;
        let value = null;
        let success = true;
        try {
            if (data.action === "get") {
                value = sessionStorage.getItem(key);
            } else if (data.action === "set" && typeof data.value === "string") {
                // Auf ein verzögertes Board-Update darf keine alte ID folgen.
                const incoming = JSON.parse(data.value);
                const existing = sessionStorage.getItem(key);
                if (existing) {
                    const old = JSON.parse(existing);
                    if (old.version === 1 && typeof old.participant_id === "string") {
                        incoming.participant_id = old.participant_id;
                    }
                }
                sessionStorage.setItem(key, JSON.stringify(incoming));
            } else if (data.action === "remove") {
                sessionStorage.removeItem(key);
            } else return;
        } catch (error) { success = false; }
        event.source.postMessage({
            type: "kompchin:tab-storage-response",
            requestId: data.requestId, value, success
        }, event.origin === "null" ? "*" : event.origin);
    }, {signal: controller.signal});
})();
</script>
""".replace("__DRAFT_KEY__", json.dumps(AUTOSAVE_KEY)), unsafe_allow_javascript=True)


def card_sorter(board, key, storage_action="", hidden=False):
    return _card_sorter_component(
        board=board,
        confidence=st.session_state.get("confidence", {}),
        participant_id=st.session_state.get("participant_id", ""),
        storage_key=AUTOSAVE_KEY,
        storage_action=storage_action,
        storage_action_id=st.session_state.get("storage_action_id", ""),
        hidden=hidden,
        key=key,
        default={"board": board, "confidence": st.session_state.get("confidence", {})},
    )

# -------------------------------------------------------------------
# Streamlit-App
# -------------------------------------------------------------------

admin_open = (
    st.session_state.get("admin_granted", False)
    and st.session_state.get("admin_dashboard_open", False)
)

st.set_page_config(
    page_title=(
        f"Admin-Dashboard {APP_PROFICIENCY}"
        if admin_open and APP_PROFICIENCY
        else (f"Karteikarten-Zuordnung {APP_PROFICIENCY}"
              if APP_PROFICIENCY else "Karteikarten-Zuordnung")
    ),
    page_icon="📊" if admin_open else "🗂️",
    layout="wide",
    initial_sidebar_state="collapsed",
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
        
    # Automatischer Zugang über speziellen Link
    link_token = st.query_params.get("access_token", "")
    correct_token = str(st.secrets.get("ACCESS_LINK_TOKEN", ""))

    if (
        link_token
        and correct_token
        and hmac.compare_digest(link_token, correct_token)
    ):
        st.session_state["access_granted"] = True
        del st.query_params["access_token"]
        st.rerun()
        
    _, mitte, _ = st.columns([1, 1.5, 1])
    with mitte:
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
                use_container_width=True,
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


def require_proficiency_selection():
    """Zweite Seite nach dem Login: Auswahl der Fertigkeit aus Secrets."""
    global APP_PROFICIENCY
    raw_options = st.secrets.get("PROFICIENCIES", [])
    if not isinstance(raw_options, (list, tuple)):
        st.error("PROFICIENCIES muss in Secrets eine Liste sein.")
        st.stop()
    options = [str(value).strip() for value in raw_options]
    if not options or any(not value for value in options) or len(set(options)) != len(options):
        st.error("Bitte eindeutige, nichtleere Fertigkeiten in PROFICIENCIES konfigurieren.")
        st.stop()

    current = st.session_state.get("selected_proficiency")
    if current in options:
        APP_PROFICIENCY = current
        return
    st.session_state.pop("selected_proficiency", None)
    APP_PROFICIENCY = None

    _, mitte, _ = st.columns([1, 1.5, 1])
    with mitte:
        st.title("Fertigkeit auswählen")
        st.write("Bitte wählen Sie die Fertigkeit aus, die Sie bearbeiten möchten.")
        st.caption("Verfügbare Fertigkeiten")

        for option in options:
            if st.button(option, key=f"proficiency_button_{option}", use_container_width=True):
                st.session_state["selected_proficiency"] = option
                APP_PROFICIENCY = option
                st.rerun()

    st.stop()


def back_to_proficiency_selection():
    """Python-Arbeitszustand trennen; Browser-Entwürfe sind je Fertigkeit isoliert."""
    for state_key in (
        "selected_proficiency", "board", "card_order", "confidence",
        "sorter_generation", "submitted", "participant_id", "pending_restored_id",
        "storage_action", "storage_action_id", "admin_dashboard_open",
    ):
        st.session_state.pop(state_key, None)


require_access_code()
require_proficiency_selection()
init_db()

# Kartenstammdaten aus Supabase laden.
CARDS, CARD_METADATA = load_cards(APP_PROFICIENCY)

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

# Der Schlüssel trennt Kompetenzen und verschiedene Kartenversionen.
# IDs statt Antwortinhalte für den Fingerprint verwenden.
card_fingerprint = hashlib.sha256(
    json.dumps(sorted((card_id, CARDS[card_id]) for card_id in CARDS),
               ensure_ascii=False).encode("utf-8")
).hexdigest()[:16]
AUTOSAVE_KEY = f"kompchin_tab_draft_v1_{APP_PROFICIENCY}_{card_fingerprint}"

# Wiederhergestellte ID vor dem Erstellen des Text-Widgets setzen.
if "pending_restored_id" in st.session_state:
    st.session_state["participant_id"] = st.session_state.pop("pending_restored_id")

if "confidence" not in st.session_state:
    st.session_state.confidence = {}

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
    st.session_state.confidence = {}
    st.session_state.sorter_generation += 1

if "submitted" not in st.session_state:
    st.session_state.submitted = False


def start_new_entry():
    # Neue Person am gleichen Browser -> neue zufällige Kartenreihenfolge.
    st.session_state.pop("card_order", None)
    st.session_state.board = initial_board()
    st.session_state.confidence = {}
    st.session_state.sorter_generation += 1
    st.session_state.submitted = False
    st.session_state.participant_id = ""
    st.session_state.storage_action = "reset"
    st.session_state.storage_action_id = str(uuid.uuid4())


st.title(f"Karteikarten-Zuordnung {APP_PROFICIENCY}")
st.button(
    "← Zurück zur Fertigkeiten-Auswahl",
    on_click=back_to_proficiency_selection,
    key="back_to_proficiency_selection",
)
st.write(
    "Ordnen Sie die Deskriptoren dem Kompetenzniveau zu, "
    "in dem eine minimal kompetente Leserin bzw. ein minimal kompetenter Leser "
    "die beschriebene Leseleistung ohne Unterstützung zuverlässig erbringt. "
    f"Anzahl der Karten: **{len(CARDS)} Karten**, "
    "Niveaus: **vor A1, A1, A2, B1, B2, C1 oder C2**."
)

# Der Sitzungsspeicher-Listener muss vor der iframe-Komponente bereitstehen.
render_autosave_storage_bridge()

participant_id = st.text_input(
    "KompChin-ID",
    key="participant_id",
    placeholder="Bitte ID angeben",
    help=(
        "Bitte keine Namen eingeben, "
        "wenn eine pseudonyme ID ausreicht."
    ),
    disabled=st.session_state.submitted,
)

if st.session_state.submitted:
    # Erst nach erfolgreicher Datenbank-Transaktion löschen.
    card_sorter(
        st.session_state.board,
        key=f"autosave_clear_{APP_PROFICIENCY}_{st.session_state.sorter_generation}",
        storage_action="clear",
        hidden=True,
    )
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


st.markdown(
    """
    <div style="font-size: 0.85rem; color: gray; line-height: 1.8;">
        Ziehen Sie die Karten in die gewünschte Niveauspalte.
        Klicken Sie auf
        <span style="
            display: inline-block;
            color: #16a34a;
            border: 1px solid #16a34a;
            border-radius: 4px;
            padding: 0px 5px;
            font-weight: bold;
            line-height: 1.4;
        ">!</span>,
        wenn Sie sich bei Ihrer Einstufung sicher sind,
        oder auf
        <span style="
            display: inline-block;
            color: #dc2626;
            border: 1px solid #dc2626;
            border-radius: 4px;
            padding: 0px 5px;
            font-weight: bold;
            line-height: 1.4;
        ">?</span>,
        wenn Sie sich unsicher sind.
        <br>
        Die Kartenreihenfolge wurde für diese Sitzung zufällig gemischt.
        Die Nummerierung der Karten entspricht <b>nicht</b>
        der tatsächlichen Reihenfolge.
    </div>
    """,
    unsafe_allow_html=True,
)


component_result = card_sorter(
    st.session_state.board,
    key=(
        "card_sorter_"
        f"{APP_PROFICIENCY}_{st.session_state.sorter_generation}"
    ),
    storage_action=st.session_state.get("storage_action", ""),
)
# Eine einmalige Reset-Anweisung wird nur an die neue Komponente geschickt.
st.session_state.storage_action = ""

if isinstance(component_result, dict):
    incoming_board = component_result.get("board")
    incoming_confidence = component_result.get("confidence", {})
    if (is_safe_board_payload(incoming_board)
            and isinstance(incoming_confidence, dict)
            and all(k in LABEL_TO_ID and type(v) is bool for k, v in incoming_confidence.items())):
        recovered_id = component_result.get("participant_id") if component_result.get("recovered") else None
        if isinstance(recovered_id, str) and len(recovered_id) <= 100:
            # Nur eine leere ID initial befüllen. Ein aktueller Eingabewert
            # darf nie durch einen älteren Entwurf überschrieben werden.
            if not st.session_state.get("participant_id", "").strip():
                st.session_state.pending_restored_id = recovered_id
        if (incoming_board != st.session_state.board
                or incoming_confidence != st.session_state.confidence
                or "pending_restored_id" in st.session_state):
            st.session_state.board = incoming_board
            st.session_state.confidence = incoming_confidence
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
    st.session_state.confidence = {}
    st.session_state.sorter_generation += 1
    st.session_state.storage_action = "reset"
    st.session_state.storage_action_id = str(uuid.uuid4())
    st.rerun()

if submit:
    clean_participant_id = participant_id.strip()

    if not clean_participant_id:
        st.error("Bitte geben Sie zuerst eine Teilnehmer:innen-ID ein.")
    elif len(clean_participant_id) > 100:
        st.error("Die Teilnehmer:innen-ID darf höchstens 100 Zeichen lang sein.")
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
                    {LABEL_TO_ID[k]: v for k, v in st.session_state.confidence.items()},
                )
            except psycopg.errors.UniqueViolation:
                st.error(
                    "Diese Teilnehmer:innen-ID wurde bereits verwendet. "
                    "Bitte prüfen Sie die ID oder verwenden Sie eine andere."
                )
            except ValueError as exc:
                st.error(str(exc))
            except psycopg.Error as exc:
                print("PostgreSQL-Datenbankfehler:", repr(exc))
                st.error(
                    "Beim Speichern ist ein Datenbankfehler aufgetreten. "
                    "Bitte versuchen Sie es erneut."
                )
            else:
                st.session_state.submitted = True
                st.rerun()
