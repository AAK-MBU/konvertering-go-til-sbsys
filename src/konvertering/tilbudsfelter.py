"""Tilbudsfelter — de felter et tilbud har ud over periode og hjemmel.

GO's foranstaltningsdata har ~40 kolonner. Nogle bliver indsatsens egne
felter (Fra/Til -> periode); resten bliver TILBUDSFELTER, som hvert tilbud
har sit eget sæt af i SBSYS: Psykolog har Visiteret, Sagsbehandler og
Indsats; ABA har ABA gruppe, Ydelse, Timer ...

Hvad testmiljøet viste 18-11-2026 (indsats 67 på testsag 183):

* Værdierne sendes I SELVE OPRETTELSEN, som ``feltVaerdier``. Et
  obligatorisk felt kan ikke udfyldes bagefter — oprettelsen afvises.
* ``tilbudFeltId`` er tilbudsfeltets EGET id (ikke typens).
* Datoer sendes som tekst i ``vaerdi``: ``"2024-05-20T00:00:00"``.
* Listefelter sendes som valgmulighedens id i
  ``tilbudFeltVaerdiListeMulighedId``. Fri tekst i et listefelt giver
  HTTP 500 — der er ingen vej uden om listen.

Id'erne er forskellige i test og produktion, og produktion er ryddet op
mens test er fuld af prøvefelter. Derfor findes felterne PÅ NAVN, pr.
tilbud, i SBSYS selv — aldrig som faste id'er i koden. Kun AKTIVE felter
og typer bruges: da Timer blev ændret fra heltal til decimaltal i
produktion, blev den gamle type deaktiveret, men lå der stadig.

To kodelister styrer det, så forretningen kan rette uden en kodeændring:

``tilbudsfelt``        ``<mappetype>|<GO-kolonne>`` -> SBSYS-feltets navn
                        (``handling = ignorer``: kolonnen konverteres ikke)
``tilbudsfeltvaerdi``  ``<GO-kolonne>|<GO-værdi>``  -> valgmulighedens tekst
                        i SBSYS. Kun hvor de er forskellige; ellers bruges
                        GO-værdien som den står (konverteringsarket).

Under konverteringen er alle tilbudsfelter ikke-obligatoriske (besluttet
18-11-2026). En værdi der ikke kan placeres, udelades derfor med en
advarsel — den afviser aldrig indsatsen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .hashing import maskér

log = logging.getLogger(__name__)

FELT_LISTE = "tilbudsfelt"
VAERDI_LISTE = "tilbudsfeltvaerdi"

# Kolonner der indeholder en person. Deres værdi skrives aldrig i logs
# eller i afvisningsrapporten.
PERSONKOLONNER = frozenset({"Sagsbehandler", "IndstilletAf"})

_AZ_I_PARENTES = re.compile(r"\((az[a-z0-9]{5})\)", re.IGNORECASE)
_AZ_ALENE = re.compile(r"az[a-z0-9]{5}", re.IGNORECASE)


@dataclass(frozen=True)
class Felt:
    """Et aktivt tilbudsfelt på ét tilbud, med sin types egenskaber."""

    id: int
    navn: str
    vaerdi_type: str          # Datetime | FreeText | Decimal | Int | Predefined
    liste_id: int | None


@dataclass
class Advarsel:
    kolonne: str
    besked: str
    vaerdi: str | None        # None for personkolonner


@dataclass
class Udfald:
    felt_vaerdier: list[dict[str, Any]] = field(default_factory=list)
    advarsler: list[Advarsel] = field(default_factory=list)


class TilbudsfeltKatalog:
    """Felter, typer og valgmuligheder hentet fra Børn og Unge-API'et.

    Hentes én gang pr. kørsel og holdes i hukommelsen: tusinder af indsatser
    deler et dusin tilbud og en håndfuld lister.
    """

    def __init__(self, klient: Any, endpoints: Any) -> None:
        self._klient = klient
        self._e = endpoints
        self._typer: dict[int, dict[str, Any]] | None = None
        self._felter: dict[int, list[Felt]] = {}
        self._muligheder: dict[int, list[dict[str, Any]]] = {}
        self._eksterne: dict[int, str] | None = None

    def _hent(self, navn: str, **sti: Any) -> Any:
        op = self._e.operation(navn, **sti)
        svar = self._klient.kald(op.metode, op.sti, operation=navn,
                                 forventede_status=op.forventede_status)
        return svar.krop if svar.krop is not None else []

    def _alle_typer(self) -> dict[int, dict[str, Any]]:
        if self._typer is None:
            self._typer = {int(t["id"]): t for t in self._hent("hent_tilbudfelttyper")
                           if isinstance(t, dict) and "id" in t}
        return self._typer

    def felter(self, tilbud_id: int) -> list[Felt]:
        """Tilbuddets AKTIVE felter, hvis type også er aktiv."""
        if tilbud_id not in self._felter:
            typer = self._alle_typer()
            felter = []
            for f in self._hent("hent_tilbudfelter", tilbud_id=tilbud_id):
                if not isinstance(f, dict) or not f.get("aktiv", True):
                    continue
                t = typer.get(int(f.get("tilbudFeltTypeId", -1)))
                if not t or not t.get("aktiv", True):
                    continue
                felter.append(Felt(int(f["id"]), str(t.get("navn") or ""),
                                   str(t.get("vaerdiType") or ""),
                                   t.get("tilbudFeltVaerdiListeId")))
            self._felter[tilbud_id] = felter
        return self._felter[tilbud_id]

    def muligheder(self, liste_id: int) -> list[dict[str, Any]]:
        if liste_id not in self._muligheder:
            self._muligheder[liste_id] = [
                m for m in self._hent("hent_vaerdilistemuligheder", liste_id=liste_id)
                if isinstance(m, dict)]
        return self._muligheder[liste_id]

    def eksterne_noegler(self) -> dict[int, str]:
        """Valgmulighed -> ekstern nøgle (fx AZ-ident i DWH-lister). Tom i både
        test og produktion 18-11-2026; bruges så snart listerne har dem."""
        if self._eksterne is None:
            try:
                raekker = self._hent("hent_eksterne_muligheder")
            except Exception as e:  # et manglende opslag må ikke stoppe indsatsen
                log.warning("Eksterne nøgler kunne ikke hentes: %s", maskér(str(e)))
                raekker = []
            self._eksterne = {
                int(r["tilbudFeltVaerdiListeMulighedId"]): str(r.get("externalKey") or "").casefold()
                for r in raekker
                if isinstance(r, dict) and r.get("tilbudFeltVaerdiListeMulighedId") is not None}
        return self._eksterne


# --- værdiomsætning -------------------------------------------------------

def dato(v: str) -> str | None:
    """GO's dd-mm-aaaa (eller aaaa-mm-dd) -> SBSYS' ``aaaa-mm-ddT00:00:00``."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v.strip(), fmt).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            continue
    return None


def timer(v: str) -> Decimal | None:
    """GO's timer -> et tal.

    GO skriver timer:minutter (``7:30`` = 7,5). Minutter på 60 eller derover
    kan ikke være minutter — ``1:75`` er skrevet som decimaltal med kolon og
    læses som 1,75. Rene tal tages med punktum eller komma.

    AFVENTER PPR (17-11-2026): om ``4:45`` er 4,75 eller 4,45. Antagelsen her
    er timer:minutter.
    """
    s = v.strip().replace(",", ".")
    if ":" in s:
        t, _, m = s.partition(":")
        if not (t.isdigit() and m.isdigit()):
            return None
        if int(m) >= 60:
            return Decimal(f"{int(t)}.{m}")
        return (Decimal(int(t)) + Decimal(int(m)) / 60).quantize(Decimal("0.01")).normalize()
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _tal_tekst(d: Decimal) -> str:
    tekst = format(d.normalize(), "f")
    return tekst.rstrip("0").rstrip(".") if "." in tekst else tekst


def _az_ident(v: str) -> str | None:
    m = _AZ_I_PARENTES.search(v)
    if m:
        return m.group(1).casefold()
    return v.strip().casefold() if _AZ_ALENE.fullmatch(v.strip()) else None


def eksterne_kandidater(v: str) -> set[str]:
    """De eksterne nøgler en GO-værdi kan svare til i en DWH-liste.

    Kun AZ-identen for brugere: et eksakt match på den nøgle DWH bruger
    (Jakob, 18-11-2026). Tallene i GO's institutionsværdier ("Navn [495]")
    bruges BEVIDST IKKE: det er ikke sikkert at de svarer til listernes
    nøgler, og et forkert gæt havner lydløst i SBSYS som en gyldig værdi.
    Institutioner mappes eksplicit i mapping/tilbudsfeltvaerdi.csv.
    """
    az = _az_ident(v)
    return {az} if az else set()


# --- selve opbygningen ----------------------------------------------------

def byg_feltvaerdier(
    katalog: TilbudsfeltKatalog,
    kodelister: Any,
    mappetype: str,
    tilbud_id: int,
    go_felter: dict[str, Any],
) -> Udfald:
    """Byg ``feltVaerdier`` til én indsats.

    Args:
        mappetype: GO's Indholdstype, fx "Psykolog".
        go_felter: Foranstaltningsdata-rækkens kolonner (fra raa_json).
    """
    udfald = Udfald()
    kolonner = kodelister.poster_med_praefiks(FELT_LISTE, f"{mappetype}|")
    if not kolonner:
        return udfald

    felter_paa_navn: dict[str, list[Felt]] = {}
    for f in katalog.felter(tilbud_id):
        felter_paa_navn.setdefault(f.navn.strip().casefold(), []).append(f)

    for kolonne, opslag in sorted(kolonner.items()):
        raa = go_felter.get(kolonne)
        if raa is None or not str(raa).strip():
            continue                      # tomt i GO: intet at sende
        raa = str(raa).strip()
        vis = None if kolonne in PERSONKOLONNER else raa

        def advar(besked: str, kolonne: str = kolonne, vis: str | None = vis) -> None:
            udfald.advarsler.append(Advarsel(kolonne, besked, vis))

        if opslag.handling == "ignorer":
            continue
        feltnavn = (opslag.sbsys_vaerdi or "").strip()
        if not feltnavn:
            advar("kolonnen er ikke mappet til et tilbudsfelt")
            continue
        kandidater = felter_paa_navn.get(feltnavn.casefold(), [])
        if not kandidater:
            advar(f"tilbuddet har intet aktivt felt '{feltnavn}'")
            continue
        if len(kandidater) > 1:
            advar(f"tilbuddet har {len(kandidater)} aktive felter med navnet '{feltnavn}'")
            continue
        felt = kandidater[0]

        vaerdi = _omsaet(katalog, kodelister, kolonne, raa, felt, advar)
        if vaerdi is not None:
            udfald.felt_vaerdier.append({"tilbudFeltId": felt.id, **vaerdi})
    return udfald


def _omsaet(katalog, kodelister, kolonne, raa, felt: Felt, advar) -> dict[str, Any] | None:
    type_ = felt.vaerdi_type.casefold()
    if type_ == "datetime":
        d = dato(raa)
        if d is None:
            advar(f"'{felt.navn}' kunne ikke læses som en dato")
        return {"vaerdi": d} if d else None

    if type_ in ("decimal", "int"):
        tal = timer(raa)
        if tal is None:
            advar(f"'{felt.navn}' kunne ikke læses som et tal")
            return None
        if type_ == "int" and tal != tal.to_integral_value():
            advar(f"'{felt.navn}' er et heltal i SBSYS, men værdien har decimaler")
            return None
        return {"vaerdi": _tal_tekst(tal)}

    if type_ == "freetext":
        return {"vaerdi": raa}

    if type_ == "predefined":
        if felt.liste_id is None:
            advar(f"'{felt.navn}' er et listefelt uden liste")
            return None
        noegle = f"{kolonne}|{raa}"
        if kodelister.er_udeladt(VAERDI_LISTE, noegle):
            # Bevidst tomt (handling = ignorer), fx Betalingssted "PPR", der
            # ikke findes i den nye opsætning. Det er en beslutning, ikke et
            # fund — ingen advarsel, og ingen søgning efter "PPR" i listen.
            return None
        oversat = kodelister.slaa_op(VAERDI_LISTE, noegle, paakraevet=False)
        tekst = (oversat or raa).strip().casefold()
        muligheder = katalog.muligheder(int(felt.liste_id))
        traef = [m for m in muligheder
                 if str(m.get("vaerdiMulighed") or "").strip().casefold() == tekst]
        if not traef:
            # DWH-brugerlisten: match på ekstern nøgle (AZ-ident).
            kandidater = eksterne_kandidater(raa)
            if kandidater:
                eksterne = katalog.eksterne_noegler()
                traef = [m for m in muligheder
                         if eksterne.get(int(m.get("id", -1))) in kandidater]
        if not traef:
            advar(f"'{felt.navn}' har ingen valgmulighed der svarer til GO-værdien")
            return None
        aktive = [m for m in traef if m.get("aktiv", True)] or traef
        return {"tilbudFeltVaerdiListeMulighedId": int(aktive[0]["id"])}

    advar(f"'{felt.navn}' har en ukendt værditype '{felt.vaerdi_type}'")
    return None
