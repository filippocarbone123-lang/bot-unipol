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

async def smart_click(page, text_target: str, timeout_ms=15000):
    pattern = re.compile(rf"{re.escape(text_target)}", re.IGNORECASE)
    locator = page.get_by_text(pattern).first
    try:
        await locator.wait_for(state="attached", timeout=timeout_ms)
    except Exception:
        locator = page.locator(f'text=/{text_target}/i').first
        await locator.wait_for(state="attached", timeout=timeout_ms)

    try:
        await locator.click(force=True, timeout=3000)
    except Exception:
        await locator.evaluate("node => (node.closest('button, a, label, div.p-button, div.p-radiobutton, li, input') || node).click()")

async def estrai_testo_o_input(frame, etichetta: str) -> str:
    """Cerca l'etichetta ed estrae il valore dal testo o dall'input affiancato."""
    try:
        cell = frame.locator(f'td:has-text("{etichetta}"), th:has-text("{etichetta}")').first
        if await cell.is_visible(timeout=300):
            target = frame.locator(f'td:has-text("{etichetta}") + td, th:has-text("{etichetta}") + td').first
            if await target.count() > 0:
                inp = target.locator('input, select').first
                if await inp.count() > 0:
                    v = await inp.input_value()
                    if v and v.strip() and v.strip() != "125":
                        return v.strip()
                testo = await target.text_content()
                if testo and testo.strip():
                    return testo.strip()
    except Exception:
        pass
    return ""

async def estrai_dati_bda(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        targa_spazi = formatta_targa_spazi(targa)
        targa_pulita = targa.replace(" ", "").upper()

        print(f"[{record_id}] Avvio elaborazione per targa: {targa_pulita}", flush=True)
        requests.patch(url_trattativa, headers=headers, json={"fields": {"Stato Bot Estrazione": "In Corso"}})

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()

            try:
                # ==========================================
                # 🔒 ZONA IN CASSAFORTE (LOGIN & MFA)
                # ==========================================
                print("1/4 Inserimento Username e Password...", flush=True)
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

                print("2/4 Schermata intermedia MFA...", flush=True)
                login_mfa_btn = page.locator('input[type="submit"], button').first
                await login_mfa_btn.wait_for(state="visible", timeout=25000)
                await login_mfa_btn.click()

                print("3/4 Inserimento codice OTP...", flush=True)
                code_input = page.locator('input[type="text"], input[type="number"], input').first
                await code_input.wait_for(state="visible", timeout=25000)

                totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
                codice_otp = totp.now()
                
                await code_input.fill("")
                await code_input.type(codice_otp, delay=100)
                
                print("Clic su pulsante Login OTP...", flush=True)
                await page.locator('input[type="submit"], button').first.click()
                
                print("Attesa 6s per registrazione sessione di sicurezza...", flush=True)
                await asyncio.sleep(6)

                print("Forzatura URL Leonardo...", flush=True)
                leonardo_url = "https://essig.unipolsai.it/WorkspaceWeb/app/configuratore_questionari/questionario"
                
                try:
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    print(f"Interferenza di sistema ({e}), riprovo la forzatura...", flush=True)
                    await asyncio.sleep(2)
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=20000)

                # ==========================================
                # 🔓 MODULO 1: PREVENTIVATORE UNIPOL
                # ==========================================
                print("Ingresso nel Preventivatore Unipol...", flush=True)
                await page.wait_for_timeout(3000)

                await smart_click(page, "PRODOTTI")
                await page.wait_for_timeout(1500)
                await smart_click(page, "ALTRI PRODOTTI DANNI")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(1500)
                await smart_click(page, "AUTO/NATANTI")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(1500)
                await smart_click(page, "RCA SINGOLE")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(4000)

                print("Compilazione maschera Preventivo...", flush=True)
                for frame in page.frames:
                    try:
                        t_inp = frame.locator('input[name*="targa" i], input[id*="targa" i], td:has-text("Targa") + td input').first
                        if await t_inp.is_visible(timeout=500):
                            await t_inp.fill(targa_spazi)
                        
                        c_inp = frame.locator('input[name*="sub" i], input[name*="cip" i], td:has-text("CIP") + td input').first
                        if await c_inp.is_visible(timeout=500):
                            await c_inp.fill("125")
                    except Exception:
                        continue

                for frame in page.frames:
                    try:
                        btn = frame.locator('button:has-text("Prosegui"), input[value*="Prosegui" i]').first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            break
                    except Exception:
                        continue

                await page.wait_for_timeout(6000)

                # Variabili globali estrazione
                nome, cf, data_nas, residenza, prov, cap = "", "", "", "", "", ""
                marca, modello, kw, data_immat, alimentazione = "", "", "", "", ""
                classe_cu, compagnia_provenienza = "", ""

                print("Estrazione Dati Anagrafici e Veicolo con Navigazione Schede...", flush=True)
                
                for frame in page.frames:
                    try:
                        # TAB 1: DATI ASSICURATIVI
                        tab1 = frame.locator('text="DATI ASSICURATIVI", td:has-text("DATI ASSICURATIVI")').first
                        if await tab1.is_visible(timeout=300):
                            await tab1.click(force=True)
                            await page.wait_for_timeout(1000)
                            marca = await estrai_testo_o_input(frame, "Codice marca") or await estrai_testo_o_input(frame, "Marca")
                            modello = await estrai_testo_o_input(frame, "Descrizione modello")
                            kw = await estrai_testo_o_input(frame, "KW")
                            data_immat = await estrai_testo_o_input(frame, "Data prima immatricolazione")
                            alimentazione = await estrai_testo_o_input(frame, "Alimentazione")

                        # TAB 2: Figure contrattuali
                        tab2 = frame.locator('text="Figure contrattuali", td:has-text("Figure contrattuali")').first
                        if await tab2.is_visible(timeout=300):
                            await tab2.click(force=True)
                            await page.wait_for_timeout(1000)
                            nome = await estrai_testo_o_input(frame, "Nominativo")
                            cf = await estrai_testo_o_input(frame, "Cod.Fisc") or await estrai_testo_o_input(frame, "C.F")
                            data_nas = await estrai_testo_o_input(frame, "Data di nascita")
                            residenza = await estrai_testo_o_input(frame, "Indirizzo")
                            prov = await estrai_testo_o_input(frame, "Prov")
                            cap = await estrai_testo_o_input(frame, "CAP")

                        # TAB 3: Posizione assicurativa
                        tab3 = frame.locator('text="Posizione assicurativa", td:has-text("Posizione assicurativa")').first
                        if await tab3.is_visible(timeout=300):
                            await tab3.click(force=True)
                            await page.wait_for_timeout(1000)
                            classe_cu = await estrai_testo_o_input(frame, "Classe CU")
                            compagnia_provenienza = await estrai_testo_o_input(frame, "Impresa")
                    except Exception:
                        continue

                # ==========================================
                # 🔓 MODULO 2: CONSULTAZIONE BDA ANIA
                # ==========================================
                print("Passaggio al Modulo BDA per Storia Assicurativa (Fallback)...", flush=True)
                await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

                await smart_click(page, "Strumenti")
                await page.wait_for_timeout(1000)
                await smart_click(page, "Danni")
                await page.wait_for_timeout(1000)
                await smart_click(page, "RCA AUTO")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONSULTAZIONE BDA")
                await page.wait_for_timeout(3000)

                print("Inserimento targa in BDA...", flush=True)
                for frame in page.frames:
                    try:
                        inp = frame.locator('input[name*="targa" i], input[id*="targa" i]').first
                        if await inp.is_visible(timeout=500):
                            await inp.fill(targa_pulita)
                            btn = frame.locator('button:has-text("Avanti"), input[value*="Avanti" i]').first
                            await btn.click()
                            break
                    except Exception:
                        continue

                await page.wait_for_timeout(4000)

                # Recupero dati mancanti da ANIA
                for _ in range(12):
                    for frame in page.frames:
                        if not marca: marca = await estrai_testo_o_input(frame, "Marca:")
                        if not modello: modello = await estrai_testo_o_input(frame, "Descrizione modello:")
                        if not kw: kw = await estrai_testo_o_input(frame, "KW:")
                        if not data_immat: data_immat = await estrai_testo_o_input(frame, "Data prima immatricolazione:")
                        if not alimentazione: alimentazione = await estrai_testo_o_input(frame, "Alimentazione:")
                    if marca or kw: break
                    await asyncio.sleep(1)

                for frame in page.frames:
                    try:
                        btn_att = frame.locator('button:has-text("Visualizza Attestato"), input[value*="Attestato" i]').first
                        if await btn_att.is_visible(timeout=500):
                            await btn_att.click()
                            break
                    except Exception:
                        continue

                await page.wait_for_timeout(2000)

                for _ in range(12):
                    for frame in page.frames:
                        if not classe_cu: classe_cu = await estrai_testo_o_input(frame, "Classe CU:")
                        if not compagnia_provenienza: compagnia_provenienza = await estrai_testo_o_input(frame, "Impresa:")
                    if classe_cu: break
                    await asyncio.sleep(1)

                print(f"VALORI FINALI -> Nome: '{nome}', CF: '{cf}', Marca: '{marca}', Modello: '{modello}', CU: '{classe_cu}', Comp: '{compagnia_provenienza}'", flush=True)

                # ==========================================
                # 💾 SALVATAGGIO SU AIRTABLE
                # ==========================================
                raw_fields = {
                    "Nome": nome, "Codice Fiscale": cf, "cl_datanascita": data_nas, "cl_indirizzo": residenza,
                    "cl_provincia": prov, "cl_cap": cap, "Marca": marca, "Modello": modello, "KW": kw,
                    "Data immatricolazione": data_immat, "Alimentazione": alimentazione,
                    "Classe CU": classe_cu, "Compagnia Provenienza": compagnia_provenienza,
                    "Stato Bot Estrazione": "Dati Estratti"
                }

                cleaned_fields = {k: v for k, v in raw_fields.items() if v != ""}
                requests.patch(url_trattativa, headers=headers, json={"fields": cleaned_fields})
                print(f"[{record_id}] ESTRAZIONE COMPLETATA CON SUCCESSO!", flush=True)

            except Exception as e:
                print(f"[{record_id}] ERRORE: {str(e)[:250]}", flush=True)
                requests.patch(url_trattativa, headers=headers, json={"fields": {"Stato Bot Estrazione": "Errore"}})
            finally:
                await context.close()
                await browser.close()

@app.post("/estrai")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")
    background_tasks.add_task(estrai_dati_bda, record_id, targa, data_nascita)
    return {"status": "Estrazione Avviata", "record_id": record_id}
