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

async def click_elemento_dinamico(page, testo: str, max_tentativi=20) -> bool:
    """Cerca un testo o un pulsante su TUTTI gli iframe della pagina ed esegue il clic."""
    for _ in range(max_tentativi):
        for frame in [page.main_frame] + page.frames:
            try:
                locator = frame.get_by_text(testo, exact=False).first
                if await locator.is_visible(timeout=300):
                    await locator.click(force=True)
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return False

async def cattura_testo_globale(page) -> str:
    """Estrae sia il testo visibile sia tutti i valori contenuti dentro i campi input/select di ogni iframe."""
    testo_aggregato = []
    for frame in page.frames:
        try:
            dump = await frame.evaluate('''() => {
                let result = [];
                if (document.body) {
                    result.push("=== BODY TEXT ===");
                    result.push(document.body.innerText);
                }
                result.push("=== INPUT & SELECT VALUES ===");
                const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
                inputs.forEach(i => {
                    let val = i.value || "";
                    if (val.trim() !== "" && val.trim() !== "125") {
                        let name = i.name || i.id || "";
                        let closestTd = i.closest('td');
                        let label = "";
                        if (closestTd && closestTd.previousElementSibling) {
                            label = closestTd.previousElementSibling.innerText.trim();
                        }
                        result.push(`[FIELD] ${label} (${name}) => ${val.trim()}`);
                    }
                });
                return result.join("\\n");
            }''')
            if dump and dump.strip():
                testo_aggregato.append(dump)
        except Exception:
            continue
    return "\n--- FRAME SEPARATOR ---\n".join(testo_aggregato)

def estrai_con_regex(pattern: str, testo: str, default: str = "") -> str:
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

            await context.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

            page = await context.new_page()

            try:
                # 1. LOGIN & MFA
                print("1/4 Inserimento Username e Password...", flush=True)
                await page.goto("https://essig.unipolsai.it/my-policy", wait_until="commit", timeout=40000)
                
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
                    await page.goto(leonardo_url, wait_until="commit", timeout=20000)
                except Exception as e:
                    print(f"Interferenza di rete ({e}), proseguo...", flush=True)

                await page.wait_for_timeout(4000)

                # 2. PREVENTIVATORE UNIPOL (Navigazione Multi-Frame Dinamica)
                print("Ingresso nel Preventivatore Unipol...", flush=True)
                await click_elemento_dinamico(page, "PRODOTTI")
                await page.wait_for_timeout(1500)

                print("Selezione ALTRI PRODOTTI DANNI...", flush=True)
                await click_elemento_dinamico(page, "ALTRI PRODOTTI DANNI")
                await page.wait_for_timeout(800)
                await click_elemento_dinamico(page, "CONFERMA")
                await page.wait_for_timeout(1500)

                print("Selezione AUTO/NATANTI...", flush=True)
                await click_elemento_dinamico(page, "AUTO/NATANTI")
                await page.wait_for_timeout(800)
                await click_elemento_dinamico(page, "CONFERMA")
                await page.wait_for_timeout(1500)

                print("Selezione RCA SINGOLE...", flush=True)
                await click_elemento_dinamico(page, "RCA SINGOLE")
                await page.wait_for_timeout(1000)
                await click_elemento_dinamico(page, "CONFERMA")

                print("Attesa caricamento maschera Preventivo RCA...", flush=True)
                target_frame = None
                for _ in range(30):
                    for frame in page.frames:
                        if "DanniWeb" in frame.url or await frame.locator('input[type="text"]:enabled').count() >= 2:
                            target_frame = frame
                            break
                    if target_frame:
                        break
                    await asyncio.sleep(1)

                if not target_frame:
                    target_frame = page.main_frame

                print(f"Compilazione CIP (125) e Targa ('{targa_spazi}')...", flush=True)
                
                # Compilazione filtrando rigidamente solo campi di testo editabili (esclude checkbox/radio)
                cip_input = target_frame.locator('input[type="text"][id*="cSagPrp" i], input[type="text"][name*="cSagPrp" i]').first
                if await cip_input.count() > 0 and await cip_input.is_visible():
                    await cip_input.fill("125")

                targa_input = target_frame.locator('input[type="text"][id*="trg" i], input[type="text"][name*="trg" i], td:has-text("Targa") + td input[type="text"]:enabled').first
                if await targa_input.count() > 0 and await targa_input.is_visible():
                    await targa_input.fill(targa_spazi)
                else:
                    inputs_editabili = []
                    all_text_inputs = target_frame.locator('input[type="text"]')
                    for i in range(await all_text_inputs.count()):
                        inp = all_text_inputs.nth(i)
                        if await inp.is_visible() and await inp.is_enabled():
                            is_readonly = await inp.get_attribute("readonly") or await inp.get_attribute("aria-readonly")
                            if not is_readonly or is_readonly == "false":
                                inputs_editabili.append(inp)
                    if len(inputs_editabili) >= 2:
                        await inputs_editabili[0].fill("125")
                        await inputs_editabili[1].fill(targa_spazi)

                print("Invio form maschera con clic su Prosegui...", flush=True)
                prosegui_btn = target_frame.locator('input[value*="Prosegui" i], button:has-text("Prosegui"), a:has-text("Prosegui")').first
                await prosegui_btn.click()

                # 3. ATTESA DINAMICA RISULTATI PREVENTIVO
                print("Attesa calcolo ed elaborazione del Preventivo (fino a 40s)...", flush=True)
                result_frame = None
                for _ in range(40):
                    for frame in page.frames:
                        try:
                            if await frame.locator('text=/GARANZIE E SERVIZI|DATI ASSICURATIVI|POSIZIONE ASSICURATIVA/i').count() > 0:
                                result_frame = frame
                                break
                        except Exception:
                            continue
                    if result_frame:
                        break
                    await asyncio.sleep(1)

                if result_frame:
                    print("RISULTATI PREVENTIVO RILEVATI! Apertura schede...", flush=True)
                    for tab_name in ["DATI ASSICURATIVI", "Figure contrattuali", "Posizione assicurativa", "Veicolo"]:
                        try:
                            t = result_frame.locator(f'text="{tab_name}"').first
                            if await t.is_visible(timeout=1000):
                                await t.click(force=True)
                                await page.wait_for_timeout(1200)
                        except Exception:
                            continue
                else:
                    print("ATTENZIONE: Pagina risultati non rilevata in tempo, eseguo comunque il dump...", flush=True)

                # Cattura e stampa del Dump Testuale
                print("Cattura del dump testuale completo...", flush=True)
                testo_completo = await cattura_testo_globale(page)

                print("\n==================== STAMPA DUMP TESTO UNIPOL ====================", flush=True)
                print(testo_completo[:3500], flush=True)
                print("===================================================================\n", flush=True)

                # Regex universali di estrazione
                cf = estrai_con_regex(r"\b([A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z])\b", testo_completo)
                nome = estrai_con_regex(r"(?:Nominativo|PROPRIETARIO|Cliente)\s*[:=>\n\-]+\s*([A-Z\s\']+)", testo_completo)
                data_nas = estrai_con_regex(r"(?:Data di nascita|Nato il)\s*[:=>\n\-]+\s*(\d{2}/\d{2}/\d{4})", testo_completo)
                residenza = estrai_con_regex(r"(?:Indirizzo|Residenza)\s*[:=>\n\-]+\s*([^\n]+)", testo_completo)
                prov = estrai_con_regex(r"(?:Prov|Provincia)\s*[:=>\n\-]+\s*([A-Z]{2})\b", testo_completo)
                
                marca = estrai_con_regex(r"(?:Codice marca|Marca)\s*[:=>\n\-]+\s*([^\n]+)", testo_completo)
                modello = estrai_con_regex(r"(?:Descrizione modello|Modello)\s*[:=>\n\-]+\s*([^\n]+)", testo_completo)
                kw = estrai_con_regex(r"\bKW\s*[:=>\n\-]+\s*(\d+(?:[.,]\d+)?)", testo_completo)
                data_immat = estrai_con_regex(r"(?:Data prima immatricolazione|Immatricolazione)\s*[:=>\n\-]+\s*(\d{2}/\d{2}/\d{4})", testo_completo)
                alimentazione = estrai_con_regex(r"(?:Alimentazione)\s*[:=>\n\-]+\s*([A-Z0-9\s]+)", testo_completo)
                
                classe_cu = estrai_con_regex(r"(?:Classe CU|CU di assegnazione|CU)\s*[:=>\n\-]+\s*(\d+)", testo_completo)
                compagnia_provenienza = estrai_con_regex(r"(?:Impresa|Compagnia di provenienza|Compagnia)\s*[:=>\n\-]+\s*([^\n]+)", testo_completo)

                print(f"VALORI ESTRATTI CON DUMP -> Nome: '{nome}', CF: '{cf}', Marca: '{marca}', Modello: '{modello}', CU: '{classe_cu}', Compagnia: '{compagnia_provenienza}'", flush=True)

                # 4. SALVATAGGIO SU AIRTABLE
                print("Mappatura e Salvataggio dei dati su Airtable...", flush=True)
                raw_fields = {
                    "Nome": nome.strip(),
                    "Codice Fiscale": cf.strip(),
                    "cl_datanascita": data_nas.strip(),
                    "cl_indirizzo": residenza.strip(),
                    "cl_provincia": prov.strip(),
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

@app.get("/")
async def root():
    return {"status": "Bot Unipol Online"}

@app.post("/estrai")
@app.post("/estrai/")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")
    
    background_tasks.add_task(estrai_dati_preventivatore, record_id, targa, data_nascita)
    return {"status": "Estrazione Avviata", "record_id": record_id}
