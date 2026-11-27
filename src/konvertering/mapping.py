"""Kodelister: oversættelse fra GO-værdier til SBSYS-værdier.

Ejerskabet ligger hos forretningen, ikke hos udviklerne. Derfor:

* Kildeformatet er CSV i mapping/ — kan redigeres i Excel, reviewes i en
  pull request, og har en synlig historik over hvem der besluttede hvad.
* ``konv mapping load`` indlæser CSV'erne i map.kodeliste.
* ``konv mapping mangler`` viser hvad der endnu ikke er taget stilling til.
* En manglende mapping er en BLOKERENDE fejl, ikke en advarsel. Vi gætter
  aldrig på hvilken KLE-kode en børnesag skal ligge under.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .sbsys.errors import ManglerMapping

log = logging.getLogger(__name__)

KOLONNER = ["liste", "go_vaerdi", "go_tekst", "sbsys_vaerdi", "sbsys_tekst",
            "handling", "kommentar", "godkendt_af", "godkendt_dato"]

# Et listenavn bliver til et filnavn. Bevidst snævert: kodelisterne hedder
# kle, afdeling, sagsskabelon — og et navn der ikke kan blive en fil, er en
# fejl i dataene, ikke noget der skal rettes op på i stilhed.
_FILNAVN = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class Opslag:
    sbsys_vaerdi: str | None
    handling: str


class Kodelister:
    """Læses én gang ved kørslens start og holdes i hukommelsen.

    Kodelisterne er små (tusindvis af rækker), og et databaseopslag pr. felt
    pr. objekt ville koste millioner af roundtrips på en fuld konvertering.
    """

    def __init__(self, poster: dict[tuple[str, str], Opslag]) -> None:
        self._poster = poster

    @classmethod
    def fra_db(cls, db: Database) -> Kodelister:
        raekker = db.hent_alle(
            "SELECT liste, go_vaerdi, sbsys_vaerdi, handling FROM map.kodeliste WHERE gyldig = 1"
        )
        poster = {
            (r["liste"], r["go_vaerdi"]): Opslag(r["sbsys_vaerdi"], r["handling"])
            for r in raekker
        }
        log.info("Indlæste %d mapping-poster", len(poster))
        return cls(poster)

    def slaa_op(self, liste: str, go_vaerdi: str | None, *, paakraevet: bool = True) -> str | None:
        """Oversæt én værdi.

        ``paakraevet=False`` bruges til felter hvor tomt er acceptabelt.
        Alt andet kaster ManglerMapping, så objektet lander i afvisnings-
        rapporten frem for i SBSYS med en forkert værdi.
        """
        if go_vaerdi is None or not str(go_vaerdi).strip():
            if paakraevet:
                raise ManglerMapping(liste, "<tom>")
            return None

        post = self._poster.get((liste, str(go_vaerdi)))
        if post is None:
            if paakraevet:
                raise ManglerMapping(liste, str(go_vaerdi))
            return None

        if post.handling == "ignorer":
            return None
        if post.handling == "bloker":
            raise ManglerMapping(liste, str(go_vaerdi))
        if not post.sbsys_vaerdi:
            if paakraevet:
                raise ManglerMapping(liste, str(go_vaerdi))
            return None
        return post.sbsys_vaerdi

    def findes(self, liste: str, go_vaerdi: str) -> bool:
        """Har listen en række med netop denne GO-værdi (uanset handling)?"""
        return (liste, go_vaerdi) in self._poster

    def poster_med_praefiks(self, liste: str, praefiks: str) -> dict[str, Opslag]:
        """Alle poster i listen hvis GO-værdi starter med præfikset —
        nøglet på resten af værdien. Til sammensatte nøgler som
        ``<mappetype>|<GO-kolonne>`` i ``tilbudsfelt``."""
        return {go[len(praefiks):]: post for (lst, go), post in self._poster.items()
                if lst == liste and go.startswith(praefiks)}

    def er_udeladt(self, liste: str, go_vaerdi: str | None) -> bool:
        """Er værdien mappet med ``handling = ignorer``?

        Til objekter der bevidst ikke skal konverteres — fx foranstaltnings-
        typer hvor konverteringsarket siger "data skal IKKE konverteres".
        Det er en beslutning, ikke en fejl, og objektet skal springes over
        frem for at havne i afvisningsrapporten.
        """
        if go_vaerdi is None:
            return False
        post = self._poster.get((liste, str(go_vaerdi)))
        return post is not None and post.handling == "ignorer"

    def __len__(self) -> int:
        return len(self._poster)


# Testmiljøets egne id'er og feltnavne ligger i mapping/testmiljoe/ og
# mærkes med dette i kommentaren, så de kan genkendes i map.kodeliste.
TESTMAPPE = "testmiljoe"
TESTMAERKE = "TESTMILJØ"


def indlaes_mapping(db: Database, mappe: Path, miljoe: str) -> int:
    """Indlæs kodelisterne til det miljø kørslen er sat op til (KONV_MILJOE).

    * ``mapping/*.csv`` er PRODUKTIONENS værdier og indlæses altid.
    * ``mapping/testmiljoe/*.csv`` er testmiljøets afvigelser — andre id'er og
      feltnavne (fx "ABA gruppe1" for "ABA gruppe"). De lægges OVENPÅ, men kun
      når miljøet er test.
    * I produktion deaktiveres testrækker der står tilbage (gyldig = 0), så
      ingen test-id kan slippe med i en produktionskørsel.

    Før 18-11-2026 lå testværdierne i SQL-filer man skulle huske at køre
    efter `mapping load` — og huske IKKE at køre før en produktionskørsel.
    """
    antal = indlaes_csv(db, mappe)
    testmappe = mappe / TESTMAPPE
    if miljoe == "test":
        if testmappe.is_dir():
            antal += indlaes_csv(db, testmappe, kommentar_praefiks=TESTMAERKE)
        return antal
    slaaet_fra = db.udfoer(
        "UPDATE map.kodeliste SET gyldig = 0 WHERE gyldig = 1 AND kommentar LIKE ?",
        f"{TESTMAERKE}%",
    )
    if slaaet_fra:
        log.warning("%d testmiljø-rækker deaktiveret (miljø: %s)", slaaet_fra, miljoe)
    return antal


def aktive_testvaerdier(db: Database) -> int:
    """Antal aktive kodelisterækker der kommer fra testmiljøet."""
    raekke = db.hent_en(
        "SELECT COUNT(*) AS antal FROM map.kodeliste WHERE gyldig = 1 AND kommentar LIKE ?",
        f"{TESTMAERKE}%",
    )
    return int((raekke or {}).get("antal") or 0)


def indlaes_csv(db: Database, mappe: Path, *, kommentar_praefiks: str | None = None) -> int:
    """Indlæs alle CSV-filer i mappen til map.kodeliste.

    Filnavnet er listenavnet: mapping/kle.csv -> liste='kle'.
    Eksisterende rækker opdateres, nye tilføjes; ingen slettes automatisk —
    en mapping der forsvinder fra CSV'en skal fjernes bevidst.

    ``kommentar_praefiks`` sættes foran hver kommentar (testmiljøets mærke).
    """
    i_alt = 0
    for fil in sorted(mappe.glob("*.csv")):
        liste = fil.stem
        raekker: list[tuple] = []
        with fil.open(encoding="utf-8-sig", newline="") as f:
            for raekke in csv.DictReader(f, delimiter=";"):
                raekker.append(
                    (
                        liste,
                        (raekke.get("go_vaerdi") or "").strip(),
                        (raekke.get("go_tekst") or "").strip() or None,
                        (raekke.get("sbsys_vaerdi") or "").strip() or None,
                        (raekke.get("sbsys_tekst") or "").strip() or None,
                        (raekke.get("handling") or "map").strip(),
                        _kommentar(raekke.get("kommentar"), kommentar_praefiks),
                        (raekke.get("godkendt_af") or "").strip() or None,
                        (raekke.get("godkendt_dato") or "").strip() or None,
                    )
                )
        if not raekker:
            continue
        # hurtig=False: en MERGE med fast_executemany kan matche ingenting
        # uden at fejle. Kodelisterne er små, så prisen er ligegyldig.
        beroert = db.udfoer_mange(
            """
            MERGE map.kodeliste AS t
            USING (SELECT ? AS liste, ? AS go_vaerdi) AS s
              ON t.liste = s.liste AND t.go_vaerdi = s.go_vaerdi
            WHEN MATCHED THEN UPDATE SET
                go_tekst = ?, sbsys_vaerdi = ?, sbsys_tekst = ?,
                handling = ?, kommentar = ?, godkendt_af = ?, godkendt_dato = ?,
                gyldig = 1
            WHEN NOT MATCHED THEN INSERT
                (liste, go_vaerdi, go_tekst, sbsys_vaerdi, sbsys_tekst,
                 handling, kommentar, godkendt_af, godkendt_dato)
                VALUES (s.liste, s.go_vaerdi, ?, ?, ?, ?, ?, ?, ?);
            """,
            [(r[0], r[1], *r[2:], *r[2:]) for r in raekker],
            hurtig=False,
        )
        if beroert < len(raekker):
            log.warning(
                "%s: %d rækker i filen, men kun %d blev skrevet. Tjek "
                "map.kodeliste — en række der hverken fejler eller lander "
                "er næsten altid en tom go_vaerdi eller en forkert separator "
                "(filen skal bruge semikolon)",
                fil.name, len(raekker), beroert,
            )
        log.info("Indlæste %d rækker fra %s (%d skrevet)", len(raekker), fil.name, beroert)
        i_alt += beroert
    return i_alt


def _kommentar(tekst: str | None, praefiks: str | None) -> str | None:
    tekst = (tekst or "").strip()
    if not praefiks:
        return tekst or None
    return tekst if tekst.startswith(praefiks) else f"{praefiks}: {tekst}".rstrip(": ")


def skriv_mappe(db: Database, mappe: Path) -> dict[str, int]:
    """Skriv map.kodeliste tilbage til én CSV pr. liste.

    Modstykket til ``indlaes_csv``. Filerne er kilden, databasen er kopien
    — men kopien overlever, når en fil bliver overskrevet af en udpakning,
    slettet ved en fejl eller aldrig committet. Uden vejen tilbage er en
    fagpersons formiddagsarbejde væk, selv om værdierne står i databasen
    hele tiden.

    Returns:
        Antal rækker pr. liste, så det kan siges højt hvad der blev skrevet.
    """
    raekker = db.hent_alle(
        f"SELECT {', '.join(KOLONNER)} FROM map.kodeliste ORDER BY liste, go_vaerdi"
    )
    pr_liste: dict[str, list[dict]] = {}
    for r in raekker:
        pr_liste.setdefault(str(r["liste"]), []).append(r)

    mappe.mkdir(parents=True, exist_ok=True)
    talt: dict[str, int] = {}
    ugyldige: list[str] = []
    for liste, dens_raekker in pr_liste.items():
        # Filnavnet ER listenavnet — se mapping/README.md. Derfor må navnet
        # kun indeholde det et filnavn kan bære på alle platforme. Windows
        # afviser ? * : < > | " og skråstreger, og et navn der begynder med
        # punktum er skjult. Et navn som "?" er typisk et tabt æøå: en
        # INSERT uden N-præfiks gør 'bølge' til 'b?lge' på en kodeside uden
        # ø. Derfor nævnes den rå værdi — den er selve diagnosen.
        if not _FILNAVN.fullmatch(liste):
            ugyldige.append(liste)
            log.warning(
                "Springer listen %r over: navnet kan ikke blive et filnavn. "
                "Et '?' i navnet er som regel et tabt æ/ø/å fra en INSERT "
                "uden N-præfiks — ret rækken i map.kodeliste", liste)
            continue
        sti = mappe / f"{liste}.csv"
        with sti.open("w", encoding="utf-8-sig", newline="") as f:
            skriver = csv.DictWriter(f, fieldnames=KOLONNER, delimiter=";",
                                     extrasaction="ignore")
            skriver.writeheader()
            for r in dens_raekker:
                skriver.writerow({k: "" if r.get(k) is None else r[k] for k in KOLONNER})
        talt[liste] = len(dens_raekker)

    if ugyldige:
        log.warning("%d listenavn(e) kunne ikke skrives: %s. Find dem med: "
                    "SELECT DISTINCT liste FROM map.kodeliste",
                    len(ugyldige), ", ".join(repr(n) for n in ugyldige))
    return talt


def skriv_mangler_csv(db: Database, sti: Path) -> int:
    """Eksportér uafklarede værdier, så de kan sendes til fagpersonerne."""
    raekker = db.hent_alle(
        "SELECT liste, go_vaerdi, go_tekst, kommentar FROM map.v_mangler ORDER BY liste, go_vaerdi"
    )
    sti.parent.mkdir(parents=True, exist_ok=True)
    with sti.open("w", encoding="utf-8-sig", newline="") as f:
        skriver = csv.DictWriter(f, fieldnames=KOLONNER, delimiter=";")
        skriver.writeheader()
        for r in raekker:
            skriver.writerow({**{k: "" for k in KOLONNER}, **r, "handling": "map"})
    return len(raekker)
