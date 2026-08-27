"""
models.py — Modello dati canonico del comparatore Protexa.

Questo file e' il pezzo piu' importante di tutto il progetto.

Ogni compagnia (Unipol, Genertel, Prima, HDI, Cattolica, Allianz...) parla
un linguaggio diverso. La regola e': tutto quello che entra nel sistema viene
tradotto in queste classi, e tutto quello che esce parte da queste classi.
Cosi' aggiungere una compagnia significa scrivere un adapter nuovo, senza
toccare nulla di quello che gia' funziona.

Nomi dei campi in italiano perche' il dominio e' italiano: quando fra sei mesi
qualcuno legge "cu_assegnazione" sa esattamente di cosa si parla.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    """
    Base comune a tutti i modelli.

    validate_assignment=True fa girare i validatori anche quando un campo viene
    assegnato dopo la creazione dell'oggetto. Serve davvero: il bot compila i
    campi uno alla volta man mano che li legge dalla pagina, quindi la maggior
    parte delle assegnazioni avviene dopo la costruzione.
    """
    model_config = ConfigDict(validate_assignment=True)


# ---------------------------------------------------------------------------
# Enumerazioni  (i valori sono quelli che si vedono a video in K-UBE)
# ---------------------------------------------------------------------------

class TipoPreventivo(str, Enum):
    RINNOVO = "Rinnovo"
    VOLTURA = "Voltura"
    BERSANI = "Bersani"
    BERSANI_FAMILIARE = "Bersani Familiare"


class TipoAnagrafica(str, Enum):
    PERSONA_FISICA = "Persona Fisica"
    PERSONA_GIURIDICA = "Persona Giuridica"


class Sesso(str, Enum):
    M = "M"
    F = "F"


class Alimentazione(str, Enum):
    BENZINA = "Benzina"
    DIESEL = "Diesel"
    GPL = "GPL"
    METANO = "Metano"
    IBRIDA = "Ibrida"
    ELETTRICA = "Elettrica"


class Cambio(str, Enum):
    MANUALE = "Manuale"
    AUTOMATICO = "Automatico"


class FormaTariffaria(str, Enum):
    BONUS_MALUS = "BONUS MALUS"
    FRANCHIGIA = "FRANCHIGIA"
    PEJUS = "PEJUS"


class TipoResponsabilita(str, Enum):
    """Le due macro-righe della tabella sinistri ANIA."""
    PRINCIPALE = "principale"
    PARITARIA = "paritaria"


class CategoriaDanno(str, Enum):
    """Le tre sotto-righe sotto ogni macro-riga."""
    COSE = "cose"
    PERSONE = "persone"
    MISTI = "misti"


class StatoPreventivo(str, Enum):
    BOZZA = "Bozza"
    DATI_RECUPERATI = "Dati Recuperati"
    IN_QUOTAZIONE = "In Quotazione"
    QUOTATO = "Quotato"
    IN_EMISSIONE = "In Emissione"
    EMESSO = "Emesso"
    ERRORE = "Errore"


# ---------------------------------------------------------------------------
# Blocchi anagrafici
# ---------------------------------------------------------------------------

class Indirizzo(_Base):
    via: str = ""
    civico: str = ""
    cap: str = ""
    citta: str = ""
    provincia: str = ""          # sigla a 2 lettere
    istat_comune: str = ""       # utile per le compagnie che tariffano per comune

    @field_validator("provincia")
    @classmethod
    def _provincia_maiuscola(cls, v: str) -> str:
        return (v or "").strip().upper()[:2]

    def completo(self) -> str:
        pezzi = [f"{self.via} {self.civico}".strip(), self.cap, self.citta, self.provincia]
        return ", ".join(p for p in pezzi if p)


class Persona(_Base):
    """Contraente, proprietario, guidatore: stessa struttura, ruoli diversi."""

    tipo: TipoAnagrafica = TipoAnagrafica.PERSONA_FISICA

    nome: str = ""
    cognome: str = ""
    ragione_sociale: str = ""    # valorizzata solo se persona giuridica

    codice_fiscale: str = ""
    partita_iva: str = ""

    sesso: Optional[Sesso] = None
    data_nascita: Optional[date] = None
    luogo_nascita: str = ""
    provincia_nascita: str = ""

    residenza: Indirizzo = Field(default_factory=Indirizzo)
    domicilio: Optional[Indirizzo] = None   # None = coincide con la residenza

    email: str = ""
    cellulare: str = ""

    # Solo per il guidatore
    data_patente: Optional[date] = None

    @field_validator("codice_fiscale", "partita_iva")
    @classmethod
    def _pulisci_codici(cls, v: str) -> str:
        return (v or "").replace(" ", "").strip().upper()

    @property
    def nominativo(self) -> str:
        if self.tipo is TipoAnagrafica.PERSONA_GIURIDICA:
            return self.ragione_sociale
        return f"{self.cognome} {self.nome}".strip()

    @property
    def anni_patente(self) -> Optional[int]:
        """Molte compagnie tariffano sull'anzianita' di patente."""
        if not self.data_patente:
            return None
        oggi = date.today()
        anni = oggi.year - self.data_patente.year
        if (oggi.month, oggi.day) < (self.data_patente.month, self.data_patente.day):
            anni -= 1
        return max(anni, 0)

    @property
    def eta(self) -> Optional[int]:
        if not self.data_nascita:
            return None
        oggi = date.today()
        anni = oggi.year - self.data_nascita.year
        if (oggi.month, oggi.day) < (self.data_nascita.month, self.data_nascita.day):
            anni -= 1
        return anni


# ---------------------------------------------------------------------------
# Veicolo
# ---------------------------------------------------------------------------

class Veicolo(_Base):
    targa: str = ""
    telaio: str = ""
    tipo_veicolo: str = "AUTOVETTURA"

    marca: str = ""
    modello: str = ""
    allestimento: str = ""

    # Codici di allestimento: sono la chiave per mappare lo stesso veicolo su
    # portali diversi. Nel video del preventivatore Unipol si vedono nel popup
    # di scelta modello (es. 096590, 097785...).
    codice_allestimento_unipol: str = ""
    codice_allestimento_ania: str = ""
    codice_quattroruote: str = ""

    data_immatricolazione: Optional[date] = None
    data_acquisto: Optional[date] = None
    anno_modello: Optional[int] = None

    kw: Optional[float] = None
    cv_fiscali: Optional[int] = None
    cilindrata: Optional[int] = None
    alimentazione: Optional[Alimentazione] = None

    posti: Optional[int] = None
    cambio: Optional[Cambio] = None
    gancio_traino: bool = False
    valore_veicolo: Optional[float] = None

    km_annui: Optional[int] = None
    uso: str = "Privato"
    antifurto: str = ""
    proprieta_da: Optional[date] = None

    @field_validator("targa")
    @classmethod
    def _targa_normalizzata(cls, v: str) -> str:
        return (v or "").replace(" ", "").replace("-", "").strip().upper()

    @property
    def targa_con_spazi(self) -> str:
        """Unipol vuole la targa nel formato 'EP 661 CW'."""
        t = self.targa
        return f"{t[:2]} {t[2:5]} {t[5:]}" if len(t) == 7 else t

    @property
    def eta_veicolo(self) -> Optional[int]:
        if not self.data_immatricolazione:
            return None
        return date.today().year - self.data_immatricolazione.year


# ---------------------------------------------------------------------------
# Storico sinistri e posizione assicurativa
# ---------------------------------------------------------------------------

class RigaSinistri(_Base):
    """Una casella della tabella ATRC: anno + tipo responsabilita' + categoria."""
    anno: int
    responsabilita: TipoResponsabilita
    categoria: Optional[CategoriaDanno] = None   # None = riga totale
    numero: int = 0


class StoricoSinistri(_Base):
    """
    La tabella che si vede sia in K-UBE (Storico Sinistri ATRC) sia nella pagina
    BDA di Unipol (Sezionale sinistri). Copre gli ultimi 11 anni.
    """
    righe: list[RigaSinistri] = Field(default_factory=list)

    @property
    def anni(self) -> list[int]:
        return sorted({r.anno for r in self.righe})

    @property
    def totale_sinistri(self) -> int:
        return sum(r.numero for r in self.righe if r.categoria is None)

    def valore(self, anno: int, resp: TipoResponsabilita,
               cat: Optional[CategoriaDanno] = None) -> int:
        for r in self.righe:
            if r.anno == anno and r.responsabilita is resp and r.categoria is cat:
                return r.numero
        return 0

    def pulito(self) -> bool:
        """Nessun sinistro negli ultimi 5 anni: molte compagnie applicano sconti."""
        limite = date.today().year - 5
        return all(r.numero == 0 for r in self.righe if r.anno >= limite)


class PosizioneAssicurativa(_Base):
    """Tutto quello che la pagina BDA / IBDV ANIA restituisce sulla targa."""

    posizione: str = ""                    # es. "PRESENZA DOCUMENTAZIONE STATO ASSICURATIVO"
    dettaglio_posizione: str = ""          # es. "ATTESTATO SCADUTO DA MENO DI 5 ANNI"

    compagnia_provenienza_codice: str = ""
    compagnia_provenienza: str = ""

    numero_polizza: str = ""
    scadenza_attestato: Optional[date] = None
    forma_tariffaria: Optional[FormaTariffaria] = None

    cu_provenienza: Optional[int] = None
    cu_assegnazione: Optional[int] = None
    classe_interna_provenienza: str = ""
    classe_interna_assegnazione: str = ""

    codici_legge: str = "NO BENEFICI DI LEGGE"
    polizza_gratuita: bool = False
    cu_art_134bis: bool = False

    franchigie_non_corrisposte: int = 0
    importo_franchigie: float = 0.0
    codice_iur: str = ""

    cf_contraente_ania: str = ""
    nominativo_contraente_ania: str = ""
    cf_avente_diritto: str = ""
    nominativo_avente_diritto: str = ""

    sinistri: StoricoSinistri = Field(default_factory=StoricoSinistri)

    @field_validator("cu_provenienza", "cu_assegnazione", mode="before")
    @classmethod
    def _cu_valida(cls, v):
        """
        La classe CU e' un intero fra 1 e 18. Questo validatore e' la soluzione
        strutturale al falso positivo "NA" descritto nella relazione tecnica:
        qualunque cosa non sia un numero 1-18 viene scartata a monte.
        """
        if v in (None, "", "--", "NA"):
            return None
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 18 else None


# ---------------------------------------------------------------------------
# Garanzie
# ---------------------------------------------------------------------------

class Garanzie(_Base):
    """Le spunte dello Step 6, raggruppate come in K-UBE."""

    # Opzioni contrattuali
    guida_esperta: bool = False
    rinuncia_rivalsa: bool = False
    riparazione_diretta: bool = False
    bonus_protetto: bool = False
    dispositivo_satellitare: bool = False

    # CVT - danni veicolo
    incendio: bool = False
    furto_parziale_totale: bool = False
    eventi_naturali: bool = False
    eventi_socio_politici: bool = False
    cristalli: bool = False
    kasko: bool = False

    # Protezione e tutela
    infortuni_conducente: bool = False
    assistenza_stradale: bool = False
    tutela_legale: bool = False

    massimale_rca: str = "Oltre 10M"
    franchigia_cvt: Optional[float] = None

    def attive(self) -> list[str]:
        etichette = {
            "guida_esperta": "Guida Esperta",
            "rinuncia_rivalsa": "Rinuncia alla Rivalsa",
            "riparazione_diretta": "Riparazione Diretta",
            "bonus_protetto": "Bonus Protetto",
            "dispositivo_satellitare": "Dispositivo Satellitare",
            "incendio": "Incendio",
            "furto_parziale_totale": "Furto Parziale/Totale",
            "eventi_naturali": "Eventi Naturali",
            "eventi_socio_politici": "Eventi Socio-Politici",
            "cristalli": "Cristalli",
            "kasko": "Kasko",
            "infortuni_conducente": "Infortuni Conducente",
            "assistenza_stradale": "Assistenza Stradale",
            "tutela_legale": "Tutela Legale",
        }
        return [lbl for campo, lbl in etichette.items() if getattr(self, campo)]


# ---------------------------------------------------------------------------
# Quotazione restituita da una compagnia
# ---------------------------------------------------------------------------

class Quotazione(_Base):
    compagnia: str
    prodotto: str
    codice_convenzione: str = ""

    premio_annuo: Optional[float] = None
    premio_semestrale: Optional[float] = None
    premio_lordo_rca: Optional[float] = None
    imposte_e_diritti: Optional[float] = None
    sconto_applicato: Optional[float] = None      # percentuale

    garanzie_incluse: list[str] = Field(default_factory=list)

    numero_preventivo: str = ""
    link_preventivo: str = ""
    pdf_precontrattuale: str = ""

    tempo_calcolo_sec: Optional[float] = None
    calcolato_il: datetime = Field(default_factory=datetime.now)

    errore: str = ""

    @property
    def valida(self) -> bool:
        return not self.errore and self.premio_annuo is not None


# ---------------------------------------------------------------------------
# L'oggetto principale
# ---------------------------------------------------------------------------

class PreventivoRCA(_Base):
    """Un preventivo completo. E' l'oggetto che attraversa tutto il sistema."""

    id: str = ""
    stato: StatoPreventivo = StatoPreventivo.BOZZA

    # Step 1
    tipo_preventivo: TipoPreventivo = TipoPreventivo.RINNOVO
    targa: str = ""
    targa_bersani: str = ""                 # veicolo da cui eredita la classe
    data_effetto: Optional[date] = None

    # Step 2-3
    contraente: Persona = Field(default_factory=Persona)
    proprietario_uguale_contraente: bool = True
    proprietario: Optional[Persona] = None
    guidatore_uguale_contraente: bool = True
    guidatore: Optional[Persona] = None

    # Step 4
    veicolo: Veicolo = Field(default_factory=Veicolo)

    # Step 5
    posizione: PosizioneAssicurativa = Field(default_factory=PosizioneAssicurativa)

    # Step 6
    garanzie: Garanzie = Field(default_factory=Garanzie)
    privacy_accettata: bool = False
    precontrattuale_consegnata: bool = False

    # Step 7
    quotazioni: list[Quotazione] = Field(default_factory=list)
    quotazione_scelta: Optional[str] = None   # "COMPAGNIA::PRODOTTO"

    # Tracciabilita'
    creato_il: datetime = Field(default_factory=datetime.now)
    aggiornato_il: datetime = Field(default_factory=datetime.now)
    collaboratore: str = ""
    airtable_record_id: str = ""
    note_bot: str = ""

    def contraente_effettivo(self) -> Persona:
        return self.contraente

    def proprietario_effettivo(self) -> Persona:
        return self.contraente if self.proprietario_uguale_contraente else (self.proprietario or self.contraente)

    def guidatore_effettivo(self) -> Persona:
        return self.contraente if self.guidatore_uguale_contraente else (self.guidatore or self.contraente)

    def migliore_quotazione(self) -> Optional[Quotazione]:
        valide = [q for q in self.quotazioni if q.valida]
        return min(valide, key=lambda q: q.premio_annuo) if valide else None

    def pronto_per_quotare(self) -> tuple[bool, list[str]]:
        """Validazione prima di lanciare le 12 compagnie: meglio fermarsi qui
        che ricevere 12 errori identici dai portali."""
        mancanti: list[str] = []
        c = self.contraente
        if not self.targa:
            mancanti.append("Targa")
        if not c.codice_fiscale and not c.partita_iva:
            mancanti.append("Codice fiscale del contraente")
        if not c.residenza.cap:
            mancanti.append("CAP di residenza")
        if not self.veicolo.data_immatricolazione:
            mancanti.append("Data di immatricolazione")
        if self.posizione.cu_assegnazione is None:
            mancanti.append("Classe CU di assegnazione")
        if not self.data_effetto:
            mancanti.append("Data di effetto")
        if self.tipo_preventivo in (TipoPreventivo.BERSANI, TipoPreventivo.BERSANI_FAMILIARE) \
                and not self.targa_bersani:
            mancanti.append("Targa del veicolo Bersani")
        if not self.privacy_accettata:
            mancanti.append("Dichiarazione privacy")
        return (len(mancanti) == 0, mancanti)
