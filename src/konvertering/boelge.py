"""Bølger.

Konverteringen leveres i ni portioner — PPR, Børneterapien, skolerne,
dagtilbuddene, klubberne, borgersagerne og emnesagerne — hver med sit eget datadump
fra Netcompany og sin egen undervisning bagefter.

Alle ni er sager. Bølgen er derfor et *filter*, ikke en objekttype: en
kolonne i ``stg.*`` og et ``WHERE`` i arbejdslisten. Der er ingen
bølge-specifik loader og skal ikke være det.

Den vigtigste funktion i denne fil er ``valider``. En stavefejl i en
bølgekode giver ellers en kørsel der behandler nul rækker, rapporterer nul
fejl og ser fuldstændig grøn ud — den farligste slags fejl vi kan have,
fordi den bliver opdaget efter undervisningen i stedet for før.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import Database
from .sbsys.errors import KonverteringsFejl

log = logging.getLogger(__name__)


class UkendtBoelge(KonverteringsFejl):
    def __init__(self, boelge: str, kendte: list[str]) -> None:
        super().__init__(
            f"Ukendt bølge '{boelge}'. Gyldige: {', '.join(kendte) or '(ingen oprettet)'}"
        )
        self.boelge = boelge
        self.kendte = kendte


class TomBoelge(KonverteringsFejl):
    """Bølgen findes, men der er intet at indlæse i den.

    Det er næsten altid en fejl i bølgetildelingen (extract-laget) og aldrig
    noget vi bare skal køre videre forbi.
    """


@dataclass(frozen=True)
class Boelge:
    boelge: str
    navn: str
    raekkefoelge: int
    planlagt_uge: str | None
    status: str


def hent_alle(db: Database) -> list[Boelge]:
    raekker = db.hent_alle(
        "SELECT boelge, navn, raekkefoelge, planlagt_uge, status "
        "FROM konv.boelge ORDER BY raekkefoelge"
    )
    return [Boelge(**r) for r in raekker]


def valider(db: Database, boelge: str | None) -> str | None:
    """Kontrollér bølgekoden før en kørsel går i gang.

    Returnerer koden uændret, eller None hvis der køres på tværs af bølger.
    Kaster ``UkendtBoelge`` ved en kode der ikke findes — bevidst en
    hård fejl, ikke en advarsel.
    """
    if boelge is None:
        return None

    kendte = [b.boelge for b in hent_alle(db)]
    if boelge not in kendte:
        raise UkendtBoelge(boelge, kendte)
    return boelge


def antal_til_indlaesning(db: Database, objekt_type: str, boelge: str | None) -> int:
    """Hvor mange objekter mangler indlæsning?

    Viewets navn hentes fra loaderen selv. Da det lå i en tabel her OGSÅ,
    blev den glemt da notat-loaderen kom til, og `konv load notat` faldt
    med en KeyError før den overhovedet nåede at prøve.
    """
    from .loaders import LOADERS

    try:
        view = LOADERS[objekt_type].arbejdsliste
    except KeyError:
        raise KonverteringsFejl(
            f"Ukendt objekttype '{objekt_type}'. Kendte: {', '.join(sorted(LOADERS))}"
        ) from None
    sql = f"SELECT COUNT(*) AS antal FROM {view}"
    if boelge:
        sql += " WHERE boelge = ?"
        raekke = db.hent_en(sql, boelge)
    else:
        raekke = db.hent_en(sql)
    return int(raekke["antal"]) if raekke else 0


def kraev_arbejde(db: Database, objekt_type: str, boelge: str | None) -> int:
    """Stop før kørslen hvis der ikke er noget at lave.

    En tom bølge ligner en vellykket kørsel i enhver rapport. Den skal
    derfor sige fra højlydt, så nogen kigger på bølgetildelingen frem for
    at notere 'ingen fejl' og gå videre til undervisningen.
    """
    antal = antal_til_indlaesning(db, objekt_type, boelge)
    if antal != 0 or boelge is None:
        return antal

    # "Ingen til indlæsning" har to vidt forskellige årsager, og at forveksle
    # dem koster en eftermiddag: enten er bølgen aldrig blevet tildelt noget,
    # eller også er den færdig. Tæl efter i stedet for at gætte.
    raekke = db.hent_en(
        """
        SELECT SUM(CASE WHEN status = 'loaded' THEN 1 ELSE 0 END) AS indlaest,
               COUNT(*) AS i_alt
        FROM   konv.id_map
        WHERE  objekt_type = ? AND boelge = ?
        """,
        objekt_type, boelge,
    )
    indlaest = int((raekke or {}).get("indlaest") or 0)
    if indlaest:
        raise TomBoelge(
            f"Bølgen '{boelge}' har ingen {objekt_type}-objekter TILBAGE: "
            f"alle {indlaest} er allerede indlæst. Det er ikke en fejl — "
            f"arbejdslisten tømmes efterhånden som objekter får status "
            f"'loaded'. Se dem med:\n"
            f"  SELECT kilde_noegle, sbsys_id, sbsys_nummer FROM konv.id_map "
            f"WHERE objekt_type = '{objekt_type}' AND boelge = '{boelge}';\n"
            f"Skal de køres igen (fx fordi de blev indlæst i attrappen), så "
            f"nulstil først: konv nulstil --objekt-type {objekt_type} "
            f"--boelge {boelge} --ogsaa-indlaeste"
        )
    raise TomBoelge(
        f"Bølgen '{boelge}' har ingen {objekt_type}-objekter til indlæsning, "
        f"og ingen er indlæst. Tjek bølgetildelingen i extract-laget og "
        f"stg.v_boelge_mangler."
    )


def markér_afstemt(db: Database, boelge: str, af: str) -> None:
    db.udfoer(
        """
        UPDATE konv.boelge
        SET    status = 'afstemt', afstemt_af = ?, afstemt_dato = SYSUTCDATETIME()
        WHERE  boelge = ?
        """,
        af,
        boelge,
    )


def gate_status(db: Database, boelge: str | None = None) -> list[dict]:
    """Er bølgen klar til undervisning? Ét opslag, ét svar."""
    sql = "SELECT * FROM konv.v_boelge_gate"
    if boelge:
        return db.hent_alle(sql + " WHERE boelge = ?", boelge)
    return db.hent_alle(sql + " ORDER BY boelge")
