"""Forbered kodelisterne: hent begge sider og par dem hvor det er entydigt.

En mapning har to sider. **SBSYS-siden kan hentes** — id'er og navne på
skabeloner, afdelinger, paragraffer, foranstaltninger og tilbud ligger i
API'et. **GO-siden kan læses** — de distinkte værdier står i `stg.*`.

Det der IKKE kan hentes, er parringen. At GO's "Psykologsamtaler" svarer
til netop dét tilbud under netop dén paragraf er en faglig vurdering, og
den skal en fagperson træffe.

Men det meste af arbejdet er ikke vurdering, det er afskrift. Derfor
forbereder vi arket: GO's værdier står i venstre side, de kandidater
SBSYS tilbyder står i kommentaren, og hvor navnet er entydigt identisk,
er feltet udfyldt på forhånd med en opfordring til at bekræfte.

Der sættes ALDRIG `godkendt_af`. Et automatisk forslag er et forslag.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Kilde:
    """Hvor de to sider af én kodeliste kommer fra."""

    tabeller: tuple[tuple[str, str], ...]   # (tabel, kolonne) i stg
    operation: str | None = None            # endpoint der giver SBSYS-siden
    id_felt: str = "Id"
    navn_felt: str = "Navn"
    bu: bool = False                        # ligger i Børn og Unge-API'et
    faste: tuple[str, ...] = ()             # når listen er en enum i koden


# Lister med sammensatte nøgler, der vedligeholdes i hånden ud fra
# konverteringsarket og derfor ikke kan forberedes fra én stg-kolonne:
#   tilbudsfelt        <mappetype>|<GO-kolonne> -> SBSYS-feltnavn
#   tilbudsfeltvaerdi  <GO-kolonne>|<GO-værdi>  -> SBSYS-valgmulighed
# Se src/konvertering/tilbudsfelter.py.
HAANDHOLDTE = frozenset({"tilbudsfelt", "tilbudsfeltvaerdi"})

# GO's kolonnenavne ender på _go; det er bevidst, så man kan se hvilken
# side af mapningen man står på, når man læser en SELECT.
KILDER: dict[str, Kilde] = {
    "kle": Kilde((("stg.sag", "kle_go"),)),
    "facet": Kilde((("stg.sag", "facet_go"),)),
    "sagstype": Kilde((("stg.sag", "sagstype_go"),)),
    "sagsskabelon": Kilde((("stg.sag", "sagstype_go"),), "hent_skabeloner"),
    "sagsstatus": Kilde((("stg.sag", "status_go"),), "hent_statusser"),
    "afdeling": Kilde((("stg.sag", "afdeling_go"),), "hent_afdelinger"),
    "fortrolighed": Kilde((("stg.sag", "fortrolighed_go"),)),
    "bevaring": Kilde((("stg.sag", "bevaring_go"),)),
    "bruger": Kilde(
        (("stg.sag", "ansvarlig_go"), ("stg.dokument", "forfatter_go"),
         ("stg.notat", "forfatter_go")),
        "soeg_bruger", navn_felt="LogonId",
    ),
    "dokumentkategori": Kilde((("stg.dokument", "kategori_go"),)),
    "notattype": Kilde((("stg.notat", "notat_type_go"),)),
    # Indsatser: ét katalog, tre lister. Samme kald, forskellige felter.
    # Alle tre slås op på mappetypen — tilbuddet ER typen (17-11-2026).
    "paragraf": Kilde((("stg.indsats", "indholdstype_go"),), "hent_katalog",
                      id_felt="paragrafId", navn_felt="paragraf", bu=True),
    "foranstaltning": Kilde((("stg.indsats", "indholdstype_go"),), "hent_katalog",
                            id_felt="foranstaltningId", navn_felt="foranstaltning", bu=True),
    "tilbud": Kilde((("stg.indsats", "indholdstype_go"),), "hent_katalog",
                    id_felt="tilbudId", navn_felt="tilbud", bu=True),
    "udslagsgivende_forhold": Kilde((("stg.indsats", "udslagsgivende_go"),),
                                    "hent_udslagsgivende", bu=True),
    "enhedstype": Kilde((("stg.indsats", "enhedstype_go"),),
                        faste=("Time", "Doegn", "Uge", "Maaned", "Aar", "Stk")),
}

KOLONNER = [
    "liste", "go_vaerdi", "go_tekst", "sbsys_vaerdi", "sbsys_tekst",
    "handling", "kommentar", "godkendt_af", "godkendt_dato",
]


def _noegle(tekst: Any) -> str:
    """Form der kan sammenlignes på tværs af to systemers skrivemåder.

    "PPR Nord", "ppr-nord" og "PPRNORD" er det samme ord for et menneske.
    Det er de ikke for en streng-sammenligning, og hele pointen her er at
    spare mennesket for de sammenligninger den kan lave selv.
    """
    return re.sub(r"[^a-z0-9æøå]", "", str(tekst or "").casefold())


def go_vaerdier(db: Any, kilde: Kilde) -> list[str]:
    """De distinkte værdier GO faktisk bruger — ikke dem det kunne bruge.

    En kodeliste over alt der er muligt er ubrugelig. En over de 23
    værdier der står i data, kan udfyldes på en formiddag.
    """
    fundet: set[str] = set()
    for tabel, kolonne in kilde.tabeller:
        try:
            raekker = db.hent_alle(
                f"SELECT DISTINCT {kolonne} AS v FROM {tabel} "
                f"WHERE {kolonne} IS NOT NULL AND LTRIM(RTRIM({kolonne})) <> ''"
            )
        except Exception as e:  # noqa: BLE001 — en manglende tabel er ikke fatal
            log.debug("%s.%s kunne ikke læses: %s", tabel, kolonne, e)
            continue
        fundet.update(str(r["v"]).strip() for r in raekker)
    return sorted(fundet)


def sbsys_muligheder(raekker: list[dict], kilde: Kilde) -> list[tuple[str, str]]:
    """(id, navn) fra SBSYS-svaret, uden dubletter og i navnerækkefølge."""
    ud = {
        (str(r.get(kilde.id_felt)), str(r.get(kilde.navn_felt) or ""))
        for r in raekker
        if isinstance(r, dict) and r.get(kilde.id_felt) is not None
    }
    return sorted(ud, key=lambda p: p[1].casefold())


def forslag(go_vaerdi: str, muligheder: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Kandidater til én GO-værdi, bedst først.

    Først entydigt samme navn, derefter dem hvor det ene indeholder det
    andet. Ingen fuzzy-afstand: et forslag der ligner lidt, får folk til
    at trykke ja uden at læse, og det er værre end ingen forslag.
    """
    noegle = _noegle(go_vaerdi)
    if not noegle:
        return []
    praecise = [m for m in muligheder if _noegle(m[1]) == noegle]
    if praecise:
        return praecise
    return [
        m for m in muligheder
        if _noegle(m[1]) and (noegle in _noegle(m[1]) or _noegle(m[1]) in noegle)
    ][:3]


def byg_raekker(liste: str, go: list[str], muligheder: list[tuple[str, str]],
                kendte: set[str] | None = None) -> list[dict[str, str]]:
    """Ét ark, klar til en fagperson.

    Args:
        liste: Listenavnet, som også bliver filnavnet.
        go: GO's distinkte værdier.
        muligheder: SBSYS-siden som (id, navn).
        kendte: GO-værdier der allerede er mappet. De tages med, så filen
            er fuldstændig, men markeres, så ingen laver arbejdet igen.

    Returns:
        Rækker i kodelisteformat.
    """
    kendte = kendte or set()
    raekker: list[dict[str, str]] = []
    for vaerdi in go:
        kandidater = forslag(vaerdi, muligheder)
        raekke = {k: "" for k in KOLONNER}
        raekke["liste"] = liste
        raekke["go_vaerdi"] = vaerdi
        raekke["go_tekst"] = vaerdi
        raekke["handling"] = "map"

        if vaerdi in kendte:
            raekke["kommentar"] = "allerede mappet — rør den ikke"
        elif len(kandidater) == 1:
            raekke["sbsys_vaerdi"] = kandidater[0][0]
            raekke["sbsys_tekst"] = kandidater[0][1]
            raekke["kommentar"] = "FORSLAG: navnet er identisk — bekræft"
        elif kandidater:
            raekke["kommentar"] = "kandidater: " + " | ".join(
                f"{i}={n}" for i, n in kandidater
            )
        elif muligheder:
            raekke["kommentar"] = "intet oplagt match — vælg i opslagsfilen"
        else:
            raekke["kommentar"] = "SBSYS-siden kendes ikke; udfyld i hånden"
        raekker.append(raekke)
    return raekker
