"""Trin 1 af værdimappingen: hvilke værdier er der FAKTISK brug for?

GO's frie tekstfelter (Bevillingssted, SkoleEllerInstitution ...) rummer den
samme institution stavet på mange måder: "Mårslet skole", "Mårslet Skole",
"Mårslet Skole [693]". Før der kan bygges en mappingtabel mod SBSYS'
værdilister (trin 2), skal stavemåderne samles til én værdi hver.

Dette modul FORESLÅR samlingen. Et menneske godkender. Intet herfra bruges
direkte af konverteringen — den matcher stadig eksakt på GO's stavemåde.

Tre niveauer, i stigende grad af tvivl:

* ``auto``    Kun sikre forskelle: store/små bogstaver, mellemrum, en
              forkert klamme ("[400}"), "(sdt)" mod "(SDT)", ", d.i.i." mod
              ", d.i.i". Samme værdi uden tvivl.
* ``forslag`` Sandsynligvis samme værdi: samme navn med og uden nummer,
              forkortelser ("Bhv." = Børnehave), tal foran navnet, eller en
              mulig stavefejl. Skal godkendes.
* ``afklar``  Kræver en beslutning, ikke bare et ja: nummerkonflikter,
              "XXX"/"ÆNDRET"-markeringer, kategorier som "Særl. bh.",
              kommuner der ikke er kommuner.

To fund fra GO's data (21-11-2026) styrer reglerne:

1. Et tal FORAN navnet er nogle gange LIS-nummeret ("158 Møllevang
   dagtilbud" = "[158]") og nogle gange ikke ("407 Skæringhus" mod
   "Skæringhus [207]"). Det bruges derfor kun til at FORESLÅ, og
   begrundelsen siger hvilket tilfælde det er.
2. Selv "[N]" er ikke entydigt: "[660]" står både på SFO Elev Skole og
   SFO Ravnbakkeskolen. Samme nummer med forskellige navne samles derfor
   ALDRIG automatisk — det markeres.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

# GO-kolonne (= fanen i distinkte_vaerdier.xlsx) -> den liste den renses i.
# Kolonner der ender i SAMME SBSYS-værdiliste renses sammen, så samme
# stavemåde ikke kan få to forskellige afgørelser.
MAALLISTE: dict[str, str] = {
    "AndenBetalingskommune": "kommune",
    "Betalingskommune": "kommune",
    "Betalingssted": "bevillingssted",
    "Bevillingssted": "bevillingssted",
    # v_institution_ppr er en delmængde af v_skole_el_institution_ppr:
    # navnene er de samme institutioner, så de renses som én liste.
    "Institutionsnavn": "institution",
    "SkoleEllerInstitution": "institution",
    "Klassetrin": "klassetrin",
}

# Faner der BEVIDST ikke behandles her, og hvorfor. Læses slet ikke.
UDELADTE_FANER: dict[str, str] = {
    "Sagsbehandler": "medarbejdernavne. Mappes til SBSYS-brugere (AZ-ident), "
                     "ikke ved at samle stavemåder, og skal ikke i CSV-filer",
}

DANSKE_KOMMUNER = frozenset("""
Albertslund Allerød Assens Ballerup Billund Bornholm Brøndby Brønderslev
Dragør Egedal Esbjerg Fanø Favrskov Faxe Fredensborg Fredericia Frederiksberg
Frederikshavn Frederikssund Furesø Faaborg-Midtfyn Gentofte Gladsaxe Glostrup
Greve Gribskov Guldborgsund Haderslev Halsnæs Hedensted Helsingør Herlev
Herning Hillerød Hjørring Holbæk Holstebro Horsens Hvidovre Høje-Taastrup
Hørsholm Ikast-Brande Ishøj Jammerbugt Kalundborg Kerteminde Kolding København
Køge Langeland Lejre Lemvig Lolland Lyngby-Taarbæk Læsø Mariagerfjord
Middelfart Morsø Norddjurs Nordfyns Nyborg Næstved Odder Odense Odsherred
Randers Rebild Ringkøbing-Skjern Ringsted Roskilde Rudersdal Rødovre Samsø
Silkeborg Skanderborg Skive Slagelse Solrød Sorø Stevns Struer Svendborg
Syddjurs Sønderborg Thisted Tønder Tårnby Vallensbæk Varde Vejen Vejle
Vesthimmerland Viborg Vordingborg Ærø Aabenraa Aalborg Aarhus
""".split())  # noqa: SIM905

# Værdier set i GO, der beskriver en KATEGORI, ikke en bestemt institution.
# Eksplicit liste frem for et gæt: en ny kategori skal en person tage stilling til.
KATEGORIER = frozenset({"0", "alm. bh.", "særl. bh.", "særl.bh.", "gl. amt", "dagpleje", "ppr"})

# Forkortelser, kun brugt til at SAMMENLIGNE — aldrig i den foreslåede værdi.
_FORKORTELSER = [
    (r"\bbhv\b\.?", "børnehave"),
    (r"\bdtb\b\.?", "dagtilbud"),
    (r"\bidrætsinst(itutionen|itution)?\b\.?", "idrætsinstitution"),
    (r"\bintegr\b\.?", "integreret"),
    (r"\binst\b\.?", "institution"),
    (r"\bprivat\b\.?", "privat"),
    (r"\bselvejede\b", "selvejende"),
]
# Tilføjelser der ikke skiller to institutioner ad i SAMMENLIGNINGEN.
# (SDT) er med: forslaget viser det, så den der godkender, kan sige nej.
_TILFOEJELSER = r"\((sdt|adm\.enhed)\)|,\s*d\.i\.i\.?|,\s*bhv\.?$"

STAVEFEJL_GRAENSE = 0.80

STATUS_ORDEN = {"auto": 0, "forslag": 1, "afklar": 2}

REVIEW_KOLONNER = ["liste", "go_vaerdi", "go_kolonner", "foreslaaet_vaerdi", "lis_nr",
                   "status", "begrundelse", "godkendt", "rettet_vaerdi"]


def ryd(raa: object) -> str:
    """Sikker oprydning. Ændrer aldrig hvilken værdi der menes."""
    tekst = "" if raa is None else str(raa)
    if isinstance(raa, float) and raa.is_integer():
        tekst = str(int(raa))
    tekst = re.sub(r"\s+", " ", tekst.replace(" ", " ")).strip()
    tekst = re.sub(r"\[\s*(\d+)\s*[\]})]", r"[\1]", tekst)        # "[ 534]", "[400}"
    tekst = re.sub(r"(\S)\[(\d+)\]", r"\1 [\2]", tekst)            # "Charlottehøj[148]"
    tekst = re.sub(r"\(sdt\)", "(SDT)", tekst, flags=re.IGNORECASE)
    tekst = re.sub(r",\s*d\.i\.i\.?(?=\s|$)", ", d.i.i", tekst)
    return tekst


def _dele(renset: str) -> tuple[str | None, str, str | None]:
    """(tal foran, navn, LIS-nummer) — "65 Skovslottet (SDT) [65]"."""
    lis = None
    if m := re.search(r"\s*\[(\d+)\]$", renset):
        lis, renset = m.group(1), renset[: m.start()]
    foran = None
    if m := re.match(r"(\d+)\s+(\D.*)$", renset):
        foran, renset = m.group(1), m.group(2)
    return foran, renset, lis


def sammenlign_navn(navn: str) -> str:
    """Navnet uden alt det, der ikke skiller to institutioner ad."""
    s = navn.casefold()
    s = re.sub(r"^(xxx|ændret\s*-\s*)", "", s)
    s = re.sub(_TILFOEJELSER, " ", s)
    for moenster, erstatning in _FORKORTELSER:
        s = re.sub(moenster, erstatning, s)
    s = s.replace("aa", "å")
    s = re.sub(r"[.\-/,'´`]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Gruppe:
    """Stavemåder der er samme værdi efter SIKKER oprydning."""
    stavemaader: dict[str, set[str]] = field(default_factory=dict)  # rå -> GO-kolonner
    renset: set[str] = field(default_factory=set)
    foran: str | None = None
    navn: str = ""
    lis: str | None = None
    flag: list[str] = field(default_factory=list)

    @property
    def etiket(self) -> str:
        # Flest store bogstaver: "Mårslet Skole" frem for "Mårslet skole".
        return max(self.renset, key=lambda s: (sum(c.isupper() for c in s), s))


@dataclass
class Raekke:
    liste: str
    go_vaerdi: str
    go_kolonner: str
    foreslaaet_vaerdi: str
    lis_nr: str
    status: str
    begrundelse: str
    godkendt: str = ""
    rettet_vaerdi: str = ""


def saml(vaerdier: dict[str, list[object]]) -> dict[str, list[Raekke]]:
    """GO-kolonne -> rå værdier  =>  liste -> gennemsynsrækker."""
    vaerdier = {k: v for k, v in vaerdier.items() if k not in UDELADTE_FANER}
    ukendte = sorted(set(vaerdier) - set(MAALLISTE))
    if ukendte:
        raise ValueError(f"Ukendte faner: {', '.join(ukendte)}. Kendte: {', '.join(MAALLISTE)}")

    pr_liste: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for kolonne, raekker in vaerdier.items():
        for raa in raekker:
            tekst = "" if raa is None else str(raa).strip()
            if isinstance(raa, float) and raa.is_integer():
                tekst = str(int(raa))
            if tekst:
                pr_liste[MAALLISTE[kolonne]][tekst].add(kolonne)

    return {liste: _saml_liste(liste, stave) for liste, stave in sorted(pr_liste.items())}


def _saml_liste(liste: str, stave: dict[str, set[str]]) -> list[Raekke]:
    # 1. Sikker gruppering
    grupper: dict[str, Gruppe] = {}
    for raa, kolonner in stave.items():
        renset = ryd(raa)
        g = grupper.setdefault(renset.casefold(), Gruppe())
        g.stavemaader[raa] = kolonner
        g.renset.add(renset)
    for g in grupper.values():
        foran, navn, lis = _dele(g.etiket) if liste != "klassetrin" else (None, g.etiket, None)
        g.foran, g.navn, g.lis = foran, navn, lis
        g.flag = _flag(liste, g)

    noegler = sorted(grupper)
    sammen = {n: n for n in noegler}
    grunde: dict[str, list[str]] = defaultdict(list)

    def rod(n: str) -> str:
        while sammen[n] != n:
            sammen[n] = sammen[sammen[n]]
            n = sammen[n]
        return n

    def numre(n: str) -> set[str]:
        return {grupper[m].lis for m in noegler if rod(m) == rod(n) and grupper[m].lis}

    def forbind(a: str, b: str, grund: str) -> None:
        ra, rb = rod(a), rod(b)
        if ra == rb:
            return
        if len(numre(a) | numre(b)) > 1:     # aldrig to LIS-numre i én værdi
            return
        sammen[rb] = ra
        grunde[a].append(f"{grund}: {grupper[b].etiket}")
        grunde[b].append(f"{grund}: {grupper[a].etiket}")

    # 2. Samme navn efter forkortelser og tilføjelser (inkl. tal foran)
    pr_navn: dict[str, list[str]] = defaultdict(list)
    for n in noegler:
        pr_navn[sammenlign_navn(grupper[n].navn)].append(n)
    for navn_noegle, medlemmer in pr_navn.items():
        if len(medlemmer) < 2 or not navn_noegle:
            continue
        for a in medlemmer:
            for b in medlemmer:
                if a >= b:
                    continue
                fa, fb = grupper[a].foran, grupper[b].foran
                if fa and fb and fa != fb:
                    # "151 Smilehullet" og "60 Smilehullet": to tal, måske to steder
                    for x, y in ((a, b), (b, a)):
                        grupper[x].flag.append(
                            f"samme navn med andet tal foran: {grupper[y].etiket}")
                    continue
                forbind(a, b, _grund_samme_navn(grupper[a], grupper[b]))

    # 3. Mulige stavefejl — kun mellem værdier der ikke allerede er samlet
    if liste != "klassetrin":
        navne = {n: sammenlign_navn(grupper[n].navn) for n in noegler}
        for i, a in enumerate(noegler):
            for b in noegler[i + 1:]:
                if rod(a) == rod(b):
                    continue
                if liste == "kommune" and {grupper[a].etiket, grupper[b].etiket} <= DANSKE_KOMMUNER:
                    continue    # Skanderborg og Sønderborg er begge rigtige kommuner
                grund = stavefejl(navne[a], navne[b])
                if grund:
                    forbind(a, b, grund)

    # 3b. Tal foran = LIS-nummeret på en værdi med et fælles ord i navnet
    #     ("2025 Børnehaven Valnødden (SDT)" og "Børnehuset Valnødden (sdt) [2025]")
    for a in noegler:
        ga = grupper[a]
        if not ga.foran:
            continue
        for b in noegler:
            gb = grupper[b]
            if gb.lis == ga.foran and _ord(ga.navn) & _ord(gb.navn):
                forbind(a, b, "tal foran = LIS-nummeret på")

    # 4. Samme LIS-nummer på værdier der IKKE er samlet
    pr_nr: dict[str, set[str]] = defaultdict(set)
    for n in noegler:
        if grupper[n].lis:
            pr_nr[grupper[n].lis].add(rod(n))
    for nr, roedder in pr_nr.items():
        if len(roedder) > 1:
            for n in noegler:
                if rod(n) in roedder:
                    andre = sorted({grupper[m].etiket for m in noegler
                                    if rod(m) in roedder and rod(m) != rod(n) and grupper[m].lis == nr})
                    grupper[n].flag.append(f"nummer [{nr}] bruges også af: {' / '.join(andre)}")

    # 5. Rækker
    komponenter: dict[str, list[str]] = defaultdict(list)
    for n in noegler:
        komponenter[rod(n)].append(n)

    valgte: dict[str, tuple[str, str]] = {}          # rod -> (værdi, LIS-nummer)
    for r, medlemmer in komponenter.items():
        g_valgt = grupper[max(medlemmer, key=lambda n: _rang(grupper[n]))]
        vaerdi = g_valgt.etiket
        if g_valgt.foran and not g_valgt.lis:
            vaerdi = g_valgt.navn      # tallet foran er ikke bekræftet som LIS-nummer
        lis = next((grupper[n].lis for n in medlemmer if grupper[n].lis), None) or ""
        valgte[r] = (vaerdi, lis)
    # To værdier der IKKE er samlet, må ikke ende med samme navn ("151
    # Smilehullet" og "60 Smilehullet"). Så beholder de tallet foran.
    optalt = defaultdict(int)
    for vaerdi, lis in valgte.values():
        optalt[(vaerdi.casefold(), lis)] += 1
    for r, (vaerdi, lis) in list(valgte.items()):
        if optalt[(vaerdi.casefold(), lis)] > 1:
            valgte[r] = (grupper[max(komponenter[r], key=lambda n: _rang(grupper[n]))].etiket, lis)

    ud: list[Raekke] = []
    for r, medlemmer in komponenter.items():
        vaerdi, lis = valgte[r]
        for n in medlemmer:
            g = grupper[n]
            for raa, kolonner in sorted(g.stavemaader.items()):
                begrundelse = list(g.flag)
                if g.foran:
                    begrundelse.append(_om_tal_foran(g.foran, lis))
                begrundelse += grunde.get(n, [])
                sikker = _sikker_forskel(raa, g)
                if sikker:
                    begrundelse.append(sikker)
                status = ("afklar" if g.flag else
                          "forslag" if len(medlemmer) > 1 or g.foran else "auto")
                ud.append(Raekke(liste, raa, ", ".join(sorted(kolonner)), vaerdi, lis,
                                 status, " | ".join(begrundelse)))
    ud.sort(key=lambda r: (r.foreslaaet_vaerdi.casefold(), r.go_vaerdi.casefold(), r.go_vaerdi))
    return ud


def _rang(g: Gruppe) -> tuple:
    uden_tilfoejelser = not re.search(_TILFOEJELSER, g.navn, flags=re.IGNORECASE)
    return (g.lis is not None, not g.flag, g.foran is None, uden_tilfoejelser,
            sum(c.isupper() for c in g.etiket))


# Tilføjelser der ignoreres i sammenligningen, og hvad begrundelsen kalder dem.
_MARKOERER = [
    (r"\(sdt\)", "(SDT)"),
    (r",\s*d\.i\.i", "d.i.i"),
    (r"\(adm\.enhed\)", "(adm.enhed)"),
    (r",\s*bhv\.?$", "', bhv'"),
]


def _grund_samme_navn(a: Gruppe, b: Gruppe) -> str:
    """Hvad der præcis blev set bort fra, så den der godkender kan se det."""
    dele = []
    if bool(a.lis) != bool(b.lis):
        dele.append("den ene uden nummer")
    if bool(a.foran) or bool(b.foran):
        dele.append("tal foran ignoreret")
    na, nb = a.navn, b.navn
    for moenster, navn in _MARKOERER:
        ia = bool(re.search(moenster, na, flags=re.IGNORECASE))
        ib = bool(re.search(moenster, nb, flags=re.IGNORECASE))
        if ia != ib:
            dele.append(f"{navn} ignoreret")
        na = re.sub(moenster, "", na, flags=re.IGNORECASE)
        nb = re.sub(moenster, "", nb, flags=re.IGNORECASE)
    if _kort(na) != _kort(nb):
        dele.append("forkortelse/stavemåde")
    return "samme navn (" + ", ".join(dele or ["kun store/små bogstaver"]) + ")"


def _kort(navn: str) -> str:
    return re.sub(r"\s+", " ", navn).strip().casefold()


def stavefejl(a: str, b: str) -> str:
    """En begrundelse hvis a og b ligner samme navn med en stavefejl, ellers "".

    Kun SMÅ forskelle inde i ét ord ("Børenhuset"/"Børnehuset",
    "Særlige"/"Særligt") eller samme bogstaver med andre mellemrum
    ("Menigheds bhv"/"Menighedsbørnehave"). To forskellige ord ("Harlev"/
    "Hasle", "Vest"/"Øst") er to forskellige steder, ikke en stavefejl.
    """
    if a == b:
        return ""
    if a.replace(" ", "") == b.replace(" ", ""):
        return "samme navn, andre mellemrum, som"
    oa, ob = a.split(), b.split()
    if len(oa) != len(ob):
        return ""
    forskellige = [(x, y) for x, y in zip(oa, ob, strict=True) if x != y]
    if len(forskellige) != 1:
        return ""
    x, y = forskellige[0]
    if re.search(r"\d", x + y) or min(len(x), len(y)) < 5:
        return ""
    if SequenceMatcher(None, x, y).ratio() >= STAVEFEJL_GRAENSE:
        return f"mulig stavefejl ({x}/{y}) af"
    return ""


def _ord(navn: str) -> set[str]:
    """Navnets betydende ord — til at se om to navne overhovedet handler om det samme."""
    almindelige = {"børnehaven", "børnehuset", "børnehave", "dagtilbud", "vuggestuen",
                   "skole", "skolen", "klubben", "afdeling", "område", "aarhus", "århus"}
    return {o for o in re.findall(r"\w+", sammenlign_navn(navn))
            if len(o) >= 4 and o not in almindelige}


def _om_tal_foran(foran: str, lis: str) -> str:
    if not lis:
        return f"tal foran ({foran}): tjek mod Jakobs liste om det er LIS-nummeret"
    if foran == lis:
        return f"tal foran ({foran}) = LIS-nummeret"
    return f"tal foran ({foran}) er IKKE LIS-nummeret [{lis}]"


def _sikker_forskel(raa: str, g: Gruppe) -> str:
    if len(g.stavemaader) < 2 and ryd(raa) == raa:
        return ""
    if ryd(raa) != raa:
        return "renset: mellemrum/klammer/SDT/d.i.i"
    return "kun forskel i store/små bogstaver"


def _flag(liste: str, g: Gruppe) -> list[str]:
    flag = []
    tekst = g.etiket
    if liste == "kommune" and tekst not in DANSKE_KOMMUNER:
        flag.append("ikke en dansk kommune")
    if liste == "klassetrin" and not (tekst.isdigit() and 0 <= int(tekst) <= 10):
        flag.append("ikke et klassetrin 0-10")
    if liste != "klassetrin" and tekst.casefold() in KATEGORIER:
        flag.append("kategori, ikke en bestemt institution")
    if tekst.upper().startswith("XXX"):
        flag.append("markeret XXX i GO (nedlagt?)")
    if tekst.upper().startswith("ÆNDRET"):
        flag.append("markeret ÆNDRET i GO (omdøbt eller nedlagt enhed)")
    if re.search(r"\(\d\)", tekst):
        flag.append("nummereret variant (1)/(2)")
    return flag


def vaerdiliste(raekker: list[Raekke]) -> list[dict[str, str]]:
    """Én række pr. foreslået værdi — udkastet til trin 2."""
    pr_vaerdi: dict[tuple[str, str], list[Raekke]] = defaultdict(list)
    for r in raekker:
        pr_vaerdi[(r.foreslaaet_vaerdi, r.lis_nr)].append(r)
    return [
        {"liste": rs[0].liste, "foreslaaet_vaerdi": v, "lis_nr": nr,
         "stavemaader": str(len(rs)),
         "status": max((r.status for r in rs), key=STATUS_ORDEN.__getitem__)}
        for (v, nr), rs in sorted(pr_vaerdi.items(), key=lambda kv: kv[0][0].casefold())
    ]


def laes_xlsx(sti: Path) -> tuple[dict[str, list[object]], list[str]]:
    """Én fane pr. GO-kolonne, første række er overskriften.

    Returnerer (faner, navnene på de udeladte faner). En udeladt fane
    læses ikke — dens indhold kommer aldrig ind i programmet.
    """
    import openpyxl

    bog = openpyxl.load_workbook(sti, read_only=True, data_only=True)
    try:
        faner = {ark.title: [r[0] for r in ark.iter_rows(min_row=2, values_only=True) if r]
                 for ark in bog.worksheets if ark.title not in UDELADTE_FANER}
        return faner, [navn for navn in bog.sheetnames if navn in UDELADTE_FANER]
    finally:
        bog.close()


def gennemsyn_sti(kilde: Path, mappe: Path | None = None) -> Path:
    """"distinkte_vaerdier.xlsx" -> "distinkte_vaerdier-gennemsyn.xlsx", som
    standard i SAMME mappe som kilden."""
    return (mappe or kilde.parent) / f"{kilde.stem}-gennemsyn.xlsx"

_BREDDER = {"liste": 14, "go_vaerdi": 45, "go_kolonner": 22, "foreslaaet_vaerdi": 45,
            "lis_nr": 8, "status": 10, "begrundelse": 80, "godkendt": 10, "rettet_vaerdi": 40}


def skriv(resultat: dict[str, list[Raekke]], sti: Path) -> Path:
    """Én Excel-fil, én fane pr. liste.

    Excel frem for CSV: en CSV deles i kolonner efter Windows'
    listeseparator, og den er komma på nogle maskiner og semikolon på
    andre. Så stod alt i én kolonne (21-11-2026).
    """
    import openpyxl
    from openpyxl.styles import Font

    sti.parent.mkdir(parents=True, exist_ok=True)
    bog = openpyxl.Workbook()
    bog.remove(bog.active)
    for liste, raekker in resultat.items():
        ark = bog.create_sheet(liste)
        ark.append(REVIEW_KOLONNER)
        for r in raekker:
            ark.append([getattr(r, k) for k in REVIEW_KOLONNER])
        for celle in ark[1]:
            celle.font = Font(bold=True)
        for nr, kolonne in enumerate(REVIEW_KOLONNER, start=1):
            ark.column_dimensions[openpyxl.utils.get_column_letter(nr)].width = _BREDDER[kolonne]
        ark.freeze_panes = "A2"
        ark.auto_filter.ref = ark.dimensions
    bog.save(sti)
    return sti
