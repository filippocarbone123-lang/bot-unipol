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
    return False

async def clicca_subtab(page, result_frame, nomi_tab: list) -> bool:
    """Clicca direttamente la sotto-scheda cercando tra i possibili titoli."""
    frames = [result_frame] + [f for f in page.frames if f != result_frame]
    for frame in frames:
        if not frame:
            continue
        for nome in nomi_tab:
            for selector in [
                f'a:has-text("{nome}")',
                f'button:has-text("{nome}")',
                f'span:has-text("{nome}")',
                f'td:has-text("{nome}")',
                f'text="{nome}"'
            ]:
                try:
                    el = frame.locator(selector).first
                    if await el.is_visible(timeout=600):
                        await el.click(force=True)
                        return True
                except Exception:
                    continue
    return False

async def estrai_tutti_i_campi_dalla_pagina(page) -> dict:
    """Estrae i dati da qualsiasi elemento (inclusi readonly, disabled, select e span) rispettando la struttura DOM."""
    mappa = {}
    for frame in page.frames:
        try:
            dati = await frame.evaluate('''() => {
                let res = {};
                
                const pulisci = (s) => (s || '').trim().replace(/[:*]/g, '');

                const registra = (lbl, val) => {
                    let label = pulisci(lbl);
                    let valore = pulisci(val);
                    if (!label || label.length < 2 || label.length > 60 || /^\\d+$/.test(label)) return;
                    
                    let valLow = valore.toLowerCase();
                    if (valore && 
                        !valLow.includes('seleziona') && 
                        !valLow.includes('cerca') && 
                        valore !== 'ui-button' && 
                        valore !== '125' && 
                        !valore.includes('javax.faces') &&
                        !valore.includes('\\n')) {
                        res[label] = valore;
                    }
                };

                // 1. Scan Input, Select e Textarea
                const inputs = Array.from(document.querySelectorAll('input, select, textarea'));
                inputs.forEach(i => {
                    let val = '';
                    if (i.tagName.toLowerCase() === 'select') {
                        val = i.options[i.selectedIndex] ? i.options[i.selectedIndex].text : '';
                    } else {
                        val = i.value || i.getAttribute('value') || '';
                    }
                    
                    let labelText = '';
                    if (i.id) {
                        let l = document.querySelector(`label[for="${i.id}"]`);
                        if (l) labelText = l.innerText || l.textContent;
                    }
                    if (!labelText) {
                        let parent = i.closest('td, th, .ui-panelgrid-cell, div');
                        let prev = parent ? parent.previousElementSibling : null;
                        if (prev) labelText = prev.innerText || prev.textContent;
                    }
                    registra(labelText, val);
                });

                // 2. Scan PrimeFaces Dropdown
                const pfLabels = Array.from(document.querySelectorAll('.ui-selectonemenu-label, .ui-selectonemenu-title'));
                pfLabels.forEach(pf => {
                    let txt = pf.innerText || pf.textContent || '';
                    let parent = pf.closest('td, th, .ui-panelgrid-cell, div');
                    let prev = parent ? parent.previousElementSibling : null;
                    if (prev) registra(prev.innerText || prev.textContent, txt);
                });

                // 3. Scan celle TD/TH adiacenti
                const rows = Array.from(document.querySelectorAll('tr, .ui-panelgrid-cell, .ui-g'));
                rows.forEach(r => {
                    const children = Array.from(r.children);
                    if (children.length >= 2) {
                        for (let idx = 0; idx < children.length - 1; idx++) {
                            let lbl = children[idx].innerText || children[idx].textContent || '';
                            let valCell = children[idx + 1];
                            let val = '';
                            let inp = valCell.querySelector('input, select, textarea');
                            if (inp) {
                                val = inp.tagName.toLowerCase() === 'select' ? 
                                    (inp.options[inp.selectedIndex] ? inp.options[inp.selectedIndex].text : '') : 
                                    (inp.value || inp.getAttribute('value') || '');
                            } else {
                                val = valCell.innerText || valCell.textContent || '';
                            }
                            registra(lbl, val);
                        }
                    }
                });

                return res;
            }''')
            if dati:
                mappa.update(dati)
        except Exception:
            continue
    return mappa

def cerca_in_mappa(mappa: dict, keywords: list) -> str:
    scarti = ["proprietario", "contraente", "usufruttuario", "conducente", "aggiungi una figura", "0", "cerca", "m20", "m30", "m40", "na", "--"]
    for k_map, v_map in mappa.items():
        k_clean = k_map.strip().lower()
        v_clean = v_map.strip()
        
        if not v_clean or v_clean.lower() in scarti or "\n" in v_clean:
            continue

        for kw in keywords:
            kw_clean = kw.strip().lower()
            # Uso dei confini di parola (\b) per evitare che "CU" combaci con "di CUi"
            pattern = r'\b' + re.escape(kw_clean) + r'\b'
            if re.search(pattern, k_clean):
                # Sanificazione specifica per la Classe CU (es. "c013" -> "13")
                if kw_clean in ["classe cu", "cu", "classe cu assegnata", "classe cu di assegnazione"]:
                    m_num = re.search(r'\b(\d{1,2})\b', v_clean)
                    if m_num:
                        return m_num.group(1)
                return v_clean
    return ""

def pulisci_valore(valore: str) -> str:
    if not valore:
        return ""
    v = valore.strip()
    scarti = ["cerca", "seleziona", "ui-button", "codice marca", "descrizione modello", "impresa", "compagnia", "proprietario", "contraente", "0", "m20", "m30", "m40", "na", "--"]
    if v.lower() in scarti or "\n" in v or len(v) > 120:
        return ""
    return v

async def estrai_dati_preventivatore(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json"
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        targa_spazi = formatta_targa_spazi(targa)
        targa_pulita = targa.replace(" ", "").upper()

        print(f"[{record_id}] Avvio estrazione corretta per targa: {targa_pulita} (Formattata: {targa_spazi})", flush=True)
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

                # 3. ATTESA RISULTATI E SCANSIONE SUB-TAB
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

                if not result_frame:
                    result_frame = target_frame

                mappa_totale = {}
                print("RISULTATI PREVENTIVO RILEVATI! Clic e lettura delle 3 schede...", flush=True)

                # --- SCHEDA 1: DATI ASSICURATIVI -> Veicolo/natante ---
                print("1/3 Clic su 'DATI ASSICURATIVI' / 'Veicolo/natante'...", flush=True)
                await clicca_subtab(page, result_frame, ["DATI ASSICURATIVI"])
                await page.wait_for_timeout(1000)
                await clicca_subtab(page, result_frame, ["Veicolo/natante", "Veicolo"])
                await page.wait_for_timeout(2500)
                d1 = await estrai_tutti_i_campi_dalla_pagina(page)
                print(f" -> Scheda Veicolo: {len(d1)} campi estratti", flush=True)
                mappa_totale.update(d1)

                # --- SCHEDA 2: Figure contrattuali ---
                print("2/3 Clic su 'Figure contrattuali'...", flush=True)
                await clicca_subtab(page, result_frame, ["Figure contrattuali"])
                await page.wait_for_timeout(2500)
                d2 = await estrai_tutti_i_campi_dalla_pagina(page)
                print(f" -> Scheda Figure Contrattuali: {len(d2)} campi estratti", flush=True)
                mappa_totale.update(d2)

                # --- SCHEDA 3: Posizione assicurativa ---
                print("3/3 Clic su 'Posizione assicurativa'...", flush=True)
                await clicca_subtab(page, result_frame, ["Posizione assicurativa"])
                await page.wait_for_timeout(2500)
                d3 = await estrai_tutti_i_campi_dalla_pagina(page)
                print(f" -> Scheda Posizione Assicurativa: {len(d3)} campi estratti", flush=True)
                mappa_totale.update(d3)

                print("\n================ MAPPA DATI ESTRATTI DAL DOM ================", flush=True)
                for k, v in list(mappa_totale.items())[:30]:
                    print(f"  [{k}] => {v}", flush=True)
                print("=============================================================\n", flush=True)

                # Estrattore mirato
                cf = cerca_in_mappa(mappa_totale, ["Cod.Fisc/P.IVA", "Cod.Fisc", "C.F.", "Codice Fiscale"])
                nome = cerca_in_mappa(mappa_totale, ["Nominativo", "Cliente"])
                data_nas = cerca_in_mappa(mappa_totale, ["Data di nascita", "Nato il"])
                residenza = cerca_in_mappa(mappa_totale, ["Indirizzo", "Residenza"])
                prov = cerca_in_mappa(mappa_totale, ["Prov", "Provincia"])
                cap = cerca_in_mappa(mappa_totale, ["CAP"])
                
                marca = cerca_in_mappa(mappa_totale, ["Codice marca", "Marca"])
                modello = cerca_in_mappa(mappa_totale, ["Descrizione modello", "Modello"])
                kw_raw = cerca_in_mappa(mappa_totale, ["KW"])
                data_immat = cerca_in_mappa(mappa_totale, ["Data prima immatricolazione", "Immatricolazione"])
                alimentazione_raw = cerca_in_mappa(mappa_totale, ["Alimentazione"])

                # Conversione numerica KW
                kw = None
                if kw_raw:
                    clean_kw = re.sub(r'[^\d.,]', '', kw_raw).replace(',', '.')
                    if clean_kw:
                        try:
                            num = float(clean_kw)
                            if num > 0:
                                kw = int(num) if num.is_integer() else num
                        except ValueError:
                            kw = None

                # Single-Select Alimentazione
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

                classe_cu = cerca_in_mappa(mappa_totale, ["Classe CU assegnata", "Classe CU di assegnazione", "Classe CU"])
                compagnia_provenienza = cerca_in_mappa(mappa_totale, ["Compagnia di provenienza", "Impresa", "Compagnia"])

                nome_clean = pulisci_valore(nome)
                cf_clean = pulisci_valore(cf)
                marca_clean = pulisci_valore(marca)
                modello_clean = pulisci_valore(modello)
                compagnia_clean = pulisci_valore(compagnia_provenienza)

                print(f"VALORI REALI ESTRATTI -> Nome: '{nome_clean}', CF: '{cf_clean}', Marca: '{marca_clean}', Modello: '{modello_clean}', KW: {kw}, CU: '{classe_cu}', Compagnia: '{compagnia_clean}'", flush=True)

                # 4. SALVATAGGIO SU AIRTABLE CON SANIFICAZIONE AUTOMATICA
                print("Mappatura e Salvataggio dei dati su Airtable...", flush=True)
                
                raw_fields = {
                    "Codice Fiscale": cf_clean,
                    "cl_datanascita": data_nas,
                    "cl_indirizzo": residenza,
                    "cl_cap": cap,
                    "cl_provincia": prov,
                    "Marca": marca_clean,
                    "Modello": modello_clean,
                    "KW": kw,
                    "Data immatricolazione": data_immat,
                    "Alimentazione": alimentazione,
                    "Classe CU": classe_cu,
                    "Compagnia Provenienza": compagnia_clean,
                    "Stato Bot Estrazione": "Dati Estratti"
                }

                if nome_clean:
                    raw_fields["Nome"] = nome_clean

                cleaned_fields = {k: v for k, v in raw_fields.items() if v is not None and v != ""}

                for _ in range(5):
                    res = requests.patch(url_trattativa, headers=headers, json={"fields": cleaned_fields})
                    if res.status_code == 200:
                        print(f"[{record_id}] ESTRAZIONE E MAPPATURA COMPLETATE CON SUCCESSO!", flush=True)
                        break
                    elif res.status_code == 422:
                        err_text = res.text
                        print(f"Rilevato errore campo Airtable 422 ({err_text}), sanificazione...", flush=True)
                        
                        unk_match = re.search(r'Unknown field name:\s*\\?"([^\\"]+)\\?"', err_text)
                        val_match = re.search(r'Field\s*\\?"([^\\"]+)\\?"\s*cannot accept', err_text, re.IGNORECASE)
                        
                        if unk_match:
                            bad_field = unk_match.group(1)
                            print(f"Rimuovo campo non riconosciuto '{bad_field}'...", flush=True)
                            cleaned_fields.pop(bad_field, None)
                        elif val_match:
                            bad_field = val_match.group(1)
                            print(f"Rimuovo campo con valore non valido '{bad_field}'...", flush=True)
                            cleaned_fields.pop(bad_field, None)
                        else:
                            cleaned_fields.pop("KW", None)
                            cleaned_fields.pop("Alimentazione", None)
                            cleaned_fields.pop("cl_cap", None)
                    else:
                        res.raise_for_status()
                        break

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
