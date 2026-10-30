"""Streamlit: deskriptiver Vergleich der Runden für verschiedene Fertigkeiten.

Start: streamlit run rundenvergleich_app.py
Secrets: DATABASE_URL, ADMIN_CODE, APP_PROFICIENCY = "gewünschte_Fertigkeit".
Optional kann VERGLEICH_PROFICIENCY den Wert für diese App überschreiben.
Die zweite Runde heißt automatisch "Lesen Runde 2", "Hören Runde 2" usw.
Die App liest die vorhandenen Tabellen nur aus; sie schreibt keine Daten in die Datenbank.

Optionale tabgetrennte Gruppen-CSV/TSV (UTF-8), eine Zeile pro Mitglied:
    group_id\tparticipant_id
    G01, Teilgruppe; A\tP001
    G01, Teilgruppe; A\tP002
    G02\tP003

'group_id' ist die in Runde 2 eingegebene Teilnehmer:innen-ID (Gruppen-ID).
'participant_id' ist die individuelle Teilnehmer:innen-ID aus Runde 1.
"""

from __future__ import annotations

import hmac
import html
import io
from collections import Counter

import altair as alt
import pandas as pd
import psycopg
import streamlit as st


# Hier dieselbe Konfiguration wie in der Haupt-App nutzen.
# VERGLEICH_PROFICIENCY ist optional und hat Vorrang, wenn man die
# Vergleichs-App unabhängig von der Haupt-App konfigurieren möchte.
BASE_PROFICIENCY = str(
    st.secrets.get(
        "VERGLEICH_PROFICIENCY", st.secrets.get("APP_PROFICIENCY", "Lesen")
    )
).strip()
ROUND_1 = BASE_PROFICIENCY
ROUND_2 = f"{BASE_PROFICIENCY} Runde 2"
DOWNLOAD_PREFIX = {"Lesen": "lesen", "Hören": "hoeren", "Schreiben": "schreiben"}.get(
    BASE_PROFICIENCY, "rundenvergleich"
)
# Verbindliche Reihenfolge in ALLEN Auswertungen und Diagrammen.
LEVELS = ["Pre-A1", "A1", "A2", "B1", "B2"]
POSITION = {level: index for index, level in enumerate(LEVELS)}

# Kurzer Hilfetext pro Tabellenkopf. Die Texte erscheinen beim Hover
# über den Spaltenüberschriften (Streamlit column_config.help).
CARD_COLUMN_HELP = {
    "Karte": "Gemeinsame Karten-ID aus Runde 1; in Runde 2 wird die vorangestellte 2 entfernt.",
    "Text": "Wortlaut des Deskriptors aus Runde 1. Der Text ist in beiden Runden gleich.",
    "Kategorie": "Inhaltliche Kategorie der Karte aus den Stammdaten.",
    "Referenzniveau": "Vorab hinterlegtes Niveau der Karte (Pre-A1 bis B2).",
    "Einzelurteile R1": "Anzahl individueller Einstufungen dieser Karte in Runde 1.",
    "Gruppenurteile R2": "Anzahl gemeinsamer Gruppenentscheidungen dieser Karte in Runde 2.",
    "Modalniveau R1": "Am häufigsten gewähltes Niveau in Runde 1; Gleichstände werden gemeinsam angezeigt.",
    "Modalniveau R2": "Am häufigsten gewähltes Niveau in Runde 2; Gleichstände werden gemeinsam angezeigt.",
    "Modalanteil R1 %": "Anteil der häufigsten Einstufung an allen Einzelurteilen in Runde 1.",
    "Modalanteil R2 %": "Anteil der häufigsten Einstufung an allen Gruppenurteilen in Runde 2.",
    "Medianposition R1": "Median der Einzelurteile auf der Skala Pre-A1=0 bis B2=4.",
    "Medianposition R2": "Median der Gruppenurteile auf der Skala Pre-A1=0 bis B2=4.",
    "Δ Medianposition (Stufen)": "Medianposition R2 minus R1; positiv = höhere Gruppeneinstufungen.",
    "Referenztreffer R1 %": "Anteil der Einzelurteile, die genau dem Referenzniveau entsprechen.",
    "Referenztreffer R2 %": "Anteil der Gruppenurteile, die genau dem Referenzniveau entsprechen.",
    "Δ Referenztreffer (PP)": "Referenztreffer R2 minus R1 in Prozentpunkten (PP).",
    "Ø Referenzabstand R1": "Mittlerer absoluter Abstand der Einzelurteile zum Referenzniveau in Stufen.",
    "Ø Referenzabstand R2": "Mittlerer absoluter Abstand der Gruppenurteile zum Referenzniveau in Stufen.",
    "Δ Ø Referenzabstand": "Mittlerer Referenzabstand R2 minus R1; negativ = näher an der Referenz.",
}

FREQUENCY_COLUMN_HELP = {
    "Niveau": "Mögliche Niveaustufe der Karte von Pre-A1 bis B2.",
    "R1: Anzahl": "Wie viele Einzelpersonen diese Stufe in Runde 1 gewählt haben.",
    "R2: Anzahl": "Wie viele Gruppen diese Stufe in Runde 2 gewählt haben.",
}

GROUP_COLUMN_HELP = {
    "Gruppe": "Gruppenkennung aus Runde 2 (dort als Teilnehmer:innen-ID verwendet).",
    "Karte": "Gemeinsame Karten-ID für beide Erhebungsrunden.",
    "Mitglieder laut Liste": "Anzahl der Personen, die in der hochgeladenen Gruppen-CSV dieser Gruppe zugeordnet sind.",
    "Einzelurteile R1 verfügbar": "Anzahl dieser Gruppenmitglieder mit einer vorhandenen Einzelbewertung für die Karte.",
    "Modalniveau der Mitglieder R1": "Häufigste frühere Einstufung unter den verfügbaren Einzelurteilen dieser Gruppe.",
    "Gruppenentscheidung R2": "Von der Gruppe gemeinsam vergebene Niveaustufe in Runde 2.",
    "Mitglieder mit gleicher Einstufung R1": "Wie viele Mitglieder hatten das spätere Gruppenniveau schon in Runde 1 gewählt?",
    "Anteil vorher gleicher Einzelurteile %": "Anteil dieser Mitglieder an den verfügbaren Einzelurteilen in Prozent.",
    "R2 entspricht einem R1-Modalniveau": "Ja, wenn die Gruppenentscheidung zu den häufigsten früheren Einstufungen der Mitglieder gehört.",
    "In R1 von keinem Mitglied gewählt": "Ja, wenn keines der verfügbaren Einzelurteile das spätere Gruppenniveau hatte.",
    "Ø Abstand der R1-Einzelurteile zum R2-Gruppenurteil": "Durchschnittlicher absoluter Niveauabstand der früheren Einzelurteile zur Gruppenentscheidung.",
}

GROUP_SUMMARY_HELP = {
    "Gruppe": "Kennung der Gruppe aus Runde 2.",
    "Verglichene Karten": "Zahl der Karten mit einer Gruppenentscheidung und verfügbaren früheren Einzelurteilen.",
    "Ø Anteil vorher gleicher Einzelurteile %": "Mittlerer Anteil der Mitglieder, die das spätere Gruppenniveau vorher gewählt hatten.",
    "Entscheidungen, die zuvor kein Mitglied wählte": "Zahl der Karten, bei denen die Gruppe ein von keinem Mitglied vorher gewähltes Niveau vergab.",
}

QUALITY_COLUMN_HELP = {
    "basis_id": "Gemeinsame Karten-ID nach Entfernen der führenden 2 aus Runde 2.",
    "card_text_r1": "Gespeicherter Kartentext in Runde 1.",
    "card_text_r2": "Gespeicherter Kartentext in Runde 2.",
    "reference_level_r1": "Hinterlegtes Referenzniveau in Runde 1.",
    "reference_level_r2": "Hinterlegtes Referenzniveau in Runde 2.",
}


def column_tooltips(dataframe, explanations, *, wide_text=()):
    """Spalten passend zum Datentyp mit Tooltip am Tabellenkopf darstellen."""
    result = {}
    for name in dataframe.columns:
        help_text = explanations.get(name, "Spalte der Auswertung.")
        if pd.api.types.is_bool_dtype(dataframe[name]):
            result[name] = st.column_config.CheckboxColumn(name, help=help_text)
        elif pd.api.types.is_integer_dtype(dataframe[name]):
            result[name] = st.column_config.NumberColumn(
                name, help=help_text, format="%d"
            )
        elif pd.api.types.is_numeric_dtype(dataframe[name]):
            result[name] = st.column_config.NumberColumn(
                name, help=help_text, format="%.2f"
            )
        else:
            result[name] = st.column_config.TextColumn(
                name, help=help_text,
                width="large" if name in wide_text else None,
            )
    return result


def card_table_height(number_of_rows):
    """Alle Karten ohne vertikalen Scrollbereich; kompatibel mit älterem Streamlit."""
    try:
        version = tuple(int(part) for part in st.__version__.split(".")[:2])
    except (AttributeError, ValueError):
        version = (0, 0)
    # Ab Streamlit 1.52 passt sich die Höhe dem Tabelleninhalt selbst an.
    if version >= (1, 52):
        return "content"
    return max(150, 60 + number_of_rows * 36)


st.set_page_config(
    page_title=f"{BASE_PROFICIENCY} – Rundenvergleich",
    page_icon="📊",
    layout="wide",
)


def require_admin():
    """Eigene Admin-Anmeldung; kein öffentlicher Datenzugriff."""
    if st.session_state.get("rundenvergleich_admin", False):
        with st.sidebar:
            if st.button("Admin abmelden"):
                st.session_state["rundenvergleich_admin"] = False
                st.rerun()
        return

    st.title(f"🔒 {BASE_PROFICIENCY} – Rundenvergleich")
    st.write("Bitte zuerst mit dem Admin-Code anmelden.")
    with st.form("rundenvergleich_login"):
        entered = st.text_input("Admin-Code", type="password")
        submitted = st.form_submit_button("Anmelden")

    if submitted:
        correct = str(st.secrets.get("ADMIN_CODE", ""))
        if correct and hmac.compare_digest(entered.strip(), correct):
            st.session_state["rundenvergleich_admin"] = True
            st.rerun()
        st.error("Admin-Code ungültig oder nicht konfiguriert.")
    st.stop()


@st.cache_data(ttl=60)
def load_from_db():
    """Zwei SELECTs, keine Änderungen an Schema oder Bestandsdaten."""
    with psycopg.connect(
        str(st.secrets["DATABASE_URL"]),
        sslmode="require",
        connect_timeout=10,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT card_id, proficiency, card_text, reference_level,
                       item_category, sort_order
                  FROM public.cards
                 WHERE proficiency IN (%s, %s)
                 ORDER BY proficiency, sort_order
                """,
                (ROUND_1, ROUND_2),
            )
            cards_rows = cur.fetchall()

            cur.execute(
                """
                SELECT s.participant_id, s.proficiency, a.card_id,
                       a.level, a.confidence
                  FROM public.submissions AS s
                  JOIN public.assignments AS a
                    ON a.submission_id = s.submission_id
                  JOIN public.cards AS c
                    ON c.card_id = a.card_id
                   AND c.proficiency = s.proficiency
                 WHERE s.proficiency IN (%s, %s)
                """,
                (ROUND_1, ROUND_2),
            )
            rating_rows = cur.fetchall()

    cards = pd.DataFrame(
        cards_rows,
        columns=[
            "card_id", "proficiency", "card_text", "reference_level",
            "item_category", "sort_order",
        ],
    )
    ratings = pd.DataFrame(
        rating_rows,
        columns=["participant_id", "proficiency", "card_id", "level", "confidence"],
    )
    return cards, ratings


def normalize_cards(cards):
    """Nur bei Runde-2-Karten genau EINE führende '2' entfernen."""
    df = cards.copy()
    df["basis_id"] = df["card_id"]
    second = df["proficiency"].eq(ROUND_2)
    valid_prefix = df["card_id"].str.startswith("2") & df["card_id"].str.len().gt(1)
    df.loc[second & valid_prefix, "basis_id"] = df.loc[
        second & valid_prefix, "card_id"
    ].str[1:]
    df.loc[second & ~valid_prefix, "basis_id"] = None
    return df


def norm_text(value):
    return " ".join(str(value).split()).casefold()


def assemble_data(cards, ratings):
    cards = normalize_cards(cards)
    r1_cards = cards[cards["proficiency"] == ROUND_1]
    r2_cards = cards[cards["proficiency"] == ROUND_2]

    pairs = r1_cards.merge(
        r2_cards,
        on="basis_id",
        how="inner",
        suffixes=("_r1", "_r2"),
        validate="one_to_one",
    )
    pairs["referenz_gleich"] = (
        pairs["reference_level_r1"] == pairs["reference_level_r2"]
    )
    pairs["text_gleich"] = pairs.apply(
        lambda row: norm_text(row["card_text_r1"]) == norm_text(row["card_text_r2"]),
        axis=1,
    )
    good_pairs = pairs[pairs["referenz_gleich"] & pairs["text_gleich"]].copy()

    ratings = ratings.merge(
        cards[["card_id", "proficiency", "basis_id"]],
        on=["card_id", "proficiency"],
        how="inner",
        validate="many_to_one",
    )
    r1 = ratings[ratings["proficiency"].eq(ROUND_1)].copy()
    r2 = ratings[ratings["proficiency"].eq(ROUND_2)].copy()

    return r1_cards, r2_cards, pairs, good_pairs, r1, r2


def level_counts(ratings):
    return ratings["level"].value_counts().reindex(LEVELS, fill_value=0)


def level_summary(ratings, reference):
    counts = level_counts(ratings)
    number = int(counts.sum())
    if number == 0:
        return {
            "n": 0, "mode": "—", "mode_share": None,
            "median": None, "reference_hit": None, "reference_distance": None,
        }

    highest = int(counts.max())
    modes = [level for level in LEVELS if counts[level] == highest]
    ordered = sorted(POSITION[level] for level in ratings["level"] if level in POSITION)
    middle = len(ordered) // 2
    median = (
        float(ordered[middle])
        if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    distance = sum(
        counts[level] * abs(POSITION[level] - POSITION[reference])
        for level in LEVELS
    ) / number
    return {
        "n": number,
        "mode": " / ".join(modes),
        "mode_share": 100.0 * highest / number,
        "median": median,
        "reference_hit": 100.0 * int(counts[reference]) / number,
        "reference_distance": distance,
    }


def r2_to_r1_compare(good_pairs, r1, r2):
    result = []
    for _, card in good_pairs.sort_values("sort_order_r1").iterrows():
        basis_id = card["basis_id"]
        ref = card["reference_level_r1"]
        a = level_summary(r1[r1["basis_id"].eq(basis_id)], ref)
        b = level_summary(r2[r2["basis_id"].eq(basis_id)], ref)

        def difference(left, right):
            return None if left is None or right is None else right - left

        result.append({
            "Karte": basis_id,
            "Text": card["card_text_r1"],
            "Kategorie": card["item_category_r1"],
            "Referenzniveau": ref,
            "Einzelurteile R1": a["n"],
            "Gruppenurteile R2": b["n"],
            "Modalniveau R1": a["mode"],
            "Modalniveau R2": b["mode"],
            "Modalanteil R1 %": a["mode_share"],
            "Modalanteil R2 %": b["mode_share"],
            "Medianposition R1": a["median"],
            "Medianposition R2": b["median"],
            "Δ Medianposition (Stufen)": difference(a["median"], b["median"]),
            "Referenztreffer R1 %": a["reference_hit"],
            "Referenztreffer R2 %": b["reference_hit"],
            "Δ Referenztreffer (PP)": difference(a["reference_hit"], b["reference_hit"]),
            "Ø Referenzabstand R1": a["reference_distance"],
            "Ø Referenzabstand R2": b["reference_distance"],
            "Δ Ø Referenzabstand": difference(
                a["reference_distance"], b["reference_distance"]
            ),
        })

    result_df = pd.DataFrame(result)
    if not result_df.empty:
        number_cols = result_df.select_dtypes(include="number").columns
        result_df[number_cols] = result_df[number_cols].round(2)
    return result_df


def load_membership(file):
    try:
        # Explizit TAB als Trennzeichen verwenden: Kommata und Semikolons
        # sind reguläre Bestandteile von Gruppenkennungen.
        membership = pd.read_csv(
            io.BytesIO(file.getvalue()),
            sep="\t",
            encoding="utf-8-sig",
            dtype=str,
            keep_default_na=False,
        )
    except (ValueError, pd.errors.ParserError, UnicodeError) as exc:
        raise ValueError("Die tabgetrennte Gruppen-CSV/TSV konnte nicht eingelesen werden.") from exc
    membership.columns = [str(x).strip().lstrip("\ufeff") for x in membership.columns]
    if not {"group_id", "participant_id"}.issubset(membership.columns):
        raise ValueError("Erforderliche tabgetrennte Spalten: group_id und participant_id (getrennt durch TAB).")
    membership = membership[["group_id", "participant_id"]].dropna().copy()
    for col in ("group_id", "participant_id"):
        membership[col] = membership[col].astype(str).str.strip()
    membership = membership[
        membership["group_id"].ne("") & membership["participant_id"].ne("")
    ].drop_duplicates()
    if membership.empty:
        raise ValueError("Die Gruppen-CSV enthält keine gültigen Zuordnungen.")
    return membership


def build_group_comparison(membership, r1, r2, allowed_ids):
    """R2-Gruppenentscheidung vs. deren MITGLIEDER-Einzelurteile aus R1.

    Es wird ausdrücklich KEINE individuelle Wiederholungsbewertung konstruiert.
    """
    filtered_r1 = r1[r1["basis_id"].isin(allowed_ids)]
    members_r1 = membership.merge(
        filtered_r1[["participant_id", "basis_id", "level"]],
        on="participant_id",
        how="inner",
    )
    grouped_prior = {
        (g, c): df["level"].tolist()
        for (g, c), df in members_r1.groupby(["group_id", "basis_id"])
    }
    count_members = membership.groupby("group_id")["participant_id"].nunique().to_dict()
    result = []
    for row in r2[r2["basis_id"].isin(allowed_ids)].itertuples(index=False):
        group_id = str(row.participant_id)
        if group_id not in count_members:
            continue
        prior_levels = grouped_prior.get((group_id, row.basis_id), [])
        if not prior_levels:
            continue

        counts = Counter(prior_levels)
        modal = [level for level in LEVELS if counts[level] == max(counts.values())]
        count_same = counts[row.level]
        result.append({
            "Gruppe": group_id,
            "Karte": row.basis_id,
            "Mitglieder laut Liste": count_members[group_id],
            "Einzelurteile R1 verfügbar": len(prior_levels),
            "Modalniveau der Mitglieder R1": " / ".join(modal),
            "Gruppenentscheidung R2": row.level,
            "Mitglieder mit gleicher Einstufung R1": count_same,
            "Anteil vorher gleicher Einzelurteile %": round(
                100 * count_same / len(prior_levels), 1
            ),
            "R2 entspricht einem R1-Modalniveau": row.level in modal,
            "In R1 von keinem Mitglied gewählt": count_same == 0,
            "Ø Abstand der R1-Einzelurteile zum R2-Gruppenurteil": round(
                sum(abs(POSITION[level] - POSITION[row.level]) for level in prior_levels)
                / len(prior_levels), 2,
            ),
        })
    return pd.DataFrame(result)


def download_csv(label, dataframe, filename, *, key):
    st.download_button(
        label,
        data=dataframe.to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        key=key,
    )


def show_quality(r1_cards, r2_cards, pairs, r1, r2):
    all_1 = set(r1_cards["basis_id"].dropna())
    all_2 = set(r2_cards["basis_id"].dropna())
    st.write(f"**Karten in Runde 1:** {len(r1_cards)}")
    st.write(f"**Karten in Runde 2:** {len(r2_cards)}")
    st.write(f"**Karten mit zuordenbarer ID:** {len(pairs)}")
    st.write(f"**Nur in Runde 1:** {len(all_1 - all_2)} (bei Teilstichprobe erwartet)")
    st.write(f"**Nur in Runde 2:** {len(all_2 - all_1)}")
    st.write(f"**Einzel-IDs mit Abgaben R1:** {r1['participant_id'].nunique()}")
    st.write(f"**Gruppen-IDs mit Abgaben R2:** {r2['participant_id'].nunique()}")

    invalid_prefix = r2_cards[r2_cards["basis_id"].isna()]
    if not invalid_prefix.empty:
        st.error("Runde-2-Karten ohne führende '2': " + ", ".join(invalid_prefix["card_id"]))
    if all_2 - all_1:
        st.warning("Runde-2-Karten ohne R1-Pendant: " + ", ".join(sorted(all_2 - all_1)))
    mismatches = pairs[~(pairs["referenz_gleich"] & pairs["text_gleich"])]
    if not mismatches.empty:
        st.error(
            "Unterschiedliche Texte oder Referenzniveaus zwischen R1 und R2. "
            "Diese Karten sind vom Vergleich ausgeschlossen."
        )
        st.dataframe(
            mismatches[["basis_id", "card_text_r1", "card_text_r2",
                        "reference_level_r1", "reference_level_r2"]],
            hide_index=True, use_container_width=True,
            column_config=column_tooltips(
                mismatches[["basis_id", "card_text_r1", "card_text_r2",
                            "reference_level_r1", "reference_level_r2"]],
                QUALITY_COLUMN_HELP,
                wide_text=("card_text_r1", "card_text_r2"),
            ),
        )
    if r2.empty:
        st.info("Es liegen noch keine Abgaben für Runde 2 vor.")


def main():
    if BASE_PROFICIENCY not in {"Lesen", "Hören", "Schreiben"}:
        st.error(
            f"Ungültige Kompetenz {BASE_PROFICIENCY!r}. "
            "Bitte APP_PROFICIENCY (oder VERGLEICH_PROFICIENCY) "
            "auf 'Lesen', 'Hören' oder 'Schreiben' setzen."
        )
        st.stop()

    require_admin()
    st.title(f"📊 {BASE_PROFICIENCY} – Vergleich von Runde 1 und Runde 2")
    st.caption(
        "Runde 1 = individuelle Bewertungen; Runde 2 = gemeinsame Gruppenentscheidungen. "
        "Die Ergebnisse werden nur deskriptiv gegenübergestellt, nicht als "
        "individuelle Vorher-Nachher-Messungen interpretiert."
    )
    if st.button("Daten neu laden"):
        load_from_db.clear()
        st.rerun()

    try:
        cards, ratings = load_from_db()
    except (psycopg.Error, KeyError, ValueError) as exc:
        print("Datenbankfehler im Rundenvergleich:", repr(exc))
        st.error("Daten konnten nicht geladen werden. Bitte Zugang und Datenbank prüfen.")
        st.stop()

    if cards.empty:
        st.info(f"Keine Karten für {ROUND_1!r} oder {ROUND_2!r} in public.cards gefunden.")
        st.stop()

    try:
        r1_cards, r2_cards, pairs, good_pairs, r1, r2 = assemble_data(cards, ratings)
        table = r2_to_r1_compare(good_pairs, r1, r2)
    except (ValueError, KeyError) as exc:
        st.error(f"Datenfehler bei der Zuordnung: {exc}")
        st.stop()

    tab_cards, tab_groups, tab_quality = st.tabs(
        ["🗂️ Kartenvergleich", "👥 Gruppenvergleich (optional)", "🔍 Datenprüfung"]
    )

    with tab_cards:
        if table.empty:
            st.warning("Keine vergleichbaren Karten gefunden. Bitte Datenprüfung ansehen.")
        else:
            complete = table[
                table["Einzelurteile R1"].gt(0) & table["Gruppenurteile R2"].gt(0)
            ]
            a, b, c = st.columns(3)
            a.metric("Gepaarte Karten-IDs", len(table))
            b.metric("Karten mit Urteilen in beiden Runden", len(complete))
            changed = int((complete["Modalniveau R1"] != complete["Modalniveau R2"]).sum())
            c.metric("Karten mit anderem Modalniveau", changed)
            st.caption(
                "Die letzte Kennzahl zählt auch Änderungen bei gleich häufigen Modalniveaus. "
                "Ein Vergleich der Übereinstimmungsprozentsätze ist wegen "
                "unterschiedlicher Bewertungseinheiten und Gruppenzahlen deskriptiv."
            )
            # Alle Kartenzeilen ohne internen vertikalen Scrollbereich.
            # Die vielen Kennzahl-Spalten benötigen ggf. horizontales Scrollen.
            st.dataframe(
                table,
                hide_index=True,
                use_container_width=True,
                height=card_table_height(len(table)),
                column_config=column_tooltips(
                    table, CARD_COLUMN_HELP, wide_text=("Text",)
                ),
            )
            download_csv(
                "📥 Kartenvergleich als CSV", table,
                f"{DOWNLOAD_PREFIX}_rundenvergleich_karten.csv", key="download_karten"
            )
            raw_export = pd.concat([r1, r2], ignore_index=True)
            raw_export["karte_in_beiden_runden_vergleichbar"] = (
                raw_export["basis_id"].isin(set(good_pairs["basis_id"]))
            )
            download_csv(
                "📥 Rohdaten beider Runden (mit gemeinsamer Karten-ID)",
                raw_export, f"{DOWNLOAD_PREFIX}_rundenvergleich_rohdaten.csv", key="download_rohdaten"
            )

            # Responsives Standard-Auswahlfeld: auf breiten Bildschirmen
            # möglichst vollständiger Text, auf schmalen abgeschnitten.
            # Keine CSS-Regeln für Umbrüche oder Überbreite.
            card_texts = table.set_index("Karte")["Text"].to_dict()
            selected_id = st.selectbox(
                "Karte für den Verteilungsvergleich wählen",
                table["Karte"].tolist(),
                format_func=lambda base: f"{base} – {card_texts[base]}",
                width="stretch",
                key="vergleich_karte",
            )
            # Kartentext und intendiertes Niveau gut lesbar in Schwarz zeigen.
            # html.escape verhindert, dass Datenbanktexte als HTML interpretiert werden.
            intended_level = table.loc[
                table["Karte"].eq(selected_id), "Referenzniveau"
            ].iloc[0]
            st.markdown(
                '<div style="background:#ffffff; color:#000000; '
                'padding:10px 14px; border-radius:6px; margin-bottom:12px;">'
                f'<div><strong>Kartentext:</strong> {html.escape(str(card_texts[selected_id]))}</div>'
                f'<div style="margin-top:5px;"><strong>Intendiertes Niveau:</strong> '
                f'{html.escape(str(intended_level))}</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            counts1 = level_counts(r1[r1["basis_id"].eq(selected_id)])
            counts2 = level_counts(r2[r2["basis_id"].eq(selected_id)])
            n1, n2 = int(counts1.sum()), int(counts2.sum())
            # Die Niveaus explizit ordnen: Eine automatische alphabetische
            # Sortierung würde Pre-A1 fälschlich hinter B2 setzen.
            frequencies = pd.DataFrame({
                "Niveau": LEVELS,
                "Einzelurteile R1 (%)": (
                    (100 * counts1 / n1).tolist() if n1 else [0.0] * len(LEVELS)
                ),
                "Gruppenurteile R2 (%)": (
                    (100 * counts2 / n2).tolist() if n2 else [0.0] * len(LEVELS)
                ),
            })
            frequency_long = frequencies.melt(
                id_vars="Niveau", var_name="Runde", value_name="Anteil (%)"
            )
            round_order = ["Einzelurteile R1 (%)", "Gruppenurteile R2 (%)"]
            chart = (
                alt.Chart(frequency_long)
                .mark_bar()
                .encode(
                    x=alt.X("Niveau:N", sort=LEVELS, title="Niveau"),
                    xOffset=alt.XOffset("Runde:N", sort=round_order),
                    y=alt.Y(
                        "Anteil (%):Q", title="Anteil der Bewertungen (%)",
                        scale=alt.Scale(domain=[0, 100]),
                    ),
                    color=alt.Color(
                        "Runde:N", sort=round_order, legend=alt.Legend(title="")
                    ),
                    tooltip=[
                        alt.Tooltip("Niveau:N", title="Niveau"),
                        alt.Tooltip("Runde:N", title="Runde"),
                        alt.Tooltip("Anteil (%):Q", title="Anteil (%)", format=".1f"),
                    ],
                )
                .properties(height=350)
            )
            st.write(f"**{selected_id}** – {n1} Einzelurteile / {n2} Gruppenurteile")
            st.altair_chart(chart, use_container_width=True)

            # Auch die Tabelle wird stets in der CEFR-Reihenfolge ausgegeben.
            frequencies_count = pd.DataFrame({
                "Niveau": LEVELS,
                "R1: Anzahl": counts1.reindex(LEVELS).tolist(),
                "R2: Anzahl": counts2.reindex(LEVELS).tolist(),
            })
            st.dataframe(
                frequencies_count,
                hide_index=True,
                use_container_width=True,
                column_config=column_tooltips(
                    frequencies_count, FREQUENCY_COLUMN_HELP
                ),
            )
            st.caption(
                "Medianposition: Pre-A1=0, A1=1, A2=2, B1=3, B2=4. "
                "Positive Δ-Werte stehen für tendenziell höhere Gruppeneinstufungen. "
                "Die Zahlen setzen für eine deskriptive Darstellung gleich große Niveauabstände voraus. "
                "Ein negativer Δ-Wert beim Referenzabstand bedeutet geringeren Abstand "
                "zum hinterlegten Referenzniveau."
            )

    with tab_groups:
        st.write(
            "Optional lässt sich jede Gruppenentscheidung aus Runde 2 mit den "
            "früheren Einzelurteilen **der Mitglieder genau dieser Gruppe** vergleichen. "
            "Dafür wird eine Zuordnungstabelle benötigt."
        )
        st.code(
            "group_id\tparticipant_id\n"
            "Gruppe A, Teil 1; Team Nord\tP001\n"
            "Gruppe A, Teil 1; Team Nord\tP002\n"
            "Gruppe B; Team Süd, 2\tP003",
            language="text",
        )
        st.caption(
            "group_id = in Runde 2 als Teilnehmer:innen-ID eingegebene Gruppenkennung; "
            "participant_id = individuelle Kennung aus Runde 1. "
            "Eine Zeile pro Gruppenmitglied; keine echten Namen nötig. "
            "Die zwei Spalten müssen durch einen Tabulator (TAB) getrennt sein; "
            "Kommata und Semikolons innerhalb der Gruppen-ID bleiben erhalten."
        )
        upload = st.file_uploader(
            "Gruppenzuordnung (tabgetrennte CSV/TSV) hochladen",
            type=["csv", "tsv", "txt"],
        )
        if upload is not None:
            try:
                membership = load_membership(upload)
                group_ids = set(r2["participant_id"].dropna())
                person_ids = set(r1["participant_id"].dropna())
                unknown_groups = sorted(set(membership["group_id"]) - group_ids)
                unknown_people = sorted(set(membership["participant_id"]) - person_ids)
                missing_groups = sorted(group_ids - set(membership["group_id"]))
                if unknown_groups:
                    st.warning("Gruppen ohne R2-Abgabe: " + ", ".join(unknown_groups))
                if unknown_people:
                    st.warning("Mitglieds-IDs ohne R1-Abgabe: " + ", ".join(unknown_people))
                if missing_groups:
                    st.warning("R2-Gruppen ohne Mitgliederliste: " + ", ".join(missing_groups))
                repeated = membership.groupby("participant_id")["group_id"].nunique()
                if repeated.gt(1).any():
                    st.warning("Mindestens eine Person ist mehreren Gruppen zugeordnet.")

                group_table = build_group_comparison(
                    membership, r1, r2, set(good_pairs["basis_id"])
                )
                if group_table.empty:
                    st.info("Keine passenden Gruppenentscheidungen und Einzelurteile gefunden.")
                else:
                    group_names = sorted(group_table["Gruppe"].unique().tolist())
                    chosen = st.selectbox("Gruppe anzeigen", ["Alle Gruppen", *group_names])
                    current = (
                        group_table if chosen == "Alle Gruppen"
                        else group_table[group_table["Gruppe"].eq(chosen)]
                    )
                    st.dataframe(
                        current,
                        hide_index=True,
                        use_container_width=True,
                        column_config=column_tooltips(current, GROUP_COLUMN_HELP),
                    )
                    download_csv(
                        "📥 Gruppenvergleich als CSV", group_table,
                        f"{DOWNLOAD_PREFIX}_rundenvergleich_gruppen.csv", key="download_gruppen"
                    )
                    summary = (
                        group_table.groupby("Gruppe", as_index=False)
                        .agg(
                            **{
                                "Verglichene Karten": ("Karte", "nunique"),
                                "Ø Anteil vorher gleicher Einzelurteile %": (
                                    "Anteil vorher gleicher Einzelurteile %", "mean"
                                ),
                                "Entscheidungen, die zuvor kein Mitglied wählte": (
                                    "In R1 von keinem Mitglied gewählt", "sum"
                                ),
                            }
                        )
                    )
                    summary["Ø Anteil vorher gleicher Einzelurteile %"] = (
                        summary["Ø Anteil vorher gleicher Einzelurteile %"].round(1)
                    )
                    st.subheader("Zusammenfassung nach Gruppen")
                    st.dataframe(
                        summary,
                        hide_index=True,
                        use_container_width=True,
                        column_config=column_tooltips(
                            summary, GROUP_SUMMARY_HELP
                        ),
                    )
                    st.caption(
                        "Die Prozentwerte verwenden pro Gruppenentscheidung nur die "
                        "verfügbaren Einzelurteile der zugeordneten Mitglieder. "
                        "Sie zeigen, ob die spätere Gruppenentscheidung bereits vor "
                        "der Diskussion vertreten wurde – nicht, welche Person "
                        "ihre Meinung geändert hat."
                    )
            except ValueError as exc:
                st.error(str(exc))

    with tab_quality:
        show_quality(r1_cards, r2_cards, pairs, r1, r2)
        st.caption(
            "Die Auswahl der Karten für Runde 2 ist möglicherweise gezielt und "
            "nicht repräsentativ. Deshalb beziehen sich die Vergleiche nur auf "
            "identische, in beiden Runden vorhandene Karten. Karten mit geändertem "
            "Wortlaut oder Referenzniveau werden ausgeschlossen."
        )


if __name__ == "__main__":
    main()
