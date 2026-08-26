import os
import re
import pyotp
import requests
import asyncio
from fastapi import FastAPI, BackgroundTasks
from playwright.async_api import async_playwright

app = FastAPI()
bot_semaphore = asyncio.Semaphore(1)

UNIPOL_USER = os.getenv("UNIPOL_USER")
UNIPOL_PASS = os.getenv("UNIPOL_PASS")
RAW_SECRET = os.getenv("UNIPOL_TOTP_SECRET") or ""
UNIPOL_TOTP_SECRET = RAW_SECRET.replace(" ", "").strip().upper()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

def formatta_targa_spazi(targa_raw: str) -> str:
    clean = targa_raw.replace(" ", "").upper()
    if len(clean) == 7:
        return f"{clean[:2]} {clean[2:5]} {clean[5:]}"
    return clean

async def cattura_testo_globale(page) -> str:
    """Apre tutti gli iframe, legge il testo visibile E tutti i valori degli input/select, unendoli in un unico dump."""
    testo_aggregato = []
    
    for frame in page.frames:
        try:
            dump = await frame.evaluate('''() => {
                let txt = document.body ? document.body.innerText : "";
                const inputs = Array.from(document.querySelectorAll('input, select'));
                inputs.forEach(i => {
                    let label = i.name || i.id || "campo";
                    let parentTd = i.closest('td');
                    if (parentTd && parentTd.previousElementSibling) {
                        label = parentTd.previousElementSibling.innerText.trim();
                    }
                    if (i.value && i.value.trim() !== "") {
                        txt += `\\n${label} : ${i.value.trim()}`;
                    }
                });
                return txt;
            }''')
            if dump and dump.strip():
                testo_aggregato.append(dump)
        except Exception:
            continue
            
    return "\n--- FRAME SEPARATOR ---\n".join(testo_aggregato)

def estrai_con_regex(pattern: str, testo: str, default: str = "") -> str:
    """Cerca un match di testo tramite Regex e restituisce il primo gruppo valido."""
    m = re.search(pattern, testo, re.IGNORECASE | re.MULTILINE)
    if m and m.group(1):
        v = m.group(1).strip()
        return v if v != "125" else default
    return default

async def estrai_dati_preventivatore(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        targa_spazi = formatta_targa_spazi(targa)
        targa_pulita = targa.replace(" ", "").upper()

        print(f"[{record_id}] Avvio DUMP TESTUALE per targa: {targa_pulita} (Formattata: {targa_spazi})", flush=True)
        requests.patch(
            url_trattativa,
            headers=headers,
            json={"fields": {"Stato Bot Estrazione": "In Corso"}}
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--no-zygote",
                    "--js-flags=--max-old-space-size=256",
                    "--disable-accelerated-2d-canvas",
                    "--disable-background-networking"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )

            # Blocco risorse multimediali per azzerare l'uso della RAM
            await context
