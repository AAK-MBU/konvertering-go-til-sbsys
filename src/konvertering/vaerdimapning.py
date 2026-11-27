"""Trin 2 af værdimappingen: fra det godkendte gennemsyn til mappingrækker.

Trin 1 (vaerdier.py) samlede GO's stavemåder og foreslog én værdi pr.
gruppe. Et menneske har derefter gået filen igennem: sat "x" i
``godkendt`` og skrevet ``rettet_vaerdi`` hvor forslaget var forkert.

Dette modul læser den gennemgåede fil og:

1. finder den ENDELIGE værdi for hver GO-stavemåde:
   ``rettet_vaerdi`` hvis udfyldt, ellers forslaget hvis det er ``auto``
   eller godkendt. Alt andet er uafklaret og mappes ikke.
2. tjekker værdien mod SBSYS' egen valgliste (en JSON-fil hentet med
   ``konv kald GET /bu_api/tilbudfeltvaerdiliste/<id>/tilbudfeltvaerdilistemulighed``).
   Kun et eksakt navn (uanset store/små bogstaver og mellemrum) tæller.
   Har SBSYS en værdi med SAMME LIS-nummer men andet navn, vises den som
   forslag — den bruges aldrig af sig selv.
3. skriver rækker i mapping/tilbudsfeltvaerdi.csv-formatet for de
   værdier der er klar, og en Excel-fil med alt det der ikke er.

Intet skrives i mapping/. Rækkerne kopieres derind efter gennemsyn.
"""

from __future__ import annotations

import csv
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .mapping import KOLONNER as MAPPING_KOLONNER

VAERDI_LISTE = "tilbudsfeltvaerdi"

# SBSYS-værdilisten for hver liste i gennemsynet (prod, 23-11-2026).
SBSYS_LISTE_ID: dict[str, int] = {
    "bevillingssted": 23,
    "institution": 27,
    "klassetrin": 26,
    "kommune": 2,
}

_LIS = re.compile(r"\[(\d+)\]\s*$")
_NEJ = {"nej", "n", "0", "false", "-"}

KLAR = "klar"
UAFKLARET = "uafklaret"
MANGLER_I_SBSYS = "mangler_i_sbsys"
IKKE_TJEKKET = "ikke_tjekket"
INGEN_TEKSTMATCH = "ingen_tekstmatch"   # intet id i GO, ikke fundet i prod -> konvertering_

# --- Tekstreglen (besluttet 24-11-2026) ---------------------------------------
# For GO-værdier UDEN id i bevillingssted og institution stoler vi ikke på
# gennemsynets navnegæt (fx "Arvepr. Carolines Børneasyl" -> "... [503]",
# som viste sig forkert). I stedet:
#   a. håndmappet til en privatskole "[instnr N]"  -> beholdes (PDF'en, 3.3)
#   b. indeholder "dagtilbud", "skole" eller "school" -> navnet slås op i
#      prods liste (uden "[..]", store/små og mellemrum er ligegyldige).
#      Præcis ét træf -> det bruges. Ellers gælder gennemsynets afgørelse
#      (fx "Tranbjerg skole" -> "Tranbjergskolen [725]").
#   c. alt andet                                   -> konvertering_
# Har GO-værdien et id ("[N]", "[instnr N]" eller et tal foran), gælder
# gennemsynets afgørelse som hidtil.
TEKSTREGEL_LISTER = frozenset({"bevillingssted", "institution"})
_TEKSTREGEL_ORD = re.compile(r"dagtilbud|skole|school", re.IGNORECASE)
_GO_ID = re.compile(r"\[\s*(instnr\s*)?\d+\s*[\]}]|^\s*\d+\s")


def _norm(tekst: str) -> str:
    return re.sub(r"\s+", " ", tekst).strip().casefold()


def _uden_nr(tekst: str) -> str:
    """Navnet uden "[N]" / "[instnr N]" til sidst — kun til forslag."""
    return _norm(re.sub(r"\[(instnr\s*)?\d+\]\s*$", "", tekst))


def lis_nr(tekst: str | None) -> str | None:
    m = _LIS.search(tekst or "")
    return m.group(1) if m else None


def id_noegle(tekst: str | None) -> str | None:
    """"Navn [N]" -> "lisid_N", "Navn [instnr N]" -> "instnr_N", ellers None."""
    if m := _INSTNR.search(tekst or ""):
        return f"instnr_{m.group(1)}"
    if nr := lis_nr(tekst):
        return f"lisid_{nr}"
    return None


@dataclass
class Valg:
    """Én GO-stavemåde og hvad gennemsynet besluttede for den."""

    liste: str
    go_vaerdi: str
    go_kolonner: list[str]
    endelig: str | None
    kilde: str                # rettet | godkendt | auto | ""
    kommentar: str = ""
    forslag: str = ""         # trin 1's forslag, som det stod i filen
    godkendt: str = ""        # kolonnen "godkendt", som den stod i filen
    sbsys_id: int | None = None
    sbsys_navn: str | None = None
    status: str = UAFKLARET
    bemaerkning: str = ""


@dataclass
class Resultat:
    valg: list[Valg] = field(default_factory=list)
    tjek: list[dict[str, str]] = field(default_factory=list)
    lister_tjekket: set[str] = field(default_factory=set)


def _celle(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def laes_gennemsyn(sti: Path) -> list[Valg]:
    """Læs det gennemgåede gennemsyn. Kolonnerne findes på overskriften,
    så en tilføjet kolonne (fx ``kommentar``) eller ny rækkefølge er ok."""
    import openpyxl

    bog = openpyxl.load_workbook(sti, read_only=True, data_only=True)
    ud: list[Valg] = []
    try:
        for ark in bog.worksheets:
            raekker = ark.iter_rows(values_only=True)
            hoved = [_celle(h) for h in next(raekker, ())]
            mangler = {"go_vaerdi", "foreslaaet_vaerdi", "status"} - set(hoved)
            if mangler:
                raise ValueError(f"Fanen {ark.title} mangler kolonnerne {sorted(mangler)}")
            ix = {h: i for i, h in enumerate(hoved)}

            def g(r: tuple, k: str, ix: dict = ix) -> str:
                return _celle(r[ix[k]]) if k in ix and ix[k] < len(r) else ""

            for r in raekker:
                go = g(r, "go_vaerdi")
                if not go:
                    continue
                rettet, godkendt = g(r, "rettet_vaerdi"), g(r, "godkendt")
                status, forslag = g(r, "status"), g(r, "foreslaaet_vaerdi")
                if rettet:
                    endelig, kilde = rettet, "rettet"
                elif godkendt and godkendt.casefold() not in _NEJ:
                    endelig, kilde = forslag, "godkendt"
                elif status == "auto" and not (godkendt.casefold() in _NEJ and godkendt):
                    endelig, kilde = forslag, "auto"
                else:
                    endelig, kilde = None, ""
                kolonner = [k.strip() for k in g(r, "go_kolonner").split(",") if k.strip()]
                ud.append(Valg(g(r, "liste") or ark.title, go, kolonner, endelig or None,
                               kilde, g(r, "kommentar"), forslag, godkendt))
    finally:
        bog.close()
    return ud


# Lister ``--hent-sbsys`` henter. Institution er med, selv om prods liste 27
# ikke er færdig: værdier der mangler dér, står som mangler_i_sbsys og bliver
# klar ved næste kørsel, når NC har fyldt listen op.
HENTES_AUTOMATISK = ("bevillingssted", "institution", "kommune", "klassetrin")


def laes_sbsys_liste(sti: Path) -> list[dict]:
    """En JSON-fil med SBSYS' valgmuligheder, som ``konv kald ... --ud`` gemmer den."""
    return laes_sbsys_liste_data(json.loads(sti.read_text(encoding="utf-8-sig")))


def laes_sbsys_liste_data(krop: object) -> list[dict]:
    """SBSYS' valgmuligheder fra et allerede indlæst JSON-svar."""
    if isinstance(krop, dict):
        krop = krop.get("Results") or krop.get("body") or []
    return [m for m in krop if isinstance(m, dict) and m.get("vaerdiMulighed")]


def har_go_id(go_vaerdi: str) -> bool:
    """"Navn [N]", "Navn [instnr N]" (også med "}"), eller "N Navn"."""
    return bool(_GO_ID.search(go_vaerdi))


def anvend_tekstregel(valg: list[Valg], sbsys: dict[str, list[dict]]) -> None:
    """Erstat gennemsynets afgørelse for GO-værdier UDEN id (se TEKSTREGEL_LISTER).

    Kun for lister hvis prod-liste er givet — uden den kan b. ikke afgøres."""
    for liste in TEKSTREGEL_LISTER & set(sbsys):
        pr_navn: dict[str, list[dict]] = defaultdict(list)
        for m in sbsys[liste]:
            if m.get("aktiv", True):
                pr_navn[_uden_nr(m["vaerdiMulighed"])].append(m)
        for v in valg:
            if v.liste != liste or har_go_id(v.go_vaerdi):
                continue
            if v.endelig and _INSTNR.search(v.endelig):
                continue                                         # a. privatskole
            if _TEKSTREGEL_ORD.search(v.go_vaerdi):              # b.
                traef = pr_navn.get(_norm(v.go_vaerdi), [])
                if len(traef) == 1:
                    v.endelig, v.kilde = traef[0]["vaerdiMulighed"], "tekstregel"
                continue                                         # ellers: gennemsynet
            v.endelig, v.kilde, v.status = None, "tekstregel", INGEN_TEKSTMATCH  # c.
            v.bemaerkning = ("Intet id i GO og hverken 'dagtilbud', 'skole' eller "
                             "'school' -> konvertering_")


def byg(valg: list[Valg], sbsys: dict[str, list[dict]]) -> Resultat:
    """Tjek hver endelig værdi mod SBSYS' liste og find uoverensstemmelser."""
    anvend_tekstregel(valg, sbsys)
    res = Resultat(valg=valg, lister_tjekket=set(sbsys))

    for liste, muligheder in sbsys.items():
        paa_navn: dict[str, list[dict]] = defaultdict(list)
        paa_id: dict[str, list[dict]] = defaultdict(list)
        uden_nr: dict[str, list[dict]] = defaultdict(list)
        for m in muligheder:
            paa_navn[_norm(m["vaerdiMulighed"])].append(m)
            uden_nr[_uden_nr(m["vaerdiMulighed"])].append(m)
            if noegle := id_noegle(m["vaerdiMulighed"]):
                paa_id[noegle].append(m)
        for v in (v for v in valg if v.liste == liste and v.endelig):
            traef = paa_navn.get(_norm(v.endelig), [])
            aktive = [m for m in traef if m.get("aktiv", True)] or traef
            if aktive:
                v.sbsys_id, v.sbsys_navn = int(aktive[0]["id"]), aktive[0]["vaerdiMulighed"]
                v.status = KLAR
                if len(traef) > 1:
                    v.bemaerkning = f"SBSYS har {len(traef)} valgmuligheder med dette navn"
                continue
            # Navnet afviger, men nummeret ("[N]" / "[instnr N]") er det samme:
            # præcis én aktiv valgmulighed med det nummer -> den bruges
            # (besluttet 27-11-2026). Nummeret er identiteten, navnet kun en etiket.
            noegle = id_noegle(v.endelig)
            kandidater = paa_id.get(noegle, []) if noegle else []
            aktive = [m for m in kandidater if m.get("aktiv", True)]
            if len(aktive) == 1:
                v.sbsys_id, v.sbsys_navn = int(aktive[0]["id"]), aktive[0]["vaerdiMulighed"]
                v.status = KLAR
                v.bemaerkning = (f"Samme nummer, andet navn: GO '{v.endelig}' -> "
                                 f"SBSYS '{v.sbsys_navn}'")
                continue
            v.status = MANGLER_I_SBSYS
            if kandidater:
                navne = " / ".join(sorted({m["vaerdiMulighed"] for m in kandidater}))
                v.bemaerkning = (f"SBSYS har {len(aktive)} aktive valgmuligheder med samme "
                                 f"nummer: {navne}")
            elif kandidater := uden_nr.get(_uden_nr(v.endelig), []):
                navne = " / ".join(sorted({m["vaerdiMulighed"] for m in kandidater}))
                v.bemaerkning = f"SBSYS har samme navn med andet/intet nummer: {navne}"

    for v in valg:
        if v.endelig and v.liste not in sbsys:
            v.status = IKKE_TJEKKET

    # Samme LIS-nummer -> mere end én endelig værdi: en af dem er forkert.
    pr_nr: dict[tuple[str, str], set[str]] = defaultdict(set)
    for v in valg:
        if v.endelig and (nr := lis_nr(v.endelig)):
            pr_nr[(v.liste, nr)].add(v.sbsys_navn or v.endelig)
    for (liste, nr), navne in sorted(pr_nr.items()):
        if len(navne) > 1:
            res.tjek.append({"liste": liste, "go_vaerdi": "", "endelig": " / ".join(sorted(navne)),
                             "problem": f"LIS-nr [{nr}] har {len(navne)} forskellige endelige navne"})

    # GO-værdien har et LIS-nummer, den endelige værdi et ANDET eller intet.
    for v in valg:
        go_nr, ny_nr = lis_nr(v.go_vaerdi), lis_nr(v.endelig)
        if v.endelig and go_nr and go_nr != ny_nr:
            res.tjek.append({
                "liste": v.liste, "go_vaerdi": v.go_vaerdi, "endelig": v.endelig,
                "problem": (f"GO har [{go_nr}], den endelige værdi har "
                            + (f"[{ny_nr}]" if ny_nr else "intet nummer")
                            + (f". Kommentar: {v.kommentar}" if v.kommentar else ""))})
    return res


def eksisterende(sti: Path) -> dict[str, str]:
    """``<GO-kolonne>|<GO-værdi>`` -> sbsys_vaerdi fra den nuværende mapping."""
    if not sti.exists():
        return {}
    with sti.open(encoding="utf-8-sig", newline="") as f:
        return {(r.get("go_vaerdi") or "").strip(): (r.get("sbsys_vaerdi") or "").strip()
                for r in csv.DictReader(f, delimiter=";")
                if (r.get("liste") or "").strip() == VAERDI_LISTE}


def mapping_raekker(res: Resultat, kilde: str, idag: date | None = None) -> list[dict[str, str]]:
    """Rækker til mapping/tilbudsfeltvaerdi.csv — kun dem der er KLAR.

    Én række pr. GO-kolonne værdien optræder i, fordi nøglen i
    ``tilbudsfeltvaerdi`` er ``<GO-kolonne>|<GO-værdi>``.
    """
    dato = (idag or date.today()).strftime("%d-%m-%Y")
    ud = []
    for v in sorted(res.valg, key=lambda v: (v.liste, v.go_vaerdi.casefold())):
        if v.status != KLAR:
            continue
        for kolonne in v.go_kolonner:
            ud.append({
                "liste": VAERDI_LISTE,
                "go_vaerdi": f"{kolonne}|{v.go_vaerdi}",
                "go_tekst": v.go_vaerdi,
                "sbsys_vaerdi": v.sbsys_navn or "",
                "sbsys_tekst": v.sbsys_navn or "",
                "handling": "map",
                "kommentar": f"Fra {kilde} ({v.kilde}), SBSYS-id {v.sbsys_id}, {dato}.",
                "godkendt_af": "",
                "godkendt_dato": "",
            })
    return ud


def skriv_csv(raekker: list[dict[str, str]], sti: Path) -> Path:
    for r in raekker:
        if any(";" in (r[k] or "") for k in ("go_vaerdi", "sbsys_vaerdi")):
            raise ValueError(f"Semikolon i værdien, kan ikke stå i mapping-CSV'en: {r['go_vaerdi']}")
    with sti.open("w", encoding="utf-8", newline="") as f:
        skriver = csv.DictWriter(f, fieldnames=MAPPING_KOLONNER, delimiter=";",
                                 lineterminator="\n")
        skriver.writeheader()
        skriver.writerows(raekker)
    return sti


def skriv_muligheder_xlsx(muligheder: list[dict[str, str]], sti: Path) -> Path:
    """Den beregnede tabel (alle rækker) + de navne LIS skal oprette (unikke)."""
    import openpyxl
    from openpyxl.styles import Font

    bog = openpyxl.Workbook()
    bog.remove(bog.active)

    def ark(navn: str, hoved: list[str], raekker: list[list], bredder: list[int]) -> None:
        a = bog.create_sheet(navn)
        a.append(hoved)
        for r in raekker:
            a.append(r)
        for c in a[1]:
            c.font = Font(bold=True)
        for i, b in enumerate(bredder, start=1):
            a.column_dimensions[openpyxl.utils.get_column_letter(i)].width = b
        a.freeze_panes = "A2"
        a.auto_filter.ref = a.dimensions

    hoved = ["liste", "go_feltnavn", "go_feltvaerdi", "vaerdiliste_external_key",
             "vaerdimulighed_external_key", "vaerdimulighed_navn", "noegletype", "status",
             "kommentar"]
    ark("vaerdilistemuligheder", hoved, [[m[k] for k in hoved] for m in muligheder],
        [14, 22, 45, 26, 50, 50, 13, 16, 60])
    nye: dict[tuple[str, str], dict[str, str]] = {}
    for m in muligheder:
        if m["status"] in (MANGLER_I_SBSYS, UAFKLARET, INGEN_TEKSTMATCH):
            nye.setdefault((m["liste"], m["vaerdimulighed_external_key"]), m)
    ark("til_lis", ["liste", "vaerdiliste_external_key", "vaerdimulighed_external_key",
                    "vaerdimulighed_navn", "noegletype", "status"],
        [[m["liste"], m["vaerdiliste_external_key"], m["vaerdimulighed_external_key"],
          m["vaerdimulighed_navn"], m["noegletype"], m["status"]]
         for _, m in sorted(nye.items())],
        [14, 26, 50, 50, 13, 16])
    bog.save(sti)
    return sti


def skriv_xlsx(res: Resultat, konflikter: list[dict[str, str]], sti: Path) -> Path:
    """Alt til gennemsyn: hver stavemåde, de endelige værdier og problemerne."""
    import openpyxl
    from openpyxl.styles import Font

    bog = openpyxl.Workbook()
    bog.remove(bog.active)

    def ark(navn: str, hoved: list[str], raekker: list[list], bredder: list[int]) -> None:
        a = bog.create_sheet(navn)
        a.append(hoved)
        for r in raekker:
            a.append(r)
        for c in a[1]:
            c.font = Font(bold=True)
        for i, b in enumerate(bredder, start=1):
            a.column_dimensions[openpyxl.utils.get_column_letter(i)].width = b
        a.freeze_panes = "A2"
        a.auto_filter.ref = a.dimensions

    # Endelige værdier: én række pr. værdi, det er dem der skal findes i SBSYS.
    pr_vaerdi: dict[tuple[str, str], list[Valg]] = defaultdict(list)
    for v in res.valg:
        if v.endelig:
            pr_vaerdi[(v.liste, v.endelig)].append(v)
    ark("endelige",
        ["liste", "endelig_vaerdi", "lis_nr", "go_stavemaader", "status", "sbsys_id",
         "sbsys_navn", "bemaerkning"],
        [[liste, vaerdi, lis_nr(vaerdi), len(vs), vs[0].status, vs[0].sbsys_id,
          vs[0].sbsys_navn, vs[0].bemaerkning]
         for (liste, vaerdi), vs in sorted(pr_vaerdi.items(),
                                           key=lambda kv: (kv[0][0], kv[0][1].casefold()))],
        [14, 50, 8, 10, 16, 9, 50, 60])
    ark("stavemaader",
        ["liste", "go_kolonner", "go_vaerdi", "endelig_vaerdi", "kilde", "status",
         "sbsys_id", "kommentar"],
        [[v.liste, ", ".join(v.go_kolonner), v.go_vaerdi, v.endelig, v.kilde, v.status,
          v.sbsys_id, v.kommentar]
         for v in sorted(res.valg, key=lambda v: (v.liste, v.go_vaerdi.casefold()))],
        [14, 22, 45, 50, 10, 16, 9, 50])
    ark("tjek", ["liste", "go_vaerdi", "endelig", "problem"],
        [[t["liste"], t["go_vaerdi"], t["endelig"], t["problem"]] for t in res.tjek]
        + [[k["liste"], k["go_vaerdi"], k["endelig"], k["problem"]] for k in konflikter],
        [14, 45, 50, 80])
    bog.save(sti)
    return sti


# --- Værdilistemuligheder med nøgler (konverterings-PDF'en, trin 3-4) -------

# Fast navnerum til konvertering_-nøgler. MÅ ALDRIG ÆNDRES: nøglerne
# beregnes ud fra det (uuid5), så samme liste + navn altid giver samme
# nøgle, også efter tabellen er slettet og bygget igen.
NAVNERUM = uuid.UUID("d6e5a33e-4914-4e6a-8f9a-a9f5ca30cc0d")

# VaerdiListeExternalKey pr. liste. Kun dem PDF'en nævner; resten skal LIS oplyse.
VAERDILISTE_NOEGLE: dict[str, str] = {
    "bevillingssted": "v_bevillingssted_ppr",
    "institution": "v_skole_el_institution_ppr",
    "klassetrin": "v_klassetrin",
    "kommune": "v_kommune",
}

KOMMUNEKODER = Path(__file__).resolve().parents[2] / "mapping" / "reference" / "kommunekoder.csv"


def laes_kommunekoder(sti: Path = KOMMUNEKODER) -> dict[str, str]:
    """Navn (normaliseret) -> kommunekode. Kilde: Danmarks Statistik (FOLK1A,
    OMRÅDE), hentet 24-11-2026; matcher prods liste 2 navn for navn."""
    if not sti.exists():
        return {}
    with sti.open(encoding="utf-8-sig", newline="") as f:
        return {_norm(r["navn"]): r["kode"].strip() for r in csv.DictReader(f, delimiter=";")}

_INSTNR = re.compile(r"\[instnr\s*(\d+)\]\s*$", re.IGNORECASE)

LISID, INSTNR, KOMMUNEKODE, KONVERTERING = "lisid", "instnr", "kommunekode", "konvertering"


def konvertering_noegle(liste: str, navn: str) -> str:
    """Deterministisk nøgle: samme liste + navn giver altid samme uuid.
    Store/små bogstaver og ekstra mellemrum tæller ikke."""
    return f"konvertering_{uuid.uuid5(NAVNERUM, f'{liste}|{_norm(navn)}')}"


def mulighed_noegle(liste: str, navn: str,
                    kommunekoder: dict[str, str] | None = None) -> tuple[str, str]:
    """(VaerdiMulighedExternalKey, nøgletype) for et endeligt navn.

    "Navn [N]" -> lisid_N; "Navn [instnr N]" -> instreg_instnr_N;
    kommune -> kommunekoden (tom hvis ukendt); alt andet -> konvertering_<uuid5>.
    """
    if m := _INSTNR.search(navn):
        return f"instreg_instnr_{m.group(1)}", INSTNR
    if nr := lis_nr(navn):
        return f"lisid_{nr}", LISID
    if liste == "kommune":
        kode = (kommunekoder or {}).get(_norm(navn))
        if kode:
            return kode, KOMMUNEKODE
    return konvertering_noegle(liste, navn), KONVERTERING


def tabel_status(status: str) -> str:
    """Nøgletabellens tre statusser:

    klar             findes i SBSYS
    mangler_i_sbsys  har en nøgle; LIS skal oprette den (også alle konvertering_)
    uafklaret        kræver en afgørelse (eller listen er ikke tjekket mod SBSYS)
    """
    if status == KLAR:
        return KLAR
    if status in (MANGLER_I_SBSYS, INGEN_TEKSTMATCH):
        return MANGLER_I_SBSYS
    return UAFKLARET


def vaerdilistemuligheder(res: Resultat,
                          kommunekoder: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Én række pr. GO-kolonne og GO-værdi, som note 3 i konverterings-PDF'en.

    Er værdien uafklaret i gennemsynet, bruges GO-værdien selv som navn
    (som "Markblomsten" i PDF'en)."""
    ud = []
    if kommunekoder is None:
        kommunekoder = laes_kommunekoder()
    for v in sorted(res.valg, key=lambda v: (v.liste, v.go_vaerdi.casefold())):
        navn = v.sbsys_navn or v.endelig or v.go_vaerdi
        noegle, type_ = mulighed_noegle(v.liste, navn, kommunekoder)
        kommentar = []
        if v.liste == "kommune" and type_ == KONVERTERING:
            kommentar.append("Ingen kommunekode fundet")
        if v.status == INGEN_TEKSTMATCH:
            kommentar.append(v.bemaerkning + ". GO-værdien bruges som navn")
        elif v.status == UAFKLARET:
            kommentar.append("Uafklaret i gennemsynet - GO-værdien bruges som navn")
        elif v.status == MANGLER_I_SBSYS:
            kommentar.append("Findes ikke i SBSYS - LIS skal oprette den")
        elif v.status == IKKE_TJEKKET:
            kommentar.append("Ikke tjekket mod SBSYS' liste")
        if v.kommentar:
            kommentar.append(v.kommentar)
        for kolonne in v.go_kolonner or [""]:
            ud.append({
                "liste": v.liste,
                "go_feltnavn": kolonne,
                "go_feltvaerdi": v.go_vaerdi,
                "vaerdiliste_external_key": VAERDILISTE_NOEGLE.get(v.liste, ""),
                "vaerdimulighed_external_key": noegle,
                "vaerdimulighed_navn": navn,
                "noegletype": type_,
                "status": tabel_status(v.status),
                "kommentar": ". ".join(kommentar),
            })
    return ud


def kolonner_for(liste: str) -> set[str]:
    """GO-kolonnerne der renses i ``liste`` (fx bevillingssted ->
    Betalingssted, Bevillingssted)."""
    from .vaerdier import MAALLISTE

    return {kolonne for kolonne, maal in MAALLISTE.items() if maal == liste}


def flet(sti: Path, nye: list[dict[str, str]],
         fjern: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], list[str], list[str]]:
    """Flet rækker ind i mapping/tilbudsfeltvaerdi.csv.

    Linjebaseret, så alle andre rækker står præcis som før — også deres
    citationstegn og rækkefølge. En nøgle der findes i forvejen erstattes
    på sin plads; nye nøgler tilføjes til sidst.

    Nøgler i ``fjern`` slettes — men kun rækker med ``handling = map``. En
    bevidst ``ignorer``-række (fx PPR) står urørt.

    Returnerer (erstattede, tilføjede, fjernede nøgler).
    """
    import io

    def linje(r: dict[str, str]) -> str:
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=MAPPING_KOLONNER, delimiter=";",
                       lineterminator="\n").writerow(r)
        return buf.getvalue()

    nye_pr_noegle = {r["go_vaerdi"]: r for r in nye}
    linjer = sti.read_text(encoding="utf-8").splitlines(keepends=True)
    erstattet: list[str] = []
    fjernet: list[str] = []
    handling_ix = MAPPING_KOLONNER.index("handling")
    ud: list[str] = []
    for nr, tekst in enumerate(linjer):
        felter = next(csv.reader([tekst], delimiter=";"), [])
        if nr > 0 and len(felter) > handling_ix and felter[0] == VAERDI_LISTE \
                and felter[1].strip() in fjern and felter[1].strip() not in nye_pr_noegle \
                and felter[handling_ix].strip() == "map":
            fjernet.append(felter[1].strip())
            continue
        if nr > 0 and len(felter) > 1 and felter[0] == VAERDI_LISTE \
                and felter[1].strip() in nye_pr_noegle:
            noegle = felter[1].strip()
            ud.append(linje(nye_pr_noegle[noegle]))
            erstattet.append(noegle)
        else:
            ud.append(tekst if tekst.endswith("\n") else tekst + "\n")
    tilfoejet = [k for k in nye_pr_noegle if k not in set(erstattet)]
    ud += [linje(nye_pr_noegle[k]) for k in tilfoejet]
    sti.write_text("".join(ud), encoding="utf-8")
    return erstattet, tilfoejet, fjernet


def konflikter_med_mapping(raekker: list[dict[str, str]], nu: dict[str, str]) -> list[dict[str, str]]:
    """Nøgler der allerede står i mapping/tilbudsfeltvaerdi.csv med ANDET mål."""
    ud = []
    for r in raekker:
        gammel = nu.get(r["go_vaerdi"])
        if gammel is not None and _norm(gammel) != _norm(r["sbsys_vaerdi"]):
            ud.append({"liste": "mapping", "go_vaerdi": r["go_vaerdi"], "endelig": r["sbsys_vaerdi"],
                       "problem": f"mapping/tilbudsfeltvaerdi.csv siger allerede: {gammel}"})
    return ud
