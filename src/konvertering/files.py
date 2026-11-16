"""Dokumentfiler på GO's fileshare.

Netcompany lægger filerne på et netværksdrev med en fast struktur
(løsningsbeskrivelsen afsnit 1.9.4, bekræftet mod testdumpet 16-11-2026):

    \\\\<server>\\GO_Files\\                 <- KONV_FIL_ROD
        PPR-2015-123456-001\\                <- mappe = sagens GOSagsID
            5097.rtf                         <- fil = DokumentID + endelse
        ELM-2022-123456-001\\
            4571585.pdf
        ELM-2022-123456-001_1\\              <- overløb: max 1000 filer pr. mappe

Extract skriver stien som ``<SagsId>/<DokumentID>.<endelse>`` med endelsen
fra GO's oprindelige filnavn. Opslaget her tager sig af det, kun
filsystemet kan svare på:

* **Overløbsmapper.** Filen kan ligge i ``<SagsId>_1``, ``_2`` …
* **Endelsens store og små bogstaver.** GO's filnavn kan sige ``.tif``
  mens filen hedder ``.TIF``. Linux skelner; Windows gør ikke.
* **Manglende endelse.** Har GO's filnavn ingen endelse, ledes der efter
  ``<DokumentID>.*``.

Der gættes aldrig. Findes filen flere steder — samme navn i to af sagens
mapper, eller to filer med samme dokument-id og forskellig endelse — er
resultatet ``tvetydig``, ikke det første træf. Et forkert bilag på en
børnesag er værre end et manglende.

Der ledes heller ikke uden for sagens egne mapper. Den tidligere udgave
søgte i et indeks over hele sharet på GO's oprindelige filnavn; med
filnavne der er rene tal, ville det kunne hente et ANDET dokuments fil.

Scanne-fasen (``konv filer scan``) kører FØR indlæsning og skriver
resultatet i stg.dokument, så antallet af dokumenter i problemer er kendt
inden SBSYS røres.
"""

from __future__ import annotations

import logging
import mimetypes
from collections import OrderedDict
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
FILSTATUS_TVETYDIG = "tvetydig"

# Over 1000 filer pr. mappe giver _1, _2 … Ingen sag har i nærheden af
# tusind overløbsmapper; grænsen er kun et værn mod en uendelig løkke.
_MAKS_OVERLOEB = 1000

# Mappelister der holdes i hukommelsen. Kun mapper hvor det direkte opslag
# fejlede, bliver listet, så de fleste kørsler rammer den knap.
_MAKS_LISTEDE_MAPPER = 256


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
        self._lister: OrderedDict[Path, list[Path]] = OrderedDict()

    @property
    def rod(self) -> Path:
        """Roden filerne søges under. Bruges i fejlbeskeder, hvor den næsten
        altid er svaret på hvorfor ingenting kunne findes."""
        return self._rod

    # -- opslag -------------------------------------------------------

    def find_alle(self, relativ_sti: str | None) -> list[Path]:
        """Alle filer der matcher stien, på tværs af sagens mapper.

        Præcis ét træf er det eneste brugbare svar. Nul betyder at filen
        mangler; flere betyder at vi ikke ved hvilken der er den rigtige.
        """
        if not relativ_sti:
            return []
        ren = _normaliser_sti(relativ_sti)
        mappe_rel, _, navn = ren.rpartition("/")
        if not navn:
            return []

        fundne: list[Path] = []
        for mappe in self._sagens_mapper(mappe_rel):
            # Det hurtige tilfælde: præcis det navn extract byggede. Ét stat-
            # kald, og på Windows er det i forvejen ufølsomt for store/små.
            direkte = mappe / navn
            if direkte.is_file():
                fundne.append(direkte)
                continue
            fundne.extend(self._match_i_mappe(mappe, navn))

        # Samme fil set to gange (fx via det direkte opslag på et
        # case-ufølsomt filsystem) er stadig én fil.
        unikke: dict[str, Path] = {}
        for sti in fundne:
            unikke.setdefault(str(sti).casefold(), sti)
        return list(unikke.values())

    def find(self, relativ_sti: str | None) -> Path | None:
        """Filen, hvis den findes præcis ét sted. Ellers None."""
        fundne = self.find_alle(relativ_sti)
        return fundne[0] if len(fundne) == 1 else None

    def undersoeg(self, relativ_sti: str | None, beregn_checksum: bool = True) -> FilResultat:
        fundne = self.find_alle(relativ_sti)
        if not fundne:
            return FilResultat(FILSTATUS_MANGLER, None, None, None, None,
                               f"Ikke fundet: {relativ_sti}")
        if len(fundne) > 1:
            navne = ", ".join(str(s.relative_to(self._rod)) for s in fundne[:5])
            return FilResultat(FILSTATUS_TVETYDIG, None, None, None, None,
                               f"Findes {len(fundne)} steder: {navne}")

        sti = fundne[0]
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

    # -- internt --------------------------------------------------------

    def _sagens_mapper(self, mappe_rel: str) -> list[Path]:
        """Sagens mappe og dens overløbsmapper, i den rækkefølge de findes.

        Kun det præcise navn og navn + ``_<n>``. Et præfiks-match ville lade
        ``…-001`` fange ``…-0011`` — en anden sag.
        """
        base = self._rod / mappe_rel if mappe_rel else self._rod
        mapper = [base] if base.is_dir() else []
        if not mappe_rel:
            return mapper
        for n in range(1, _MAKS_OVERLOEB + 1):
            overloeb = self._rod / f"{mappe_rel}_{n}"
            if not overloeb.is_dir():
                break
            mapper.append(overloeb)
        return mapper

    def _match_i_mappe(self, mappe: Path, navn: str) -> list[Path]:
        """Træf i én mappe når det direkte opslag fejlede.

        Med endelse: samme navn, uanset store og små bogstaver.
        Uden endelse: alle filer hvis navn før endelsen er dokument-id'et.
        """
        filer = self._liste(mappe)
        noegle = navn.casefold()
        if "." in navn:
            return [f for f in filer if f.name.casefold() == noegle]
        return [f for f in filer if f.stem.casefold() == noegle]

    def _liste(self, mappe: Path) -> list[Path]:
        if mappe in self._lister:
            self._lister.move_to_end(mappe)
            return self._lister[mappe]
        try:
            filer = [p for p in mappe.iterdir() if p.is_file()]
        except OSError as e:
            log.warning("Kunne ikke liste %s: %s", mappe, e)
            filer = []
        self._lister[mappe] = filer
        if len(self._lister) > _MAKS_LISTEDE_MAPPER:
            self._lister.popitem(last=False)
        return filer


def _normaliser_sti(raa: str) -> str:
    """Gør en sti fra databasen til en relativ sti med skråstreg.

    Extract skriver ``<SagsId>/<fil>``, men stien tåler også omvendt
    skråstreg, drevbogstav, UNC-præfiks og ``%20`` — så en sti der er
    skrevet i hånden eller kopieret fra Stifinder, også virker.
    """
    ren = raa.strip().replace("%20", " ")
    dele = PureWindowsPath(ren).parts if "\\" in ren else PurePosixPath(ren).parts
    # Fjern drevbogstav, UNC-præfiks og rod, så stien altid er relativ til roden
    dele = tuple(d for d in dele
                 if d not in ("/", "\\") and not d.endswith(":\\") and not d.endswith(":")
                 and not d.startswith("\\\\"))
    return "/".join(dele)
