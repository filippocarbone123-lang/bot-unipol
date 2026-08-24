import os
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
                # 1. Accesso Credenziali Primo Livello
                print("1/3 Inserimento Username e Password...", flush=True)
                await page.goto("https://essig.unipolsai.it/my-policy", wait_until="domcontentloaded", timeout=40000)
                
                user_input = page.locator('input[name="Username" i], input[name="username" i]').first
                await user_input.wait_for(state="visible", timeout=20000)
                await user_input.fill(UNIPOL_USER or "")

                pass_input = page.locator('input[type="password"]').first
                await pass_input.fill(UNIPOL_PASS or "")

                domain_select = page.locator('select[name="domain" i]').first
                if await domain_select.is_visible():
                    try:
                        await domain_select.select_option(label="Uniage", timeout=3000)
                    except Exception:
                        await domain_select.select_option(value="Uniage", timeout=3000)

                await page.locator('input[type="submit"], button').first.click()

                # 2. MFA Microsoft Intermedio
                print("2/3 Schermata intermedia MFA...", flush=True)
                login_mfa_btn = page.locator('input[type="submit"], button').first
                await login_mfa_btn.wait_for(state="visible", timeout=25000)
                await login_mfa_btn.click()

                # 3. Digitazione e Invio OTP Authenticator
                print("3/3 Inserimento codice OTP...", flush=True)
                code_input = page.locator('input[type="text"], input[type="number"], input').first
                await code_input.wait_for(state="visible", timeout=25000)

                totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
                codice_otp = totp.now()
                
                await code_input.fill("")
                await code_input.type(codice_otp, delay=100)
                
                print("Clic su pulsante Login OTP...", flush=True)
                await page.locator('input[type="submit"], button').first.click()
                
                # Pausa per far elaborare l'OTP ai server Microsoft/Unipol e salvare i cookie
                print("Attesa 6s per registrazione sessione di sicurezza...", flush=True)
                await asyncio.sleep(6)

                # 4. TRUCCO UTENTE: Navigazione forzata a Leonardo
                print("Forzatura URL Leonardo (bypass caricamenti lenti)...", flush=True)
                leonardo_url = "https://essig.unipolsai.it/WorkspaceWeb/app/configuratore_questionari/questionario"
                
                try:
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    # Se il redirect del server Unipol interrompe la nostra navigazione, riproviamo!
                    print(f"Interferenza di sistema ({e}), riprovo la forzatura...", flush=True)
                    await asyncio.sleep(2)
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=20000)

                print(f"Atterraggio completato su: {page.url}", flush=True)

                # 5. Navigazione Menu Strumenti
                print("Apertura menu Strumenti...", flush=True)
                strumenti_btn = page.locator('text=/Strumenti/i').first
                await strumenti_btn.wait_for(state="attached", timeout=20000)
                await strumenti_btn.click(force=True)

                print("Apertura menu DANNI...", flush=True)
                danni_btn = page.locator('text=/DANNI/i').first
                await danni_btn.wait_for(state="visible", timeout=15000)
                await danni_btn.click()

                print("Apertura menu RCA AUTO...", flush=True)
                rca_btn = page.locator('text=/RCA AUTO/i').first
                await rca_btn.wait_for(state="visible", timeout=15000)
                await rca_btn.click()

                print("Apertura CONSULTAZIONE BDA...", flush=True)
                bda_btn = page.locator('text=/CONSULTAZIONE BDA|BDA/i').first
                await bda_btn.wait_for(state="visible", timeout=15000)
                await bda_btn.click()

                # 6. Compilazione Targa nei frame
                print(f"Inserimento targa {targa} in BDA...", flush=True)
                targa_input = None
                target_frame = page.main_frame
                
                for _ in range(20):
                    for frame in page.frames:
                        inp = frame.locator('input[name*="targa" i], input[id*="targa" i]').first
                        if await inp.is_visible():
                            targa_input = inp
                            target_frame = frame
                            break
                    if targa_input:
                        break
                    await asyncio.sleep(1)

                if not targa_input:
                    targa_input = page.locator('input[name*="targa" i], input[id*="targa" i]').first
                    await targa_input.wait_for(state="visible", timeout=20000)

                await targa_input.fill(targa)
                avanti_btn = target_frame.locator('button:has-text("Avanti"), input[value*="Avanti" i]').first
                await avanti_btn.click()

                # 7. Estrattore Dati ANIA
                print("Estrazione dati veicolo...", flush=True)
                marca, modello, kw, cv, data_immat, alimentazione = "", "", "", "", "", ""
                
                found_data = False
                for _ in range(25):
                    for frame in page.frames:
                        cell = frame.locator('td:has-text("Marca:")').first
                        if await cell.is_visible():
                            marca = await frame.locator('td:has-text("Marca:") + td').text_content() or ""
                            modello = await frame.locator('td:has-text("Descrizione modello:") + td').text_content() or ""
                            kw = await frame.locator('td:has-text("KW:") + td').text_content() or ""
                            cv = await frame.locator('td:has-text("Cilindrata:") + td').text_content() or ""
                            data_immat = await frame.locator('td:has-text("Data prima immatricolazione:") + td').text_content() or ""
                            alimentazione = await frame.locator('td:has-text("Alimentazione:") + td').text_content() or ""
                            target_frame = frame
                            found_data = True
                            break
                    if found_data:
                        break
                    await asyncio.sleep(1)

                # 8. Visualizzazione Attestato e CU
                classe_cu = ""
                compagnia_provenienza = ""
                
                btn_att = target_frame.locator('button:has-text("Visualizza Attestato"), input[value*="Attestato" i]').first
                if await btn_att.is_visible():
                    await btn_att.click()
                    for _ in range(15):
                        for frame in page.frames:
                            cu_cell = frame.locator('td:has-text("Classe CU:")').first
                            if await cu_cell.is_visible():
                                classe_cu = await frame.locator('td:has-text("Classe CU:") + td').text_content() or ""
                                compagnia_provenienza = await frame.locator('td:has-text("Impresa:") + td').text_content() or ""
                                break
                        if classe_cu:
                            break
                        await asyncio.sleep(1)

                # 9. Scrittura finale su Airtable
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
                res = requests.patch(url_trattativa, headers=headers, json=payload_trattativa)
                res.raise_for_status()
                print(f"[{record_id}] ESTRAZIONE COMPLETATA CON SUCCESSO!", flush=True)

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
