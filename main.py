"""
main.py — Bot Unipol di Protexa, versione 2.0

QUESTO FILE SOSTITUISCE IL PRECEDENTE E NON TOGLIE NULLA.

Contiene due motori che convivono:

  MOTORE 1 (quello che gia' usi, invariato)
      POST /estrai
      Attraversa il preventivatore RCA e scrive su Airtable.
      Il codice e' identico a prima, riga per riga. Chi lo chiama gia' oggi
      continua a funzionare senza modifiche.

  MOTORE 2 (nuovo)
      GET  /bda/{targa}
      POST /bda/lotto
      Interroga la Banca Dati ANIA per targa. Non tocca il preventivatore.
      E' piu' veloce e tiene la sessione aperta fra una targa e l'altra.

I due motori non si disturbano: condividono lo stesso semaforo, quindi non
aprono mai due browser insieme. Su Render piano free e' obbligatorio,
altrimenti il container esaurisce la memoria.

Se i file nuovi non fossero ancora stati caricati, il motore 2 resta spento e
il motore 1 continua a lavorare: il servizio non va mai giu' per quel motivo.
"""

import asyncio
import os
import re
from datetime import datetime

import pyotp
import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from playwright.async_api import async_playwright

app = FastAPI(title="Bot Unipol Protexa", version="2.0")

# Un solo browser alla volta su tutta l'applicazione.
bot_semaphore = asyncio.Semaphore(1)

UNIPOL_USER = os.getenv("UNIPOL_USER")
UNIPOL_PASS = os.getenv("UNIPOL_PASS")
RAW_SECRET = os.getenv("UNIPOL_TOTP_SECRET") or ""
UNIPOL_TOTP_SECRET = RAW_SECRET.replace(" ", "").strip().upper()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")


# ===========================================================================
#  MOTORE 2 — BANCA DATI ANIA  (nuovo)
# ===========================================================================
#
# Gli import stanno dentro un try apposta: se uno dei file nuovi mancasse,
# il servizio parte lo stesso e il motore 1 resta in funzione.

MOTORE_BDA_ATTIVO = False
ERRORE_BDA = ""

try:
    from unipol_sessione import POOL, ErroreUnipol, TargaNonTrovata
    MOTORE_BDA_ATTIVO = True
except Exception as _e:
    ERRORE_BDA = f"{type(_e).__name__}: {_e}"


# --- Archivio dei risultati -------------------------------------------------
#
# Render chiude la connessione dopo circa novanta secondi. Una consultazione
# BDA ne richiede il doppio (login, OTP, quattro menu, ricerca), quindi
# l'endpoint non puo' far aspettare chi lo chiama: risponderebbe sempre 502.
#
# Percio' il lavoro parte in sottofondo e il risultato si ritira dopo, come al
# bar: prima si ordina, poi si ritira. Lo stesso schema del bot preventivatore.

ULTIMO_RISULTATO = {"stato": "mai avviato"}


async def _lavora_targa(targa: str):
    """Esegue la consultazione e deposita l'esito in ULTIMO_RISULTATO."""
    global ULTIMO_RISULTATO
    ULTIMO_RISULTATO = {
        "stato": "in corso",
        "targa": targa,
        "avviato_alle": datetime.now().strftime("%H:%M:%S"),
        "messaggio": "Sto lavorando. Ricarica questa pagina fra un minuto.",
    }
    print(f"[BDA] === avvio consultazione targa {targa} ===", flush=True)

    try:
        async with bot_semaphore:
            dati = await POOL.consulta(targa)

        ULTIMO_RISULTATO = {
            "stato": "completato",
            "targa": dati["targa"],
            "durata_sec": round(dati["durata_sec"], 1),
            "posizione": dati["posizione"].model_dump(mode="json"),
            "veicolo": dati["veicolo"].model_dump(mode="json"),
            "contraente": dati["contraente"].model_dump(mode="json"),
        }
        print(f"[BDA] === completata targa {targa} in "
              f"{dati['durata_sec']:.1f}s ===", flush=True)

    except Exception as e:
        # Il tipo di errore e il testo completo finiscono sia nella risposta
        # sia nei log: e' quello che permette di capire dove si e' fermato.
        import traceback
        traccia = traceback.format_exc()
        print(f"[BDA] === ERRORE su {targa} ===\n{traccia}", flush=True)
        ULTIMO_RISULTATO = {
            "stato": "errore",
            "targa": targa,
            "tipo_errore": type(e).__name__,
            "messaggio": str(e)[:600],
        }


@app.get("/bda/{targa}")
async def avvia_banca_dati(targa: str, attivita: BackgroundTasks):
    """
    Avvia la consultazione di una targa e risponde SUBITO.

    Passo 1:  https://bot-unipol.onrender.com/bda/DL389LB
    Passo 2:  https://bot-unipol.onrender.com/risultato   (dopo un minuto)
    """
    if not MOTORE_BDA_ATTIVO:
        raise HTTPException(503, f"Motore BDA non disponibile. Dettaglio: {ERRORE_BDA}")

    if ULTIMO_RISULTATO.get("stato") == "in corso":
        return {
            "avviato": False,
            "messaggio": "C'e' gia' una consultazione in corso. "
                         "Aspetta che finisca e guarda /risultato.",
            "in_corso_su": ULTIMO_RISULTATO.get("targa"),
        }

    attivita.add_task(_lavora_targa, targa)
    return {
        "avviato": True,
        "targa": targa.upper(),
        "prossimo_passo": "Apri /risultato fra circa un minuto",
        "indirizzo": "https://bot-unipol.onrender.com/risultato",
    }


@app.get("/risultato")
async def leggi_risultato():
    """Mostra l'esito dell'ultima consultazione avviata."""
    return ULTIMO_RISULTATO


# ===========================================================================
#  ENDPOINT PER MAKE — sostituisce /estrai mantenendo lo stesso contratto
# ===========================================================================
#
# Riceve lo stesso identico contenuto di /estrai:
#     {"record_id": "recXXXX", "targa": "DL389LB", "data_nascita": "..."}
#
# Cambia solo il motore: banca dati ANIA invece del preventivatore. Su Make
# basta cambiare l'indirizzo del modulo HTTP, tutto il resto resta com'e'.


def _data_it(valore) -> str:
    """Formato gg/mm/aaaa, quello che si aspettano le colonne di Airtable."""
    return valore.strftime("%d/%m/%Y") if valore else ""


def _riassunto_sinistri(storico) -> str:
    """
    Riga leggibile per Airtable, del tipo:
        "2022: 1 cose | anni non coperti: 2015-2020"

    Serve a chi guarda la tabella: la lista completa e' fatta di centinaia di
    voci e in una cella non si legge.
    """
    pezzi = []
    for r in storico.righe:
        if r.categoria is not None and r.disponibile and r.numero > 0:
            pezzi.append(f"{r.anno}: {r.numero} {r.categoria.value}")

    non_coperti = sorted({r.anno for r in storico.righe
                          if r.categoria is not None and not r.disponibile})
    testo = " | ".join(pezzi) if pezzi else "nessun sinistro"
    if non_coperti:
        testo += f" | anni senza dato: {non_coperti[0]}-{non_coperti[-1]}"
    return testo


def _campi_airtable(dati: dict) -> dict:
    """Traduce il risultato della banca dati nelle colonne della Trattativa."""
    p = dati["posizione"]
    v = dati["veicolo"]
    c = dati["contraente"]

    return {
        "targa": v.targa,
        # Anagrafica
        "Codice Fiscale": c.codice_fiscale,
        "Nome": c.nominativo,
        "cl_datanascita": _data_it(c.data_nascita),
        # Veicolo
        "Marca": v.marca,
        "Modello": v.modello,
        "KW": v.kw,
        "Cilindrata": v.cilindrata,
        "Posti": v.posti,
        "Telaio": v.telaio,
        "Data immatricolazione": _data_it(v.data_immatricolazione),
        "Alimentazione": v.alimentazione.value if v.alimentazione else "",
        # Posizione assicurativa
        "Classe CU": p.cu_assegnazione,
        "Classe CU Provenienza": p.cu_provenienza,
        "Compagnia Provenienza": p.compagnia_provenienza,
        "Codice Compagnia": p.compagnia_provenienza_codice,
        "Numero Polizza": p.numero_polizza,
        "Scadenza Attestato": _data_it(p.scadenza_attestato),
        "Forma Tariffaria": p.forma_tariffaria.value if p.forma_tariffaria else "",
        "Codice IUR": p.codice_iur,
        "Storico Sinistri": _riassunto_sinistri(p.sinistri),
        "Stato Bot Estrazione": "Dati Estratti",
    }


def _scrivi_su_airtable(record_id: str, campi: dict) -> dict:
    """
    Scrive togliendo via via i campi che la tabella non accetta.

    Il payload contiene anche colonne che potresti non avere ancora creato
    (Telaio, Cilindrata, Codice IUR...). Invece di far fallire tutto, Airtable
    segnala il nome del campo rifiutato e qui lo si toglie: si salva quello che
    la tabella conosce e si annota il resto. Meglio diciotto campi su ventuno
    che nessuno.
    """
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

    payload = {k: val for k, val in campi.items() if val not in (None, "", [])}
    scartati = []

    for _ in range(25):
        res = requests.patch(url, headers=headers,
                             json={"fields": payload, "typecast": True}, timeout=30)
        if res.status_code == 200:
            return {"ok": True, "salvati": list(payload.keys()), "scartati": scartati}

        if res.status_code != 422:
            return {"ok": False, "errore": f"HTTP {res.status_code}: {res.text[:300]}",
                    "scartati": scartati}

        testo = res.text
        campo = None
        for pattern in (r'Unknown field name:\s*\\?"([^\\"]+)\\?"',
                        r'Field\s*\\?"([^\\"]+)\\?"\s*cannot accept'):
            m = re.search(pattern, testo, re.IGNORECASE)
            if m:
                campo = m.group(1)
                break

        if campo and campo in payload:
            payload.pop(campo)
            scartati.append(campo)
            print(f"[BDA] colonna '{campo}' non presente in Airtable, la salto", flush=True)
            continue

        return {"ok": False, "errore": testo[:300], "scartati": scartati}

    return {"ok": False, "errore": "troppi campi rifiutati", "scartati": scartati}


async def _estrai_e_salva(record_id: str, targa: str):
    """Consulta la banca dati e scrive il risultato sulla Trattativa."""
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

    print(f"[BDA] === Make ha chiesto {targa} (record {record_id}) ===", flush=True)
    try:
        requests.patch(url, headers=headers,
                       json={"fields": {"Stato Bot Estrazione": "In Corso"}}, timeout=30)
    except Exception as e:
        print(f"[BDA] non riesco a segnare 'In Corso': {e}", flush=True)

    try:
        async with bot_semaphore:
            dati = await POOL.consulta(targa)

        esito = _scrivi_su_airtable(record_id, _campi_airtable(dati))

        if esito["ok"]:
            print(f"[BDA] === {targa} salvata su Airtable in "
                  f"{dati['durata_sec']:.1f}s "
                  f"({len(esito['salvati'])} campi) ===", flush=True)
            if esito["scartati"]:
                print(f"[BDA] colonne mancanti in tabella: "
                      f"{', '.join(esito['scartati'])}", flush=True)
        else:
            print(f"[BDA] === salvataggio fallito: {esito['errore']} ===", flush=True)
            requests.patch(url, headers=headers, json={"fields": {
                "Stato Bot Estrazione": "Errore",
                "Note e Errori Bot": f"Airtable: {esito['errore']}"[:1000],
            }}, timeout=30)

    except Exception as e:
        import traceback
        print(f"[BDA] === ERRORE su {targa} ===\n{traceback.format_exc()}", flush=True)
        try:
            requests.patch(url, headers=headers, json={"fields": {
                "Stato Bot Estrazione": "Errore",
                "Note e Errori Bot": f"{type(e).__name__}: {str(e)[:800]}",
            }}, timeout=30)
        except Exception:
            pass


@app.post("/estrai-bda")
@app.post("/estrai-bda/")
async def estrai_bda(data: dict, background_tasks: BackgroundTasks):
    """
    Indirizzo da usare in Make al posto di /estrai.

    Stesso contenuto in ingresso, motore piu' veloce, e i risultati finiscono
    direttamente sulla Trattativa.
    """
    record_id = data.get("record_id")
    targa = data.get("targa")

    if not record_id or not targa:
        raise HTTPException(400, "Servono 'record_id' e 'targa'")
    if not MOTORE_BDA_ATTIVO:
        raise HTTPException(503, f"Motore BDA non disponibile: {ERRORE_BDA}")

    background_tasks.add_task(_estrai_e_salva, record_id, targa)
    return {"status": "Estrazione Avviata", "record_id": record_id,
            "targa": str(targa).upper(), "motore": "banca dati ANIA"}


@app.post("/bda/lotto")
async def consulta_lotto(payload: dict):
    """
    Consulta piu' targhe riusando la stessa sessione. Lavora in sottofondo.

    Corpo della richiesta:  {"targhe": ["DL389LB", "ES211SV"]}
    """
    if not MOTORE_BDA_ATTIVO:
        raise HTTPException(503, f"Motore BDA non disponibile. Dettaglio: {ERRORE_BDA}")

    targhe = payload.get("targhe") or []
    if not isinstance(targhe, list) or not targhe:
        raise HTTPException(400, 'Attesa una lista, esempio: {"targhe": ["DL389LB"]}')
    if len(targhe) > 50:
        raise HTTPException(400, "Massimo 50 targhe per lotto")

    inizio = datetime.now()
    async with bot_semaphore:
        risultati = await POOL.consulta_molte(targhe)

    riuscite, fallite = [], []
    for r in risultati:
        if r["ok"]:
            riuscite.append({
                "targa": r["targa"],
                "posizione": r["dati"]["posizione"].model_dump(mode="json"),
                "contraente": r["dati"]["contraente"].model_dump(mode="json"),
            })
        else:
            fallite.append({"targa": r["targa"], "motivo": r["errore"]})

    totale = (datetime.now() - inizio).total_seconds()
    return {
        "richieste": len(targhe),
        "riuscite": len(riuscite),
        "fallite": len(fallite),
        "secondi_totali": round(totale, 1),
        "dati": riuscite,
        "errori": fallite,
    }


@app.get("/stato")
async def stato():
    """Pagina di controllo: dice cosa e' acceso e cosa no."""
    info = {
        "motore_preventivatore": "attivo",
        "motore_banca_dati": "attivo" if MOTORE_BDA_ATTIVO else f"spento ({ERRORE_BDA})",
        "credenziali_unipol": "presenti" if (UNIPOL_USER and UNIPOL_PASS and UNIPOL_TOTP_SECRET) else "MANCANTI",
        "credenziali_airtable": "presenti" if (AIRTABLE_API_KEY and AIRTABLE_BASE_ID) else "MANCANTI",
        "sessioni_configurate": os.getenv("UNIPOL_SESSIONI", "1"),
    }
    if MOTORE_BDA_ATTIVO:
        try:
            info["sessioni"] = POOL.statistiche()
        except Exception:
            pass
    return info


@app.on_event("shutdown")
async def chiudi_sessioni():
    """Chiude i browser quando Render spegne il servizio."""
    if MOTORE_BDA_ATTIVO:
        try:
            await POOL.chiudi()
        except Exception:
            pass


# ===========================================================================
#  MOTORE 1 — PREVENTIVATORE RCA  (invariato rispetto alla versione attuale)
# ===========================================================================

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
        'text=/Prosegui/i',
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
                f'text="{nome}"',
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

                const pfLabels = Array.from(document.querySelectorAll('.ui-selectonemenu-label, .ui-selectonemenu-title'));
                pfLabels.forEach(pf => {
                    let txt = pf.innerText || pf.textContent || '';
                    let parent = pf.closest('td, th, .ui-panelgrid-cell, div');
                    let prev = parent ? parent.previousElementSibling : null;
                    if (prev) registra(prev.innerText || prev.textContent, txt);
                });

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
    scarti = ["proprietario", "contraente", "usufruttuario", "conducente",
              "aggiungi una figura", "0", "cerca", "m20", "m30", "m40", "na", "--"]
    for k_map, v_map in mappa.items():
        k_clean = k_map.strip().lower()
        v_clean = v_map.strip()

        if not v_clean or v_clean.lower() in scarti or "\n" in v_clean:
            continue

        for kw in keywords:
            kw_clean = kw.strip().lower()
            pattern = r'\b' + re.escape(kw_clean) + r'\b'
            if re.search(pattern, k_clean):
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
    scarti = ["cerca", "seleziona", "ui-button", "codice marca", "descrizione modello",
              "impresa", "compagnia", "proprietario", "contraente", "0",
              "m20", "m30", "m40", "na", "--"]
    if v.lower() in scarti or "\n" in v or len(v) > 120:
        return ""
    return v


async def estrai_dati_preventivatore(record_id: str, targa: str, data_nascita: str):
    async with bot_semaphore:
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        }
        url_trattativa = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative/{record_id}"

        targa_spazi = formatta_targa_spazi(targa)
        targa_pulita = targa.replace(" ", "").upper()

        print(f"[{record_id}] Avvio estrazione per targa: {targa_pulita} ({targa_spazi})", flush=True)
        requests.patch(url_trattativa, headers=headers,
                       json={"fields": {"Stato Bot Estrazione": "In Corso"}})

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
                    "--disable-background-networking",
                ],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            try:
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

                print("1/3 Clic su 'DATI ASSICURATIVI' / 'Veicolo/natante'...", flush=True)
                await clicca_subtab(page, result_frame, ["DATI ASSICURATIVI"])
                await page.wait_for_timeout(1000)
                await clicca_subtab(page, result_frame, ["Veicolo/natante", "Veicolo"])
                await page.wait_for_timeout(2500)
                d1 = await estrai_tutti_i_campi_dalla_pagina(page)
                print(f" -> Scheda Veicolo: {len(d1)} campi estratti", flush=True)
                mappa_totale.update(d1)

                print("2/3 Clic su 'Figure contrattuali'...", flush=True)
                await clicca_subtab(page, result_frame, ["Figure contrattuali"])
                await page.wait_for_timeout(2500)
                d2 = await estrai_tutti_i_campi_dalla_pagina(page)
                print(f" -> Scheda Figure Contrattuali: {len(d2)} campi estratti", flush=True)
                mappa_totale.update(d2)

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

                print(f"VALORI ESTRATTI -> Nome: '{nome_clean}', CF: '{cf_clean}', "
                      f"Marca: '{marca_clean}', Modello: '{modello_clean}', KW: {kw}, "
                      f"CU: '{classe_cu}', Compagnia: '{compagnia_clean}'", flush=True)

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
                    "Stato Bot Estrazione": "Dati Estratti",
                }

                if nome_clean:
                    raw_fields["Nome"] = nome_clean

                cleaned_fields = {k: v for k, v in raw_fields.items() if v is not None and v != ""}

                for _ in range(5):
                    res = requests.patch(url_trattativa, headers=headers, json={"fields": cleaned_fields})
                    if res.status_code == 200:
                        print(f"[{record_id}] ESTRAZIONE E MAPPATURA COMPLETATE!", flush=True)
                        break
                    elif res.status_code == 422:
                        err_text = res.text
                        print(f"Errore campo Airtable 422 ({err_text}), sanificazione...", flush=True)

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
                requests.patch(url_trattativa, headers=headers,
                               json={"fields": {"Stato Bot Estrazione": "Errore"}})

            finally:
                await context.close()
                await browser.close()


@app.get("/")
async def root():
    return {
        "status": "Bot Unipol Online",
        "versione": "2.0",
        "motore_banca_dati": "attivo" if MOTORE_BDA_ATTIVO else "spento",
        "endpoint": ["/estrai", "/bda/{targa}", "/risultato", "/bda/lotto", "/stato"],
    }


@app.post("/estrai")
@app.post("/estrai/")
async def trigger_bot(data: dict, background_tasks: BackgroundTasks):
    record_id = data.get("record_id")
    targa = data.get("targa")
    data_nascita = data.get("data_nascita")

    background_tasks.add_task(estrai_dati_preventivatore, record_id, targa, data_nascita)
    return {"status": "Estrazione Avviata", "record_id": record_id}
