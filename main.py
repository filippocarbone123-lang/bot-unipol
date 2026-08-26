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

async def click_elemento_dinamico(page, testo: str, max_tentativi=20) -> bool:
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

async def estrai_campo_label(frame, keywords: list) -> str:
    """Estrae il valore reale affiancato alle etichette nelle tabelle HTML/PrimeFaces."""
    try:
        valore = await frame.evaluate('''(kws) => {
            const allElements = Array.from(document.querySelectorAll('td, th, label, span, div'));
            for (let kw of kws) {
                for (let el of allElements) {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (text === kw.toLowerCase() || (text.includes(kw.toLowerCase()) && text.length < 45)) {
                        let cell = el.closest('td, th');
                        let nextCell = cell ? cell.nextElementSibling : null;
                        
                        if (!nextCell && cell && cell.parentElement) {
                            let tds = Array.from(cell.parentElement.querySelectorAll('td, th'));
                            let idx = tds.indexOf(cell);
                            if (idx >= 0 && idx + 1 < tds.length) {
                                nextCell = tds[idx + 1];
                            }
                        }

                        if (nextCell) {
                            // 1. Cerca dentro Select o Input
                            let inp = nextCell.querySelector('input:not([type="hidden"]), select, textarea');
                            if (inp) {
                                if (inp.tagName.toLowerCase() === 'select') {
                                    let opt = inp.options[inp.selectedIndex];
                                    if (opt && opt.text && opt.text.trim()) return opt.text.trim();
                                }
                                if (inp.value && inp.value.trim()) return inp.value.trim();
                                let attrVal = inp.getAttribute('value');
                                if (attrVal && attrVal.trim()) return attrVal.trim();
                            }

                            // 2. Componenti PrimeFaces (.ui-selectonemenu-label)
                            let pfLabel = nextCell.querySelector('.ui-selectonemenu-label, .ui-selectonemenu-title');
                            if (pfLabel && pfLabel.innerText && pfLabel.innerText.trim()) {
                                let t = pfLabel.innerText.trim();
                                if (!t.toLowerCase().includes('seleziona')) return t;
                            }

                            // 3. Testo semplice nella cella adiacente
                            let cellText = (nextCell.innerText || nextCell.textContent || '').trim();
                            if (cellText && cellText.toLowerCase() !== 'cerca' && cellText !== 'ui-button') {
                                return cellText;
                            }
                        }
                    }
                }
            }
            return '';
        }''', keywords)
        return valore if valore else ""
    except Exception:
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

                # 3. NAVIGAZIONE SCHEDE AJAX ED ESTRAZIONE
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

                data_immat, alimentazione, kw, marca, modello = "", "", "", "", ""
                nome, cf, data_nas, residenza, cap, prov = "", "", "", "", "", ""
                classe_cu, compagnia_provenienza = "", ""

                if result_frame:
                    print("RISULTATI PREVENTIVO RILEVATI! Lettura schede...", flush=True)

                    # --- SCHEDA 1: DATI ASSICURATIVI / Veicolo ---
                    tab_dati = result_frame.locator('text="DATI ASSICURATIVI"').first
                    if await tab_dati.is_visible():
                        await tab_dati.click(force=True)
                        await page.wait_for_timeout(2000)

                    sub_veic = result_frame.locator('text=/Veicolo/i').first
                    if await sub_veic.is_visible():
                        await sub_veic.click(force=True)
                        await page.wait_for_timeout(2000)

                    data_immat = await estrai_campo_label(result_frame, ["Data prima immatricolazione"])
                    alimentazione = await estrai_campo_label(result_frame, ["Alimentazione"])
                    kw = await estrai_campo_label(result_frame, ["KW"])
                    marca = await estrai_campo_label(result_frame, ["Codice marca", "Marca"])
                    modello = await estrai_campo_label(result_frame, ["Descrizione modello", "Modello"])

                    # --- SCHEDA 2: Figure contrattuali ---
                    sub_fig = result_frame.locator('text="Figure contrattuali"').first
                    if await sub_fig.is_visible():
                        await sub_fig.click(force=True)
                        await page.wait_for_timeout(2500) # Attesa carica AJAX

                    nome = await estrai_campo_label(result_frame, ["Nominativo", "PROPRIETARIO"])
                    cf = await estrai_campo_label(result_frame, ["Cod.Fisc/P.IVA", "Cod.Fisc", "C.F"])
                    data_nas = await estrai_campo_label(result_frame, ["Data di nascita"])
                    residenza = await estrai_campo_label(result_frame, ["Indirizzo"])
                    cap = await estrai_campo_label(result_frame, ["CAP"])
                    prov = await estrai_campo_label(result_frame, ["Prov"])

                    # --- SCHEDA 3: Posizione assicurativa ---
                    sub_pos = result_frame.locator('text="Posizione assicurativa"').first
                    if await sub_pos.is_visible():
                        await sub_pos.click(force=True)
                        await page.wait_for_timeout(2500) # Attesa carica AJAX

                    classe_cu = await estrai_campo_label(result_frame, ["Classe CU di assegnazione", "Classe CU"])
                    compagnia_provenienza = await estrai_campo_label(result_frame, ["Impresa", "Compagnia di provenienza"])

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
                
                # Se Airtable segnala un campo sconosciuto/invalido (es. cl_cap), lo rimuove e riprova
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
