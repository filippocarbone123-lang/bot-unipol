import os
import pyotp
import requests
import traceback
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks
from playwright.async_api import async_playwright

app = FastAPI()

UNIPOL_USER = os.getenv("UNIPOL_USER")
UNIPOL_PASS = os.getenv("UNIPOL_PASS")
UNIPOL_TOTP_SECRET = os.getenv("UNIPOL_TOTP_SECRET")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

async def estrai_dati_unipol(record_id: str, targa: str, data_nascita: str):
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    url_quotazioni = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Quotazioni_Preventivi"
    url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

    # Genera codice 2FA instantaneo
    totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
    code_2fa = totp.now()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # 1. Login e Autenticazione 2FA su Portale Unipol
            await page.goto("https://www.unipol.it") # Sostituire con URL login portale intermediari
            await page.fill('input[name="Username"]', UNIPOL_USER or "")
            await page.fill('input[name="Password"]', UNIPOL_PASS or "")
            await page.click('button[type="submit"]')

            await page.fill('input[name="code"]', code_2fa)
            await page.click('button[type="submit"]')

            # 2. Inserimento Targa e Data di Nascita
            # (Lo script compila i campi targa e data_nascita sul portale)

            # --- ESEMPIO DATI ESTRATTI DAL PORTAL UNIPOL ---
            # Sostituire con i selettori di estrazione effettivi
            stato_quotazione = "Calcolato"
            num_preventivo = "UNIP-2026-98765"
            premio_annuo = 450.00
            premio_semestrale = 235.00
            premio_rca = 380.00
            imposte = 70.00
            marca = "FIAT"
            modello = "Panda"
            allestimento = "1.0 Hybrid Lounge"
            alimentazione = "Ibrida"
            kw = 51
            cv = 70
            valore_veicolo = 12500

            # 3. Inserimento Nuova Riga su Quotazioni_Preventivi
            payload_preventivo = {
                "fields": {
                    "Trattativa": [record_id],
                    "Compagnia": "Unipol",
                    "Stato Quotazione": stato_quotazione,
                    "Numero Preventivo": num_preventivo,
                    "Premio Annuo": premio_annuo,
                    "Premio Semestrale": premio_semestrale,
                    "Premio Lordo RCA": premio_rca,
                    "Imposte e Diritti": imposte,
                    "Marca": marca,
                    "Modello": modello,
                    "Allestimento": allestimento,
                    "Tipo di Alimentazione": alimentazione,
                    "KW": kw,
                    "CV": cv,
                    "Valore Veicolo": valore_veicolo,
                    "Data Ora Calcolo": datetime.now().isoformat()
                }
            }
            res = requests.post(url_quotazioni, headers=headers, json=payload_preventivo)
            res.raise_for_status()

            # 4. Aggiorna lo Stato della Trattativa Madre
            requests.patch(
                url_trattativa,
                headers=headers,
                json={"fields": {"Stato Bot Estrazione": "Dati Estratti"}}
            )

        except Exception as e:
            errore_msg = str(e)
            print(f"Errore Bot Unipol: {errore_msg}")
            
            # Crea riga preventivo con Errore
            payload_errore = {
                "fields": {
                    "Trattativa": [record_id],
                    "Compagnia": "Unipol",
                    "Stato Quotazione": "Non Quotabile",
                    "Note e Errori Bot": errore_msg[:500]
                }
            }
            requests.post(url_quotazioni, headers=headers, json=payload_errore)

            # Segnala errore sulla Trattativa
            requests.patch(
                url_trattativa,
                headers=headers,
                json={"fields": {"Stato Bot Estrazione": "Errore"}}
            )
        finally:
            await browser.close()

@app.post("/estrai")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")
    
    background_tasks.add_task(estrai_dati_unipol, record_id, targa, data_nascita)
    return {"status": "Bot avviato", "record_id": record_id}
