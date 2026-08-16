import os
import pyotp
import requests
from fastapi import FastAPI, BackgroundTasks
from playwright.async_api import async_playwright

app = FastAPI()

UNIPOL_USER = os.getenv("UNIPOL_USER")
UNIPOL_PASS = os.getenv("UNIPOL_PASS")
UNIPOL_TOTP_SECRET = os.getenv("UNIPOL_TOTP_SECRET")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

async def estrai_dati_unipol(record_id: str, targa: str, data_nascita: str):
    totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
    code_2fa = totp.now()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login e navigazione automatica
            await page.goto("https://www.unipol.it")
            await page.fill('input[name="Username"]', UNIPOL_USER or "")
            await page.fill('input[name="Password"]', UNIPOL_PASS or "")
            await page.click('button[type="submit"]')

            await page.fill('input[name="code"]', code_2fa)
            await page.click('button[type="submit"]')

            # Aggiornamento Airtable
            headers = {
                "Authorization": f"Bearer {AIRTABLE_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "fields": {
                    "Stato Bot Estrazione": "Dati Estratti"
                }
            }
            requests.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}",
                headers=headers,
                json=payload
            )
        except Exception as e:
            print(f"Errore: {e}")
        finally:
            await browser.close()

@app.post("/estrai")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")
    
    background_tasks.add_task(estrai_dati_unipol, record_id, targa, data_nascita)
    return {"status": "Bot avviato", "record_id": record_id}
