"""
unipol_bda.py — Estrazione dati assicurativi dalla Banca Dati ANIA di Unipol.

Percorso nel portale:  Rami Auto > IBDV ANIA ricerca per targa > consultazione bda

Rispetto al percorso "preventivatore" usato finora, questa pagina:
  - vuole in ingresso la sola targa
  - e' HTML classico, senza iframe JSF/PrimeFaces e senza tab AJAX
  - restituisce in un colpo solo attestato, classi CU e sezionale sinistri

Il risultato viene tradotto negli oggetti di models.py.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from typing import Optional

import pyotp
from playwright.async_api import Browser, Page, async_playwright

from models import (
    CategoriaDanno,
    FormaTariffaria,
    Persona,
    PosizioneAssicurativa,
    RigaSinistri,
    StoricoSinistri,
    TipoResponsabilita,
    Veicolo,
)

# ---------------------------------------------------------------------------
# Configurazione (tutto da variabili d'ambiente, mai in chiaro nel codice)
# ---------------------------------------------------------------------------

UNIPOL_USER = os.getenv("UNIPOL_USER", "")
UNIPOL_PASS = os.getenv("UNIPOL_PASS", "")
UNIPOL_TOTP_SECRET = (os.getenv("UNIPOL_TOTP_SECRET") or "").replace(" ", "").strip().upper()
UNIPOL_DOMINIO = os.getenv("UNIPOL_DOMINIO", "Uniage")

BASE_URL = os.getenv("UNIPOL_BASE_URL", "https://essig.unipolsai.it")
LOGIN_URL = f"{BASE_URL}/my-policy"

# Se conosci l'URL diretto della pagina BDA, mettilo qui: il bot salta la
# navigazione dei menu e guadagna ~8 secondi per targa.
BDA_URL = os.getenv("UNIPOL_BDA_URL", "")

ARGS_CHROMIUM = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-setuid-sandbox",
    "--no-zygote",
    "--js-flags=--max-old-space-size=256",
    "--disable-accelerated-2d-canvas",
    "--disable-background-networking",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class ErroreUnipol(Exception):
    """Errore recuperabile: la targa non e' stata trovata, sessione scaduta, ecc."""


# ---------------------------------------------------------------------------
# Utilita' di parsing
# ---------------------------------------------------------------------------

def _norm(s: Optional[str]) -> str:
    """Normalizza un'etichetta: minuscole, niente punteggiatura, spazi singoli."""
    if not s:
        return ""
    s = s.replace("\xa0", " ")
    s = re.sub(r"[.:*°/]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _data_it(valore: str) -> Optional[date]:
    """Converte gg/mm/aaaa (o gg-mm-aaaa) in date. Restituisce None se non valido."""
    if not valore:
        return None
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", valore.strip())
    if not m:
        return None
    g, mm, a = (int(x) for x in m.groups())
    try:
        return date(a, mm, g)
    except ValueError:
        return None


def _intero(valore: str) -> Optional[int]:
    if valore is None:
        return None
    m = re.search(r"-?\d+", str(valore).replace(".", "").strip())
    return int(m.group()) if m else None


def _decimale(valore: str) -> float:
    if not valore:
        return 0.0
    v = re.sub(r"[^\d,.\-]", "", str(valore))
    # formato italiano: 1.234,56  ->  1234.56
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return 0.0


# JavaScript iniettato nella pagina.
#
# Legge le coppie etichetta -> valore camminando le celle di tabella, e legge
# separatamente la tabella del sezionale sinistri.
#
# Due accortezze che risolvono problemi gia' incontrati:
#  - sui <select> prende SOLO l'opzione selezionata, non tutti gli <option>
#    (era la causa del dump delle 110 province)
#  - legge .value degli input, non innerText, perche' i campi sono readonly
_JS_ESTRAI = r"""
() => {
    const testo = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\u00a0/g,' ').trim();

    // Testo dell'etichetta associata a un controllo (serve per i radio).
    const etichettaDi = (el) => {
        if (el.id) {
            const l = document.querySelector(`label[for="${el.id}"]`);
            if (l) return testo(l);
        }
        // Su queste pagine il testo sta subito dopo il radio, senza <label>
        let n = el.nextSibling;
        while (n) {
            const t = (n.textContent || '').replace(/\u00a0/g,' ').trim();
            if (t) return t;
            n = n.nextSibling;
        }
        return '';
    };

    // Valore "vero" di un controllo di form
    const valoreControllo = (el) => {
        const tag = el.tagName.toLowerCase();
        if (tag === 'select') {
            const opt = el.options[el.selectedIndex];
            return opt ? opt.text.trim() : '';
        }
        if (el.type === 'checkbox') return el.checked ? 'SI' : 'NO';
        if (el.type === 'radio') {
            // Un gruppo di radio ha piu' pulsanti: conta solo quello scelto, e
            // conta la sua etichetta. Rispondere 'SI' perche' un radio del
            // gruppo e' selezionato direbbe il contrario del vero quando la
            // scelta e' 'No'.
            if (!el.checked) return '';
            return etichettaDi(el) || 'SI';
        }
        return (el.value || el.getAttribute('value') || '').trim();
    };

    // ---- 1. Coppie etichetta -> valori -------------------------------------
    // Una riga tipica e':  <td>Compagnia di provenienza</td><td><input 924><input NOME></td>
    // Puo' quindi esserci piu' di un valore per etichetta: li teniamo tutti.
    const coppie = {};
    const aggiungi = (label, valori) => {
        const l = label.trim();
        if (!l || l.length > 70) return;
        const puliti = valori.map(v => v.trim()).filter(v => v !== '');
        if (!puliti.length) return;
        if (!coppie[l]) coppie[l] = [];
        coppie[l].push(...puliti);
    };

    document.querySelectorAll('tr').forEach(tr => {
        const celle = Array.from(tr.children);
        for (let i = 0; i < celle.length - 1; i++) {
            const etichetta = testo(celle[i]);
            if (!etichetta || celle[i].querySelector('input, select, textarea')) continue;

            // Un campo puo' avere piu' valori distribuiti su celle diverse:
            // "C.F./P.I contraente" ha il codice fiscale in una cella e il
            // nominativo in quella dopo. Leggendo solo la cella successiva il
            // nome andava perso.
            let controlli = [];
            for (let k = i + 1; k < Math.min(i + 4, celle.length); k++) {
                const trovati = Array.from(celle[k].querySelectorAll('input, select, textarea'));
                if (trovati.length) {
                    controlli.push(...trovati);
                    continue;
                }
                // Cella con del testo e senza controlli: e' l'etichetta
                // successiva, quindi il campo corrente finisce qui.
                if (testo(celle[k])) break;
            }

            if (controlli.length) {
                aggiungi(etichetta, controlli.map(valoreControllo));
            } else {
                const v = testo(celle[i + 1]);
                if (v && v.length < 120 && !v.includes('\n')) aggiungi(etichetta, [v]);
            }
        }
    });

    // Fallback: <label for="..."> classici
    document.querySelectorAll('label[for]').forEach(lab => {
        const el = document.getElementById(lab.getAttribute('for'));
        if (el && el.tagName) aggiungi(testo(lab), [valoreControllo(el)]);
    });

    // ---- 2. Tabella del sezionale sinistri ---------------------------------
    // Individuata dall'intestazione "Tipo sinistro" seguita dagli anni.
    let sinistri = null;
    for (const tabella of document.querySelectorAll('table')) {
        const intestazioni = Array.from(tabella.querySelectorAll('th, thead td'))
                                  .map(testo);
        const haTipo = intestazioni.some(h => /tipo\s*sinistro/i.test(h));
        const anni = intestazioni.filter(h => /^(19|20)\d{2}$/.test(h.trim()));
        if (!haTipo || anni.length < 3) continue;

        const righe = [];
        tabella.querySelectorAll('tr').forEach(tr => {
            const celle = Array.from(tr.children);
            if (celle.length < 2) return;
            const etichetta = testo(celle[0]);
            if (!etichetta || /tipo\s*sinistro/i.test(etichetta)) return;
            const valori = celle.slice(1).map(td => {
                const inp = td.querySelector('input, select');
                return inp ? valoreControllo(inp) : testo(td);
            });
            righe.push({ etichetta, valori });
        });

        sinistri = { anni: anni.map(a => parseInt(a, 10)), righe };
        break;
    }

    // ---- 3. Messaggi di errore o avviso della pagina ------------------------
    const avvisi = Array.from(document.querySelectorAll('.error, .errore, .messaggio, .alert, span[class*="err"]'))
        .map(testo).filter(t => t && t.length < 300);

    return { coppie, sinistri, avvisi, titolo: document.title };
}
"""


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def _login(page: Page) -> None:
    """Autenticazione con username, password, dominio e OTP TOTP a 6 cifre."""
    if not (UNIPOL_USER and UNIPOL_PASS and UNIPOL_TOTP_SECRET):
        raise ErroreUnipol(
            "Credenziali Unipol mancanti. Imposta UNIPOL_USER, UNIPOL_PASS e "
            "UNIPOL_TOTP_SECRET fra le variabili d'ambiente."
        )

    await page.goto(LOGIN_URL, wait_until="commit", timeout=40_000)

    campo_user = page.locator('input[name="Username" i], input[name="username" i]').first
    await campo_user.wait_for(state="visible", timeout=20_000)
    await campo_user.fill(UNIPOL_USER)

    await page.locator('input[type="password"]').first.fill(UNIPOL_PASS)

    dominio = page.locator('select[name="domain" i]').first
    if await dominio.count() and await dominio.is_visible():
        try:
            await dominio.select_option(label=UNIPOL_DOMINIO, timeout=3_000)
        except Exception:
            await dominio.select_option(value=UNIPOL_DOMINIO, timeout=3_000)

    await page.locator('input[type="submit"], button').first.click()

    # Schermata intermedia: un solo pulsante di conferma
    conferma = page.locator('input[type="submit"], button').first
    await conferma.wait_for(state="visible", timeout=25_000)
    await conferma.click()

    # Codice OTP
    campo_otp = page.locator('input[type="text"], input[type="number"], input').first
    await campo_otp.wait_for(state="visible", timeout=25_000)
    await campo_otp.fill("")
    await campo_otp.type(pyotp.TOTP(UNIPOL_TOTP_SECRET).now(), delay=100)
    await page.locator('input[type="submit"], button').first.click()

    # Il portale registra la sessione di sicurezza lato server: qui l'attesa
    # e' necessaria, senza si finisce su una pagina di sessione non valida.
    await asyncio.sleep(6)


# ---------------------------------------------------------------------------
# Navigazione fino alla maschera BDA
# ---------------------------------------------------------------------------

async def _clic_testo(page: Page, testo: str, tentativi: int = 12) -> bool:
    """Cerca un elemento cliccabile per testo in tutti i frame della pagina."""
    for _ in range(tentativi):
        for frame in [page.main_frame, *page.frames]:
            try:
                el = frame.get_by_text(testo, exact=False).first
                if await el.is_visible(timeout=300):
                    await el.click(force=True)
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return False


async def _vai_a_bda(page: Page) -> None:
    if BDA_URL:
        await page.goto(BDA_URL, wait_until="domcontentloaded", timeout=30_000)
        return

    # Navigazione a menu. Piu' fragile dell'URL diretto: appena mi passi l'URL
    # della barra indirizzi, valorizza UNIPOL_BDA_URL e questo ramo non serve piu'.
    for voce in ("RAMI AUTO", "IBDV ANIA", "consultazione bda"):
        if not await _clic_testo(page, voce):
            raise ErroreUnipol(
                f"Voce di menu '{voce}' non trovata. Imposta UNIPOL_BDA_URL "
                f"con l'URL diretto della pagina BDA."
            )
        await page.wait_for_timeout(1_500)


async def _cerca_targa(page: Page, targa: str) -> None:
    """Compila la maschera 'Identificativo veicolo' e preme Avanti."""
    campo = None
    for selettore in ('input[name*="targa" i]', 'input[id*="targa" i]', 'input[type="text"]'):
        loc = page.locator(selettore).first
        if await loc.count() and await loc.is_visible():
            campo = loc
            break
    if campo is None:
        raise ErroreUnipol("Campo Targa non trovato nella maschera BDA.")

    await campo.fill("")
    await campo.type(targa, delay=50)

    for selettore in ('input[value*="Avanti" i]', 'button:has-text("Avanti")',
                      'a:has-text("Avanti")', 'input[type="submit"]'):
        bottone = page.locator(selettore).first
        if await bottone.count() and await bottone.is_visible():
            await bottone.click(force=True)
            break
    else:
        await campo.press("Enter")

    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_500)


# ---------------------------------------------------------------------------
# Traduzione della pagina negli oggetti del modello
# ---------------------------------------------------------------------------

def _parse_alimentazione(codice: str) -> Optional["Alimentazione"]:
    """
    Traduce il codice alimentazione della Motorizzazione.

    Sulla pagina compare come sigla, non come parola. Le sigle composte vanno
    controllate PRIMA di quelle singole, altrimenti si sbaglia:
        G  = gasolio            -> Diesel
        GP = benzina/GPL        -> GPL      (non "G" seguito da altro)
        GM = benzina/metano     -> Metano
        B  = benzina
        IB = ibrido
    Cercando "G" per primo, un veicolo GPL verrebbe classificato Diesel.
    """
    from models import Alimentazione as _Alim

    c = (codice or "").strip().upper()
    if not c:
        return None

    # Sigle composte, per prime.
    if c == "GP" or "GPL" in c:
        return _Alim.GPL
    if c == "GM" or "METANO" in c:
        return _Alim.METANO
    if c in ("IB", "I") or "IBRID" in c:
        return _Alim.IBRIDA
    if c in ("EL", "E") or "ELETTR" in c:
        return _Alim.ELETTRICA

    # Sigle singole.
    if c in ("G", "D") or "GASOLIO" in c or "DIESEL" in c:
        return _Alim.DIESEL
    if c == "B" or "BENZINA" in c:
        return _Alim.BENZINA
    if c == "L":
        return _Alim.GPL
    if c == "M":
        return _Alim.METANO
    return None


# Espressioni per la pagina "dati bda" (paginaD0.do). I valori stanno in celle
# di tabella accanto all'etichetta, quindi fra etichetta e valore possono
# esserci spazi, tabulazioni o a capo: da qui il \s+ ovunque.
_CAMPI_VEICOLO = {
    "telaio": r"Telaio\s+([A-Z0-9]{8,25})",
    "omologazione": r"Omologazione\s+([A-Z0-9]{4,20})",
    "fabbrica": r"Fabbrica e modello\s+(.+?)(?:\s*\n|\s{2,}Nazionalit)",
    "tipo_veicolo": r"Tipo veicolo\s+([A-Z][A-Z ]{3,40}?)(?:\s*\n|\s{2,}Categoria)",
    "categoria": r"Categoria\s+([A-Z][A-Z' ]{3,70}?)(?:\s*\n|\s{2,}Uso\b)",
    "uso": r"\bUso\s+([A-Z]+)",
    "cilindrata": r"Cilindrata\s*cc\s+([\d.]+)",
    "cv_fiscali": r"Potenza\s*fis\.?\s*cv\s+(\d+)",
    "kw": r"Potenza\s*max\.?\s*kw\s+([\d.,]+)",
    "alimentazione": r"Alimentazione\s+([A-Z]{1,12})",
    "posti": r"Num\.?\s*posti\s+(\d+)",
    "data_immatricolazione": r"Data immatricolazione\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
}


def parse_pagina_veicolo(testo: str, veicolo: Optional[Veicolo] = None) -> Veicolo:
    """
    Legge i dati tecnici del veicolo dalla pagina 'dati bda'.

    E' la pagina che compare subito dopo aver cercato la targa e contiene
    telaio, omologazione, cilindrata, potenza, alimentazione e posti: gli
    stessi dati che prima si ricavavano attraversando il preventivatore.
    """
    v = veicolo or Veicolo()
    testo = (testo or "").replace("\xa0", " ")

    trovato: dict[str, str] = {}
    for nome, pattern in _CAMPI_VEICOLO.items():
        m = re.search(pattern, testo, re.IGNORECASE)
        if m:
            trovato[nome] = m.group(1).strip()

    v.telaio = trovato.get("telaio", v.telaio)
    v.tipo_veicolo = trovato.get("tipo_veicolo", v.tipo_veicolo) or "AUTOVETTURA"
    v.categoria = trovato.get("categoria", v.categoria)
    # Uso e categoria si conservano come li scrive la Motorizzazione, in
    # maiuscolo: sono voci di un elenco ufficiale, non testo libero.
    v.uso = trovato.get("uso") or v.uso or "PROPRIO"

    # "FIAT AUTO SPA 199BXC1A 05" -> marca FIAT, resto come codice modello.
    fabbrica = trovato.get("fabbrica", "")
    if fabbrica:
        v.allestimento = fabbrica
        pezzi = fabbrica.split()
        if pezzi:
            v.marca = pezzi[0].upper()
            if not v.modello and len(pezzi) > 1:
                v.modello = " ".join(pezzi[1:])

    if "cilindrata" in trovato:
        v.cilindrata = _intero(trovato["cilindrata"])
    if "cv_fiscali" in trovato:
        v.cv_fiscali = _intero(trovato["cv_fiscali"])
    if "posti" in trovato:
        v.posti = _intero(trovato["posti"])
    if "kw" in trovato:
        kw = _decimale(trovato["kw"])
        if kw > 0:
            v.kw = int(kw) if float(kw).is_integer() else kw
    if "alimentazione" in trovato:
        alim = _parse_alimentazione(trovato["alimentazione"])
        if alim:
            v.alimentazione = alim
    if "data_immatricolazione" in trovato:
        data = _data_it(trovato["data_immatricolazione"])
        if data:
            v.data_immatricolazione = data

    return v


def _trova(coppie: dict[str, list[str]], *chiavi: str) -> list[str]:
    """
    Cerca un'etichetta fra quelle raccolte e restituisce i suoi valori.

    Il confronto usa confini di parola sull'etichetta normalizzata. E' la stessa
    difesa che serviva contro il falso positivo "di CUi con danni a solo cose":
    'cu' non combacia mai con 'cui'.
    """
    for chiave in chiavi:
        pattern = r"\b" + re.escape(_norm(chiave)) + r"\b"
        for etichetta, valori in coppie.items():
            if re.search(pattern, _norm(etichetta)):
                return valori
    return []


def _primo(coppie: dict[str, list[str]], *chiavi: str) -> str:
    valori = _trova(coppie, *chiavi)
    return valori[0] if valori else ""


def _parse_sinistri(blocco: Optional[dict]) -> StoricoSinistri:
    """
    Traduce la tabella del sezionale.

    Struttura tipica (dal video BDA_UNIPOL):
        TOT. SIN. PAGATI CON RESP. PRINCIPALE   --  --  ...
          SOLO COSE                             00  00  ...
          SOLO PERSONE                          00  00  ...
          MISTI (tra persone e cose)            00  00  ...
        TOT. SIN. PAGATI CON RESP. PARITARIA    --  --  ...
          SOLO COSE / SOLO PERSONE / MISTI      ...
    """
    storico = StoricoSinistri()
    if not blocco:
        return storico

    anni: list[int] = blocco.get("anni") or []
    responsabilita_corrente = TipoResponsabilita.PRINCIPALE

    for riga in blocco.get("righe", []):
        etichetta = _norm(riga.get("etichetta", ""))
        valori = riga.get("valori", [])

        if "paritaria" in etichetta:
            responsabilita_corrente = TipoResponsabilita.PARITARIA
            categoria = None
        elif "principale" in etichetta:
            responsabilita_corrente = TipoResponsabilita.PRINCIPALE
            categoria = None
        # L'ordine dei tre controlli qui sotto e' obbligatorio e non e' un
        # dettaglio di stile. La riga dei misti si chiama per esteso
        # "MISTI (tra persone e cose)": cercando prima "cose" o "persone" i
        # sinistri misti verrebbero contati nella categoria sbagliata, e un
        # conteggio sbagliato dei sinistri produce un premio sbagliato.
        elif "misti" in etichetta:
            categoria = CategoriaDanno.MISTI
        elif "persone" in etichetta:
            categoria = CategoriaDanno.PERSONE
        elif "cose" in etichetta:
            categoria = CategoriaDanno.COSE
        else:
            continue

        for idx, anno in enumerate(anni):
            if idx >= len(valori):
                break
            grezzo = (valori[idx] or "").strip().upper()

            # 'NA' = dato non disponibile per quell'anno.
            # '--' = riga di totale, il portale non la valorizza.
            # '00' = zero sinistri, che e' un'informazione vera e diversa.
            if grezzo in ("NA", "N.A.", "N/A"):
                disponibile, numero = False, 0
            elif grezzo in ("", "--", "-"):
                disponibile, numero = True, 0
            else:
                disponibile, numero = True, (_intero(grezzo) or 0)

            storico.righe.append(
                RigaSinistri(anno=anno, responsabilita=responsabilita_corrente,
                             categoria=categoria, numero=numero,
                             disponibile=disponibile)
            )

    return storico


def _parse_forma_tariffaria(valore: str) -> Optional[FormaTariffaria]:
    v = (valore or "").upper()
    if "BONUS" in v:
        return FormaTariffaria.BONUS_MALUS
    if "FRANCHIGIA" in v:
        return FormaTariffaria.FRANCHIGIA
    if "PEJUS" in v:
        return FormaTariffaria.PEJUS
    return None


def traduci_pagina_bda(grezzo: dict) -> tuple[PosizioneAssicurativa, Veicolo, Persona]:
    """Da dizionario grezzo estratto dal DOM agli oggetti del modello."""
    coppie: dict[str, list[str]] = grezzo.get("coppie", {})

    # --- Posizione assicurativa ---
    compagnia = _trova(coppie, "Compagnia di provenienza")
    codice_compagnia = compagnia[0] if compagnia else ""
    nome_compagnia = compagnia[1] if len(compagnia) > 1 else ""
    # Se la cella e' unica ("924 ZAVAROVALNICA..."), separa codice e nome
    if codice_compagnia and not nome_compagnia:
        m = re.match(r"^\s*(\d{1,4})\s+(.+)$", codice_compagnia)
        if m:
            codice_compagnia, nome_compagnia = m.group(1), m.group(2)

    cf_contraente = _trova(coppie, "C F P I contraente", "CF contraente", "codice fiscale contraente")
    cf_avente = _trova(coppie, "C F P I avente diritto", "avente diritto")

    posizione = PosizioneAssicurativa(
        posizione=_primo(coppie, "Posizione assicurativa"),
        dettaglio_posizione=_primo(coppie, "Dettaglio posizione"),
        compagnia_provenienza_codice=codice_compagnia,
        compagnia_provenienza=nome_compagnia,
        numero_polizza=_primo(coppie, "Numero Polizza", "Numero polizza"),
        scadenza_attestato=_data_it(_primo(coppie, "Scadenza attestato")),
        forma_tariffaria=_parse_forma_tariffaria(_primo(coppie, "Forma tariffaria")),
        cu_provenienza=_primo(coppie, "Classe CU di provenienza"),
        cu_assegnazione=_primo(coppie, "Classe CU di assegnazione"),
        classe_interna_provenienza=_primo(coppie, "Classe IMPRESA di provenienza"),
        classe_interna_assegnazione=_primo(coppie, "Classe IMPRESA di assegnazione"),
        polizza_gratuita=_primo(coppie, "Polizza Gratuita").upper().startswith("S"),
        cu_art_134bis=_primo(coppie, "CU art 134bis CAP").upper().startswith("S"),
        franchigie_non_corrisposte=_intero(_primo(coppie, "Franchigie non corrisposte")) or 0,
        importo_franchigie=_decimale(_primo(coppie, "Importo")),
        codice_iur=_primo(coppie, "Codice IUR"),
        cf_contraente_ania=cf_contraente[0] if cf_contraente else "",
        nominativo_contraente_ania=cf_contraente[1] if len(cf_contraente) > 1 else "",
        cf_avente_diritto=cf_avente[0] if cf_avente else "",
        nominativo_avente_diritto=cf_avente[1] if len(cf_avente) > 1 else "",
        sinistri=_parse_sinistri(grezzo.get("sinistri")),
    )

    # --- Veicolo (la BDA da' solo l'essenziale: marca/modello vengono dopo) ---
    targa_telaio = _primo(coppie, "Targa Telaio", "Targa")
    veicolo = Veicolo(
        targa=targa_telaio,
        tipo_veicolo=_primo(coppie, "Tipo veicolo") or "AUTOVETTURA",
    )

    # --- Contraente, per quanto ANIA ne sa ---
    nominativo = posizione.nominativo_contraente_ania or posizione.nominativo_avente_diritto
    cognome, _, nome = nominativo.partition(" ")
    contraente = Persona(
        codice_fiscale=posizione.cf_contraente_ania or posizione.cf_avente_diritto,
        cognome=cognome.strip(),
        nome=nome.strip(),
    )
    _completa_da_codice_fiscale(contraente)

    return posizione, veicolo, contraente


# ---------------------------------------------------------------------------
# Il codice fiscale contiene data di nascita e sesso: usiamoli
# ---------------------------------------------------------------------------

_MESI_CF = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "H": 6,
            "L": 7, "M": 8, "P": 9, "R": 10, "S": 11, "T": 12}


def _completa_da_codice_fiscale(persona: Persona) -> None:
    """Ricava sesso e data di nascita dal codice fiscale, se non gia' presenti."""
    cf = (persona.codice_fiscale or "").upper()
    if len(cf) != 16:
        return

    try:
        anno_cf = int(cf[6:8])
        mese = _MESI_CF.get(cf[8])
        giorno = int(cf[9:11])
    except (ValueError, TypeError):
        return
    if not mese:
        return

    from models import Sesso  # import locale per evitare cicli a import-time

    sesso = Sesso.F if giorno > 40 else Sesso.M
    if giorno > 40:
        giorno -= 40

    # Finestra di 100 anni: chi nasce "in futuro" e' del secolo scorso.
    secolo = 2000 + anno_cf
    if secolo > date.today().year:
        secolo -= 100

    try:
        nascita = date(secolo, mese, giorno)
    except ValueError:
        return

    if persona.sesso is None:
        persona.sesso = sesso
    if persona.data_nascita is None:
        persona.data_nascita = nascita


# ---------------------------------------------------------------------------
# Funzione pubblica
# ---------------------------------------------------------------------------

# Una sola istanza di browser alla volta: su Render free (512 MB) due Chromium
# in parallelo fanno andare il container in Out Of Memory.
_semaforo = asyncio.Semaphore(int(os.getenv("UNIPOL_CONCORRENZA", "1")))


async def consulta_bda(targa: str, headless: bool = True) -> dict:
    """
    Interroga la Banca Dati ANIA per una targa.

    Restituisce un dizionario con:
        posizione   PosizioneAssicurativa
        veicolo     Veicolo
        contraente  Persona
        durata_sec  float
        grezzo      dict, il DOM estratto (utile per il debug)

    Solleva ErroreUnipol se la targa non e' presente in banca dati.
    """
    targa = (targa or "").replace(" ", "").replace("-", "").upper()
    if not re.fullmatch(r"[A-Z0-9]{5,10}", targa):
        raise ErroreUnipol(f"Targa non valida: '{targa}'")

    async with _semaforo:
        inizio = datetime.now()
        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(headless=headless, args=ARGS_CHROMIUM)
            contesto = await browser.new_context(user_agent=USER_AGENT)
            page = await contesto.new_page()
            try:
                await _login(page)
                await _vai_a_bda(page)
                await _cerca_targa(page, targa)

                grezzo = await page.evaluate(_JS_ESTRAI)

                if not grezzo.get("coppie"):
                    avvisi = " | ".join(grezzo.get("avvisi", [])) or "pagina vuota"
                    raise ErroreUnipol(f"Nessun dato per la targa {targa}: {avvisi}")

                posizione, veicolo, contraente = traduci_pagina_bda(grezzo)

                if posizione.cu_assegnazione is None and not posizione.compagnia_provenienza:
                    avvisi = " | ".join(grezzo.get("avvisi", [])) or "attestato non presente in ANIA"
                    raise ErroreUnipol(f"Targa {targa} senza attestato: {avvisi}")

                veicolo.targa = veicolo.targa or targa
                return {
                    "posizione": posizione,
                    "veicolo": veicolo,
                    "contraente": contraente,
                    "durata_sec": (datetime.now() - inizio).total_seconds(),
                    "grezzo": grezzo,
                }
            finally:
                await contesto.close()
                await browser.close()


# ---------------------------------------------------------------------------
# Prova da riga di comando:  python unipol_bda.py DL389LB
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    async def _prova() -> None:
        targa = sys.argv[1] if len(sys.argv) > 1 else "DL389LB"
        risultato = await consulta_bda(targa, headless=os.getenv("HEADLESS", "1") == "1")
        posizione: PosizioneAssicurativa = risultato["posizione"]
        print(json.dumps(posizione.model_dump(mode="json"), indent=2, ensure_ascii=False))
        print(f"\nCompletato in {risultato['durata_sec']:.1f} s")

    asyncio.run(_prova())
