"""Dokumentfiler fra GO's fileshare-eksport.

Filerne leveres som en mappestruktur ved siden af bacpac'en. Det er
erfaringsmæssigt den del der giver flest overraskelser:

* stier i basen matcher ikke altid dem på disken (æøå, mellemrum, /, %20)
* filnavne fra SharePoint kan være længere end Windows' 260-tegns grænse
* der er filer i basen som ikke findes på disken, og omvendt
* nogle filer er 0 bytes

Derfor er der en separat scanne-fase FØR indlæsning: vi finder, måler og
checksummer alle filer og skriver resultatet i stg.dokument. Så ved vi
præcis hvor mange dokumenter der er i problemer, inden vi rører SBSYS.
"""

from __future__ import annotations

import logging
import mimetypes
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import FilIndstillinger
from .hashing import fil_sha256
from .sbsys.errors import FilFejl

log = logging.getLogger(__name__)

FILSTATUS_OK = "ok"
FILSTATUS_MANGLER = "mangler"
FILSTATUS_TOM = "tom"
FILSTATUS_FOR_STOR = "for_stor"


@dataclass(frozen=True)
class FilResultat:
    status: str
    sti: Path | None
    stoerrelse: int | None
    sha256: str | None
    mime: str | None
    besked: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == FILSTATUS_OK


class FilOpslag:
    def __init__(self, indstillinger: FilIndstillinger) -> None:
        self._i = indstillinger
        self._rod = indstillinger.rod.resolve()
        self._indeks: dict[str, Path] | None = None

    @property
    def rod(self) -> Path:
        """Roden filerne søges under. Bruges i fejlbeskeder, hvor den næsten
        altid er svaret på hvorfor ingenting kunne findes."""
        return self._rod

    # -- opslag -------------------------------------------------------

    def find(self, relativ_sti: str | None, filnavn: str | None = None) -> Path | None:
        """Find filen. Tre forsøg, i faldende sikkerhed."""
        if relativ_sti:
            kandidat = self._rod / _normaliser_sti(relativ_sti)
            if kandidat.is_file():
                return kandidat
            # Unicode-normalisering: æøå gemmes forskelligt på macOS/Windows/Linux
            if self._i.normaliser_navne:
                nfc = self._rod / unicodedata.normalize("NFC", _normaliser_sti(relativ_sti))
                if nfc.is_file():
                    return nfc
                nfd = self._rod / unicodedata.normalize("NFD", _normaliser_sti(relativ_sti))
                if nfd.is_file():
                    return nfd
        if filnavn:
            # Sidste udvej: opslag i indeks over hele sharet, bygget én gang.
            traef = self._byg_indeks().get(_indeksnoegle(filnavn))
            if traef:
                return traef
        return None

    def undersoeg(self, relativ_sti: str | None, filnavn: str | None = None,
                  beregn_checksum: bool = True) -> FilResultat:
        sti = self.find(relativ_sti, filnavn)
        if sti is None:
            return FilResultat(FILSTATUS_MANGLER, None, None, None, None,
                               f"Ikke fundet: {relativ_sti or filnavn}")

        stoerrelse = sti.stat().st_size
        if stoerrelse == 0 and not self._i.tillad_tomme_filer:
            return FilResultat(FILSTATUS_TOM, sti, 0, None, None, "Filen er 0 bytes")

        maks = self._i.maks_stoerrelse_mb * 1024 * 1024
        if stoerrelse > maks:
            return FilResultat(
                FILSTATUS_FOR_STOR, sti, stoerrelse, None, None,
                f"{stoerrelse / 1024 / 1024:.1f} MB overstiger grænsen på {self._i.maks_stoerrelse_mb} MB",
            )

        mime = mimetypes.guess_type(sti.name)[0] or "application/octet-stream"
        checksum = fil_sha256(sti) if beregn_checksum else None
        return FilResultat(FILSTATUS_OK, sti, stoerrelse, checksum, mime)

    def laes(self, sti: Path) -> bytes:
        try:
            return sti.read_bytes()
        except OSError as e:
            raise FilFejl(f"Kunne ikke læse filen: {e}", sti=str(sti)) from e

    # -- indeks -------------------------------------------------------

    def _byg_indeks(self) -> dict[str, Path]:
        """Indeks over filnavn -> sti. Bygges dovent, én gang pr. kørsel.

        Ved kollisioner (samme filnavn flere steder) beholdes ingen af dem:
        et forkert dokument på en børnesag er værre end et manglende.
        """
        if self._indeks is not None:
            return self._indeks

        log.info("Bygger filindeks over %s ...", self._rod)
        indeks: dict[str, Path] = {}
        kollisioner: set[str] = set()
        for sti in self._rod.rglob("*"):
            if not sti.is_file():
                continue
            noegle = _indeksnoegle(sti.name)
            if noegle in indeks and indeks[noegle] != sti:
                kollisioner.add(noegle)
            indeks[noegle] = sti
        for noegle in kollisioner:
            indeks.pop(noegle, None)
        log.info("Filindeks: %d entydige filnavne, %d tvetydige udeladt",
                 len(indeks), len(kollisioner))
        self._indeks = indeks
        return indeks


def _normaliser_sti(raa: str) -> str:
    """GO leverer stier i vilkårlig notation. Gør dem til en relativ sti."""
    ren = raa.strip().replace("%20", " ")
    dele = PureWindowsPath(ren).parts if "\\" in ren else PurePosixPath(ren).parts
    # Fjern drevbogstav, UNC-præfiks og rod, så stien altid er relativ til roden
    dele = tuple(d for d in dele if d not in ("/", "\\") and not d.endswith(":\\") and not d.endswith(":"))
    return str(Path(*dele)) if dele else ""


def _indeksnoegle(filnavn: str) -> str:
    return unicodedata.normalize("NFC", filnavn).casefold()
