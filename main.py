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
    """Formatta 'DL389LB' in 'DL 389 LB' per il Preventivatore Unipol."""
    clean = targa_raw.replace(" ", "").upper()
    if len(clean) == 7:
        return f"{clean[:2]} {clean[2:5]} {clean[5:]}"
    return clean

async def smart_click(page, text_target: str, timeout_ms=15000):
    """Individua il testo nell'interfaccia Angular ed esegue il clic."""
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

async def estrai_campo_robusto(frame, parole_chiave: list) -> str:
    """Cerca le parole chiave nelle celle HTML o negli input adiacenti e restituisce il valore."""
    for kw in parole_chiave:
        try:
            # 1. Ricerca valore in input o select vicini alla cella
            td_label = frame.locator(f'td:has-text("{kw}"), th:has-text("{kw}")').first
            if await td_label.count() > 0:
                td_val = frame.locator(f'td:has-text("{kw}") + td, th:has-text("{kw}") + td').first
                if await td_val.count() > 0:
                    inp = td_val.locator('input, select').first
                    if await inp.count() > 0:
                        v = await inp.input_value()
                        if v and v.strip() and v.strip() != "125":
                            return v.strip()
                    txt = await td_val.text_content()
                    if txt and txt.strip():
                        return txt.strip()
            
            # 2. Ricerca valore su riga TR intera
            tr = frame.locator(f'tr:has-text("{kw}")').first
            if await tr.count() > 0:
                inp = tr.locator('input, select').first
                if await inp.count() > 0:
                    v = await inp.input_value()
                    if v and v.strip() and v.strip() != "125":
                        return v.strip()
        except Exception:
            continue
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

        print(f"[{record_id}] Avvio elaborazione per targa: {targa_pulita} (Formattata: {targa_spazi})", flush=True)
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
                # ==========================================
                # 🔒 INIZIO ZONA IN CASSAFORTE (LOGIN & MFA)
                # ==========================================
                
                # 1. Accesso Credenziali Primo Livello
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

                # 2. MFA Microsoft Intermedio
                print("2/4 Schermata intermedia MFA...", flush=True)
                login_mfa_btn = page.locator('input[type="submit"], button').first
                await login_mfa_btn.wait_for(state="visible", timeout=25000)
                await login_mfa_btn.click()

                # 3. Digitazione e Invio OTP Authenticator
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

                # 4. Navigazione forzata a Leonardo
                print("Forzatura URL Leonardo...", flush=True)
                leonardo_url = "https://essig.unipolsai.it/WorkspaceWeb/app/configuratore_questionari/questionario"
                
                try:
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    print(f"Interferenza di sistema ({e}), riprovo la forzatura...", flush=True)
                    await asyncio.sleep(2)
                    await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=20000)

                print(f"Atterraggio completato su: {page.url}", flush=True)

                # ==========================================
                # 🔓 MODULO 1: PREVENTIVATORE UNIPOL (FASE A)
                # ==========================================
                print("Ingresso nel Preventivatore Unipol...", flush=True)
                await page.wait_for_timeout(3000)

                await smart_click(page, "PRODOTTI")
                await page.wait_for_timeout(1500)

                print("Selezione ALTRI PRODOTTI DANNI...", flush=True)
                await smart_click(page, "ALTRI PRODOTTI DANNI")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(1500)

                print("Selezione AUTO/NATANTI...", flush=True)
                await smart_click(page, "AUTO/NATANTI")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(1500)

                print("Selezione RCA SINGOLE...", flush=True)
                await smart_click(page, "RCA SINGOLE")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONFERMA")
                await page.wait_for_timeout(4000)

                # Compilazione Maschera Preventivo DanniWeb
                print("Compilazione maschera Preventivo (CIP 125 e Targa con spazi)...", flush=True)
                target_frame = page.main_frame
                
                for _ in range(15):
                    for frame in page.frames:
                        if "DanniWeb" in frame.url or "FA" in frame.url:
                            target_frame = frame
                            break
                        try:
                            if await frame.locator('input[type="text"]').count() >= 2:
                                target_frame = frame
                                break
                        except Exception:
                            continue
                    if target_frame != page.main_frame:
                        break
                    await asyncio.sleep(1)

                # Inserimento CIP 125 sui campi visibili
                cip_done = False
                for loc in [
                    target_frame.locator('input[name*="sub" i]'),
                    target_frame.locator('input[name*="cip" i]'),
                    target_frame.locator('input[type="text"]')
                ]:
                    cnt = await loc.count()
                    for i in range(cnt):
                        el = loc.nth(i)
                        if await el.is_visible():
                            try:
                                await el.fill("125")
                                cip_done = True
                                break
                            except Exception:
                                pass
                    if cip_done:
                        break

                # Inserimento Targa formattata sui campi visibili
                targa_done = False
                for loc in [
                    target_frame.locator('input[name*="targa" i]'),
                    target_frame.locator('input[id*="targa" i]'),
                    target_frame.locator('input[type="text"]')
                ]:
                    cnt = await loc.count()
                    for i in range(cnt):
                        el = loc.nth(i)
                        if await el.is_visible():
                            val = await el.input_value()
                            if val == "125":
                                continue
                            try:
                                await el.fill(targa_spazi)
                                targa_done = True
                                break
                            except Exception:
                                pass
                    if targa_done:
                        break

                prosegui_btn = target_frame.locator('button:has-text("Prosegui"), input[value*="Prosegui" i], a:has-text("Prosegui")').first
                try:
                    await prosegui_btn.click(timeout=5000)
                except Exception:
                    await prosegui_btn.evaluate("node => node.click()")

                # Estrazione Dati dal Preventivatore con navigazione schede
                print("Estrazione Dati Anagrafici e Veicolo dal Preventivatore...", flush=True)
                await page.wait_for_timeout(6000)

                # 1. Clic sulla scheda principale DATI ASSICURATIVI
                try:
                    tab_dati_ass = target_frame.locator('text="DATI ASSICURATIVI", td:has-text("DATI ASSICURATIVI")').first
                    if await tab_dati_ass.is_visible(timeout=4000):
                        await tab_dati_ass.click(force=True)
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

                # Estrazione Dati Veicolo da Veicolo/attestato
                marca = await estrai_campo_robusto(target_frame, ["Codice marca", "Marca"])
                modello = await estrai_campo_robusto(target_frame, ["Descrizione modello", "Modello"])
                kw = await estrai_campo_robusto(target_frame, ["KW"])
                data_immat = await estrai_campo_robusto(target_frame, ["Data prima immatricolazione"])
                alimentazione = await estrai_campo_robusto(target_frame, ["Alimentazione"])

                # 2. Clic sulla scheda Figure contrattuali
                try:
                    tab_fig = target_frame.locator('text="Figure contrattuali", td:has-text("Figure contrattuali")').first
                    if await tab_fig.is_visible(timeout=3000):
                        await tab_fig.click(force=True)
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

                nome = await estrai_campo_robusto(target_frame, ["Nominativo", "PROPRIETARIO"])
                cf = await estrai_campo_robusto(target_frame, ["Cod.Fisc", "C.F"])
                data_nas = await estrai_campo_robusto(target_frame, ["Data di nascita"])
                residenza = await estrai_campo_robusto(target_frame, ["Indirizzo"])
                prov = await estrai_campo_robusto(target_frame, ["Prov"])
                cap = await estrai_campo_robusto(target_frame, ["CAP"])

                # 3. Clic sulla scheda Posizione assicurativa
                try:
                    tab_pos = target_frame.locator('text="Posizione assicurativa", td:has-text("Posizione assicurativa")').first
                    if await tab_pos.is_visible(timeout=3000):
                        await tab_pos.click(force=True)
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

                classe_cu = await estrai_campo_robusto(target_frame, ["Classe CU di assegnazione", "Classe CU"])
                compagnia_provenienza = await estrai_campo_robusto(target_frame, ["Compagnia di provenienza", "Impresa"])

                # ==========================================
                # 🔓 MODULO 2: CONSULTAZIONE BDA ANIA (FASE B)
                # ==========================================
                print("Passaggio al Modulo BDA per Storia Assicurativa...", flush=True)
                
                await page.goto(leonardo_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)

                await smart_click(page, "Strumenti")
                await page.wait_for_timeout(1000)
                await smart_click(page, "Danni")
                await page.wait_for_timeout(1000)
                await smart_click(page, "RCA AUTO")
                await page.wait_for_timeout(1000)
                await smart_click(page, "CONSULTAZIONE BDA")

                # Compilazione Targa in BDA (Senza spazi)
                print(f"Inserimento targa {targa_pulita} in BDA...", flush=True)
                await page.wait_for_timeout(3000)

                target_frame_bda = page.main_frame
                for frame in page.frames:
                    try:
                        inp = frame.locator('input[name*="targa" i], input[id*="targa" i]').first
                        if await inp.is_visible(timeout=500):
                            target_frame_bda = frame
                            await inp.fill(targa_pulita)
                            break
                    except Exception:
                        continue

                avanti_bda = target_frame_bda.locator('button:has-text("Avanti"), input[value*="Avanti" i]').first
                await avanti_bda.click()

                # Visualizzazione Attestato in BDA e fallback dati mancanti
                print("Estrazione Classe CU e Compagnia Provenienza da BDA...", flush=True)
                await page.wait_for_timeout(4000)

                btn_att = target_frame_bda.locator('button:has-text("Visualizza Attestato"), input[value*="Attestato" i]').first
                if await btn_att.is_visible():
                    await btn_att.click()
                    await page.wait_for_timeout(2000)

                # Fallback automatico da BDA se un valore era vuoto nel Preventivatore
                if not marca:
                    marca = await estrai_campo_robusto(target_frame_bda, ["Marca:"])
                if not modello:
                    modello = await estrai_campo_robusto(target_frame_bda, ["Descrizione modello:"])
                if not kw:
                    kw = await estrai_campo_robusto(target_frame_bda, ["KW:"])
                if not data_immat:
                    data_immat = await estrai_campo_robusto(target_frame_bda, ["Data prima immatricolazione:"])
                if not alimentazione:
                    alimentazione = await estrai_campo_robusto(target_frame_bda, ["Alimentazione:"])
                if not classe_cu:
                    classe_cu = await estrai_campo_robusto(target_frame_bda, ["Classe CU di assegnazione:", "Classe CU:"])
                if not compagnia_provenienza:
                    compagnia_provenienza = await estrai_campo_robusto(target_frame_bda, ["Impresa:", "Compagnia di provenienza"])

                # Print di controllo nel terminale Render
                print(f"VALORI FINALI -> Nome: '{nome}', CF: '{cf}', Marca: '{marca}', Modello: '{modello}', CU: '{classe_cu}', Compagnia: '{compagnia_provenienza}'", flush=True)

                # ==========================================
                # 💾 SALVATAGGIO FINALE SU AIRTABLE
                # ==========================================
                print("Mappatura e Salvataggio dei dati completi su Airtable...", flush=True)
                
                raw_fields = {
                    "Nome": nome.strip(),
                    "Codice Fiscale": cf.strip(),
                    "cl_datanascita": data_nas.strip(),
                    "cl_indirizzo": residenza.strip(),
                    "cl_provincia": prov.strip(),
                    "cl_cap": cap.strip(),
                    "Marca": marca.strip(),
                    "Modello": modello.strip(),
                    "KW": kw.strip(),
                    "Data immatricolazione": data_immat.strip(),
                    "Alimentazione": alimentazione.strip(),
                    "Classe CU": classe_cu.strip(),
                    "Compagnia Provenienza": compagnia_provenienza.strip(),
                    "Stato Bot Estrazione": "Dati Estratti"
                }

                cleaned_fields = {k: v for k, v in raw_fields.items() if v != ""}
                payload_trattativa = {"fields": cleaned_fields}

                res = requests.patch(url_trattativa, headers=headers, json=payload_trattativa)
                if res.status_code != 200:
                    print(f"Risposta Error Airtable: {res.text}", flush=True)
                res.raise_for_status()
                print(f"[{record_id}] DOPPIA ESTRAZIONE COMPLETATA CON SUCCESSO!", flush=True)

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
    return {"status": "Doppia Estrazione Avviata", "record_id": record_id}
