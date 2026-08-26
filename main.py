import os
import re
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
RAW_SECRET = os.getenv("UNIPOL_TOTP_SECRET") or ""
UNIPOL_TOTP_SECRET = RAW_SECRET.replace(" ", "").strip().upper()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

def formatta_targa_spazi(targa_raw: str) -> str:
    clean = targa_raw.replace(" ", "").upper()
    if len(clean) == 7:
        return f"{clean[:2]} {clean[2:5]} {clean[5:]}"
    return clean

async def click_elemento_dinamico(page, testo: str, max_tentativi=15) -> bool:
    """Cerca e clicca un testo o sotto-tab su TUTTI gli iframe della pagina."""
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

async def invia_form_prosegui(page, target_frame) -> bool:
    selectors = [
        'input[value*="Prosegui" i]',
        'button:has-text("Prosegui")',
        'a:has-text("Prosegui")',
        '[id*="prosegui" i]',
        'text=/Prosegui/i'
    ]
    for sel in selectors:
        try:
            el = target_frame.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click(force=True)
                return True
        except Exception:
            pass

    for frame in page.frames:
        for sel in selectors:
            try:
                el = frame.locator(sel).first
                if await el.is_visible(timeout=500):
                    await el.click(force=True)
                    return True
            except Exception:
                continue

    for frame in page.frames:
        try:
            clicked = await frame.evaluate('''() => {
                const els = Array.from(document.querySelectorAll('input, button, a, span, div'));
                const btn = els.find(e => (e.value || e.innerText || "").trim().toLowerCase().includes("prosegui"));
                if (btn) {
                    (btn.closest('button, input, a') || btn).click();
                    return true;
                }
                return false;
            }''')
            if clicked:
                return True
        except Exception:
            continue
    return False

async def estrai_dati_globali(page) -> dict:
    """Scansiona tutti gli iframe della pagina per estrarre la mappa completa di etichette e valori."""
    risultato_globale = {}
    for frame in page.frames:
        try:
            dati_frame = await frame.evaluate('''() => {
                let res = {};
                const cells = Array.from(document.querySelectorAll('td, th, label, .ui-outputlabel'));
                cells.forEach(el => {
                    let labelText = (el.innerText || el.textContent || '').trim().replace(/[:*]/g, '');
                    if (labelText && labelText.length > 1 && labelText.length < 50) {
                        let parentTd = el.closest('td, th');
                        let targetTd = parentTd ? parentTd.nextElementSibling : null;
                        
                        if (targetTd) {
                            let val = '';
                            let inputEl = targetTd.querySelector('input:not([type="hidden"]), select, textarea');
                            let pfLabel = targetTd.querySelector('.ui-selectonemenu-label, .ui-selectonemenu-title');
                            
                            if (pfLabel && pfLabel.innerText && pfLabel.innerText.trim()) {
                                val = pfLabel.innerText.trim();
                            } else if (inputEl) {
                                if (inputEl.tagName.toLowerCase() === 'select') {
                                    val = inputEl.options[inputEl.selectedIndex] ? inputEl.options[inputEl.selectedIndex].text : inputEl.value;
                                } else {
                                    val = inputEl.value || inputEl.getAttribute('value') || '';
                                }
                            } else {
                                val = targetTd.innerText || targetTd.textContent || '';
                            }
                            
                            val = val.trim();
                            if (val && !val.toLowerCase().includes('cerca') && val !== 'ui-button' && !val.includes('=== ')) {
                                res[labelText] = val;
                            }
                        }
                    }
                });
                return res;
            }''')
            if dati_frame:
                risultato_globale.update(dati_frame)
        except Exception:
            continue
    return risultato_globale

def cerca_valore_mappa(mappa: dict, keywords: list) -> str:
    for k_map, v_map in mappa.items():
        for kw in keywords:
            if kw.lower() in k_map.lower():
                if v_map and "\n" not in v_map and v_map.lower() not in ["cerca", "seleziona", "ui-button"]:
                    return v_map.strip()
    return ""

async def estrai_dati_preventivatore(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        targa_spazi = formatta_targa_spazi(targa)
        targa_pulita = targa.replace(" ", "").upper()

        print(f"[{record_id}] Avvio estrazione guidata per targa: {targa_pulita} (Formattata: {targa_spazi})", flush=True)
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

                # 2. PREVENTIVATORE UNIPOL
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
                
                data_oggi = datetime.now().strftime("%d/%m/%Y")
                
                await target_frame.evaluate('''
                    ({cipVal, targaVal, dataVal}) => {
                        const dateInput = document.querySelector('input[id*="eftPol"], input[name*="eftPol"]');
                        if (dateInput && dateInput.value.includes(' ')) {
                            dateInput.value = dataVal;
                            dateInput.dispatchEvent(new Event('change', { bubbles: true }));
                        }

                        const cipInput = document.querySelector('input[id*="cSagPrp"], input[name*="cSagPrp"]');
                        if (cipInput) {
                            cipInput.value = cipVal;
                            cipInput.dispatchEvent(new Event('input', { bubbles: true }));
                            cipInput.dispatchEvent(new Event('change', { bubbles: true }));
                        }

                        const allInputs = Array.from(document.querySelectorAll('input[type="text"]:not([disabled])'));
                        let targaInput = allInputs.find(i => {
                            const idName = (i.id + ' ' + i.name).toLowerCase();
                            return !idName.includes('eftpol') && !idName.includes('csagprp') && (idName.includes('trg') || idName.includes('targa'));
                        });

                        if (!targaInput) {
                            targaInput = allInputs.find(i => {
                                const idName = (i.id + ' ' + i.name).toLowerCase();
                                const rowText = (i.closest('tr, td') || {}).innerText || "";
                                return rowText.includes('Targa') && !idName.includes('eftpol');
                            });
                        }

                        if (targaInput) {
                            targaInput.value = targaVal;
                            targaInput.dispatchEvent(new Event('input', { bubbles: true }));
                            targaInput.dispatchEvent(new Event('change', { bubbles: true }));
                            targaInput.dispatchEvent(new Event('blur', { bubbles: true }));
                        }
                    }
                ''', {"cipVal": "125", "targaVal": targa_spazi, "dataVal": data_oggi})

                await page.wait_for_timeout(1000)

                print("Invio form maschera con clic su Prosegui...", flush=True)
                await invia_form_prosegui(page, target_frame)

                # 3. ATTESA RISULTATI E APERTURA APPOSITA SCHEDE AJAX
                print("Attesa calcolo ed elaborazione del Preventivo (fino a 40s)...", flush=True)
                result_detected = False
                for _ in range(40):
                    for frame in page.frames:
                        try:
                            if await frame.locator('text=/GARANZIE E SERVIZI|DATI ASSICURATIVI|POSIZIONE ASSICURATIVA/i').count() > 0:
                                result_detected = True
                                break
                        except Exception:
                            continue
                    if result_detected:
                        break
                    await asyncio.sleep(1)

                mappa_totale = {}

                if result_detected:
                    print("RISULTATI PREVENTIVO RILEVATI! Apertura progressiva dei tab AJAX...", flush=True)
                    
                    schede_da_aprire = [
                        "DATI ASSICURATIVI",
                        "Veicolo/natante",
                        "Figure contrattuali",
                        "Posizione assicurativa"
                    ]

                    for tab in schede_da_aprire:
                        print(f"Clic su scheda: '{tab}'...", flush=True)
                        clicked = await click_elemento_dinamico(page, tab, max_tentativi=10)
                        if clicked:
                            await page.wait_for_timeout(2500) # Attesa scaricamento AJAX della scheda
                            dati_parziali = await estrai_dati_globali(page)
                            mappa_totale.update(dati_parziali)
                else:
                    print("ATTENZIONE: Risultati non rilevati in tempo, provo estrazione diretta...", flush=True)
                    mappa_totale = await estrai_dati_globali(page)

                print("\n================ MAPPA DATI ESTRATTI PRIMEFACES ================", flush=True)
                for k, v in list(mappa_totale.items())[:25]:
                    print(f"  [{k}] => {v}", flush=True)
                print("=================================================================\n", flush=True)

                # Estrattore mirato
                cf = cerca_valore_mappa(mappa_totale, ["Cod.Fisc/P.IVA", "Cod.Fisc", "C.F.", "Codice Fiscale"])
                nome = cerca_valore_mappa(mappa_totale, ["Nominativo", "Proprietario", "Cliente"])
                data_nas = cerca_valore_mappa(mappa_totale, ["Data di nascita", "Nato il"])
                residenza = cerca_valore_mappa(mappa_totale, ["Indirizzo", "Residenza"])
                prov = cerca_valore_mappa(mappa_totale, ["Prov", "Provincia"])
                cap = cerca_valore_mappa(mappa_totale, ["CAP"])
                
                marca = cerca_valore_mappa(mappa_totale, ["Codice marca", "Marca"])
                modello = cerca_valore_mappa(mappa_totale, ["Descrizione modello", "Modello"])
                kw = cerca_valore_mappa(mappa_totale, ["KW"])
                data_immat = cerca_valore_mappa(mappa_totale, ["Data prima immatricolazione", "Immatricolazione"])
                alimentazione_raw = cerca_valore_mappa(mappa_totale, ["Alimentazione"])
                
                # Format per Single-Select Airtable
                alimentazione = ""
                if "BENZINA" in alimentazione_raw.upper():
                    alimentazione = "Benzina"
                elif "DIESEL" in alimentazione_raw.upper():
                    alimentazione = "Diesel"
                elif "GPL" in alimentazione_raw.upper():
                    alimentazione = "GPL"
                elif "METANO" in alimentazione_raw.upper():
                    alimentazione = "Metano"
                elif "ELETTRICA" in alimentazione_raw.upper() or "IBRIDA" in alimentazione_raw.upper():
                    alimentazione = "Ibrida/Elettrica"

                classe_cu = cerca_valore_mappa(mappa_totale, ["Classe CU di assegnazione", "Classe CU", "CU"])
                compagnia_provenienza = cerca_valore_mappa(mappa_totale, ["Impresa", "Compagnia di provenienza"])

                print(f"VALORI REALI ESTRATTI -> Nome: '{nome}', CF: '{cf}', Marca: '{marca}', Modello: '{modello}', CU: '{classe_cu}', Compagnia: '{compagnia_provenienza}'", flush=True)

                # 4. SALVATAGGIO SU AIRTABLE CON RETRY AUTOMATICO
                print("Mappatura e Salvataggio dei dati su Airtable...", flush=True)
                raw_fields = {
                    "Nome": nome,
                    "Codice Fiscale": cf,
                    "cl_datanascita": data_nas,
                    "cl_indirizzo": residenza,
                    "cl_cap": cap,
                    "cl_provincia": prov,
                    "Marca": marca,
                    "Modello": modello,
                    "KW": kw,
                    "Data immatricolazione": data_immat,
                    "Alimentazione": alimentazione,
                    "Classe CU": classe_cu,
                    "Compagnia Provenienza": compagnia_provenienza,
                    "Stato Bot Estrazione": "Dati Estratti"
                }

                cleaned_fields = {k: v for k, v in raw_fields.items() if v != ""}
                payload_trattativa = {"fields": cleaned_fields}

                res = requests.patch(url_trattativa, headers=headers, json=payload_trattativa)
                
                # Fallback in caso di opzione/campo non presente su Airtable
                if res.status_code == 422:
                    print(f"Rilevato errore campo Airtable ({res.text}), riprovo senza campi opzionali...", flush=True)
                    err_txt = res.text
                    if "cl_cap" in err_txt:
                        cleaned_fields.pop("cl_cap", None)
                    if "Alimentazione" in err_txt:
                        cleaned_fields.pop("Alimentazione", None)
                    res = requests.patch(url_trattativa, headers=headers, json={"fields": cleaned_fields})

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
