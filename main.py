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
from typing import Optional
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

# Quixa e' un complemento: se manca, l'estrazione da Unipol funziona lo stesso.
QUIXA_ATTIVO = False
ERRORE_QUIXA = ""
try:
    from quixa import consulta_quixa
    QUIXA_ATTIVO = True
except Exception as _e:
    ERRORE_QUIXA = f"{type(_e).__name__}: {_e}"


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
    """
    Formato aaaa-mm-gg.

    Le colonne di tipo Data di Airtable accettano solo il formato
    internazionale: mandando 30/08/2021 rispondono "Cannot parse date value".
    Le colonne di tipo Testo accettano comunque questa forma, quindi va bene
    per entrambi i casi e non serve sapere in anticipo com'e' fatta la colonna.
    """
    return valore.strftime("%Y-%m-%d") if valore else ""


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


# ===========================================================================
#  RICONOSCIMENTO AUTOMATICO DELLE COLONNE DI AIRTABLE
# ===========================================================================
#
# I nomi delle colonne non sono scritti nel codice. Il bot legge un campione
# di record dalla tabella Trattative e ricava da li' come si chiamano davvero.
#
# Il motivo e' concreto: la tabella usa il prefisso "cli_" (cli_citta,
# cli_datanascita), non "cl_". Una lettera di differenza e Airtable rifiuta la
# scrittura. Invece di ricopiare i nomi a mano e sbagliarli, li si chiede alla
# tabella stessa. Se domani rinomini o aggiungi una colonna, il bot si adatta.

def _chiave(nome: str) -> str:
    """Confronto tollerante: ignora maiuscole, spazi, underscore e punti."""
    return re.sub(r"[^a-z0-9]", "", (nome or "").lower())


# Per ogni dato, i nomi con cui potrebbe comparire nella tabella.
# Il primo della lista e' quello usato se il riconoscimento non trova nulla.
ALIAS_COLONNE: dict[str, list[str]] = {
    "targa":                 ["targa", "Targa"],
    "codice_fiscale":        ["Codice Fiscale", "cf trattative", "codice fiscale"],
    "nominativo":            ["cliente", "Nome", "Nominativo"],
    "data_nascita":          ["cli_datanascita", "cl_datanascita", "Data di nascita"],
    "indirizzo":             ["cli_indirizzo", "cl_indirizzo", "Indirizzo"],
    "civico":                ["cli_civico", "cl_civico", "Civico"],
    "cap":                   ["cli_cap", "cl_cap", "CAP"],
    "citta":                 ["cli_citta", "cl_citta", "Citta", "Città"],
    "provincia":             ["cli_provincia", "cl_provincia", "Provincia"],
    "cellulare":             ["cli_cel", "cli_cell", "cellulare", "cl_cell"],
    "email":                 ["cli_email", "Email", "email"],
    "marca":                 ["Marca"],
    "modello":               ["Modello"],
    "allestimento":          ["Allestimento Veicolo", "Allestimento"],
    # Attenzione: nella colonna Airtable "Tipo veicolo" ci va la CATEGORIA di
    # Unipol ("AUTOVETTURA PER TRASPORTO DI PERSONE"), non il campo Unipol
    # chiamato "Tipo veicolo" ("AUTOVETTURA"), che e' piu' generico.
    "categoria":             ["Tipo veicolo", "Tipo Veicolo", "Categoria"],
    "uso_veicolo":           ["Uso Veicolo", "Uso"],
    "valore_veicolo":        ["Valore Veicolo", "Valore veicolo", "Valore assicurato"],
    "kw":                    ["KW"],
    "cilindrata":            ["Cilindrata"],
    "posti":                 ["Posti"],
    "telaio":                ["Telaio"],
    "data_immatricolazione": ["Data immatricolazione", "Data Immatricolazione"],
    "alimentazione":         ["Alimentazione"],
    "classe_cu":             ["Classe CU", "Classe CU assegnata"],
    "classe_cu_provenienza": ["Classe CU Provenienza", "Classe CU di provenienza"],
    "compagnia":             ["Compagnia Provenienza", "Compagnia di provenienza", "Compagnia"],
    "codice_compagnia":      ["Codice Compagnia"],
    "numero_polizza":        ["Numero Polizza"],
    "scadenza_attestato":    ["Scadenza Attestato", "Data scadenza attestato"],
    "forma_tariffaria":      ["Forma Tariffaria"],
    "codice_iur":            ["Codice IUR"],
    "storico_sinistri":      ["Storico Sinistri ATRC", "Storico Sinistri"],
    # ATTENZIONE. Qui sotto NON vanno messi alias generici come "stato",
    # "Note", "scadenza" o "decorrenza". Sono colonne di lavoro dell'agenzia:
    # se il bot ci ripiegasse sopra, sovrascriverebbe lo stato commerciale
    # delle trattative con "Dati Estratti". Meglio non scrivere niente che
    # scrivere nella colonna sbagliata.
    "stato_bot":             ["Stato Bot Estrazione"],
    "note_bot":              ["Note e Errori Bot"],
}

# Colonne di lavoro dell'agenzia: il bot non ci scrive mai, nemmeno se un
# alias dovesse ripiegarci sopra. Qui NON va 'cliente', che e' invece la
# colonna corretta per il nominativo estratto.
COLONNE_INTOCCABILI = {
    _chiave(n) for n in (
        "stato", "Note", "scadenza", "decorrenza", "tipo", "data",
        "premio conf.", "comp. confermata", "forn. scelto",
        "collaboratore", "num. ric.", "beneficiario", "id",
    )
}

_COLONNE_TROVATE: dict[str, str] = {}
_COLONNE_REALI: list[str] = []
_TIPI_COLONNE: dict[str, str] = {}
_ORIGINE_SCHEMA: str = "non ancora letto"

# Tipi che vogliono un valore testuale.
TIPI_TESTO = {
    "singleLineText", "multilineText", "richText",
    "singleSelect", "multipleSelects", "email", "phoneNumber", "url", "barcode",
}
# Tipi che vogliono un numero.
TIPI_NUMERO = {"number", "currency", "percent", "duration", "rating"}


def _adatta_valore(colonna: str, valore):
    """
    Converte il valore nel formato che la colonna si aspetta.

    Serve perche' colonne concettualmente simili hanno tipi diversi: nella
    tabella 'Classe CU' e' testo mentre 'Classe CU Provenienza' e' numero.
    Mandando lo stesso intero a entrambe, la prima lo rifiuta. Il tipo lo dice
    lo schema, quindi invece di indovinare colonna per colonna si adatta.
    """
    if valore is None or valore == "":
        return valore

    tipo = _TIPI_COLONNE.get(colonna)
    if tipo is None:
        return valore              # schema non letto: si manda com'e'

    if tipo in TIPI_TESTO:
        if isinstance(valore, float) and valore.is_integer():
            return str(int(valore))
        return str(valore)

    if tipo in TIPI_NUMERO:
        if isinstance(valore, (int, float)):
            return valore
        pulito = re.sub(r"[^\d.,\-]", "", str(valore)).replace(",", ".")
        try:
            numero = float(pulito)
        except ValueError:
            return None            # non convertibile: meglio non scrivere
        return int(numero) if numero.is_integer() else numero

    if tipo == "checkbox":
        return bool(valore)

    return valore

# Tipi di colonna che Airtable calcola da solo: scriverci dentro e' un errore.
TIPI_NON_SCRIVIBILI = {
    "formula", "rollup", "lookup", "multipleLookupValues", "count",
    "createdTime", "lastModifiedTime", "createdBy", "lastModifiedBy",
    "autoNumber", "button", "externalSyncSource",
}


def _schema_da_metadati() -> list[str]:
    """
    Chiede ad Airtable l'elenco completo delle colonne.

    E' la strada giusta: restituisce tutte le colonne, comprese quelle vuote,
    e dice anche di che tipo sono. Richiede pero' che il token abbia il
    permesso schema.bases:read. Se non ce l'ha, Airtable risponde 403 e si
    passa al metodo di ripiego.
    """
    try:
        res = requests.get(
            f"https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
            headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
            timeout=30,
        )
        if res.status_code != 200:
            print(f"[BDA] metadati non disponibili (HTTP {res.status_code}): "
                  f"al token manca il permesso schema.bases:read", flush=True)
            return []

        for tabella in res.json().get("tables", []):
            if _chiave(tabella.get("name", "")) != _chiave("Trattative"):
                continue
            nomi = []
            for campo in tabella.get("fields", []):
                tipo = campo.get("type")
                if tipo in TIPI_NON_SCRIVIBILI:
                    continue
                nomi.append(campo["name"])
                _TIPI_COLONNE[campo["name"]] = tipo
            scartate = len(tabella.get("fields", [])) - len(nomi)
            print(f"[BDA] schema completo: {len(nomi)} colonne scrivibili "
                  f"({scartate} calcolate, escluse)", flush=True)
            return nomi
    except Exception as e:
        print(f"[BDA] lettura metadati non riuscita: {e}", flush=True)
    return []


def _schema_da_campione() -> list[str]:
    """
    Ripiego: ricava i nomi dai record esistenti.

    Airtable elenca solo i campi valorizzati, quindi una colonna vuota resta
    invisibile. Per questo si leggono piu' pagine invece di cento record soli:
    le colonne aggiunte di recente sono piene solo nelle righe recenti, e
    fermandosi alla prima pagina non si vedrebbero mai.
    """
    nomi: set[str] = set()
    offset = None
    try:
        for _ in range(6):
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            res = requests.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Trattative",
                headers={"Authorization": f"Bearer {AIRTABLE_API_KEY}"},
                params=params, timeout=30,
            )
            if res.status_code != 200:
                break
            corpo = res.json()
            for record in corpo.get("records", []):
                nomi.update(record.get("fields", {}).keys())
            offset = corpo.get("offset")
            if not offset:
                break
    except Exception as e:
        print(f"[BDA] lettura campione non riuscita: {e}", flush=True)

    print(f"[BDA] colonne ricavate dai record: {len(nomi)} "
          f"(le colonne sempre vuote non compaiono)", flush=True)
    return sorted(nomi)


def _colonne_della_tabella() -> list[str]:
    global _COLONNE_REALI, _ORIGINE_SCHEMA
    if _COLONNE_REALI:
        return _COLONNE_REALI

    dai_metadati = _schema_da_metadati()
    if dai_metadati:
        _COLONNE_REALI, _ORIGINE_SCHEMA = dai_metadati, "schema completo"
    else:
        _COLONNE_REALI, _ORIGINE_SCHEMA = _schema_da_campione(), "campione di record (parziale)"
    return _COLONNE_REALI


def _mappa_colonne() -> dict[str, str]:
    """Associa a ogni dato il nome della colonna che esiste davvero."""
    global _COLONNE_TROVATE
    if _COLONNE_TROVATE:
        return _COLONNE_TROVATE

    reali = _colonne_della_tabella()
    per_chiave = {_chiave(n): n for n in reali}

    mappa: dict[str, str] = {}
    for dato, alias in ALIAS_COLONNE.items():
        for candidato in alias:
            trovata = per_chiave.get(_chiave(candidato))
            if trovata:
                mappa[dato] = trovata
                break
        else:
            # Nessun riscontro: si tenta comunque il nome principale. Se la
            # colonna non c'e', la sanificazione 422 la togliera'.
            mappa[dato] = alias[0]

    _COLONNE_TROVATE = mappa
    return mappa


def _colonna_calcolata(nome: str) -> bool:
    """
    Le colonne di tipo Lookup o Formula non sono scrivibili.

    Nella tabella ce n'e' almeno una, 'cf (from Codice Fiscale)': scrivendoci
    Airtable risponde che il campo e' calcolato. Si riconoscono dal '(from '
    nel nome, che e' la forma con cui Airtable nomina i lookup.
    """
    return "(from " in (nome or "").lower()


@app.get("/airtable/colonne")
async def elenco_colonne():
    """
    Mostra quali colonne il bot ha riconosciuto e dove scrivera' ogni dato.

    Utile dopo aver aggiunto o rinominato una colonna:
        https://bot-unipol.onrender.com/airtable/colonne
    """
    mappa = _mappa_colonne()
    reali = set(_colonne_della_tabella())
    mancanti = sorted(dato for dato, colonna in mappa.items() if colonna not in reali)
    return {
        "come_ho_letto_le_colonne": _ORIGINE_SCHEMA,
        "avvertenza": (
            "Elenco parziale: ricavato dai record esistenti, quindi le colonne "
            "sempre vuote non compaiono e risultano mancanti anche se esistono. "
            "Per un elenco completo aggiungi il permesso schema.bases:read al "
            "token Airtable."
            if "campione" in _ORIGINE_SCHEMA else
            "Elenco completo, letto dallo schema della base."
        ),
        "quante_colonne": len(reali),
        "colonne_trovate_in_tabella": sorted(reali),
        "tipo_di_ogni_colonna": dict(sorted(_TIPI_COLONNE.items())),
        "dove_scrive_ogni_dato": mappa,
        "tipo_di_ogni_colonna": {c: _TIPI_COLONNE.get(c, "sconosciuto")
                                 for c in sorted(set(mappa.values()) & reali)},
        "dati_senza_colonna": mancanti,
        "colonne_protette_mai_scritte": sorted(
            c for c in reali if _chiave(c) in COLONNE_INTOCCABILI
        ),
    }


@app.post("/airtable/ricarica-colonne")
async def ricarica_colonne():
    """Da usare dopo aver creato colonne nuove, per farle riconoscere subito."""
    global _COLONNE_TROVATE, _COLONNE_REALI, _TIPI_COLONNE
    _COLONNE_TROVATE, _COLONNE_REALI, _TIPI_COLONNE = {}, [], {}
    return {"stato": "riconoscimento azzerato", "colonne": len(_colonne_della_tabella())}


def _campi_airtable(dati: dict) -> dict:
    """Traduce il risultato della banca dati nelle colonne reali della tabella."""
    p, v, c = dati["posizione"], dati["veicolo"], dati["contraente"]
    col = _mappa_colonne()
    quixa = dati.get("quixa") or {}

    # Marca e modello: vince Quixa quando ha risposto.
    #
    # Unipol restituisce il veicolo come lo registra la Motorizzazione, cioe'
    # "FIAT AUTO SPA 199BXC1A 05" o "KIA" e basta. Quixa restituisce il nome
    # commerciale, "Renault / CLIO 2a SERIE", che e' quello che le altre
    # compagnie si aspettano. Il dato Unipol resta come ripiego.
    marca = quixa.get("marca") or v.marca
    modello = quixa.get("modello") or v.modello

    valori = {
        "targa":                 v.targa,
        "codice_fiscale":        c.codice_fiscale,
        "nominativo":            c.nominativo,
        "data_nascita":          _data_it(c.data_nascita),
        "indirizzo":             c.residenza.via,
        "civico":                c.residenza.civico,
        "cap":                   c.residenza.cap,
        "citta":                 c.residenza.citta,
        "provincia":             c.residenza.provincia,
        "cellulare":             c.cellulare,
        "email":                 c.email,
        "marca":                 marca,
        "modello":               modello,
        "allestimento":          quixa.get("allestimento", ""),
        "categoria":             v.categoria,
        "uso_veicolo":           v.uso,
        "valore_veicolo":        quixa.get("valore_veicolo"),
        "kw":                    v.kw,
        "cilindrata":            v.cilindrata,
        "posti":                 v.posti,
        "telaio":                v.telaio,
        "data_immatricolazione": _data_it(v.data_immatricolazione),
        "alimentazione":         v.alimentazione.value if v.alimentazione else "",
        "classe_cu":             p.cu_assegnazione,
        "classe_cu_provenienza": p.cu_provenienza,
        "compagnia":             p.compagnia_provenienza,
        "codice_compagnia":      p.compagnia_provenienza_codice,
        "numero_polizza":        p.numero_polizza,
        "scadenza_attestato":    _data_it(p.scadenza_attestato),
        "forma_tariffaria":      p.forma_tariffaria.value if p.forma_tariffaria else "",
        "codice_iur":            p.codice_iur,
        "storico_sinistri":      _riassunto_sinistri(p.sinistri),
    }

    campi = {}
    for dato, valore in valori.items():
        colonna = col.get(dato)
        if not colonna or _colonna_calcolata(colonna):
            continue
        if _chiave(colonna) in COLONNE_INTOCCABILI:
            print(f"[BDA] non scrivo in '{colonna}': e' una colonna di lavoro "
                  f"dell'agenzia", flush=True)
            continue
        campi[colonna] = _adatta_valore(colonna, valore)

    stato = col.get("stato_bot", "Stato Bot Estrazione")
    if _chiave(stato) not in COLONNE_INTOCCABILI:
        campi[stato] = "Dati Estratti"
    return campi


def _campo_rifiutato_da_airtable(risposta) -> Optional[str]:
    """
    Ricava dal messaggio di errore il nome della colonna che ha fatto fallire
    la scrittura.

    Airtable segnala il problema in almeno tre formulazioni diverse:
        Unknown field name: "Telaio"
        Field "KW" cannot accept the provided value
        Cannot parse date value "30/08/2021" for field Data immatricolazione
    L'ultima non ha il nome fra virgolette, ed e' quella che prima sfuggiva.
    """
    try:
        messaggio = risposta.json().get("error", {}).get("message", "")
    except Exception:
        messaggio = risposta.text or ""

    for pattern in (
        r'Unknown field name:\s*"?([^"]+?)"?\s*$',
        r'Field\s*"([^"]+)"\s*cannot accept',
        r'for field\s+"?(.+?)"?\s*$',
        r'field\s*"([^"]+)"',
        r'computed field\s*"?([^"]+?)"?\s*$',
    ):
        m = re.search(pattern, messaggio.strip(), re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


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
        campo = _campo_rifiutato_da_airtable(res)

        if campo and campo in payload:
            payload.pop(campo)
            scartati.append(campo)
            print(f"[BDA] Airtable rifiuta '{campo}', la salto", flush=True)
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

    col = _mappa_colonne()
    col_stato = col.get("stato_bot", "Stato Bot Estrazione")
    col_note = col.get("note_bot", "Note e Errori Bot")

    print(f"[BDA] === Make ha chiesto {targa} (record {record_id}) ===", flush=True)
    try:
        requests.patch(url, headers=headers,
                       json={"fields": {col_stato: "In Corso"}}, timeout=30)
    except Exception as e:
        print(f"[BDA] non riesco a segnare 'In Corso': {e}", flush=True)

    try:
        async with bot_semaphore:
            dati = await POOL.consulta(targa)

        # Quixa gira DOPO Unipol, non insieme: due Chromium contemporanei
        # occupano circa 600 MB e Render nel piano gratuito ne mette a
        # disposizione 512. In sequenza si resta sotto la soglia.
        #
        # Se Quixa fallisce non si ferma niente: i dati di Unipol sono gia' in
        # mano e sono quelli che contano. Quixa aggiunge il nome commerciale.
        if QUIXA_ATTIVO:
            async with bot_semaphore:
                esito_quixa = await consulta_quixa(targa)
            if esito_quixa.get("ok"):
                dati["quixa"] = esito_quixa
                alternativi = esito_quixa.get("allestimenti_disponibili") or []
                if len(alternativi) > 1:
                    print(f"[QUIXA] attenzione: {len(alternativi)} allestimenti per "
                          f"{targa}, scritto il primo. Da verificare a mano: "
                          f"{' / '.join(alternativi[:5])}", flush=True)
            else:
                print(f"[QUIXA] nessun dato per {targa}: "
                      f"{esito_quixa.get('errore')}", flush=True)

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
                col_stato: "Errore",
                col_note: f"Airtable: {esito['errore']}"[:1000],
            }}, timeout=30)

    except Exception as e:
        import traceback
        print(f"[BDA] === ERRORE su {targa} ===\n{traceback.format_exc()}", flush=True)
        try:
            requests.patch(url, headers=headers, json={"fields": {
                col_stato: "Errore",
                col_note: f"{type(e).__name__}: {str(e)[:800]}",
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


@app.get("/debug/rete")
async def debug_rete():
    """
    Verifica se il server Unipol risponde a Render, senza aprire il browser.

    E' una semplice chiamata di rete: risponde in pochi secondi e distingue
    le due situazioni che contano.

      "connessione rifiutata" o "tempo scaduto" su tutti gli indirizzi
          -> il server non risponde a Render. Se dal tuo browser entri, e'
             un blocco sull'indirizzo di rete del bot.

      un codice HTTP qualsiasi (200, 403, 503)
          -> il server risponde, il problema e' un altro.

        https://bot-unipol.onrender.com/debug/rete
    """
    from unipol_sessione import BASE_URL, LOGIN_ALTERNATIVI

    def _prova(indirizzo: str) -> dict:
        inizio = datetime.now()
        try:
            res = requests.get(
                indirizzo, timeout=10, allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/122.0.0.0 Safari/537.36"},
            )
            testo = re.sub(r"\s+", " ", res.text)[:300]
            return {
                "indirizzo": indirizzo,
                "codice_http": res.status_code,
                "indirizzo_finale": res.url,
                "secondi": round((datetime.now() - inizio).total_seconds(), 1),
                "contiene_maschera_accesso": bool(
                    re.search(r'name=["\']?Username', res.text, re.I)),
                "inizio_pagina": testo,
            }
        except Exception as e:
            return {
                "indirizzo": indirizzo,
                "errore": f"{type(e).__name__}: {str(e)[:200]}",
                "secondi": round((datetime.now() - inizio).total_seconds(), 1),
            }

    indirizzi = [BASE_URL] + LOGIN_ALTERNATIVI
    esiti = await asyncio.gather(
        *(asyncio.to_thread(_prova, u) for u in dict.fromkeys(indirizzi))
    )

    risponde = any("codice_http" in e for e in esiti)
    return {
        "il_server_risponde_a_render": risponde,
        "come_leggerlo": (
            "Il server risponde: il blocco non e' a livello di rete."
            if risponde else
            "Nessuna risposta. Se dal tuo browser entri normalmente, "
            "l'indirizzo di rete di Render e' bloccato o filtrato."
        ),
        "prove": esiti,
    }


@app.get("/debug/login")
async def debug_login():
    """
    Apre la pagina di accesso Unipol e riferisce esattamente cosa contiene.

    Serve a smettere di tirare a indovinare quando il login non va: mostra
    indirizzo, titolo, testo e nomi dei campi della pagina che il bot trova
    davvero, che possono essere diversi da quelli che vedi tu dal tuo browser
    (indirizzo IP diverso, nessun cookie, lingua diversa).

        https://bot-unipol.onrender.com/debug/login
    """
    if not MOTORE_BDA_ATTIVO:
        raise HTTPException(503, f"Motore BDA non disponibile: {ERRORE_BDA}")

    from playwright.async_api import async_playwright
    from unipol_sessione import ARGS_CHROMIUM, LOGIN_ALTERNATIVI, LOGIN_URL, USER_AGENT

    async with bot_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=ARGS_CHROMIUM)
            contesto = await browser.new_context(user_agent=USER_AGENT, locale="it-IT")
            page = await contesto.new_page()
            try:
                esiti = []
                risposta = None
                for indirizzo in ([LOGIN_URL] if LOGIN_URL else []) + [
                        u for u in LOGIN_ALTERNATIVI if u != LOGIN_URL]:
                    try:
                        # Attese corte: tre indirizzi da trenta secondi
                        # superavano il limite oltre il quale Render chiude
                        # la connessione, e la pagina non si apriva mai.
                        risposta = await page.goto(indirizzo, wait_until="commit",
                                                   timeout=12_000)
                        await page.wait_for_timeout(1_500)
                        trovata = 0
                        for f in page.frames:
                            try:
                                trovata += await f.locator(
                                    'input[name="Username" i], '
                                    'input[name="username" i]').count()
                            except Exception:
                                continue
                        esiti.append({
                            "indirizzo": indirizzo,
                            "codice_http": risposta.status if risposta else None,
                            "indirizzo_finale": page.url,
                            "maschera_di_accesso": bool(trovata),
                        })
                        if trovata:
                            break
                    except Exception as e:
                        esiti.append({"indirizzo": indirizzo,
                                      "errore": f"{type(e).__name__}: {str(e)[:120]}"})
                await page.wait_for_timeout(1_000)
                from unipol_sessione import _testo_pagina
                testo = await _testo_pagina(page, 2_000)
                campi = await page.evaluate("""() =>
                    Array.from(document.querySelectorAll('input, select, button'))
                         .slice(0, 30)
                         .map(e => ({
                             tag: e.tagName.toLowerCase(),
                             type: e.type || null,
                             name: e.name || null,
                             id: e.id || null,
                             value: e.tagName.toLowerCase() === 'button' ? e.innerText : (e.value || null)
                         }))
                """)
                return {
                    "indirizzi_provati": esiti,
                    "indirizzo_finale": page.url,
                    "codice_http": risposta.status if risposta else None,
                    "titolo": await page.title(),
                    "testo_pagina": testo,
                    "riquadri": [f.url for f in page.frames],
                    "campi_presenti": campi,
                }
            except Exception as e:
                return {
                    "errore": f"{type(e).__name__}: {str(e)[:400]}",
                    "indirizzo_finale": page.url,
                }
            finally:
                await contesto.close()
                await browser.close()


@app.get("/stato")
async def stato():
    """Pagina di controllo: dice cosa e' acceso e cosa no."""
    info = {
        "motore_preventivatore": "attivo",
        "motore_banca_dati": "attivo" if MOTORE_BDA_ATTIVO else f"spento ({ERRORE_BDA})",
        "motore_quixa": "attivo" if QUIXA_ATTIVO else f"spento ({ERRORE_QUIXA})",
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
