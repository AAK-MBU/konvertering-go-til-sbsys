"""Databaseadgang.

Bevidst tyndt: pyodbc + eksplicit SQL. Ingen ORM.
Konverteringen er sætorienteret og SQL-tung; en ORM ville skjule netop de
query-planer vi har brug for at kunne læse, når der er 2 mio. dokumenter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from .config import DatabaseIndstillinger

log = logging.getLogger(__name__)


class SqlScriptFejl(RuntimeError):
    """En sætning i et SQL-script fejlede. Siger hvor."""

    def __init__(self, navn: str, nr: int, i_alt: int, blok: str, aarsag: Exception) -> None:
        linjer = [linje for linje in blok.splitlines() if linje.strip()]
        uddrag = "\n".join(f"    {linje}" for linje in linjer[:6])
        if len(linjer) > 6:
            uddrag += f"\n    ... ({len(linjer) - 6} linjer mere)"
        super().__init__(
            f"{navn}: sætning {nr} af {i_alt} fejlede.\n{uddrag}\n  -> {aarsag}"
        )
        self.navn = navn
        self.nr = nr
        self.blok = blok
        self.aarsag = aarsag


def indstil_tegnsaet(forbindelse: Any, pyodbc: Any) -> None:
    """Danske bogstaver ind og ud af SQL Server — korrekt på Windows OG Linux.

    Tidligere stod alt på UTF-8. På Windows læser ODBC-driveren de bytes som
    Windows-1252, så alt VI skrev, blev gemt forvansket ("Åben" som
    "Ã…ben"), og forvanskningen gik ud igen ved læsning. Det så rigtigt ud,
    indtil der kom ægte data fra GOStaging: "Åben" gav UnicodeDecodeError
    (20-11-2026, tools/tegnsaet_tjek.py).

    * NVARCHAR (SQL_WCHAR) og al tekst vi SENDER: UTF-16LE — SQL Servers
      egen Unicode-form. Ingen omkodning undervejs, på nogen platform.
    * VARCHAR (SQL_CHAR): driveren afleverer i klientens tegnsæt —
      Windows-1252 på Windows, UTF-8 på Linux — så det er det der læses med.
    """
    import locale

    forbindelse.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16le")
    forbindelse.setdecoding(pyodbc.SQL_CHAR, encoding=locale.getpreferredencoding(False))
    forbindelse.setencoding(encoding="utf-16le")


class Database:
    def __init__(self, indstillinger: DatabaseIndstillinger) -> None:
        self._i = indstillinger
        self._forbindelse: Any = None

    def _connect(self) -> Any:
        import pyodbc  # importeres dovent, så tests kan køre uden ODBC-driver

        forbindelse = pyodbc.connect(
            self._i.odbc_connection_string,
            timeout=self._i.timeout_sekunder,
            autocommit=False,
        )
        indstil_tegnsaet(forbindelse, pyodbc)
        return forbindelse

    @property
    def forbindelse(self) -> Any:
        if self._forbindelse is None:
            self._forbindelse = self._connect()
        return self._forbindelse

    @contextmanager
    def transaktion(self) -> Iterator[Any]:
        markoer = self.forbindelse.cursor()
        try:
            yield markoer
            self.forbindelse.commit()
        except Exception:
            self.forbindelse.rollback()
            raise
        finally:
            markoer.close()

    def hent_alle(self, sql: str, *parametre: Any) -> list[dict[str, Any]]:
        with self.forbindelse.cursor() as markoer:
            markoer.execute(sql, *parametre)
            kolonner = [k[0] for k in markoer.description]
            return [dict(zip(kolonner, raekke, strict=False)) for raekke in markoer.fetchall()]

    def hent_i_sider(
        self,
        kilde: str,
        *filtre: str,
        parametre: Sequence[Any] = (),
        graense: int | None = None,
        side: int = 500,
        noeglekolonne: str = "kilde_noegle",
    ) -> Iterator[dict[str, Any]]:
        """Læs et stort resultatsæt i sider, uden en åben cursor undervejs.

        Dette er den eneste sikre måde at læse en arbejdsliste på, når man
        samtidig skriver til databasen: ``udfoer()`` committer, og et commit
        river tæppet væk under en åben cursor på samme forbindelse. Fejlen
        hedder "Function sequence error (SQLFetch)" og rammer først efter
        første side — altså typisk efter 500 rækker i produktion og slet ikke
        i en lille test.

        Sideopdelingen sker på nøglen, ikke med OFFSET. Det er bevidst:
        arbejdslisterne *krymper* mens vi arbejder (indlæste objekter falder
        ud af viewet), og med OFFSET ville vi springe rækker over. Ved at gå
        frem efter nøgle rammer vi hver række præcis én gang — også selv om
        fejlede rækker bliver liggende i viewet.
        """
        sidste = ""
        hentet = 0
        hvor = "".join(filtre)

        while True:
            top = side if graense is None else min(side, graense - hentet)
            if top <= 0:
                return

            raekker = self.hent_alle(
                f"SELECT TOP {int(top)} * FROM {kilde} "
                f"WHERE {noeglekolonne} > ?{hvor} "
                f"ORDER BY {noeglekolonne}",
                sidste,
                *parametre,
            )
            if not raekker:
                return

            for raekke in raekker:
                yield raekke
                hentet += 1

            sidste = raekker[-1][noeglekolonne]

    def stroem(
        self, sql: str, *parametre: Any, batch: int = 500
    ) -> Iterator[dict[str, Any]]:
        """Læs store resultatsæt uden at hive alt i hukommelsen.

        ADVARSEL: cursoren holdes åben mens der itereres. Skriver man til
        databasen undervejs — og ``udfoer()`` committer — dør cursoren med
        "Function sequence error". Skal du både læse og skrive, så brug
        ``hent_i_sider()``.

        Denne er til rene læsninger, fx at fylde en workqueue.
        """
        markoer = self.forbindelse.cursor()
        try:
            markoer.execute(sql, *parametre)
            kolonner = [k[0] for k in markoer.description]
            while True:
                raekker = markoer.fetchmany(batch)
                if not raekker:
                    break
                for raekke in raekker:
                    yield dict(zip(kolonner, raekke, strict=False))
        finally:
            markoer.close()

    def hent_en(self, sql: str, *parametre: Any) -> dict[str, Any] | None:
        raekker = self.hent_alle(sql, *parametre)
        return raekker[0] if raekker else None

    def udfoer(self, sql: str, *parametre: Any) -> int:
        with self.transaktion() as markoer:
            markoer.execute(sql, *parametre)
            return markoer.rowcount

    def udfoer_mange(self, sql: str, raekker: Sequence[Sequence[Any]],
                     hurtig: bool = False) -> int:
        """Kør samme sætning for mange rækker. Returnerer berørte rækker.

        Standard er én række ad gangen, og returværdien er det antal rækker
        databasen FAKTISK ændrede — så "jeg skrev 16 rækker" ikke kan være
        usandt.

        ``hurtig=True`` bruger pyodbc's ``fast_executemany``. Den er farlig
        for alt med et WHERE- eller ON-led: pyodbc binder parametrene efter
        den FØRSTE rækkes typer og længder, og sammenligningen kan
        stilfærdigt matche ingenting — ingen fejl, ingen rækker. Det er set
        to gange i dette projekt: en MERGE i ``mapping load``, og 16-11-2026
        en UPDATE i ``filer scan``, der efterlod fil_status NULL på samtlige
        dokumenter mens loggen meldte et korrekt resultat. I hurtig tilstand
        er returværdien derfor bare antallet af input-rækker, IKKE en
        bekræftelse. Brug den kun til rene INSERT, og kontrollér bagefter.
        """
        if not raekker:
            return 0
        with self.transaktion() as markoer:
            if hurtig:
                markoer.fast_executemany = True
                markoer.executemany(sql, raekker)
                return len(raekker)
            beroert = 0
            for raekke in raekker:
                markoer.execute(sql, *raekke)
                beroert += max(markoer.rowcount, 0)
            return beroert

    def koer_script(self, sql_tekst: str, navn: str = "script",
                    vis_resultater: bool = False) -> list[list[dict[str, Any]]]:
        """Kør et sqlcmd-agtigt script med GO-separatorer.

        Fejler en blok, fortæller undtagelsen hvilken fil, hvilken blok og
        hvilken sætning det drejer sig om. SQL Servers egen besked peger ofte
        på et nøgleord midt i sætningen — fx "Incorrect syntax near 'by'" for
        en kolonne der hedder `by` — og uden konteksten er den svær at placere
        i en fil på 180 linjer.

        ``vis_resultater`` henter rækkerne fra de blokke der SELECTer. Uden
        den kørte et rent rapportscript uden at vise noget som helst — og
        et script hvis eneste formål er at svare på et spørgsmål, skal
        svare.

        Returns:
            Ét resultatsæt pr. blok der returnerede rækker. Tom liste når
            scriptet kun skriver.
        """
        blokke = [b.strip() for b in _split_go(sql_tekst) if b.strip()]
        resultater: list[list[dict[str, Any]]] = []
        with self.transaktion() as markoer:
            for nr, blok in enumerate(blokke, start=1):
                try:
                    markoer.execute(blok)
                    if not vis_resultater:
                        continue
                    # Én blok kan indeholde flere SELECTs. nextset() tager
                    # dem i rækkefølge; description er None for en blok der
                    # kun skriver.
                    while True:
                        if markoer.description:
                            kolonner = [k[0] for k in markoer.description]
                            resultater.append(
                                [dict(zip(kolonner, r, strict=False))
                                 for r in markoer.fetchall()]
                            )
                        if not markoer.nextset():
                            break
                except Exception as e:
                    raise SqlScriptFejl(navn, nr, len(blokke), blok, e) from e
        return resultater

    def luk(self) -> None:
        if self._forbindelse is not None:
            self._forbindelse.close()
            self._forbindelse = None


def _split_go(sql_tekst: str) -> Iterable[str]:
    aktuel: list[str] = []
    for linje in sql_tekst.splitlines():
        if linje.strip().upper() == "GO":
            yield "\n".join(aktuel)
            aktuel = []
        else:
            aktuel.append(linje)
    yield "\n".join(aktuel)
