"""ID-map: garantien mod dubletter.

Det væsentligste design i hele konverteringen. Reglen er:

    Intet objekt sendes til SBSYS uden at der FØRST er reserveret en række
    i konv.id_map, og ingen række kan reserveres to gange (unik constraint).

Konsekvens: kørslen kan afbrydes hvor som helst — strømsvigt, timeout,
ctrl-c, worker der dør — og genstartes uden at der opstår dubletter i SBSYS.

Der findes ét scenarie id_map ikke alene kan dække: kaldet når frem til
SBSYS, objektet oprettes, men svaret går tabt (netværk dør). Derfor skriver
loaderne desuden kilde_nøglen med ind i et felt på objektet i SBSYS, så vi
kan genfinde forældreløse objekter. Se docs/adr/ADR-0001-idempotens.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .db import Database
from .models import Objekttype
from .sbsys.errors import KonverteringsFejl

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdMapPost:
    objekt_type: str
    kilde_noegle: str
    sbsys_id: str | None
    sbsys_nummer: str | None
    payload_hash: str | None
    status: str
    forsoeg: int
    boelge: str | None = None


class IdMap:
    def __init__(self, db: Database, kilde_system: str = "GO") -> None:
        self._db = db
        self._kilde = kilde_system

    # -- opslag -------------------------------------------------------

    def hent(self, objekt_type: Objekttype | str, kilde_noegle: str) -> IdMapPost | None:
        raekke = self._db.hent_en(
            """
            SELECT objekt_type, kilde_noegle, sbsys_id, sbsys_nummer,
                   payload_hash, status, forsoeg, boelge
            FROM   konv.id_map
            WHERE  kilde_system = ? AND objekt_type = ? AND kilde_noegle = ?
            """,
            self._kilde,
            str(objekt_type),
            kilde_noegle,
        )
        return IdMapPost(**raekke) if raekke else None

    def sbsys_id(self, objekt_type: Objekttype | str, kilde_noegle: str) -> str | None:
        post = self.hent(objekt_type, kilde_noegle)
        return post.sbsys_id if post and post.status == "loaded" else None

    def kendte_sbsys_id(self, objekt_type: Objekttype | str, sbsys_ider: list[str]) -> set[str]:
        """Hvilke af disse SBSYS-id'er har konverteringen selv registreret?

        Bruges når et opslag i SBSYS finder flere ens objekter: dem vi
        kender, er allerede oprettet af os og kan ikke være det tabte.
        """
        if not sbsys_ider:
            return set()
        pladser = ", ".join("?" for _ in sbsys_ider)
        raekker = self._db.hent_alle(
            f"""
            SELECT sbsys_id FROM konv.id_map
            WHERE  kilde_system = ? AND objekt_type = ? AND sbsys_id IN ({pladser})
            """,
            self._kilde,
            str(objekt_type),
            *sbsys_ider,
        )
        return {str(r["sbsys_id"]) for r in raekker}

    # -- reservation og resultat --------------------------------------

    def reserver(
        self,
        objekt_type: Objekttype | str,
        kilde_noegle: str,
        payload_hash: str,
        run_id: int | None = None,
        boelge: str | None = None,
    ) -> IdMapPost:
        """Opret eller genbrug en række, og tæl forsøg op.

        MERGE i én sætning, så to samtidige workers ikke kan oprette hver sin
        række. Unik-constrainten er sidste værn, men MERGE sparer os for at
        skulle fange constraint-violations i normaldrift.

        ``boelge`` er rapporteringsdimension og indgår bevidst ikke i
        matchningen: flyttes en sag mellem to bølger undervejs, må den stadig
        kun oprettes én gang i SBSYS.
        """
        self._db.udfoer(
            """
            MERGE konv.id_map WITH (HOLDLOCK) AS t
            USING (SELECT ? AS kilde_system, ? AS objekt_type, ? AS kilde_noegle) AS s
              ON  t.kilde_system = s.kilde_system
              AND t.objekt_type  = s.objekt_type
              AND t.kilde_noegle = s.kilde_noegle
            WHEN MATCHED AND t.status <> 'loaded' THEN
                UPDATE SET forsoeg = t.forsoeg + 1,
                           payload_hash = ?,
                           run_id = ?,
                           boelge = COALESCE(?, t.boelge),
                           opdateret = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (kilde_system, objekt_type, kilde_noegle, payload_hash,
                        status, forsoeg, run_id, boelge)
                VALUES (s.kilde_system, s.objekt_type, s.kilde_noegle, ?, 'pending', 1, ?, ?);
            """,
            self._kilde,
            str(objekt_type),
            kilde_noegle,
            payload_hash,
            run_id,
            boelge,
            payload_hash,
            run_id,
            boelge,
        )
        post = self.hent(objekt_type, kilde_noegle)
        assert post is not None
        return post

    def markér_indlaest(
        self,
        objekt_type: Objekttype | str,
        kilde_noegle: str,
        sbsys_id: str,
        sbsys_nummer: str | None = None,
    ) -> None:
        self._db.udfoer(
            """
            UPDATE konv.id_map
            SET    sbsys_id = ?, sbsys_nummer = ?, status = 'loaded',
                   sidste_fejl = NULL, opdateret = SYSUTCDATETIME()
            WHERE  kilde_system = ? AND objekt_type = ? AND kilde_noegle = ?
            """,
            sbsys_id,
            sbsys_nummer,
            self._kilde,
            str(objekt_type),
            kilde_noegle,
        )

    def markér_fejlet(
        self,
        objekt_type: Objekttype | str,
        kilde_noegle: str,
        fejl: str,
        manuel: bool = False,
    ) -> None:
        self._db.udfoer(
            """
            UPDATE konv.id_map
            SET    status = ?, sidste_fejl = ?, opdateret = SYSUTCDATETIME()
            WHERE  kilde_system = ? AND objekt_type = ? AND kilde_noegle = ?
            """,
            "manual" if manuel else "failed",
            fejl[:2000],
            self._kilde,
            str(objekt_type),
            kilde_noegle,
        )

    def markér_sprunget_over(
        self, objekt_type: Objekttype | str, kilde_noegle: str, aarsag: str
    ) -> None:
        self._db.udfoer(
            """
            UPDATE konv.id_map
            SET    status = 'skipped', sidste_fejl = ?, opdateret = SYSUTCDATETIME()
            WHERE  kilde_system = ? AND objekt_type = ? AND kilde_noegle = ?
            """,
            aarsag[:2000],
            self._kilde,
            str(objekt_type),
            kilde_noegle,
        )

    def nulstil(
        self,
        objekt_type: Objekttype | str | None = None,
        boelge: str | None = None,
        kilde_noegle: str | None = None,
        regel: str | None = None,
        status: str | None = None,
        ogsaa_indlaeste: bool = False,
    ) -> int:
        """Sæt rækker tilbage til 'pending', så de kommer med i næste kørsel.

        Bruges når en mapning er rettet, eller når et testobjekt er slettet
        manuelt i SBSYS og skal indlæses igen.

        Indlæste rækker nulstilles kun med ``ogsaa_indlaeste=True``. Grunden
        er alvorlig: en nulstillet 'loaded'-række får næste kørsel til at
        oprette objektet igen. Findes det stadig i SBSYS, er resultatet en
        dublet — og i et system med børnesager er det ikke en skønhedsfejl.
        """
        if not any((objekt_type, boelge, kilde_noegle, regel, status)):
            raise KonverteringsFejl(
                "Angiv mindst ét filter. At nulstille alt ved et uheld er "
                "for let, og konsekvensen er dubletter i SBSYS."
            )

        hvor = ["kilde_system = ?"]
        params: list[object] = [self._kilde]

        if objekt_type:
            hvor.append("objekt_type = ?")
            params.append(str(objekt_type))
        if boelge:
            hvor.append("boelge = ?")
            params.append(boelge)
        if kilde_noegle:
            hvor.append("kilde_noegle = ?")
            params.append(kilde_noegle)
        if status:
            hvor.append("status = ?")
            params.append(status)
        if not ogsaa_indlaeste:
            hvor.append("status <> 'loaded'")
        if regel:
            hvor.append(
                "EXISTS (SELECT 1 FROM konv.afvist a "
                "WHERE a.kilde_noegle = konv.id_map.kilde_noegle "
                "AND a.objekt_type = konv.id_map.objekt_type AND a.regel = ?)"
            )
            params.append(regel)

        return self._db.udfoer(
            f"""
            UPDATE konv.id_map
            SET    status = 'pending', sidste_fejl = NULL,
                   opdateret = SYSUTCDATETIME()
            WHERE  {' AND '.join(hvor)}
            """,
            *params,
        )

    # -- beslutning ---------------------------------------------------

    def skal_indlaeses(
        self, objekt_type: Objekttype | str, kilde_noegle: str, payload_hash: str
    ) -> tuple[bool, str]:
        """Returnerer (skal_indlæses, begrundelse).

        Begrundelsen logges, så det altid kan forklares hvorfor et objekt
        blev sprunget over — det spørgsmål kommer garanteret fra forretningen.
        """
        post = self.hent(objekt_type, kilde_noegle)
        if post is None:
            return True, "ny"
        if post.status == "loaded":
            if post.payload_hash == payload_hash:
                return False, "allerede indlæst, uændret"
            return False, "allerede indlæst, men kildedata er ændret — kræver stillingtagen"
        if post.status == "manual":
            return False, "sat til manuel håndtering"
        if post.status == "skipped":
            return False, "bevidst sprunget over"
        return True, f"genforsøg (forsøg {post.forsoeg + 1})"


def opret_run(
    db: Database,
    fase: str,
    miljoe: str,
    dry_run: bool,
    objekt_type: str | None = None,
    kodeversion: str | None = None,
    as_session_id: str | None = None,
    boelge: str | None = None,
    maal: str | None = None,
) -> int:
    raekke = db.hent_en(
        """
        INSERT INTO konv.run
            (fase, objekt_type, boelge, miljoe, dry_run, kodeversion, as_session_id, maal)
        OUTPUT INSERTED.run_id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        fase,
        objekt_type,
        boelge,
        miljoe,
        1 if dry_run else 0,
        kodeversion,
        as_session_id,
        maal,
    )
    return int(raekke["run_id"])  # type: ignore[index]


def tidligere_maal(db: Database, objekt_type: str | None = None,
                   boelge: str | None = None) -> list[dict]:
    """Hvilke SBSYS-instanser er der allerede indlæst til?

    Kun rigtige kørsler tælles med (``dry_run = 0``): en dry-run har pr.
    definition ikke oprettet noget nogen steder.
    """
    hvor = ["m.status = 'loaded'", "r.dry_run = 0", "r.maal IS NOT NULL"]
    params: list = []
    if objekt_type:
        hvor.append("m.objekt_type = ?")
        params.append(objekt_type)
    if boelge:
        hvor.append("m.boelge = ?")
        params.append(boelge)
    return db.hent_alle(
        f"""
        SELECT r.maal, COUNT(*) AS antal, MAX(m.opdateret) AS senest
        FROM   konv.id_map m
        JOIN   konv.run r ON r.run_id = m.run_id
        WHERE  {' AND '.join(hvor)}
        GROUP  BY r.maal
        ORDER  BY antal DESC
        """,
        *params,
    )


def afslut_run(db: Database, run_id: int, status: str = "ok", note: str | None = None) -> None:
    db.udfoer(
        """
        UPDATE konv.run
        SET    afsluttet = SYSUTCDATETIME(), status = ?, note = ?
        WHERE  run_id = ?
        """,
        status,
        note,
        run_id,
    )


def oprettede_objekter(
    db: Database,
    boelge: str | None = None,
    run_id: int | None = None,
    objekt_type: str | None = None,
    kilde_system: str = "GO",
) -> list[dict]:
    """Alt hvad en kørsel faktisk oprettede i SBSYS.

    Findes fordi testkonverteringen køres ind i PRODUKTION og skal kunne
    fjernes igen bagefter. Uden en liste er alternativet at lede efter
    konverterede sager i hånden blandt rigtige sager — og at gætte forkert
    der betyder enten en overlevende testsag i produktion eller en slettet
    rigtig børnesag.

    Rækkefølgen er omvendt af oprettelsen: indsatser, notater og dokumenter
    før sagerne. Et barn kan ikke slettes efter sin forælder.
    """
    hvor = ["kilde_system = ?", "status = 'loaded'", "sbsys_id IS NOT NULL",
            "sbsys_id <> ''"]
    params: list[object] = [kilde_system]
    if boelge:
        hvor.append("boelge = ?")
        params.append(boelge)
    if run_id is not None:
        hvor.append("run_id = ?")
        params.append(run_id)
    if objekt_type:
        hvor.append("objekt_type = ?")
        params.append(objekt_type)

    return db.hent_alle(
        f"""
        SELECT objekt_type, kilde_noegle, sbsys_id, sbsys_nummer, boelge, run_id, opdateret
        FROM   konv.id_map
        WHERE  {' AND '.join(hvor)}
        ORDER  BY CASE objekt_type
                      WHEN 'indsats'  THEN 1
                      WHEN 'notat'    THEN 2
                      WHEN 'dokument' THEN 3
                      WHEN 'sag'      THEN 4
                      ELSE 5
                  END,
                  opdateret DESC
        """,
        *params,
    )


# Tabeller i vores EGEN database, i den rækkefølge de kan tømmes.
# Børn før forældre: en fremmednøgle gør rækkefølgen bindende, og en
# TRUNCATE duer ikke på en tabel der peges på.
#
# konv.boelge og map.kodeliste står IKKE på listen. Bølgerne er seedet
# fra konverteringsplanen, og kodelisterne er fagpersonernes arbejde —
# ingen af delene har noget med et dump at gøre, og begge dele er dyre
# at lave om. Kodelisterne kan ryddes særskilt, hvis nogen beder om det.
NULSTIL_TABELLER = (
    "konv.afstemning",
    "konv.api_kald",
    "konv.afvist",
    "konv.id_map",
    "konv.run",
    "stg.sag_part",
    "stg.indsats",
    "stg.notat",
    "stg.dokument",
    "stg.part",
    "stg.sag",
)


def antal_raekker(db: Database, tabeller: tuple[str, ...] = NULSTIL_TABELLER) -> dict[str, int]:
    """Hvor meget står der i hver tabel? Grundlaget for at spørge før man sletter."""
    talt: dict[str, int] = {}
    for tabel in tabeller:
        raekker = db.hent_alle(f"SELECT COUNT(*) AS n FROM {tabel}")  # noqa: S608
        talt[tabel] = int(raekker[0]["n"]) if raekker else 0
    return talt


def nulstil_database(
    db: Database,
    ogsaa_mapning: bool = False,
) -> dict[str, int]:
    """Tøm konverteringsdatabasen, så den kan bruges til næste dump.

    Vores egen database — ikke SBSYS. Værktøjet rører aldrig ved SBSYS'
    indhold; oprydningen dér håndteres af andre.

    Kaldes når et nyt dump skal ind i et rent udgangspunkt: testdumpets
    rækker må ikke blandes med det endelige. Uden nulstillingen ville
    ``konv.id_map`` stadig sige 'loaded' om objekter der ikke længere
    findes, og konverteringen ville springe dem over.

    Returns:
        Antal slettede rækker pr. tabel.
    """
    slettet: dict[str, int] = {}
    for tabel in NULSTIL_TABELLER:
        slettet[tabel] = db.udfoer(f"DELETE FROM {tabel}")  # noqa: S608
    if ogsaa_mapning:
        slettet["map.kodeliste"] = db.udfoer("DELETE FROM map.kodeliste")
    return slettet
