import asyncio
import os
import sqlite3
import subprocess
from pathlib import Path

import pandas as pd
import psycopg
import requests
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from playwright.async_api import Error as PlaywrightError

from database import connect_db, init_db, using_supabase
from monitor import envoyer_notification_telegram, surveiller_une_fois

st.set_page_config(
    page_title="KBW B2 Monitor",
    page_icon="🎓",
    layout="centered",
)

st.title("🎓 KBW B2 Monitor")
st.caption("Surveillance des places d’examen et de rattrapage.")


@st.cache_data(ttl=30)
def charger_donnees() -> tuple[pd.DataFrame, pd.DataFrame]:
    if not using_supabase() and not Path(
        os.environ.get(
            "KBW_B2_MONITOR_DB",
            Path(__file__).resolve().parent / "data" / "examens_b2_monitor.db",
        )
    ).exists():
        return pd.DataFrame(), pd.DataFrame()

    with connect_db() as conn:
        sessions_cursor = conn.execute(
            """
            SELECT source, categorie, session_id, date_examen, heure,
                   disponibilite, quantityleft, observe_a
            FROM sessions
            ORDER BY date_examen, heure, categorie
            """
        )
        alertes_cursor = conn.execute(
            """
            SELECT categorie, date_examen, heure,
                   ancienne_disponibilite, nouvelle_disponibilite, detecte_a
            FROM alertes
            ORDER BY detecte_a DESC
            """
        )

        df_sessions = pd.DataFrame(
            sessions_cursor.fetchall(),
            columns=[
                column.name if hasattr(column, "name") else column[0]
                for column in sessions_cursor.description
            ],
        )
        df_alertes = pd.DataFrame(
            alertes_cursor.fetchall(),
            columns=[
                column.name if hasattr(column, "name") else column[0]
                for column in alertes_cursor.description
            ],
        )

    return df_sessions, df_alertes


def lire_secret(nom: str) -> str | None:
    valeur = os.environ.get(nom)
    if valeur:
        return valeur

    try:
        return st.secrets.get(nom)
    except StreamlitSecretNotFoundError:
        return None


telegram_token = lire_secret("TELEGRAM_BOT_TOKEN")
telegram_chat_id = lire_secret("TELEGRAM_CHAT_ID")
supabase_db_url = lire_secret("SUPABASE_DB_URL")
if supabase_db_url:
    os.environ["SUPABASE_DB_URL"] = supabase_db_url

try:
    if using_supabase():
        init_db()
    df_sessions, df_alertes = charger_donnees()
except (psycopg.Error, sqlite3.Error) as erreur:
    st.error(f"Impossible d’accéder à la base de données : {erreur}")
    st.stop()

with st.container():
    (
        col_sessions,
        col_disponibles,
        col_alertes,
        col_action,
        col_test,
    ) = st.columns(
        [1, 1, 1, 1.5, 1.5]
    )
    disponibles = (
        int((df_sessions["disponibilite"] == "disponible").sum())
        if not df_sessions.empty
        else 0
    )
    col_sessions.metric("Sessions", len(df_sessions))
    col_disponibles.metric("Disponibles", disponibles)
    col_alertes.metric("Alertes", len(df_alertes))
    verifier = col_action.button(
        "Vérifier maintenant",
        width="stretch",
        type="primary",
    )
    tester_telegram = col_test.button(
        "Tester Telegram",
        width="stretch",
        help="Envoie un exemple marqué TEST, sans modifier la base.",
    )

if not (telegram_token and telegram_chat_id):
    st.caption(
        "Notifications Telegram désactivées : configure "
        "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans les secrets."
    )

if verifier:
    with st.spinner("Vérification API et DOM en cours..."):
        try:
            df_nouvelles_alertes = asyncio.run(
                surveiller_une_fois(
                    telegram_token=telegram_token,
                    telegram_chat_id=telegram_chat_id,
                )
            )
            st.cache_data.clear()

            if df_nouvelles_alertes.empty:
                st.success("Aucun changement détecté.")
            else:
                places_liberees = (
                    df_nouvelles_alertes["nouvelle_disponibilite"] == "disponible"
                ).sum()
                st.success(
                    f"{int(places_liberees)} place(s) possiblement libérée(s)."
                    if places_liberees
                    else "Changement(s) détecté(s), aucune place libérée."
                )
        except (
            OSError,
            ValueError,
            KeyError,
            sqlite3.Error,
            psycopg.Error,
            subprocess.CalledProcessError,
            requests.RequestException,
            PlaywrightError,
            asyncio.TimeoutError,
        ) as erreur:
            st.error(f"Erreur pendant la surveillance : {erreur}")

    try:
        df_sessions, df_alertes = charger_donnees()
    except (psycopg.Error, sqlite3.Error) as erreur:
        st.error(f"Impossible de recharger la base de données : {erreur}")

if tester_telegram:
    if not telegram_token or not telegram_chat_id:
        st.error(
            "Test impossible : configure TELEGRAM_BOT_TOKEN et "
            "TELEGRAM_CHAT_ID dans les secrets."
        )
    else:
        try:
            envoyer_notification_telegram(
                {
                    "categorie": "douala_b2",
                    "date_examen": "20/10/2026",
                    "heure": "09:00",
                    "ancienne_disponibilite": "indisponible",
                    "nouvelle_disponibilite": "disponible",
                },
                telegram_token,
                telegram_chat_id,
                test=True,
            )
        except requests.HTTPError as erreur:
            code_http = (
                erreur.response.status_code
                if erreur.response is not None
                else "inconnu"
            )
            st.error(
                f"Telegram a refusé le test (HTTP {code_http}). "
                "Vérifie le token du bot et le chat_id."
            )
        except (requests.RequestException, ValueError, RuntimeError) as erreur:
            st.error(
                f"Échec de l’envoi du test Telegram "
                f"({type(erreur).__name__})."
            )
        else:
            st.success("Message de test envoyé dans le chat Telegram.")

st.divider()
st.subheader("🚨 Alertes")

if df_alertes.empty:
    st.info("Aucune alerte pour le moment.")
else:
    for _, alerte in df_alertes.iterrows():
        with st.container(border=True):
            st.markdown(
                f"**{alerte['categorie']}** — "
                f"{alerte['date_examen']} à {alerte['heure']}"
            )
            st.markdown(
                f"`{alerte['ancienne_disponibilite']}` → "
                f"`{alerte['nouvelle_disponibilite']}`"
            )

st.divider()
st.subheader("📅 Sessions")

filtre = st.segmented_control(
    "Affichage",
    options=["Toutes", "Disponibles", "Yaoundé écrit"],
    default="Toutes",
)
df_affichage = df_sessions.copy()

if filtre == "Disponibles":
    df_affichage = df_affichage[
        df_affichage["disponibilite"] == "disponible"
    ]
elif filtre == "Yaoundé écrit":
    df_affichage = df_affichage[
        df_affichage["categorie"] == "yaounde_b2_ecrit"
    ]

colonnes_sessions = [
    "date_examen",
    "heure",
    "categorie",
    "disponibilite",
    "source",
]
st.dataframe(
    df_affichage[colonnes_sessions]
    if not df_affichage.empty
    else pd.DataFrame(columns=colonnes_sessions),
    width="stretch",
    hide_index=True,
)