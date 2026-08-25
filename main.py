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
    """Formatta 'FA859BH' in 'FA 859 BH' per il Preventivatore Unipol."""
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

async def get_val_from_any_frame(page, keywords: list) -> str:
    """Scansiona DINAMICAMENTE tutti gli iframe attivi per trovare il valore affiancato alle etichette."""
    for frame in page.frames:
        for kw in keywords:
            try:
                tds = frame.locator(f'td:has-text("{kw}"), th:has-text("{kw}")')
                count = await tds.count()
                for i in range(count):
                    td = tds.nth(i)
                    if await td.is_visible(timeout=200):
                        next_td = td.locator('xpath=following-sibling::td[1]')
                        if await next_td.count() > 0:
                            inp = next_td.locator('input, select').first
                            if await inp.count() > 0 and await inp.is_visible(timeout=200):
                                val = await inp.input_value()
                                if val and val.strip() and val.strip() != "125":
                                    return val.strip()
                            txt = await next_td.text_content()
                            if txt and txt.strip():
                                return txt.strip()
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

                # Compilazione CIP 125 e Targa su qualunque iframe visibile
                print("Compilazione maschera Preventivo...", flush=True)
                for frame in page.frames:
                    try:
                        c_inp = frame.locator('input[name*="sub" i], input[name*="cip" i], td:has-text("CIP") + td input').first
                        if await c_inp.is_visible(timeout=500):
                            await c_inp.fill("125")

                        t_inp = frame.locator('input[name*="targa" i], input[id*="targa" i], td:has-text("Targa") + td input').first
                        if await t_inp.is_visible(timeout=500):
                            await t_inp.fill(targa_spazi)
                    except Exception:
                        continue

                for frame in page.frames:
                    try:
                        btn = frame.locator('button:has-text("Prosegui"), input[value*="Prosegui" i], a:has-text("Prosegui")').first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            break
                    except Exception:
                        continue

                await page.wait_for_timeout(6000)

                # Estrazione Scheda DATI ASSICURATIVI
                for frame in page.frames:
                    try:
                        tab1 = frame.locator('text="DATI ASSICURATIVI", td:has-text("DATI ASSICURATIVI")').first
                        if await tab1.is_visible(timeout=500):
                            await tab1.click(force=True)
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass

                marca = await get_val_from_any_frame(page, ["Codice marca", "Marca"])
                modello = await get_val_from_any_frame(page, ["Descrizione modello", "Modello"])
                kw = await get_val_from_any_frame(page, ["KW"])
                data_immat = await get_val_from_any_frame(page, ["Data prima immatricolazione"])
                alimentazione = await get_val_from_any_frame(page, ["Alimentazione"])

                # Estrazione Scheda Figure contrattuali
                for frame in page.frames:
                    try:
                        tab2 = frame.locator('text="Figure contrattuali", td:has-text("Figure contrattuali")').first
                        if await tab2.is_visible(timeout=500):
                            await tab2.click(force=True)
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass

                nome = await get_val_from_any_frame(page, ["Nominativo", "PROPRIETARIO"])
                cf = await get_val_from_any_frame(page, ["Cod.Fisc", "C.F"])
                data_nas = await get_val_from_any_frame(page, ["Data di nascita"])
                residenza = await get_val_from_any_frame(page, ["Indirizzo"])
                prov = await get_val_from_any_frame(page, ["Prov"])
                cap = await get_val_from_any_frame(page, ["CAP"])

                # Estrazione Scheda Posizione assicurativa
                for frame in page.frames:
                    try:
                        tab3 = frame.locator('text="Posizione assicurativa", td:has-text("Posizione assicurativa")').first
                        if await tab3.is_visible(timeout=500):
                            await tab3.click(force=True)
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass

                classe_cu = await get_val_from_any_frame(page, ["Classe CU di assegnazione", "Classe CU"])
                compagnia_provenienza = await get_val_from_any_frame(page, ["Compagnia di provenienza", "Impresa"])

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
                await page.wait_for_timeout(3000)

                print(f"Inserimento targa {targa_pulita} in BDA...", flush=True)
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

                # Fallback dati Veicolo da ANIA se mancanti
                for _ in range(10):
                    if not marca: marca = await get_val_from_any_frame(page, ["Marca:"])
                    if not modello: modello = await get_val_from_any_frame(page, ["Descrizione modello:"])
                    if not kw: kw = await get_val_from_any_frame(page, ["KW:"])
                    if not data_immat: data_immat = await get_val_from_any_frame(page, ["Data prima immatricolazione:"])
                    if not alimentazione: alimentazione = await get_val_from_any_frame(page, ["Alimentazione:"])
                    if marca or kw: break
                    await asyncio.sleep(1)

                # Clic su Visualizza Attestato BDA
                for frame in page.frames:
                    try:
                        btn_att = frame.locator('button:has-text("Visualizza Attestato"), input[value*="Attestato" i]').first
                        if await btn_att.is_visible(timeout=500):
                            await btn_att.click()
                            await page.wait_for_timeout(2500)
                            break
                    except Exception:
                        continue

                # Fallback CU e Compagnia da ANIA se mancanti
                for _ in range(10):
                    if not classe_cu: classe_cu = await get_val_from_any_frame(page, ["Classe CU di assegnazione:", "Classe CU:"])
                    if not compagnia_provenienza: compagnia_provenienza = await get_val_from_any_frame(page, ["Impresa:", "Compagnia di provenienza"])
                    if classe_cu: break
                    await asyncio.sleep(1)

                print(f"VALORI FINALI ESTRATTI -> Nome: '{nome}', CF: '{cf}', Marca: '{marca}', Modello: '{modello}', CU: '{classe_cu}', Compagnia: '{compagnia_provenienza}'", flush=True)

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
                print(f"[{record_id}] ESTRAZIONE E MAPPATURA COMPLETATE CON SUCCESSO!", flush=True)

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
