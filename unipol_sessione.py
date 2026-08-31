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
from typing import Optional

import pyotp
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from models import Persona, PosizioneAssicurativa, Veicolo
from unipol_bda import (
    ARGS_CHROMIUM,
    USER_AGENT,
    ErroreUnipol,
    _JS_ESTRAI,
    parse_pagina_veicolo,
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

# Indirizzo della pagina di accesso.
#
# E' "my.policy" con il PUNTO, non "my-policy" col trattino: si legge nella
# barra degli indirizzi del video del login, ed e' il nome standard della
# pagina di accesso dei sistemi F5, quelli davanti al portale Unipol. Con il
# trattino il server risponde "404 Not Found - IBM_HTTP_Server".
LOGIN_URL = os.getenv("UNIPOL_LOGIN_URL", f"{BASE_URL}/my.policy")

# Se la maschera non compare, si provano questi altri indirizzi: chiedendo una
# pagina protetta, il sistema di accesso reindirizza da solo alla sua maschera.
LOGIN_ALTERNATIVI = [
    f"{BASE_URL}/my.policy",
    f"{BASE_URL}/",
    f"{BASE_URL}/my-policy",
]

# La pagina che hai segnalato. Il bot la prova per prima: se il server
# risponde con la maschera, risparmia la navigazione a menu; se risponde con
# l'errore di sessione, passa automaticamente ai menu.
URL_RCAUTO_DIRETTO = os.getenv(
    "UNIPOL_URL_RCAUTO",
    f"{BASE_URL}/Danni/essigRA/danni/rcauto/paginaD1.do",
)

# Pagina di partenza dentro Leonardo, da cui si apre il menu Strumenti.
LEONARDO_URL = os.getenv(
    "UNIPOL_LEONARDO_URL",
    f"{BASE_URL}/WorkspaceWeb/app/configuratore_questionari/questionario",
)

# Percorso reale dentro il menu Strumenti di Leonardo:
#     Strumenti -> (sezione SERVIZI) DANNI -> RCA AUTO -> CONSULTAZIONE BDA
# L'ultima voce apre una scheda nuova del browser.
PERCORSO_MENU = [
    v.strip() for v in os.getenv(
        "UNIPOL_PERCORSO_BDA",
        "Strumenti|DANNI|RCA AUTO|CONSULTAZIONE BDA",
    ).split("|") if v.strip()
]

# Dopo quanto tempo di inattivita' consideriamo la sessione da rinfrescare.
MINUTI_VITA_SESSIONE = int(os.getenv("UNIPOL_MINUTI_SESSIONE", "20"))

# Frasi che indicano "devi rifare il login": l'accesso non c'e' piu'.
_SEGNALI_SESSIONE_PERSA = re.compile(
    r"(sessione\s+(non\s+valida|scaduta|terminata)|"
    r"effettua(re)?\s+(nuovamente\s+)?il\s+login|"
    r"accesso\s+negato|utente\s+non\s+autenticato|"
    r"HTTP\s+Status\s+(4|5)\d\d)",
    re.IGNORECASE,
)

# Frasi che indicano "il percorso si e' rotto, ma sei ancora dentro".
#
# Sono un caso diverso e vanno trattate diversamente: qui rifare il login
# sarebbe un errore, basta tornare su Leonardo e ripercorrere i menu.
#
# La prima riguarda il divieto di sessioni contemporanee: il portale non
# ammette lo stesso utente collegato due volte. Se qualcuno e' dentro dal
# proprio browser mentre il bot lavora, uno dei due viene buttato fuori.
_SEGNALI_PERCORSO_ROTTO = re.compile(
    r"(sessioni\s+concorrenti|"
    r"sessione\s+di\s+lavoro\s+deve\s+essere\s+interrotta|"
    r"errore\s+non\s+previsto|"
    r"operazione\s+richiesta\s+non\s+e?'?\s*stata\s+completata|"
    r"errore\s+di\s+sistema)",
    re.IGNORECASE,
)


def _log(messaggio: str) -> None:
    """
    Scrive una riga nei log di Render.

    Serve a vedere dove si ferma il bot quando qualcosa non va: senza questo
    diario, un errore di rete o un menu che cambia nome lasciano solo una
    pagina bianca e nessun indizio.
    """
    print(f"[BDA] {messaggio}", flush=True)


async def _testo_pagina(pagina, limite: int = 6_000) -> str:
    """
    Restituisce il testo di una pagina, anche quando non ha un <body>.

    Le pagine divise in riquadri (frameset) non hanno un elemento <body>:
    leggerlo va in timeout e ogni riconoscimento fallisce senza spiegazione.
    Qui si prova prima il testo visibile, poi si ripiega sul codice grezzo e
    sul contenuto dei singoli riquadri.
    """
    pezzi = []
    try:
        pezzi.append(await pagina.inner_text("body", timeout=3_000))
    except Exception:
        pass

    if not any(p.strip() for p in pezzi):
        try:
            grezzo = await pagina.content()
            senza_tag = re.sub(r"<script.*?</script>|<style.*?</style>", " ", grezzo,
                               flags=re.S | re.I)
            senza_tag = re.sub(r"<[^>]+>", " ", senza_tag)
            pezzi.append(senza_tag)
        except Exception:
            pass

    for frame in pagina.frames:
        if frame is pagina.main_frame:
            continue
        try:
            pezzi.append(await frame.inner_text("body", timeout=2_000))
        except Exception:
            continue

    return re.sub(r"\s+", " ", " ".join(pezzi)).strip()[:limite]


class SessioneScaduta(ErroreUnipol):
    """L'accesso non c'e' piu': serve un nuovo login."""


class PercorsoInterrotto(ErroreUnipol):
    """
    La sessione di lavoro dentro l'applicazione si e' rotta, ma l'accesso
    e' ancora valido. Si risolve tornando su Leonardo e rifacendo i menu,
    NON rifacendo il login.
    """


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
        self._pagina: Optional[Page] = None            # scheda BDA (dove si lavora)
        self._pagina_leonardo: Optional[Page] = None   # scheda con il menu Strumenti
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
        """
        Apre il browser, fa login e si porta sulla maschera BDA.

        Viene chiamata una volta sola: le targhe successive riusano la stessa
        sessione senza rifare login e OTP.
        """
        await self._apri_browser()
        await self._login()
        await self._vai_alla_maschera()
        self._ultimo_uso = datetime.now()

    async def chiudi(self) -> None:
        # Prima di spegnere il browser si esce dal portale, cosi' la procedura
        # di accesso non resta aperta: e' quella che, al tentativo successivo,
        # fa comparire "access policy evaluation is already in progress".
        try:
            if self._pagina and not self._pagina.is_closed():
                for selettore in ('a:has-text("Log Out")', 'a:has-text("Logout")',
                                  'a[href*="logout" i]', 'a[href*="hangup" i]'):
                    bottone = self._pagina.locator(selettore).first
                    if await bottone.count() and await bottone.is_visible(timeout=800):
                        await bottone.click(force=True)
                        await self._pagina.wait_for_timeout(1_500)
                        _log("uscita dal portale eseguita")
                        break
        except Exception:
            pass

        for risorsa, chiudi in (
            (self._contesto, "close"), (self._browser, "close"), (self._playwright, "stop"),
        ):
            if risorsa is not None:
                try:
                    await getattr(risorsa, chiudi)()
                except Exception:
                    pass
        self._playwright = self._browser = self._contesto = None
        self._pagina = self._pagina_leonardo = None
        self._sulla_maschera = False

    async def _apri_browser(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=ARGS_CHROMIUM
        )

        # Nessun riuso dei cookie fra un riavvio e l'altro: si e' rivelato
        # una fonte di problemi (il portale mostrava schermate a meta'
        # autenticazione) senza vantaggi reali. La sessione si riusa fra una
        # targa e l'altra tenendo aperto il browser, che e' un'altra cosa.
        self._contesto = await self._browser.new_context(user_agent=USER_AGENT)
        self._pagina = await self._contesto.new_page()

        # Immagini e font non servono e costano banda e RAM: li blocchiamo.
        await self._contesto.route(
            re.compile(r"\.(png|jpe?g|gif|svg|woff2?|ttf|ico)(\?|$)", re.I),
            lambda rotta: asyncio.ensure_future(rotta.abort()),
        )

    # -- login --------------------------------------------------------------

    async def _sblocca_sessione_bloccata(self) -> bool:
        """
        Sblocca la pagina "Access policy evaluation is already in progress".

        E' il sistema di accesso che protegge il portale: ogni tentativo di
        accesso interrotto a meta' lascia una procedura aperta, e al tentativo
        successivo mostra questa pagina invece della maschera. La pagina stessa
        offre la soluzione, un collegamento "here" che apre una sessione nuova.

        Senza questo, dopo qualche tentativo fallito il bot non riesce piu' ad
        accedere e sembra un blocco: e' invece la coda dei propri errori.
        """
        page = self._pagina
        testo = await _testo_pagina(page, 2_000)
        if not re.search(r"access policy evaluation is already in progress|"
                         r"create a new session", testo, re.I):
            return False

        _log("procedura di accesso rimasta aperta, ne apro una nuova")

        for selettore in ('a:has-text("here")', 'a[href*="my.policy"]',
                          'a[href*="hangup"]', 'a[href*="logout"]', 'a'):
            try:
                collegamento = page.locator(selettore).first
                if await collegamento.count() and await collegamento.is_visible(timeout=1_500):
                    await collegamento.click(force=True)
                    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(2_500)
                    return True
            except Exception:
                continue

        # Nessun collegamento cliccabile: si azzerano i cookie, che e' l'altro
        # modo di far dimenticare al portale la procedura in sospeso.
        try:
            await self._contesto.clear_cookies()
            _log("nessun collegamento trovato, azzerati i cookie della sessione")
            return True
        except Exception:
            return False

    async def _apri_maschera_login(self):
        """
        Apre la pagina di accesso e restituisce il campo Utente.

        Prova gli indirizzi noti finche' la maschera non compare. Se nessuno
        funziona, descrive nei log cosa ha trovato invece di lasciare un
        timeout muto: un errore che dice solo "campo non trovato" costringe a
        indovinare, e ci abbiamo gia' perso una serata.
        """
        page = self._pagina
        selettore = 'input[name="Username" i], input[name="username" i]'

        indirizzi = [LOGIN_URL] + [u for u in LOGIN_ALTERNATIVI if u != LOGIN_URL]
        for indirizzo in indirizzi:
            try:
                await page.goto(indirizzo, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                _log(f"  {indirizzo} non raggiungibile ({type(e).__name__})")
                continue
            await page.wait_for_timeout(2_500)

            # Due passate: se la prima trova la pagina della procedura gia'
            # aperta, la si sblocca e si riprova sullo stesso indirizzo.
            for passata in range(2):
                for frame in [page.main_frame, *page.frames]:
                    campo = frame.locator(selettore).first
                    try:
                        if await campo.count() and await campo.is_visible(timeout=3_000):
                            dove = "" if frame is page.main_frame else " (dentro un riquadro)"
                            if indirizzo != LOGIN_URL or dove or passata:
                                _log(f"  maschera trovata su {indirizzo}{dove}")
                            return campo
                    except Exception:
                        continue

                if passata == 0 and await self._sblocca_sessione_bloccata():
                    continue
                break

            _log(f"  nessuna maschera su {indirizzo}")

        await self._descrivi_pagina(page)
        raise ErroreUnipol(
            "Maschera di accesso non trovata su nessuno degli indirizzi noti. "
            "Sopra c'e' la descrizione dell'ultima pagina vista."
        )

    async def _login(self) -> None:
        """
        Sequenza di accesso identica a quella del bot originale.

        Non c'e' riconoscimento di schermate ne' verifiche intermedie: si
        compilano le credenziali, si preme il pulsante della schermata MFA, si
        scrive il codice e si forza l'URL di Leonardo. Il portale, dopo il
        codice, atterra su una pagina 404: la "forzatura" e' il modo di
        aggirarla, ed e' il motivo per cui questa sequenza funziona.

        Ogni tentativo di renderla piu' furba ha peggiorato le cose. Se un
        giorno il portale cambiasse davvero, meglio partire da qui.
        """
        if not (UNIPOL_USER and UNIPOL_PASS and UNIPOL_TOTP_SECRET):
            raise ErroreUnipol(
                "Credenziali mancanti: imposta UNIPOL_USER, UNIPOL_PASS e "
                "UNIPOL_TOTP_SECRET fra le variabili d'ambiente."
            )

        page = self._pagina

        _log("1/4 inserimento utente e password")
        campo_user = await self._apri_maschera_login()
        await campo_user.fill(UNIPOL_USER)

        await page.locator('input[type="password"]').first.fill(UNIPOL_PASS)

        dominio = page.locator('select[name="domain" i]').first
        if await dominio.is_visible():
            try:
                await dominio.select_option(label=UNIPOL_DOMINIO, timeout=3_000)
            except Exception:
                await dominio.select_option(value=UNIPOL_DOMINIO, timeout=3_000)

        await page.locator('input[type="submit"], button').first.click()

        _log("2/4 schermata intermedia MFA")
        bottone_mfa = page.locator('input[type="submit"], button').first
        await bottone_mfa.wait_for(state="visible", timeout=25_000)
        await bottone_mfa.click()

        _log("3/4 inserimento codice OTP")
        campo_codice = page.locator(
            'input[type="text"], input[type="number"], input').first
        await campo_codice.wait_for(state="visible", timeout=25_000)

        totp = pyotp.TOTP(UNIPOL_TOTP_SECRET)
        codice = totp.now()
        if codice == self.ultimo_otp:
            # Un codice vale 30 secondi e non e' riutilizzabile: se e' lo
            # stesso di poco fa si aspetta la finestra successiva.
            attesa = 30 - (int(datetime.now().timestamp()) % 30) + 1
            _log(f"stesso codice di poco fa, attendo {attesa}s")
            await asyncio.sleep(attesa)
            codice = totp.now()
        self.ultimo_otp = codice

        await campo_codice.fill("")
        await campo_codice.type(codice, delay=100)
        await page.locator('input[type="submit"], button').first.click()

        _log("attesa 6s per la registrazione della sessione")
        await asyncio.sleep(6)

        _log("4/4 forzatura URL Leonardo")
        try:
            await page.goto(LEONARDO_URL, wait_until="commit", timeout=20_000)
        except Exception as e:
            _log(f"interferenza di rete ({type(e).__name__}), proseguo")
        await page.wait_for_timeout(4_000)

    async def _descrivi_pagina(self, pagina: Page) -> None:
        """
        Scrive nei log cosa c'e' davvero sulla pagina non riconosciuta.

        Senza questo, ogni tentativo di correzione e' un'ipotesi: si vede solo
        che il riconoscimento fallisce, non cosa il portale stia mostrando.
        """
        try:
            url = pagina.url
        except Exception:
            url = "?"
        try:
            titolo = await pagina.title()
        except Exception:
            titolo = "?"
        testo = (await _testo_pagina(pagina, 500)) or "(nessun testo leggibile)"
        try:
            riquadri = [f.url for f in pagina.frames]
        except Exception:
            riquadri = []
        try:
            campi = await pagina.evaluate("""() =>
                Array.from(document.querySelectorAll('input, select, button'))
                     .slice(0, 25)
                     .map(e => `${e.tagName.toLowerCase()}[type=${e.type||'-'}]`
                               + `[name=${e.name||'-'}][id=${e.id||'-'}]`)
            """)
        except Exception:
            campi = []

        _log("--- PAGINA NON RICONOSCIUTA ---")
        _log(f"  url    : {url}")
        _log(f"  titolo : {titolo}")
        _log(f"  testo  : {testo}")
        _log(f"  campi  : {' | '.join(campi) if campi else 'nessuno'}")
        _log(f"  riquadri: {len(riquadri)} -> {' | '.join(riquadri[:5])}")
        _log("--- fine descrizione ---")

    async def _pagina_in_errore(self) -> str:
        """
        Dice che tipo di errore mostra la pagina.

        Restituisce "" se va tutto bene, "percorso" se la sessione di lavoro
        si e' rotta (si rifanno i menu), "login" se l'accesso e' scaduto.
        """
        testa = await _testo_pagina(self._pagina, 4_000)
        if not testa:
            return "percorso"
        if _SEGNALI_PERCORSO_ROTTO.search(testa):
            return "percorso"
        if _SEGNALI_SESSIONE_PERSA.search(testa):
            return "login"
        return ""

    async def _chiudi_segnalazione(self) -> bool:
        """
        Chiude la finestrella "Segnalazione" che il portale apre sugli errori.

        Finche' resta aperta copre la pagina e ogni clic successivo fallisce.
        """
        for selettore in ('input[value*="Chiudi" i]', 'button:has-text("Chiudi")',
                          'a:has-text("Chiudi")', 'td:has-text("Chiudi")'):
            try:
                bottone = self._pagina.locator(selettore).first
                if await bottone.count() and await bottone.is_visible(timeout=800):
                    await bottone.click(force=True)
                    await self._pagina.wait_for_timeout(800)
                    _log("chiusa la finestrella di segnalazione")
                    return True
            except Exception:
                continue
        return False

    async def _clic_menu(self, pagina: Page, testo: str, tentativi: int = 12) -> bool:
        """
        Clicca una voce del menu Strumenti.

        La corrispondenza esatta viene prima di quella parziale, ed e' una
        precauzione necessaria: sotto il menu aperto la pagina di Leonardo
        contiene la scritta "ALTRI PRODOTTI DANNI", che con una ricerca
        parziale verrebbe scambiata per la voce "DANNI" del menu.
        """
        for tentativo in range(tentativi):
            for frame in [pagina.main_frame, *pagina.frames]:
                # 1) corrispondenza esatta, ignorando maiuscole/minuscole
                try:
                    esatto = frame.get_by_text(
                        re.compile(rf"^\s*{re.escape(testo)}\s*$", re.IGNORECASE)
                    )
                    for i in range(min(await esatto.count(), 5)):
                        el = esatto.nth(i)
                        if await el.is_visible(timeout=250):
                            await el.click(force=True)
                            return True
                except Exception:
                    pass

                # 2) solo dopo qualche giro, corrispondenza parziale
                if tentativo >= 4:
                    try:
                        el = frame.get_by_text(testo, exact=False).first
                        if await el.is_visible(timeout=250):
                            await el.click(force=True)
                            return True
                    except Exception:
                        pass
            await asyncio.sleep(0.4)
        return False

    async def _campo_targa(self):
        """
        Restituisce il campo Targa della maschera di ricerca, o None.

        Il controllo e' volutamente severo. La pagina dei dati veicolo mostra
        anch'essa un campo "Targa", ma di sola lettura e con dentro la targa
        appena cercata: scambiandola per la maschera, il bot proverebbe a
        scriverci sopra la targa successiva e resterebbe bloccato.

        Due difese: il campo dev'essere davvero scrivibile, e nella pagina
        dev'esserci la dicitura "Formato targa", che compare solo sulla
        maschera di ricerca.
        """
        for selettore in ('input[name*="targa" i]:not([readonly]):not([disabled])',
                          'input[id*="targa" i]:not([readonly]):not([disabled])'):
            loc = self._pagina.locator(selettore).first
            try:
                if await loc.count() and await loc.is_editable(timeout=800):
                    return loc
            except Exception:
                continue

        # Ripiego: si accetta il primo campo di testo solo se la pagina e'
        # davvero la maschera di ricerca e solo se il campo e' scrivibile.
        testo = await _testo_pagina(self._pagina, 4_000)
        if not re.search(r"Formato\s+targa", testo, re.I):
            return None

        campi = self._pagina.locator('input[type="text"]:not([readonly]):not([disabled])')
        try:
            for i in range(min(await campi.count(), 4)):
                loc = campi.nth(i)
                if await loc.is_editable(timeout=500):
                    return loc
        except Exception:
            pass
        return None

    async def _vai_alla_maschera(self) -> None:
        """
        Si porta sulla maschera 'Identificativo veicolo' della consultazione BDA.

        Percorso:  Leonardo -> Strumenti -> DANNI -> RCA AUTO -> CONSULTAZIONE BDA

        L'ultima voce apre una scheda nuova del browser, quindi il click viene
        avvolto in expect_page: senza, il bot resterebbe sulla scheda di
        Leonardo mentre la maschera si apre altrove.
        """
        # Se una scheda BDA e' gia' aperta da una consultazione precedente,
        # la si riusa invece di rifare tutto il giro.
        if self._pagina and not self._pagina.is_closed() and await self._campo_targa():
            self._sulla_maschera = True
            return

        leonardo = self._pagina_leonardo or self._pagina
        if leonardo is None or leonardo.is_closed():
            leonardo = await self._contesto.new_page()
        self._pagina_leonardo = leonardo

        _log(f"4/6 apertura Leonardo: {LEONARDO_URL}")
        await leonardo.goto(LEONARDO_URL, wait_until="domcontentloaded", timeout=30_000)
        await leonardo.wait_for_timeout(3_000)
        await self._chiudi_segnalazione()

        voci_intermedie = PERCORSO_MENU[:-1]
        voce_finale = PERCORSO_MENU[-1]

        for voce in voci_intermedie:
            if not await self._clic_menu(leonardo, voce):
                await self._descrivi_pagina(leonardo)
                raise ErroreUnipol(
                    f"Voce di menu '{voce}' non trovata dentro Leonardo. "
                    f"Percorso atteso: {' > '.join(PERCORSO_MENU)}. "
                    f"Sopra c'e' la descrizione della pagina che il bot ha "
                    f"davanti."
                )
            _log(f"5/6 cliccata voce di menu '{voce}'")
            await leonardo.wait_for_timeout(1_200)

        # L'ultimo click apre la scheda nuova.
        nuova: Optional[Page] = None
        try:
            async with self._contesto.expect_page(timeout=20_000) as attesa:
                if not await self._clic_menu(leonardo, voce_finale):
                    raise ErroreUnipol(
                        f"Voce '{voce_finale}' non trovata nel menu RCA AUTO."
                    )
            nuova = await attesa.value
        except ErroreUnipol:
            raise
        except Exception:
            # Non ha aperto una scheda nuova: forse si e' caricata al suo posto.
            nuova = None

        if nuova is not None:
            self._pagina = nuova
            await nuova.wait_for_load_state("domcontentloaded", timeout=30_000)
        else:
            self._pagina = leonardo
        _log("6/6 scheda BDA aperta" if nuova is not None else "6/6 pagina caricata nella stessa scheda")
        await self._pagina.wait_for_timeout(2_500)

        if await self._campo_targa() is None:
            tipo = await self._pagina_in_errore()
            if tipo == "percorso":
                await self._chiudi_segnalazione()
                raise PercorsoInterrotto(
                    "La sessione di lavoro si e' interrotta prima di arrivare "
                    "alla maschera. Se qualcuno e' collegato al portale con lo "
                    "stesso utente, il portale scollega l'altro: e' il "
                    "messaggio 'sessioni concorrenti'."
                )
            if tipo == "login":
                raise SessioneScaduta("Il portale chiede di rifare l'accesso")
            await self._descrivi_pagina(self._pagina)
            raise ErroreUnipol(
                "Maschera BDA aperta ma campo Targa non trovato. "
                "Sopra c'e' la descrizione della pagina."
            )
        _log("maschera di ricerca pronta")
        self._sulla_maschera = True

    async def _torna_alla_maschera(self) -> None:
        """
        Riporta il bot alla maschera di ricerca.

        Il pulsante "Indietro" va premuto piu' volte: dalla pagina attestato si
        torna alla pagina dati veicolo, e solo da li' alla ricerca. Premerlo
        una volta sola lasciava il bot sulla pagina intermedia, dove il campo
        Targa esiste ma e' di sola lettura.
        """
        for passo in range(3):
            premuto = False
            for selettore in ('input[value*="Indietro" i]',
                              'button:has-text("Indietro")',
                              'a:has-text("Indietro")'):
                bottone = self._pagina.locator(selettore).first
                try:
                    if await bottone.count() and await bottone.is_visible(timeout=800):
                        await bottone.click(force=True)
                        await self._pagina.wait_for_load_state("domcontentloaded", timeout=20_000)
                        await self._pagina.wait_for_timeout(1_000)
                        premuto = True
                        break
                except Exception:
                    continue

            if not premuto:
                break
            if await self._campo_targa():
                _log(f"tornato alla maschera di ricerca ({passo + 1} clic su Indietro)")
                return

        # Indietro non ha funzionato: si rifa' il percorso dai menu.
        # La scheda BDA vecchia va chiusa, altrimenti se ne accumulano.
        _log("Indietro non ha riportato alla ricerca, rifaccio il percorso dai menu")
        self._sulla_maschera = False
        if self._pagina and self._pagina is not self._pagina_leonardo:
            try:
                await self._pagina.close()
            except Exception:
                pass
            self._pagina = None
        await self._vai_alla_maschera()

    # -- consultazione ------------------------------------------------------

    async def consulta(self, targa: str, ritenta: bool = True) -> dict:
        """
        Interroga la banca dati per una targa.

        Due tipi di intoppo, due rimedi diversi:
          - percorso interrotto -> si torna su Leonardo e si rifanno i menu,
            senza toccare l'accesso, che e' ancora buono
          - accesso scaduto     -> si rifa' il login da zero
        Fare il login quando basterebbe rifare i menu costa venticinque
        secondi e, se qualcun altro e' collegato, lo scollega.
        """
        targa = (targa or "").replace(" ", "").replace("-", "").upper()
        if not re.fullmatch(r"[A-Z0-9]{5,10}", targa):
            raise ErroreUnipol(f"Targa non valida: '{targa}'")

        async with self._lock:
            try:
                return await self._consulta_interna(targa)

            except PercorsoInterrotto as e:
                if not ritenta:
                    raise
                _log(f"{e}")
                _log("rifaccio il percorso dai menu, senza rifare l'accesso")
                await self._chiudi_segnalazione()
                self._sulla_maschera = False
                try:
                    await self._vai_alla_maschera()
                    return await self._consulta_interna(targa)
                except (PercorsoInterrotto, SessioneScaduta):
                    _log("non e' bastato: rifaccio l'accesso da zero")
                    await self.chiudi()
                    await self.avvia()
                    return await self._consulta_interna(targa)

            except SessioneScaduta:
                if not ritenta:
                    raise
                _log("accesso scaduto, rifaccio il login")
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
            # Non siamo sulla maschera: si rifa' il percorso una volta sola.
            _log("maschera non disponibile, ricostruisco il percorso")
            self._sulla_maschera = False
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

        tipo_errore = await self._pagina_in_errore()
        if tipo_errore == "percorso":
            await self._chiudi_segnalazione()
            raise PercorsoInterrotto(
                "La sessione di lavoro si e' interrotta durante la ricerca."
            )
        if tipo_errore == "login":
            raise SessioneScaduta("Il portale chiede di rifare l'accesso")

        # --- PAGINA 1: dati bda (paginaD0) --------------------------------
        # Contiene i dati tecnici del veicolo: telaio, omologazione,
        # cilindrata, potenza, alimentazione, posti, immatricolazione.
        _log(f"targa {targa} inviata, lettura pagina dati veicolo")
        testo_veicolo = await _testo_pagina(page, 20_000)
        veicolo = parse_pagina_veicolo(testo_veicolo)
        _log(f"veicolo: {veicolo.marca or '?'} | kw {veicolo.kw} | "
             f"alim {veicolo.alimentazione} | imm {veicolo.data_immatricolazione}")

        # --- PAGINA 2: attestato ANIA (paginaD1) --------------------------
        # Si raggiunge solo con il pulsante "Visualizza Attestato" in fondo.
        attestato_aperto = False
        for selettore in ('input[value*="Visualizza Attestato" i]',
                          'button:has-text("Visualizza Attestato")',
                          'a:has-text("Visualizza Attestato")',
                          'input[value*="Attestato" i]',
                          'text=/Visualizza\\s+Attestato/i'):
            try:
                bottone = page.locator(selettore).first
                if await bottone.count() and await bottone.is_visible(timeout=800):
                    await bottone.click(force=True)
                    await page.wait_for_load_state("domcontentloaded", timeout=25_000)
                    await page.wait_for_timeout(2_000)
                    attestato_aperto = True
                    _log("cliccato 'Visualizza Attestato'")
                    break
            except Exception:
                continue

        if not attestato_aperto:
            _log("ATTENZIONE: pulsante 'Visualizza Attestato' non trovato, "
                 "proseguo con la pagina corrente")

        grezzo = await page.evaluate(_JS_ESTRAI)
        coppie = grezzo.get("coppie", {})
        _log(f"letti {len(coppie)} campi dalla pagina attestato")

        if not coppie:
            avvisi = " | ".join(grezzo.get("avvisi", [])) or "pagina senza campi"
            await self._torna_alla_maschera()
            raise TargaNonTrovata(f"Targa {targa}: {avvisi}")

        posizione, veicolo_bda, contraente = traduci_pagina_bda(grezzo)

        # I dati tecnici arrivano dalla pagina 1, targa e tipo dalla pagina 2:
        # si tiene il valore piu' ricco fra i due.
        veicolo.targa = veicolo_bda.targa or veicolo.targa or targa
        if veicolo_bda.tipo_veicolo and not veicolo.tipo_veicolo:
            veicolo.tipo_veicolo = veicolo_bda.tipo_veicolo

        if posizione.cu_assegnazione is None and not posizione.compagnia_provenienza:
            avvisi = " | ".join(grezzo.get("avvisi", [])) or "attestato non presente in ANIA"
            _log(f"nessun attestato per {targa}: {avvisi}")
            await self._torna_alla_maschera()
            raise TargaNonTrovata(f"Targa {targa}: {avvisi}")

        _log(f"attestato: {posizione.compagnia_provenienza or '?'} | "
             f"CU {posizione.cu_assegnazione} | scad {posizione.scadenza_attestato}")

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
