import html
import os
import re
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from playwright.async_api import async_playwright

from database import DatabaseConnection, connect_db, init_db

BASE_URL = "https://kbw-personal-services.butlerapp2.de"

CATEGORIES_API = {
    "douala_b2": "douala_deutsch_b2",
    "yaounde_b2": "yaounde_deutsch_b2",
    "douala_b2_ecrit": "douala_deutsch_b2_schriftliche_wiederholungspruefung",
    "douala_b2_oral": "douala_deutsch_b2_muendlich_wiederholungspruefung",
    "yaounde_b2_oral": "younde_deutsch_b2_muendlich_wiederholungspruefung",
}

CATEGORIE_DOM = {
    "cle": "yaounde_b2_ecrit",
    "url_filter": "yaounde_deutsch_b2_schriftliche_wiederholungspruefung",
    "classe": "yaounde_deutsch_b2_schriftlich_wiederholungspruefung",
}

LIENS_CATEGORIES = {
    "douala_b2": "douala_deutsch_b2",
    "yaounde_b2": "yaounde_deutsch_b2",
    "douala_b2_ecrit": "douala_deutsch_b2_schriftliche_wiederholungspruefung",
    "douala_b2_oral": "douala_deutsch_b2_muendlich_wiederholungspruefung",
    "yaounde_b2_ecrit": "yaounde_deutsch_b2_schriftliche_wiederholungspruefung",
    "yaounde_b2_oral": "younde_deutsch_b2_muendlich_wiederholungspruefung",
}

HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/demo",
        "X-Requested-With": "XMLHttpRequest",
    }
)


def envoyer_notification_telegram(
    alerte: dict[str, str],
    token: str,
    chat_id: str,
    test: bool = False,
) -> None:
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID sont requis.")

    categorie = alerte["categorie"]
    try:
        filtre_categorie = LIENS_CATEGORIES[categorie]
    except KeyError as erreur:
        raise ValueError(f"Catégorie Telegram inconnue : {categorie}") from erreur

    lien = (
        f"{BASE_URL}/demo"
        f"#/courses?fcourses={filtre_categorie}"
    )
    titre = (
        "🧪 TEST — alerte de disponibilité"
        if test
        else "🚨 Place possiblement libérée"
    )
    message = (
        f"{titre}\n\n"
        f"Catégorie : {html.escape(categorie)}\n"
        f"Date : {html.escape(alerte['date_examen'])}\n"
        f"Heure : {html.escape(alerte['heure'])}\n"
        "Changement : "
        f"{html.escape(alerte['ancienne_disponibilite'])} → "
        f"{html.escape(alerte['nouvelle_disponibilite'])}\n\n"
        f'<a href="{lien}">Réserver maintenant</a>'
    )
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    response.raise_for_status()
    resultat: Any = response.json()
    if not isinstance(resultat, dict) or resultat.get("ok") is not True:
        raise RuntimeError("L’API Telegram a refusé la notification.")


def normaliser_id(identifiant: str | int) -> str:
    return str(identifiant).replace("row", "").strip()


def normaliser_date(date: str) -> str:
    date = date.strip()

    if "." in date:
        return datetime.strptime(date, "%d.%m.%Y").strftime("%Y-%m-%d")

    return date[:10]


def normaliser_heure(heure: str) -> str:
    heure = heure.replace(" Uhr", "").strip()
    return heure.zfill(5)


def enregistrer_et_detecter(
    conn: DatabaseConnection,
    source: str,
    categorie: str,
    session_id: str,
    date_examen: str,
    heure: str,
    disponibilite: str,
    quantityleft: int | None = None,
) -> dict[str, str] | None:
    maintenant = datetime.now(timezone.utc).isoformat()
    if os.environ.get("SUPABASE_DB_URL", "").strip():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))",
            (f"{source}:{categorie}:{session_id}",),
        )

    ancien = conn.execute(
        """
        SELECT disponibilite
        FROM sessions
        WHERE source = ? AND categorie = ? AND session_id = ?
        """,
        (source, categorie, session_id),
    ).fetchone()

    alerte = None

    if ancien is None:
        conn.execute(
            """
            INSERT INTO sessions
            (source, categorie, session_id, date_examen, heure,
             disponibilite, quantityleft, observe_a)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                categorie,
                session_id,
                date_examen,
                heure,
                disponibilite,
                quantityleft,
                maintenant,
            ),
        )
    else:
        ancienne_disponibilite = ancien[0]

        if ancienne_disponibilite != disponibilite:
            alerte = {
                "source": source,
                "categorie": categorie,
                "session_id": session_id,
                "date_examen": date_examen,
                "heure": heure,
                "ancienne_disponibilite": ancienne_disponibilite,
                "nouvelle_disponibilite": disponibilite,
                "detecte_a": maintenant,
            }

        conn.execute(
            """
            UPDATE sessions
            SET date_examen = ?, heure = ?, disponibilite = ?,
                quantityleft = ?, observe_a = ?
            WHERE source = ? AND categorie = ? AND session_id = ?
            """,
            (
                date_examen,
                heure,
                disponibilite,
                quantityleft,
                maintenant,
                source,
                categorie,
                session_id,
            ),
        )

    return alerte


def surveiller_api(conn: DatabaseConnection) -> list[dict[str, str]]:
    alertes = []

    for categorie, cle_api in CATEGORIES_API.items():
        page = 1

        while True:
            response = HTTP.get(
                f"{BASE_URL}/api/book",
                params={
                    "exclude": "trainer,appointments,extraprices,forms",
                    "page": page,
                    "perPage": 10,
                    "fcourses": cle_api,
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            timespans = (
                data["body"]["entities"]["body"].get("course_timespans", [])
            )

            for item in timespans:
                attrs = item["attributes"]
                quantityleft = attrs.get("quantityleft")
                disponibilite = (
                    "disponible"
                    if quantityleft is not None and quantityleft > 0
                    else "indisponible"
                )

                alerte = enregistrer_et_detecter(
                    conn=conn,
                    source="api",
                    categorie=categorie,
                    session_id=normaliser_id(attrs["id"]),
                    date_examen=normaliser_date(attrs["from"]["date"]),
                    heure=attrs["from"]["date"][11:16],
                    disponibilite=disponibilite,
                    quantityleft=quantityleft,
                )
                if alerte:
                    alertes.append(alerte)

            pagination = data["body"]["pagination"]["body"]
            if page >= pagination["lastPage"] or not timespans:
                break

            page += 1
            time.sleep(0.3)

    return alertes


async def installer_chromium_si_necessaire() -> None:
    async with async_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)

    if executable.is_file():
        return

    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )


async def surveiller_yaounde_ecrit_dom(
    conn: DatabaseConnection,
) -> list[dict[str, str]]:
    alertes = []
    url_cible = (
        f"{BASE_URL}/demo"
        f"#/courses?fcourses={CATEGORIE_DOM['url_filter']}"
    )

    await installer_chromium_si_necessaire()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            page = await browser.new_page()
            await page.goto(url_cible, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(3_000)
            await page.wait_for_selector(
                "table tbody tr",
                state="visible",
                timeout=30_000,
            )
            await page.wait_for_timeout(2_000)

            identifiants_vus = set()

            while True:
                lignes = page.locator("table tbody tr")
                nombre_lignes = await lignes.count()

                for index in range(nombre_lignes):
                    ligne = lignes.nth(index)
                    classes = (await ligne.get_attribute("class") or "").strip()
                    if CATEGORIE_DOM["classe"] not in classes:
                        continue

                    identifiant = await ligne.get_attribute("id")
                    if not identifiant or identifiant in identifiants_vus:
                        continue
                    identifiants_vus.add(identifiant)

                    texte_ligne = await ligne.inner_text()
                    date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", texte_ligne)
                    heure_match = re.search(r"\d{1,2}:\d{2}\s*Uhr", texte_ligne)
                    if not date_match or not heure_match:
                        continue

                    disponibilite = (
                        "disponible"
                        if "ws-data-row-booked-out" not in classes
                        else "indisponible"
                    )
                    alerte = enregistrer_et_detecter(
                        conn=conn,
                        source="dom",
                        categorie=CATEGORIE_DOM["cle"],
                        session_id=normaliser_id(identifiant),
                        date_examen=normaliser_date(date_match.group(0)),
                        heure=normaliser_heure(heure_match.group(0)),
                        disponibilite=disponibilite,
                    )
                    if alerte:
                        alertes.append(alerte)

                boutons_suivants = page.locator(".rc-pagination-next")
                if await boutons_suivants.count() == 0:
                    break

                bouton = boutons_suivants.first
                classes_bouton = await bouton.get_attribute("class") or ""
                aria_disabled = await bouton.get_attribute("aria-disabled")
                if (
                    "rc-pagination-disabled" in classes_bouton
                    or aria_disabled == "true"
                ):
                    break

                await bouton.click()
                await page.wait_for_timeout(2_000)
        finally:
            await browser.close()

    return alertes


async def surveiller_une_fois(
    telegram_token: str | None = None,
    telegram_chat_id: str | None = None,
) -> pd.DataFrame:
    init_db()

    with connect_db() as conn:
        alertes = surveiller_api(conn)
        alertes.extend(await surveiller_yaounde_ecrit_dom(conn))

        if alertes:
            conn.executemany(
                """
                INSERT INTO alertes
                (source, categorie, session_id, date_examen, heure,
                 ancienne_disponibilite, nouvelle_disponibilite, detecte_a)
                VALUES (:source, :categorie, :session_id, :date_examen,
                        :heure, :ancienne_disponibilite,
                        :nouvelle_disponibilite, :detecte_a)
                """,
                alertes,
            )

    token = telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    alertes_disponibles = [
        alerte
        for alerte in alertes
        if alerte["nouvelle_disponibilite"] == "disponible"
    ]

    if alertes_disponibles and not (token and chat_id):
        warnings.warn(
            "Transition détectée, mais Telegram n’est pas configuré. "
            "Définis TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID.",
            RuntimeWarning,
            stacklevel=2,
        )
    elif token and chat_id:
        for alerte in alertes_disponibles:
            try:
                envoyer_notification_telegram(alerte, token, chat_id)
            except (requests.RequestException, ValueError, RuntimeError) as erreur:
                warnings.warn(
                    "Échec de notification Telegram pour "
                    f"{alerte['categorie']} ({type(erreur).__name__}).",
                    RuntimeWarning,
                    stacklevel=2,
                )

    return pd.DataFrame(alertes)


if __name__ == "__main__":
    import asyncio

    asyncio.run(surveiller_une_fois())