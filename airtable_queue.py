"""
airtable_queue.py — Scrittura su Airtable con coda, batching e backoff.

IL VINCOLO, IN CHIARO

Airtable limita a 5 richieste al secondo per base, su tutti i piani. Non e' una
cosa che si compra: Business ed Enterprise hanno lo stesso tetto di 5 al
secondo. Superandolo si prende un 429 e si resta fermi 30 secondi.

Quello che cambia con il piano e':
  - chiamate al mese per workspace: Free 1.000, Team 100.000, Business illimitate
  - record per base: Team 50.000, Business 125.000, Enterprise Scale 500.000

Con 100 preventivi al giorno il tetto di 5 al secondo non e' un problema, se si
batcha. Il problema vero e' il numero di record: 100 preventivi per 12 compagnie
fanno 1.300 record al giorno, cioe' 28.600 al mese, e il piano Business si
riempie in poco piu' di quattro mesi.

La soluzione e' non scrivere su Airtable le dodici quotazioni di ogni
preventivo: su Airtable vanno la trattativa e le migliori tre. Le altre restano
nell'archivio dell'app, dove non costano nulla. Cosi' si passa da 28.600 a
8.800 record al mese.

Questo modulo mette davanti ad Airtable una coda che:
  - accorpa fino a 10 record per richiesta (batching)
  - non supera 4 richieste al secondo, con un margine sotto il tetto
  - gestisce il 429 con attesa progressiva
  - toglie i campi rifiutati con 422 invece di perdere tutta la scrittura
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

API_KEY = os.getenv("AIRTABLE_API_KEY", "")
BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")

TABELLA_TRATTATIVE = os.getenv("AIRTABLE_TABELLA_TRATTATIVE", "Trattative")
TABELLA_QUOTAZIONI = os.getenv("AIRTABLE_TABELLA_QUOTAZIONI", "Quotazioni_Preventivi")

# Margine di sicurezza sotto il tetto di 5/s dichiarato da Airtable.
RICHIESTE_AL_SECONDO = float(os.getenv("AIRTABLE_RPS", "4"))
RECORD_PER_RICHIESTA = 10          # massimo consentito dall'API
QUOTAZIONI_DA_SALVARE = int(os.getenv("AIRTABLE_QUOTAZIONI_SALVATE", "3"))

TIMEOUT = 30


# ---------------------------------------------------------------------------
# Limitatore di frequenza
# ---------------------------------------------------------------------------

class Limitatore:
    """Token bucket: lascia passare al massimo N richieste al secondo."""

    def __init__(self, al_secondo: float) -> None:
        self.intervallo = 1.0 / al_secondo
        self._prossimo = 0.0
        self._lock = asyncio.Lock()

    async def attendi(self) -> None:
        async with self._lock:
            adesso = time.monotonic()
            if adesso < self._prossimo:
                await asyncio.sleep(self._prossimo - adesso)
            self._prossimo = max(adesso, self._prossimo) + self.intervallo


_limitatore = Limitatore(RICHIESTE_AL_SECONDO)


# ---------------------------------------------------------------------------
# Chiamata HTTP di base
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _url(tabella: str, record_id: str = "") -> str:
    base = f"https://api.airtable.com/v0/{BASE_ID}/{tabella}"
    return f"{base}/{record_id}" if record_id else base


def _campo_rifiutato(testo: str) -> Optional[str]:
    for pattern in (
        r'Unknown field name:\s*\\?"([^\\"]+)\\?"',
        r'Field\s*\\?"([^\\"]+)\\?"\s*cannot accept',
        r'Insufficient permissions to create new select option.*?field\s*\\?"([^\\"]+)\\?"',
    ):
        m = re.search(pattern, testo, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return None


async def _chiama(metodo: str, url: str, corpo: dict) -> requests.Response:
    """Esegue la richiesta rispettando il limite e riprovando sui 429."""
    attesa = 5.0
    for _ in range(4):
        await _limitatore.attendi()
        risposta = await asyncio.to_thread(
            requests.request, metodo, url, headers=_headers(), json=corpo, timeout=TIMEOUT
        )
        if risposta.status_code != 429:
            return risposta
        # Airtable chiede 30 secondi di pausa dopo un 429.
        await asyncio.sleep(attesa)
        attesa = min(attesa * 2, 35)
    return risposta


# ---------------------------------------------------------------------------
# Scritture
# ---------------------------------------------------------------------------

async def aggiorna(tabella: str, record_id: str, campi: dict[str, Any]) -> dict[str, Any]:
    """Aggiorna un record togliendo via via i campi che lo schema rifiuta."""
    if not (API_KEY and BASE_ID):
        return {"ok": False, "errore": "AIRTABLE_API_KEY o AIRTABLE_BASE_ID mancanti", "scartati": []}

    payload = {k: v for k, v in campi.items() if v not in (None, "", [])}
    scartati: list[str] = []

    for _ in range(5):
        risposta = await _chiama("PATCH", _url(tabella, record_id), {"fields": payload})

        if risposta.status_code == 200:
            return {"ok": True, "scartati": scartati, "record": risposta.json()}
        if risposta.status_code != 422:
            return {"ok": False, "errore": f"HTTP {risposta.status_code}: {risposta.text[:300]}",
                    "scartati": scartati}

        campo = _campo_rifiutato(risposta.text)
        if campo and campo in payload:
            payload.pop(campo)
            scartati.append(campo)
            continue

        rimosso = False
        for sospetto in ("KW", "Alimentazione", "cl_cap", "Classe CU", "Data immatricolazione"):
            if sospetto in payload:
                payload.pop(sospetto)
                scartati.append(sospetto)
                rimosso = True
        if not rimosso:
            return {"ok": False, "errore": risposta.text[:300], "scartati": scartati}

    return {"ok": False, "errore": "Superati i tentativi di sanificazione", "scartati": scartati}


async def crea_molti(tabella: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Crea piu' record accorpandoli a dieci per richiesta.

    Trecento quotazioni diventano trenta richieste invece di trecento: sotto il
    limite di frequenza si passa da 75 secondi a 7.
    """
    if not (API_KEY and BASE_ID):
        return {"ok": False, "errore": "Credenziali Airtable mancanti", "creati": 0}
    if not records:
        return {"ok": True, "creati": 0, "ids": []}

    creati: list[str] = []
    errori: list[str] = []

    for inizio in range(0, len(records), RECORD_PER_RICHIESTA):
        lotto = records[inizio:inizio + RECORD_PER_RICHIESTA]
        corpo = {
            "records": [
                {"fields": {k: v for k, v in r.items() if v not in (None, "", [])}}
                for r in lotto
            ],
            "typecast": True,   # Airtable converte da solo stringa -> numero/select
        }
        risposta = await _chiama("POST", _url(tabella), corpo)

        if risposta.status_code == 200:
            creati.extend(r["id"] for r in risposta.json().get("records", []))
        else:
            errori.append(f"lotto {inizio // RECORD_PER_RICHIESTA}: "
                          f"HTTP {risposta.status_code} {risposta.text[:200]}")

    return {"ok": not errori, "creati": len(creati), "ids": creati, "errori": errori}


# ---------------------------------------------------------------------------
# Mappature sulle tabelle di Protexa
# ---------------------------------------------------------------------------

async def aggiorna_stato(record_id: str, stato: str, nota: str = "") -> None:
    campi: dict[str, Any] = {"Stato Bot Estrazione": stato}
    if nota:
        campi["Note e Errori Bot"] = nota[:1000]
    await aggiorna(TABELLA_TRATTATIVE, record_id, campi)


def _data(valore) -> str:
    return valore.strftime("%d/%m/%Y") if valore else ""


async def salva_trattativa(record_id: str, preventivo) -> dict[str, Any]:
    """Scrive sulla tabella Trattative i dati arrivati dalla banca dati."""
    c, v, p = preventivo.contraente, preventivo.veicolo, preventivo.posizione
    campi = {
        "targa": v.targa,
        "Codice Fiscale": c.codice_fiscale,
        "Nome": c.nominativo,
        "cl_datanascita": _data(c.data_nascita),
        "cl_indirizzo": c.residenza.via,
        "cl_civico": c.residenza.civico,
        "cl_cap": c.residenza.cap,
        "cli_citta": c.residenza.citta,
        "cl_provincia": c.residenza.provincia,
        "cellulare": c.cellulare,
        "Marca": v.marca,
        "Modello": v.modello,
        "KW": v.kw,
        "Data immatricolazione": _data(v.data_immatricolazione),
        "Alimentazione": v.alimentazione.value if v.alimentazione else "",
        "Classe CU": p.cu_assegnazione,
        "Compagnia Provenienza": p.compagnia_provenienza,
        "Numero Polizza": p.numero_polizza,
        "Scadenza Attestato": _data(p.scadenza_attestato),
        "Targa Bersani": preventivo.targa_bersani,
        "Stato Bot Estrazione": "Dati Estratti",
    }
    return await aggiorna(TABELLA_TRATTATIVE, record_id, campi)


async def salva_quotazioni(record_trattativa: str, quotazioni: list,
                           quante: int = QUOTAZIONI_DA_SALVARE) -> dict[str, Any]:
    """
    Salva su Airtable solo le migliori N quotazioni.

    E' la scelta che tiene il conto dei record sotto controllo. Le altre restano
    nell'archivio dell'app e si possono comunque consultare dal comparatore.
    """
    valide = sorted(
        (q for q in quotazioni if q.valida),
        key=lambda q: q.premio_annuo,
    )[:quante]

    records = [
        {
            "Trattative": [record_trattativa],
            "Numero Preventivo": q.numero_preventivo,
            "Compagnia": q.compagnia,
            "Prodotto": q.prodotto,
            "Premio Annuo": q.premio_annuo,
            "Premio Semestrale": q.premio_semestrale,
            "Premio Lordo RCA": q.premio_lordo_rca,
            "Imposte e Diritti": q.imposte_e_diritti,
            "Sconto Applicato": q.sconto_applicato,
            "Link Preventivo": q.link_preventivo,
            "PDF Precontrattuale": q.pdf_precontrattuale,
            "Data Ora Calcolo": q.calcolato_il.isoformat(),
        }
        for q in valide
    ]
    return await crea_molti(TABELLA_QUOTAZIONI, records)


# ---------------------------------------------------------------------------
# Stima del consumo, per decidere il piano
# ---------------------------------------------------------------------------

@dataclass
class StimaConsumo:
    preventivi_al_giorno: int = 100
    compagnie: int = 12
    giorni_lavorativi_al_mese: int = 22
    quotazioni_salvate: int = QUOTAZIONI_DA_SALVARE
    tetti_record: dict[str, int] = field(default_factory=lambda: {
        "Team": 50_000, "Business": 125_000, "Enterprise Scale": 500_000,
    })

    def calcola(self) -> dict[str, Any]:
        import math
        rec_giorno = self.preventivi_al_giorno * (1 + self.quotazioni_salvate)
        rec_mese = rec_giorno * self.giorni_lavorativi_al_mese

        chiamate_giorno = (
            self.preventivi_al_giorno * 2                                       # crea + aggiorna
            + math.ceil(self.preventivi_al_giorno * self.quotazioni_salvate / RECORD_PER_RICHIESTA)
            + self.preventivi_al_giorno * 2                                     # stati intermedi
        )
        return {
            "record_al_giorno": rec_giorno,
            "record_al_mese": rec_mese,
            "chiamate_al_giorno": chiamate_giorno,
            "chiamate_al_mese": chiamate_giorno * self.giorni_lavorativi_al_mese,
            "mesi_prima_del_tetto": {
                piano: round(tetto / rec_mese, 1)
                for piano, tetto in self.tetti_record.items()
            },
        }


if __name__ == "__main__":
    import json
    print("Solo le migliori 3 quotazioni su Airtable:")
    print(json.dumps(StimaConsumo().calcola(), indent=2, ensure_ascii=False))
    print("\nTutte e 12 le quotazioni su Airtable:")
    print(json.dumps(StimaConsumo(quotazioni_salvate=12).calcola(), indent=2, ensure_ascii=False))
