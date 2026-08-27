"""
unipol_sessione.py — Sessione persistente sul portale Unipol.

PERCHE' QUESTO FILE ESISTE

Il portale e' un'applicazione Struts (le pagine finiscono in .do). Struts tiene
lo stato della navigazione sul server: la pagina sa da dove sei arrivato. Se si
apre paginaD1.do direttamente, il server non trova la "conversazione" aperta e
risponde con l'errore che hai visto. Non e' un problema di permessi ne' di URL
sbagliato: e' proprio come funziona quel framework. Il percorso a menu quindi
non e' un ripiego, e' l'unico modo.

La conseguenza importante e' un'altra. Se il bot fa login a ogni targa, con 100
targhe al giorno spende circa 42 minuti solo in login e OTP. Con una sessione
tenuta calda ne spende 25 secondi in tutto.

Questa classe fa quindi tre cose:
  1. login una volta sola, con il codice OTP generato da pyotp
  2. navigazione a menu fino alla maschera BDA, una volta sola
  3. ciclo veloce targa dopo targa usando il pulsante "Indietro" della pagina,
     che riporta alla maschera di ricerca senza ripassare dai menu

Se la sessione scade, se ne accorge e rifa' il login da sola.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pyotp
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from models import Persona, PosizioneAssicurativa, Veicolo
from unipol_bda import (
    ARGS_CHROMIUM,
    USER_AGENT,
    ErroreUnipol,
    _JS_ESTRAI,
    traduci_pagina_bda,
)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

UNIPOL_USER = os.getenv("UNIPOL_USER", "")
UNIPOL_PASS = os.getenv("UNIPOL_PASS", "")
UNIPOL_TOTP_SECRET = (os.getenv("UNIPOL_TOTP_SECRET") or "").replace(" ", "").strip().upper()
UNIPOL_DOMINIO = os.getenv("UNIPOL_DOMINIO", "Uniage")

BASE_URL = os.getenv("UNIPOL_BASE_URL", "https://essig.unipolsai.it")
LOGIN_URL = f"{BASE_URL}/my-policy"

# La pagina che hai segnalato. Il bot la prova per prima: se il server
# risponde con la maschera, risparmia la navigazione a menu; se risponde con
# l'errore di sessione, passa automaticamente ai menu.
URL_RCAUTO_DIRETTO = os.getenv(
    "UNIPOL_URL_RCAUTO",
    f"{BASE_URL}/Danni/essigRA/danni/rcauto/paginaD1.do",
)

# Voci di menu da percorrere per arrivare alla maschera BDA.
PERCORSO_MENU = [
    v.strip() for v in os.getenv(
        "UNIPOL_PERCORSO_BDA",
        "RAMI AUTO|IBDV ANIA ricerca per targa",
    ).split("|") if v.strip()
]

# Dopo quanto tempo di inattivita' consideriamo la sessione da rinfrescare.
MINUTI_VITA_SESSIONE = int(os.getenv("UNIPOL_MINUTI_SESSIONE", "20"))

# Lo stato del browser (cookie) viene salvato qui: se il processo riparte
# entro la finestra di validita', non serve rifare login e OTP.
FILE_STATO = Path(os.getenv("UNIPOL_FILE_STATO", "/tmp/unipol_stato.json"))

# Frasi che indicano "sessione persa" o "accesso negato".
_SEGNALI_SESSIONE_PERSA = re.compile(
    r"(sessione\s+(non\s+valida|scaduta|terminata)|"
    r"effettua(re)?\s+(nuovamente\s+)?il\s+login|"
    r"accesso\s+negato|utente\s+non\s+autenticato|"
    r"errore\s+di\s+sistema|HTTP\s+Status\s+(4|5)\d\d)",
    re.IGNORECASE,
)


class SessioneScaduta(ErroreUnipol):
    """La sessione sul portale non e' piu' valida: serve un nuovo login."""


class TargaNonTrovata(ErroreUnipol):
    """La targa non ha un attestato in banca dati ANIA."""


# ---------------------------------------------------------------------------
# La sessione
# ---------------------------------------------------------------------------

class SessioneUnipol:
    """
    Una sessione viva sul portale, riutilizzabile per molte targhe.

    Uso tipico:
        sessione = SessioneUnipol()
        await sessione.avvia()
        for targa in targhe:
            dati = await sessione.consulta(targa)
        await sessione.chiudi()

    Tutte le consultazioni sono serializzate da un lock: il browser e' uno solo
    e non puo' navigare in due punti contemporaneamente. Per il parallelismo si
    usa il PoolSessioni piu' sotto.
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contesto: Optional[BrowserContext] = None
        self._pagina: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._ultimo_uso: Optional[datetime] = None
        self._sulla_maschera = False       # siamo gia' sulla maschera di ricerca?
        self.consultazioni = 0
        self.ultimo_otp = ""

    # -- ciclo di vita ------------------------------------------------------

    @property
    def viva(self) -> bool:
        if self._pagina is None or self._ultimo_uso is None:
            return False
        return datetime.now() - self._ultimo_uso < timedelta(minutes=MINUTI_VITA_SESSIONE)

    async def avvia(self) -> None:
        """Apre il browser, fa login e si porta sulla maschera BDA."""
        await self._apri_browser()
        await self._login()
        await self._vai_alla_maschera()
        self._ultimo_uso = datetime.now()

    async def chiudi(self) -> None:
        for risorsa, chiudi in (
            (self._contesto, "close"), (self._browser, "close"), (self._playwright, "stop"),
        ):
            if risorsa is not None:
                try:
                    await getattr(risorsa, chiudi)()
                except Exception:
                    pass
        self._playwright = self._browser = self._contesto = self._pagina = None
        self._sulla_maschera = False

    async def _apri_browser(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=ARGS_CHROMIUM
        )

        stato = str(FILE_STATO) if FILE_STATO.exists() else None
        self._contesto = await self._browser.new_context(
            user_agent=USER_AGENT, storage_state=stato
        )
        self._pagina = await self._contesto.new_page()

        # Immagini e font non servono e costano banda e RAM: li blocchiamo.
        await self._contesto.route(
            re.compile(r"\.(png|jpe?g|gif|svg|woff2?|ttf|ico)(\?|$)", re.I),
            lambda rotta: asyncio.ensure_future(rotta.abort()),
        )

    async def _salva_stato(self) -> None:
        try:
            FILE_STATO.parent.mkdir(parents=True, exist_ok=True)
            await self._contesto.storage_state(path=str(FILE_STATO))
        except Exception:
            pass   # il salvataggio dei cookie e' un'ottimizzazione, non un obbligo

    # -- login --------------------------------------------------------------

    async def _login(self) -> None:
        if not (UNIPOL_USER and UNIPOL_PASS and UNIPOL_TOTP_SECRET):
            raise ErroreUnipol(
                "Credenziali mancanti: imposta UNIPOL_USER, UNIPOL_PASS e "
                "UNIPOL_TOTP_SECRET nel file .env"
            )

        page = self._pagina
        await page.goto(LOGIN_URL, wait_until="commit", timeout=40_000)
        await page.wait_for_timeout(1_500)

        # Se i cookie salvati erano ancora buoni, il portale ci ha gia' fatti
        # entrare e la maschera di login non compare.
        campo_user = page.locator('input[name="Username" i], input[name="username" i]').first
        if not await campo_user.count():
            await self._salva_stato()
            return

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

        conferma = page.locator('input[type="submit"], button').first
        await conferma.wait_for(state="visible", timeout=25_000)
        await conferma.click()

        await self._inserisci_otp()

        # Il portale registra la sessione di sicurezza lato server.
        await asyncio.sleep(6)
        await self._salva_stato()

    async def _inserisci_otp(self) -> None:
        """
        Inserisce il codice TOTP.

        Un codice TOTP vale 30 secondi e Unipol non accetta due volte lo stesso.
        Se abbiamo appena usato questo codice, aspettiamo la finestra successiva
        invece di farci rifiutare il login.
        """
        page = self._pagina
        campo = page.locator('input[type="text"], input[type="number"], input').first
        await campo.wait_for(state="visible", timeout=25_000)

        totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
        codice = totp.now()
        if codice == self.ultimo_otp:
            attesa = 30 - (int(datetime.now().timestamp()) % 30) + 1
            await asyncio.sleep(attesa)
            codice = totp.now()
        self.ultimo_otp = codice

        await campo.fill("")
        await campo.type(codice, delay=100)
        await page.locator('input[type="submit"], button').first.click()

    # -- navigazione fino alla maschera BDA ---------------------------------

    async def _pagina_in_errore(self) -> bool:
        try:
            testo = await self._pagina.inner_text("body", timeout=5_000)
        except Exception:
            return True
        return bool(_SEGNALI_SESSIONE_PERSA.search(testo[:4_000]))

    async def _clic_testo(self, testo: str, tentativi: int = 10) -> bool:
        for _ in range(tentativi):
            for frame in [self._pagina.main_frame, *self._pagina.frames]:
                try:
                    el = frame.get_by_text(testo, exact=False).first
                    if await el.is_visible(timeout=300):
                        await el.click(force=True)
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.4)
        return False

    async def _campo_targa(self):
        """Restituisce il campo Targa se siamo sulla maschera, altrimenti None."""
        for selettore in ('input[name*="targa" i]', 'input[id*="targa" i]'):
            loc = self._pagina.locator(selettore).first
            if await loc.count() and await loc.is_visible():
                return loc
        # Fallback: la maschera ha l'etichetta "Targa" accanto a un input
        try:
            testo = await self._pagina.inner_text("body", timeout=3_000)
            if re.search(r"\bIdentificativo veicolo\b|\bFormato targa\b", testo, re.I):
                loc = self._pagina.locator('input[type="text"]').first
                if await loc.count():
                    return loc
        except Exception:
            pass
        return None

    async def _vai_alla_maschera(self) -> None:
        """
        Si porta sulla maschera 'Identificativo veicolo'.

        Prima prova l'URL diretto (costa una richiesta e a volte funziona se la
        conversazione Struts e' ancora aperta), poi ripiega sui menu.
        """
        page = self._pagina

        if URL_RCAUTO_DIRETTO:
            try:
                await page.goto(URL_RCAUTO_DIRETTO, wait_until="domcontentloaded", timeout=25_000)
                await page.wait_for_timeout(1_200)
                if not await self._pagina_in_errore() and await self._campo_targa():
                    self._sulla_maschera = True
                    return
            except Exception:
                pass   # atteso: passiamo ai menu

        # Percorso a menu
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)

        for voce in PERCORSO_MENU:
            if not await self._clic_testo(voce):
                raise ErroreUnipol(
                    f"Voce di menu '{voce}' non trovata. Se il portale e' cambiato, "
                    f"aggiorna UNIPOL_PERCORSO_BDA nel file .env "
                    f"(voci separate dal carattere |)."
                )
            await page.wait_for_timeout(1_500)

        if await self._campo_targa() is None:
            raise ErroreUnipol("Maschera BDA raggiunta ma campo Targa non trovato.")
        self._sulla_maschera = True

    async def _torna_alla_maschera(self) -> None:
        """
        Dopo una consultazione, il pulsante 'Indietro' riporta alla ricerca.
        E' la scorciatoia che rende veloce il ciclo su molte targhe.
        """
        for selettore in ('input[value*="Indietro" i]', 'button:has-text("Indietro")',
                          'a:has-text("Indietro")'):
            bottone = self._pagina.locator(selettore).first
            try:
                if await bottone.count() and await bottone.is_visible(timeout=800):
                    await bottone.click(force=True)
                    await self._pagina.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await self._pagina.wait_for_timeout(800)
                    if await self._campo_targa():
                        return
            except Exception:
                continue

        # Il pulsante non c'era o non ha funzionato: si rifa' il percorso.
        self._sulla_maschera = False
        await self._vai_alla_maschera()

    # -- consultazione ------------------------------------------------------

    async def consulta(self, targa: str, ritenta: bool = True) -> dict:
        """
        Interroga la banca dati per una targa e restituisce gli oggetti del
        modello. Se la sessione e' scaduta la ricostruisce e riprova una volta.
        """
        targa = (targa or "").replace(" ", "").replace("-", "").upper()
        if not re.fullmatch(r"[A-Z0-9]{5,10}", targa):
            raise ErroreUnipol(f"Targa non valida: '{targa}'")

        async with self._lock:
            try:
                return await self._consulta_interna(targa)
            except SessioneScaduta:
                if not ritenta:
                    raise
                await self.chiudi()
                await self.avvia()
                return await self._consulta_interna(targa)

    async def _consulta_interna(self, targa: str) -> dict:
        inizio = datetime.now()
        page = self._pagina

        if not self._sulla_maschera:
            await self._vai_alla_maschera()

        campo = await self._campo_targa()
        if campo is None:
            raise SessioneScaduta("Maschera di ricerca non disponibile")

        await campo.fill("")
        await campo.type(targa, delay=40)

        premuto = False
        for selettore in ('input[value*="Avanti" i]', 'button:has-text("Avanti")',
                          'a:has-text("Avanti")', 'input[type="submit"]'):
            bottone = page.locator(selettore).first
            try:
                if await bottone.count() and await bottone.is_visible(timeout=800):
                    await bottone.click(force=True)
                    premuto = True
                    break
            except Exception:
                continue
        if not premuto:
            await campo.press("Enter")

        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)

        if await self._pagina_in_errore():
            raise SessioneScaduta("Il portale ha risposto con un errore di sessione")

        grezzo = await page.evaluate(_JS_ESTRAI)
        coppie = grezzo.get("coppie", {})

        if not coppie:
            avvisi = " | ".join(grezzo.get("avvisi", [])) or "pagina senza campi"
            await self._torna_alla_maschera()
            raise TargaNonTrovata(f"Targa {targa}: {avvisi}")

        posizione, veicolo, contraente = traduci_pagina_bda(grezzo)

        if posizione.cu_assegnazione is None and not posizione.compagnia_provenienza:
            avvisi = " | ".join(grezzo.get("avvisi", [])) or "attestato non presente in ANIA"
            await self._torna_alla_maschera()
            raise TargaNonTrovata(f"Targa {targa}: {avvisi}")

        veicolo.targa = veicolo.targa or targa

        await self._torna_alla_maschera()
        self._ultimo_uso = datetime.now()
        self.consultazioni += 1

        return {
            "targa": targa,
            "posizione": posizione,
            "veicolo": veicolo,
            "contraente": contraente,
            "durata_sec": (datetime.now() - inizio).total_seconds(),
            "grezzo": grezzo,
        }


# ---------------------------------------------------------------------------
# Pool di sessioni
# ---------------------------------------------------------------------------

class PoolSessioni:
    """
    Tiene N sessioni calde e le assegna a turno.

    Con 100 targhe al giorno bastano 2 sessioni: una lavora, l'altra copre i
    momenti di picco e i rinnovi di login. Ogni Chromium occupa 250-350 MB, per
    cui il numero va dimensionato sulla RAM della macchina, non sul traffico.
    Su Render piano free (512 MB) il massimo e' 1.
    """

    def __init__(self, dimensione: int = 2, headless: bool = True) -> None:
        self.dimensione = max(1, dimensione)
        self.headless = headless
        self._sessioni: list[SessioneUnipol] = []
        self._disponibili: asyncio.Queue[SessioneUnipol] = asyncio.Queue()
        self._avviato = False

    async def avvia(self) -> None:
        if self._avviato:
            return
        for _ in range(self.dimensione):
            sessione = SessioneUnipol(headless=self.headless)
            await sessione.avvia()
            self._sessioni.append(sessione)
            await self._disponibili.put(sessione)
        self._avviato = True

    async def chiudi(self) -> None:
        for sessione in self._sessioni:
            await sessione.chiudi()
        self._sessioni.clear()
        self._avviato = False

    async def consulta(self, targa: str) -> dict:
        if not self._avviato:
            await self.avvia()
        sessione = await self._disponibili.get()
        try:
            if not sessione.viva:
                await sessione.chiudi()
                await sessione.avvia()
            return await sessione.consulta(targa)
        finally:
            await self._disponibili.put(sessione)

    async def consulta_molte(self, targhe: list[str]) -> list[dict]:
        """
        Consulta un elenco di targhe usando tutte le sessioni del pool.
        Gli errori non fermano il lotto: finiscono nel risultato come 'errore'.
        """
        async def _una(targa: str) -> dict:
            try:
                dati = await self.consulta(targa)
                return {"targa": targa, "ok": True, "dati": dati}
            except ErroreUnipol as e:
                return {"targa": targa, "ok": False, "errore": str(e)}
            except Exception as e:
                return {"targa": targa, "ok": False, "errore": f"imprevisto: {str(e)[:200]}"}

        return await asyncio.gather(*(_una(t) for t in targhe))

    def statistiche(self) -> dict:
        return {
            "sessioni": len(self._sessioni),
            "vive": sum(1 for s in self._sessioni if s.viva),
            "libere": self._disponibili.qsize(),
            "consultazioni_totali": sum(s.consultazioni for s in self._sessioni),
        }


# Istanza condivisa usata dall'API.
POOL = PoolSessioni(
    dimensione=int(os.getenv("UNIPOL_SESSIONI", "2")),
    headless=os.getenv("HEADLESS", "1") == "1",
)


# ---------------------------------------------------------------------------
# Prova:  python unipol_sessione.py DL389LB ES211SV EK806RY
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    async def _prova() -> None:
        targhe = sys.argv[1:] or ["DL389LB"]
        pool = PoolSessioni(dimensione=1, headless=os.getenv("HEADLESS", "1") == "1")
        inizio = datetime.now()
        try:
            risultati = await pool.consulta_molte(targhe)
            for r in risultati:
                if r["ok"]:
                    pos: PosizioneAssicurativa = r["dati"]["posizione"]
                    print(f"\n=== {r['targa']}  ({r['dati']['durata_sec']:.1f}s)")
                    print(json.dumps(pos.model_dump(mode="json"), indent=2, ensure_ascii=False)[:1200])
                else:
                    print(f"\n=== {r['targa']}  ERRORE: {r['errore']}")
            totale = (datetime.now() - inizio).total_seconds()
            print(f"\n{len(targhe)} targhe in {totale:.1f}s "
                  f"({totale/max(len(targhe),1):.1f}s a targa)")
        finally:
            await pool.chiudi()

    asyncio.run(_prova())
