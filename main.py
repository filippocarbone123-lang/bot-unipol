import os
import pyotp
import requests
import asyncio
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks
from playwright.async_api import async_playwright

app = FastAPI()
bot_semaphore = asyncio.Semaphore(1)

UNIPOL_USER = os.getenv("UNIPOL_USER")
UNIPOL_PASS = os.getenv("UNIPOL_PASS")
UNIPOL_TOTP_SECRET = os.getenv("UNIPOL_TOTP_SECRET")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

async def estrai_dati_bda(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        print(f"[{record_id}] Avvio elaborazione per targa: {targa}", flush=True)
        requests.patch(
            url_trattativa,
            headers=headers,
            json={"fields": {"Stato Bot Estrazione": "In Corso"}}
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            try:
                # 1. Accesso Unipol
                print("Navigazione a essig.unipolsai.it...", flush=True)
                await page.goto("https://essig.unipolsai.it/my-policy", wait_until="domcontentloaded", timeout=30000)
                
                user_input = page.locator('input[name="Username" i], input[name="username" i], input[id*="user" i]').first
                await user_input.wait_for(state="visible", timeout=20000)
                await user_input.fill(UNIPOL_USER or "")

                pass_input = page.locator('input[type="password"]').first
                await pass_input.fill(UNIPOL_PASS or "")

                # Seleziona il dominio "Uniage" corretto
                domain_select = page.locator('select[name="domain" i], select[id*="domain" i]').first
                if await domain_select.is_visible():
                    try:
                        await domain_select.select_option(label="Uniage", timeout=3000)
                    except Exception:
                        try:
                            await domain_select.select_option(value="Uniage", timeout=3000)
                        except Exception:
                            try:
                                await domain_select.select_option(value="UNIAGE", timeout=3000)
                            except Exception:
                                pass

                login_btn = page.locator('button:has-text("Login"), input[type="submit"][value*="Login" i], button[type="submit"]').first
                await login_btn.click()

                # 2. Inserimento OTP 2FA
                print("Inserimento codice OTP...", flush=True)
                code_input = page.locator('input[name="code" i], input[id*="code" i], input[type="number"]').first
                await code_input.wait_for(state="visible", timeout=20000)

                totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
                await code_input.fill(totp.now())
                
                login_btn_2fa = page.locator('button:has-text("Login"), input[type="submit"]').first
                await login_btn_2fa.click()
                
                await page.wait_for_selector('text=Strumenti', timeout=20000)

                # 3. Navigazione BDA ANIA
                print("Accesso alla sezione BDA ANIA...", flush=True)
                await page.click('text=Strumenti')
                await page.click('text=DANNI')
                await page.click('text=RCA AUTO')
                await page.click('text=CONSULTAZIONE BDA')

                # 4. Ricerca Targa
                print(f"Inserimento targa {targa} in BDA...", flush=True)
                targa_input = page.locator('input[name="targa" i], input[id*="targa" i]').first
                await targa_input.wait_for(state="visible", timeout=15000)
                await targa_input.fill(targa)
                await page.click('button:has-text("Avanti")')

                # 5. Lettura Dati Tecnici
                await page.wait_for_selector('text=Dati veicolo', timeout=15000)
                marca = await page.locator('td:has-text("Marca:") + td').text_content() or ""
                modello = await page.locator('td:has-text("Descrizione modello:") + td').text_content() or ""
                kw = await page.locator('td:has-text("KW:") + td').text_content() or ""
                cv = await page.locator('td:has-text("Cilindrata:") + td').text_content() or ""
                data_immat = await page.locator('td:has-text("Data prima immatricolazione:") + td').text_content() or ""
                alimentazione = await page.locator('td:has-text("Alimentazione:") + td').text_content() or ""

                # 6. Attestato e CU
                btn_attestato = page.locator('button:has-text("Visualizza Attestato")')
                classe_cu = ""
                compagnia_provenienza = ""

                if await btn_attestato.is_visible():
                    await btn_attestato.click()
                    await page.wait_for_selector('text=Classe CU', timeout=5000)
                    classe_cu = await page.locator('td:has-text("Classe CU:") + td').text_content() or ""
                    compagnia_provenienza = await page.locator('td:has-text("Impresa:") + td').text_content() or ""

                # 7. Aggiornamento Airtable
                print("Salvataggio dati estratti su Airtable...", flush=True)
                payload_trattativa = {
                    "fields": {
                        "Marca": marca.strip(),
                        "Modello": modello.strip(),
                        "KW": kw.strip(),
                        "CV": cv.strip(),
                        "Data Immatricolazione": data_immat.strip(),
                        "Alimentazione": alimentazione.strip(),
                        "Classe CU": classe_cu.strip(),
                        "Compagnia Provenienza": compagnia_provenienza.strip(),
                        "Stato Bot Estrazione": "Dati Estratti"
                    }
                }
                requests.patch(url_trattativa, headers=headers, json=payload_trattativa).raise_for_status()
                print(f"[{record_id}] Elaborazione completata con successo!", flush=True)

            except Exception as e:
                err_msg = str(e)[:250]
                print(f"[{record_id}] ERRORE: {err_msg}", flush=True)
                requests.patch(
                    url_trattativa,
                    headers=headers,
                    json={"fields": {"Stato Bot Estrazione": "Errore"}}
                )

            finally:
                await context.close()
                await browser.close()

@app.post("/estrai")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")
    
    background_tasks.add_task(estrai_dati_bda, record_id, targa, data_nascita)
    return {"status": "Estrazione BDA avviata", "record_id": record_id}
