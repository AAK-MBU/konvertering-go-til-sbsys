"""Sagsbehandlere: GO's fritekst -> AZ-ident (trin 1, gennemsynet).

GO's Sagsbehandler-felt er fritekst: navne, AZ-identer, logins, stavefejl.
I SBSYS bruges sagsbehandlere på to niveauer — som BRUGER på sagen
(systemregisteret) og som værdi i LIS' sagsbehandler-værdiliste på
indsatsen. Begge nøgles på AZ-identen, så de renses til ÉN liste
(besluttet 24-11-2026).

Kilder (lokalt på Windows, aldrig i repoet):

* ``distinkte_vaerdier.xlsx``, fanen ``Sagsbehandler`` (kolonne ``Sagsbehandler``)
* ``brugerliste.xlsx`` fra LIS: hele brugerhistorikken, en person kan stå
  flere gange. Kolonner ``IDENT, NAVN, STARTDATO, SLUTDATO, AKTIV``.

Rækkefølgen, i faldende sikkerhed:

1. AZ-identen står i GO-værdien                -> auto (hvis den findes i listen).
   Står der OGSÅ et navn ("Navn (azxxxxx)"), tjekkes at det passer til
   AZ-personen: passer det til en ANDEN person -> afklar (tastefejl i AZ?);
   passer det til ingen -> forslag.
2. Navnet matcher præcis én person              -> auto
3. For- og efternavn matcher præcis én person   -> forslag (mellemnavne ignoreret)
4. Næsten samme navn (stavefejl), én kandidat   -> forslag
5. Flere personer, ingen, eller initialer       -> afklar (ellers konvertering_)

PERSONDATA: dette modul skriver aldrig et navn i terminalen, i logs eller
i en fejlbesked. Kun antal. Navnene står i Excel-filen, som bliver i
OneDrive-mappen.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

LISTE = "sagsbehandler"
FANE = "Sagsbehandler"
BRUGER_KOLONNER = ("IDENT", "NAVN", "STARTDATO", "SLUTDATO", "AKTIV")

_AZ = re.compile(r"(?<![a-z0-9])(az[a-z0-9]{5})(?![a-z0-9])", re.IGNORECASE)
_JA = {"1", "ja", "j", "true", "sand", "x", "aktiv", "yes", "y"}
STAVEFEJL_GRAENSE = 0.88
MAKS_KANDIDATER = 5

AUTO, FORSLAG, AFKLAR = "auto", "forslag", "afklar"

# PPR besluttede (25-11-2026): en sagsbehandler på en indsats der ikke kan
# kobles til en AAK-bruger, får én fælles værdi i stedet for en bruger.
# Navnet er foreløbigt — det endelige navn/nøgle fastlægges senere, og så er
# det kun her det skal rettes.
UKENDT_BRUGER = "Konvertering_Ukendt_Bruger"

GENNEMSYN_KOLONNER = [
    "liste", "go_vaerdi", "foreslaaet_az", "foreslaaet_navn", "aktiv", "slutdato",
    "status", "overfoert", "begrundelse", "kandidater", "godkendt", "rettet_vaerdi",
    "kommentar"]
_BREDDER = [14, 35, 14, 35, 7, 12, 10, 22, 45, 70, 10, 16, 40]


@dataclass
class Person:
    az: str
    navne: set[str] = field(default_factory=set)
    seneste_navn: str = ""
    seneste_start: date | None = None
    start: date | None = None
    slut: date | None = None        # None = stadig åben
    aaben: bool = False
    aktiv: bool = False

    @property
    def slutdato(self) -> date | None:
        return None if self.aaben else self.slut

    def kort(self) -> str:
        tid = "aktiv" if self.aktiv else (f"slut {self.slutdato:%d-%m-%Y}"
                                          if self.slutdato else "inaktiv")
        return f"{self.seneste_navn} ({self.az}, {tid})"


@dataclass
class Raekke:
    go_vaerdi: str
    foreslaaet_az: str
    status: str
    begrundelse: str
    kandidater: list[str] = field(default_factory=list)
    godkendt: str = ""
    rettet_vaerdi: str = ""
    kommentar: str = ""
    overfoert: str = ""


def norm(navn: str) -> str:
    """Til SAMMENLIGNING: små bogstaver, uden punktummer, ét mellemrum.

    Tekst i parentes fjernes: GO skriver fx "Navn (Inaktiv)", og den
    markering siger intet om hvem personen er. (AZ-identer i parentes er
    allerede fundet inden.)

    Æ/Ø/Å og deres to-bogstavsformer er det samme navn ("Håstrup" =
    "Haastrup"); tyske ä/ö/ü skrives ae/oe/ue, og accenter
    (é, è ...) fjernes."""
    navn = re.sub(r"\([^)]*\)?", " ", navn)
    navn = re.sub(r"[.\u00a0]", " ", navn)
    navn = re.sub(r"\s*-\s*", "-", navn)
    navn = re.sub(r"\s+", " ", navn).strip().casefold()
    navn = navn.translate(_TO_BOGSTAVER)
    return "".join(c for c in unicodedata.normalize("NFKD", navn)
                   if not unicodedata.combining(c))


_TO_BOGSTAVER = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "ä": "ae", "ö": "oe",
                               "ü": "ue"})


def varianter(navn: str) -> set[str]:
    """"Efternavn, Fornavn" giver også "Fornavn Efternavn"."""
    ud = {norm(navn)}
    if navn.count(",") == 1:
        efter, for_ = navn.split(",")
        ud.add(norm(f"{for_} {efter}"))
    return {v for v in ud if v}


def _for_efter(n: str) -> tuple[str, str] | None:
    dele = n.split(" ")
    return (dele[0], dele[-1]) if len(dele) >= 2 else None


def find_az(tekst: str) -> str | None:
    m = _AZ.search(tekst)
    return m.group(1).casefold() if m else None


def _dato(v: object) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    tekst = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(tekst, fmt).date()
        except ValueError:
            continue
    return None


def _tekst(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _hoved_ix(hoved: tuple, kraevet: tuple[str, ...], fil: str) -> dict[str, int]:
    ix = {_tekst(h).upper(): i for i, h in enumerate(hoved) if h is not None}
    mangler = [k for k in kraevet if k not in ix]
    if mangler:
        raise ValueError(f"{fil} mangler kolonnerne {mangler} (fandt {sorted(ix)})")
    return {k: ix[k] for k in kraevet}


def laes_brugerliste(sti: Path) -> dict[str, Person]:
    """LIS' brugerhistorik -> én Person pr. AZ-ident."""
    import openpyxl

    bog = openpyxl.load_workbook(sti, read_only=True, data_only=True)
    try:
        ark = bog.worksheets[0]
        raekker = ark.iter_rows(values_only=True)
        ix = _hoved_ix(next(raekker, ()), BRUGER_KOLONNER, sti.name)
        personer: dict[str, Person] = {}
        for r in raekker:
            def g(k: str, r: tuple = r) -> object:
                return r[ix[k]] if ix[k] < len(r) else None
            az = find_az(_tekst(g("IDENT")))
            if not az:
                continue
            p = personer.setdefault(az, Person(az))
            navn, start, slut = _tekst(g("NAVN")), _dato(g("STARTDATO")), _dato(g("SLUTDATO"))
            if navn:
                p.navne.add(navn)
                if not p.seneste_navn or (start and (not p.seneste_start or start >= p.seneste_start)):
                    p.seneste_navn, p.seneste_start = navn, start or p.seneste_start
            if start and (not p.start or start < p.start):
                p.start = start
            if slut is None:
                p.aaben = True
            elif not p.slut or slut > p.slut:
                p.slut = slut
            p.aktiv = p.aktiv or _tekst(g("AKTIV")).casefold() in _JA
        return personer
    finally:
        bog.close()


def laes_go_sagsbehandlere(sti: Path) -> list[str]:
    """KUN fanen Sagsbehandler i distinkte_vaerdier.xlsx."""
    import openpyxl

    bog = openpyxl.load_workbook(sti, read_only=True, data_only=True)
    try:
        if FANE not in bog.sheetnames:
            raise ValueError(f"{sti.name} har ingen fane '{FANE}'")
        raekker = bog[FANE].iter_rows(values_only=True)
        ix = _hoved_ix(next(raekker, ()), (FANE.upper(),), f"{sti.name}/{FANE}")[FANE.upper()]
        set_: set[str] = set()
        ud = []
        for r in raekker:
            v = _tekst(r[ix]) if ix < len(r) else ""
            if v and v not in set_:
                set_.add(v)
                ud.append(v)
        return ud
    finally:
        bog.close()


class Register:
    """Opslag i personerne: på fuldt navn, på for+efternavn, og på enkeltnavne."""

    def __init__(self, personer: dict[str, Person]):
        self.personer = personer
        self.paa_navn: dict[str, set[str]] = defaultdict(set)
        self.paa_for_efter: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.paa_ord: dict[str, set[str]] = defaultdict(set)
        for az, p in personer.items():
            for navn in p.navne:
                for n in varianter(navn):
                    self.paa_navn[n].add(az)
                    if fe := _for_efter(n):
                        self.paa_for_efter[fe].add(az)
                    for ord_ in set(n.split(" ")):
                        if len(ord_) > 2:
                            self.paa_ord[ord_].add(az)

    def _kandidater(self, azs: set[str]) -> list[str]:
        # Aktive først, så senest fratrådte.
        ps = sorted((self.personer[a] for a in azs),
                    key=lambda p: (not p.aktiv, -(p.slutdato or date.max).toordinal()))
        return [p.kort() for p in ps[:MAKS_KANDIDATER]] + (
            [f"... og {len(ps) - MAKS_KANDIDATER} mere"] if len(ps) > MAKS_KANDIDATER else [])

    def _passer(self, navn: str, az: str) -> bool:
        for n in varianter(navn):
            for v in (v for egen in self.personer[az].navne for v in varianter(egen)):
                if n == v or _for_efter(n) == _for_efter(v) \
                        or SequenceMatcher(None, n, v).ratio() >= STAVEFEJL_GRAENSE:
                    return True
        return False

    def _navneafvigelse(self, navn: str, az: str) -> str:
        """Hvor meget af GO-navnet går igen hos AZ-personen? Til begrundelsen,
        så fx navneskift kan sorteres ud og godkendes samlet."""
        bedst = (-1, False, False, 0)
        for n in varianter(navn):
            go_ord = n.split(" ")
            for v in (v for egen in self.personer[az].navne for v in varianter(egen)):
                ord_ = v.split(" ")
                kand = (len(set(go_ord) & set(ord_)), go_ord[0] == ord_[0],
                        len(go_ord) > 1 and go_ord[-1] == ord_[-1], len(go_ord))
                bedst = max(bedst, kand)
        faelles, fornavn, efternavn, antal = bedst
        if faelles <= 0:
            return "intet navn til fælles"
        if fornavn and not efternavn and faelles >= 2:
            return f"fornavn passer, efternavn afviger, {faelles} af {antal} navne fælles " \
                   "(navneskift?)"
        if fornavn and not efternavn:
            return "kun fornavnet passer"
        if efternavn and not fornavn:
            return f"efternavn passer, fornavn afviger, {faelles} af {antal} navne fælles"
        return f"{faelles} af {antal} navne fælles"

    def _faelles_ord(self, navne: set[str], az: str) -> int:
        """Største antal navne GO-værdien deler med én af personens navne."""
        return max((len(set(n.split(" ")) & set(v.split(" ")))
                    for n in navne for egen in self.personer[az].navne
                    for v in varianter(egen)), default=0)

    def klassificer(self, go: str) -> Raekke:
        if az := find_az(go):
            if az not in self.personer:
                return Raekke(go, "", AFKLAR, "AZ-identen i GO-værdien findes ikke i brugerlisten")
            navn = _AZ.sub(" ", go.split("\\")[-1])
            if not re.search(r"[^\W\d_]{2}", norm(navn)):
                return Raekke(go, az, AUTO, "AZ-identen står i GO-værdien")
            if self._passer(navn, az):
                return Raekke(go, az, AUTO, "AZ-identen står i GO-værdien, og navnet passer")
            andre = set().union(*(self.paa_navn.get(n, set()) for n in varianter(navn))) - {az}
            if andre:
                return Raekke(go, az, AFKLAR, "Navnet passer til en ANDEN person end "
                              "AZ-identen (tastefejl i AZ?)", self._kandidater({az} | andre))
            return Raekke(go, az, FORSLAG, "AZ-identen står i GO-værdien, men navnet passer "
                          f"ikke til den person: {self._navneafvigelse(navn, az)}",
                          self._kandidater({az}))

        navne = varianter(go)
        if not navne or (" " not in next(iter(navne)) and len(next(iter(navne))) <= 4):
            return Raekke(go, "", AFKLAR, "Kun ét kort ord (initialer?)")

        eksakt = set().union(*(self.paa_navn.get(n, set()) for n in navne))
        if len(eksakt) == 1:
            return Raekke(go, next(iter(eksakt)), AUTO, "Navnet matcher præcis én person")
        if len(eksakt) > 1:
            return Raekke(go, "", AFKLAR, f"{len(eksakt)} personer med præcis dette navn",
                          self._kandidater(eksakt))

        fe = set().union(*(self.paa_for_efter.get(f, set())
                           for n in navne if (f := _for_efter(n))))
        if len(fe) == 1:
            return Raekke(go, next(iter(fe)), FORSLAG,
                          "For- og efternavn matcher én person (mellemnavn afviger)",
                          self._kandidater(fe))
        if len(fe) > 1:
            # Flest fælles navne vinder — men kun hvis ÉN har flere end alle andre
            # ("A B C D": den med "A X C D" (3) slår dem med kun "A D" (2)).
            faelles = {az: self._faelles_ord(navne, az) for az in fe}
            bedst = max(faelles.values())
            vindere = [az for az, n in faelles.items() if n == bedst]
            if len(vindere) == 1 and bedst > 2:
                az = vindere[0]
                return Raekke(go, az, FORSLAG,
                              f"{len(fe)} personer med samme for- og efternavn; kun én deler "
                              f"{bedst} navne", self._kandidater({az}) + [
                                  k for k in self._kandidater(fe - {az})])
            return Raekke(go, "", AFKLAR, f"{len(fe)} personer med samme for- og efternavn",
                          self._kandidater(fe))

        # Stavefejl: kun blandt personer der deler mindst ét ord med GO-værdien.
        bedste: dict[str, float] = {}
        for n in navne:
            kandidater = set().union(*(self.paa_ord.get(o, set()) for o in n.split(" ")))
            for az in kandidater:
                for navn in self.personer[az].navne:
                    for v in varianter(navn):
                        r = SequenceMatcher(None, n, v).ratio()
                        if r >= STAVEFEJL_GRAENSE and r > bedste.get(az, 0):
                            bedste[az] = r
        if len(bedste) == 1:
            az = next(iter(bedste))
            return Raekke(go, az, FORSLAG, f"Næsten samme navn ({bedste[az]:.0%}) - stavefejl?",
                          self._kandidater({az}))
        if len(bedste) > 1:
            return Raekke(go, "", AFKLAR, f"{len(bedste)} personer med næsten samme navn",
                          self._kandidater(set(bedste)))
        return Raekke(go, "", AFKLAR, "Ingen match i brugerlisten (får konvertering_ hvis "
                                      "den ikke afklares)")


def saml(go_vaerdier: list[str], personer: dict[str, Person]) -> list[Raekke]:
    reg = Register(personer)
    raekker = [reg.klassificer(v) for v in go_vaerdier]
    for r in raekker:
        # Afklar foreslår ingen bruger: uden en rettelse bliver den UKENDT_BRUGER.
        # Kandidaterne (også en AZ fra GO-værdien) står stadig i "kandidater".
        if r.status == AFKLAR:
            r.foreslaaet_az = ""
    return sorted(raekker,
                  key=lambda r: ({AFKLAR: 0, FORSLAG: 1, AUTO: 2}[r.status], r.go_vaerdi.casefold()))


def overfoer(raekker: list[Raekke], tidligere: Path) -> list[str]:
    """Kopiér afgørelser fra et tidligere sagsbehandler-gennemsyn (som vaerdier.overfoer).

    Et gammelt "godkendt" gælder kun hvis forslaget er det samme AZ;
    ellers flyttes den tidligere AZ til rettet_vaerdi."""
    from .vaerdier import NY, OVERFOERT, OVERFOERT_AENDRET, TIDLIGERE_UAFKLARET
    from .vaerdimapning import _NEJ

    gamle = {r["go_vaerdi"]: r for r in laes_gennemsyn(tidligere)}
    for r in raekker:
        g = gamle.get(r.go_vaerdi)
        if g is None:
            r.overfoert = NY
            continue
        r.kommentar = g["kommentar"]
        endelig = endelig_az(g)
        if not endelig:
            r.overfoert = TIDLIGERE_UAFKLARET
            if g["foreslaaet_az"] == r.foreslaaet_az:
                r.godkendt = g["godkendt"]
        elif g["rettet_vaerdi"]:
            r.rettet_vaerdi, r.overfoert = g["rettet_vaerdi"], OVERFOERT
        elif endelig == r.foreslaaet_az:
            r.overfoert = OVERFOERT
            nej = g["godkendt"].casefold() in _NEJ
            if r.status != AUTO or g["godkendt"]:
                r.godkendt = g["godkendt"] if g["godkendt"] and not nej else "x"
        else:
            r.rettet_vaerdi, r.overfoert = endelig, OVERFOERT_AENDRET
    set_nu = {r.go_vaerdi for r in raekker}
    return [k for k in gamle if k not in set_nu]


def laes_gennemsyn(sti: Path) -> list[dict[str, str]]:
    import openpyxl

    bog = openpyxl.load_workbook(sti, read_only=True, data_only=True)
    try:
        raekker = bog.worksheets[0].iter_rows(values_only=True)
        hoved = [_tekst(h) for h in next(raekker, ())]
        mangler = {"go_vaerdi", "foreslaaet_az", "status"} - set(hoved)
        if mangler:
            raise ValueError(f"{sti.name} mangler kolonnerne {sorted(mangler)}")
        ud = []
        for r in raekker:
            d = {h: _tekst(r[i]) if i < len(r) else "" for i, h in enumerate(hoved) if h}
            for k in ("godkendt", "rettet_vaerdi", "kommentar"):
                d.setdefault(k, "")
            if d["go_vaerdi"]:
                ud.append(d)
        return ud
    finally:
        bog.close()


def endelig_az(r: dict[str, str]) -> str | None:
    """rettet_vaerdi > godkendt forslag > auto-forslag. Samme regel som værdierne."""
    from .vaerdimapning import _NEJ

    godkendt = r.get("godkendt", "")
    nej = godkendt.casefold() in _NEJ and bool(godkendt)
    if r.get("rettet_vaerdi"):
        return find_az(r["rettet_vaerdi"]) or r["rettet_vaerdi"].strip().casefold()
    if r.get("foreslaaet_az") and godkendt and not nej:
        return r["foreslaaet_az"]
    if r.get("foreslaaet_az") and r.get("status") == AUTO and not nej:
        return r["foreslaaet_az"]
    return None


VAERDILISTE_NOEGLE = "v_bruger_ppr"   # LIS' sagsbehandler-værdiliste (27-11-2026)
GO_FELTNAVN = "Sagsbehandler"


def noegle_raekker(gennemsyn: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Rækker til map.vaerdilistemulighed fra det GENNEMGÅEDE gennemsyn.

    Én række pr. GO-stavemåde der er afklaret til en AZ-ident (auto,
    godkendt forslag eller rettet_vaerdi). Flere stavemåder af samme person
    får samme nøgle (AZ-identen). Tabellen er både LIS' liste over hvilke
    værdier der skal findes i v_bruger_ppr, og konverteringens opslag
    stavemåde -> AZ. Uafklarede kommer IKKE med: de ender på
    MBU-systemkontoen (besluttet 27-11-2026).

    Returnerer (rækker, antal udeladte)."""
    ud: list[dict[str, str]] = []
    udeladt = 0
    set_go: set[str] = set()
    for r in gennemsyn:
        az = find_az(endelig_az(r) or "")
        go = r["go_vaerdi"].strip()
        if not az or go in set_go:
            udeladt += not az
            continue
        set_go.add(go)
        foreslaaet = (r.get("foreslaaet_az") or "").strip().casefold()
        navn = (r.get("foreslaaet_navn") or "").strip() if foreslaaet == az else ""
        ud.append({
            "liste": LISTE,
            "go_feltnavn": GO_FELTNAVN,
            "go_feltvaerdi": go,
            "vaerdiliste_external_key": VAERDILISTE_NOEGLE,
            "vaerdimulighed_external_key": az,
            "vaerdimulighed_navn": navn or az,
            "noegletype": "az",
            "status": "klar",
            "kommentar": "" if navn else "Rettet til en anden AZ-ident - navnet står ikke i gennemsynet",
        })
    return ud, udeladt


def endelig_vaerdi(r: dict[str, str]) -> str:
    """Det GO-værdien ender som: en AZ-ident, ellers UKENDT_BRUGER."""
    return endelig_az(r) or UKENDT_BRUGER


def skriv(raekker: list[Raekke], personer: dict[str, Person], sti: Path) -> Path:
    import openpyxl
    from openpyxl.styles import Font

    bog = openpyxl.Workbook()
    ark = bog.active
    ark.title = LISTE
    ark.append(GENNEMSYN_KOLONNER)
    for r in raekker:
        p = personer.get(r.foreslaaet_az)
        navn = p.seneste_navn if p else (UKENDT_BRUGER if r.status == AFKLAR else "")
        ark.append([LISTE, r.go_vaerdi, r.foreslaaet_az, navn,
                    ("ja" if p.aktiv else "nej") if p else "",
                    p.slutdato if p else None, r.status, r.overfoert, r.begrundelse,
                    " | ".join(r.kandidater), r.godkendt, r.rettet_vaerdi, r.kommentar])
    for c in ark[1]:
        c.font = Font(bold=True)
    for i, b in enumerate(_BREDDER, start=1):
        ark.column_dimensions[openpyxl.utils.get_column_letter(i)].width = b
    ark.freeze_panes = "A2"
    ark.auto_filter.ref = ark.dimensions
    bog.save(sti)
    return sti


def opsummering(raekker: list[Raekke]) -> tuple[Counter, Counter]:
    """Kun antal — aldrig navne."""
    grund = Counter(re.sub(r"\d+%?", "N", r.begrundelse.split(" (")[0]) for r in raekker)
    return Counter(r.status for r in raekker), grund
