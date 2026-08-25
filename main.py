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

async def get_val_from_any_frame(page, keywords: list) -> str:
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

                # Apertura Modal PRODOTTI
                prodotti_btn = page.locator('button:has-text("PRODOTTI"), .p-button:has-text("PRODOTTI")').first
                await prodotti_btn.click(force=True)
                await page.wait_for_timeout(1200)

                # 1. ALTRI PRODOTTI DANNI
                print("Selezione ALTRI PRODOTTI DANNI...", flush=True)
                await page.get_by_text("ALTRI PRODOTTI DANNI").first.click(force=True)
                await page.wait_for_timeout(800)
                await page.locator('button:has-text("CONFERMA")').last.click(force=True)
                await page.wait_for_timeout(1500)

                # 2. AUTO/NATANTI
                print("Selezione AUTO/NATANTI...", flush=True)
                await page.get_by_text("AUTO/NATANTI").first.click(force=True)
                await page.wait_for_timeout(800)
                await page.locator('button:has-text("CONFERMA")').last.click(force=True)
                await page.wait_for_timeout(1500)

                # 3. RCA SINGOLE
                print("Selezione RCA SINGOLE...", flush=True)
                await page.get_by_text("RCA SINGOLE").first.click(force=True)
                await page.wait_for_timeout(1000)
                await page.locator('button:has-text("CONFERMA")').last.click(force=True)

                # Attesa e intercettazione dell'iframe DanniWeb
                print("Attesa caricamento effettivo maschera Preventivo RCA...", flush=True)
                target_frame = None
                for _ in range(30):
                    for frame in page.frames:
                        if "DanniWeb" in frame.url or await frame.locator('text=/Emissione\\/Preventivo|Targa/i').count() > 0:
                            target_frame = frame
                            break
                    if target_frame:
                        break
                    await asyncio.sleep(1)

                if not target_frame:
                    target_frame = page.main_frame

                # Compilazione CIP 125 e Targa
                print(f"Compilazione CIP (125) e Targa ('{targa_spazi}')...", flush=True)
                
                cip_box = target_frame.locator('tr:has-text("CIP") input, input[name*="sub" i], input[name*="cip" i]').first
                await cip_box.wait_for(state="visible", timeout=20000)
                await cip_box.fill("125")

                targa_box = target_frame.locator('tr:has-text("Targa") input, input[name*="targa" i], input[id*="targa" i]').first
                await targa_box.wait_for(state="visible", timeout=20000)
                await targa_box.fill(targa_spazi)

                print("Invio form maschera con clic su Prosegui...", flush=True)
                prosegui_btn = target_frame.locator('input[value="Prosegui"], button:has-text("Prosegui")').first
                await prosegui_btn.click()

                # Attesa elaborazione Preventivo
                print("Attesa elaborazione risultati Preventivo...", flush=True)
                await page.wait_for_timeout(6000)

                result_frame = None
                for _ in range(15):
                    for frame in page.frames:
                        if await frame.locator('text="DATI ASSICURATIVI"').count() > 0 or await frame.locator('text="GARANZE E SERVIZI"').count() > 0:
                            result_frame = frame
                            break
                    if result_frame:
                        break
                    await asyncio.sleep(1)

                if result_frame:
                    print("Ingresso confermato nel Preventivo! Estrazione schede...", flush=True)
                    
                    # 1. Clic scheda DATI ASSICURATIVI
                    tab_dati = result_frame.locator('text="DATI ASSICURATIVI"').first
                    await tab_dati.click(force=True)
                    await page.wait_for_timeout(2000)

                    # Sub-tab Veicolo/attestato
                    sub_veic = result_frame.locator('text=/Veicolo/i').first
                    if await sub_veic.is_visible():
                        await sub_veic.click(force=True)
                        await page.wait_for_timeout(1000)

                    marca = await get_val_from_any_frame(page, ["Codice marca", "Marca"])
                    modello = await get_val_from_any_frame(page, ["Descrizione modello", "Modello"])
                    kw = await get_val_from_any_frame(page, ["KW"])
                    data_immat = await get_val_from_any_frame(page, ["Data prima immatricolazione"])
                    alimentazione = await get_val_from_any_frame(page, ["Alimentazione"])

                    # 2. Sub-tab Figure contrattuali
                    sub_fig = result_frame.locator('text="Figure contrattuali"').first
                    if await sub_fig.is_visible():
                        await sub_fig.click(force=True)
                        await page.wait_for_timeout(1500)

                    nome = await get_val_from_any_frame(page, ["Nominativo", "PROPRIETARIO"])
                    cf = await get_val_from_any_frame(page, ["Cod.Fisc", "C.F"])
                    data_nas = await get_val_from_any_frame(page, ["Data di nascita"])
                    residenza = await get_val_from_any_frame(page, ["Indirizzo"])
                    prov = await get_val_from_any_frame(page, ["Prov"])
                    cap = await get_val_from_any_frame(page, ["CAP"])

                    # 3. Sub-tab Posizione assicurativa
                    sub_pos = result_frame.locator('text="Posizione assicurativa"').first
                    if await sub_pos.is_visible():
                        await sub_pos.click(force=True)
                        await page.wait_for_timeout(1500)

                    classe_cu = await get_val_from_any_frame(page, ["Classe CU di assegnazione", "Classe CU"])
                    compagnia_provenienza = await get_val_from_any_frame(page, ["Compagnia di provenienza", "Impresa"])

                else:
                    nome, cf, data_nas, residenza, prov, cap = "", "", "", "", "", ""
                    marca, modello, kw, data_immat, alimentazione = "", "", "", "", ""
                    classe_cu, compagnia_provenienza = "", ""

                # ==========================================
                # 🔓 MODULO 2: CONSULTAZIONE BDA ANIA (FASE B)
                # ==========================================
                print("Passaggio al Modulo BDA per Storia Assicurativa...", flush=True)
                
                try:
                    str_check = page.get_by_text("Strumenti", exact=True).first
                    if not await str_check.is_visible(timeout=1500):
                        await page.goto(leonardo_url, wait_until="commit", timeout=12000)
                except Exception:
                    pass

                await page.wait_for_timeout(1500)

                await page.get_by_text("Strumenti", exact=True).first.click(force=True)
                await page.wait_for_timeout(1000)
                await page.get_by_text("Danni", exact=True).first.click(force=True)
                await page.wait_for_timeout(1000)
                await page.get_by_text("RCA AUTO", exact=True).first.click(force=True)
                await page.wait_for_timeout(1000)
                await page.get_by_text("CONSULTAZIONE BDA", exact=True).first.click(force=True)
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

                await page.wait_for_timeout(3000)

                # Fallback dati Veicolo da ANIA se mancanti
                for _ in range(8):
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
                            await page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue

                # Fallback CU e Compagnia da ANIA se mancanti
                for _ in range(8):
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
