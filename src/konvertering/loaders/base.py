"""Fælles maskineri for alle loaders.

En loader skal kun beskrive tre ting:

    byg()     GO-række  ->  kanonisk objekt (med mapping anvendt)
    payload() kanonisk objekt -> SBSYS-payload
    send()    payload -> (sbsys_id, sbsys_nummer)

Alt det kedelige — idempotens, dry-run, fejlklassifikation, afvisnings-
rapport, statusopdatering — ligger her, så det er ens for sag, part,
dokument og notat, og så en ny objekttype koster ~40 linjer.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from ..config import Settings
from ..db import Database
from ..files import FilOpslag
from ..hashing import kort_noegle, maskér, payload_hash
from ..idmap import IdMap
from ..mapping import Kodelister
from ..models import KanoniskObjekt, Objekttype
from ..sbsys.client import SbsysKlient
from ..sbsys.endpoints import Endpoints
from ..sbsys.errors import (
    AutentifikationsFejl,
    CertifikatFejl,
    FilFejl,
    ForbigaaendeFejl,
    KonverteringsFejl,
    PermanentFejl,
    UdeladtMedVilje,
    VaertsnavnFejl,
    ValideringsFejl,
)

log = logging.getLogger(__name__)

# Hvor mange manglende filer i træk der skal til, før vi antager at det er
# KONV_FIL_ROD der peger forkert og ikke filerne der mangler. Grænsen gælder
# kun så længe INGEN fil er fundet i kørslen: har bare én virket, er roden
# rigtig, og resten er huller i leverancen som skal afvises enkeltvis.
FIL_MANGLER_GRAENSE = 25

T = TypeVar("T", bound=KanoniskObjekt)


@dataclass
class Kontekst:
    settings: Settings
    db: Database
    idmap: IdMap
    klient: SbsysKlient
    endpoints: Endpoints
    kodelister: Kodelister
    filer: FilOpslag
    # Klienten mod Børn og Unge-API'et. None når SBSYS_BU_BASE_URL ikke er
    # sat; så afvises indsatser med en besked i stedet for en AttributeError.
    bu_klient: SbsysKlient | None = None
    run_id: int | None = None
    # Udskriv payloaden i dry-run. Til gennemsyn før første rigtige kald.
    vis_payload: bool = False
    # None = kør på tværs af bølger (kun relevant i TEST).
    # I produktion sættes den altid; koden er valideret mod konv.boelge
    # inden den når hertil — se konvertering.boelge.valider().
    boelge: str | None = None


def formatér_payload(payload: dict, maskeret: bool = True) -> str:
    """Vis payloaden som den ville blive sendt.

    Bruges før første rigtige kald: man vil se præcis hvilke feltnavne og
    værdier der rammer SBSYS, ikke blot hvor mange felter der er.

    Maskeres som standard. Payloaden indeholder sagstitler og kan indeholde
    CPR-numre, og den her havner i en terminal og formentlig i en e-mail
    til en kollega bagefter.
    """
    tekst = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    return maskér(tekst) if maskeret else tekst


def boelge_filter(boelge: str | None, alias: str = "") -> tuple[str, tuple]:
    """Byg WHERE-fragment og parametre til bølgeafgrænsningen.

    Adskilt som funktion fordi det er det ene sted en fejl ville betyde at
    en bølgekørsel stilfærdigt rørte ved en anden bølges data. Testet
    isoleret i tests/test_boelge.py.
    """
    if not boelge:
        return "", ()
    praefiks = f"{alias}." if alias else ""
    return f" AND {praefiks}boelge = ?", (boelge,)


@dataclass
class Resultat:
    behandlet: int = 0
    indlaest: int = 0
    sprunget_over: int = 0
    fejlet: int = 0
    afvist: int = 0

    def __str__(self) -> str:
        return (
            f"behandlet={self.behandlet} indlæst={self.indlaest} "
            f"sprunget_over={self.sprunget_over} afvist={self.afvist} fejlet={self.fejlet}"
        )


class Loader(ABC, Generic[T]):
    objekt_type: Objekttype
    # Viewet med de objekter der mangler indlæsning. Står HER og ikke i en
    # tabel et andet sted i koden: da navnet lå to steder, blev det ene
    # glemt ved en ny objekttype, og `konv load notat` faldt med en
    # KeyError i stedet for at køre.
    arbejdsliste: str

    def __init__(self, k: Kontekst) -> None:
        self.k = k
        # Til grænsen i _fil_fejl(): tæller kun så længe intet er lykkedes.
        self._manglende_filer_i_traek = 0
        self._fil_fundet = False

    # -- skal implementeres af den enkelte loader ---------------------

    @abstractmethod
    def hent_arbejde(self, graense: int | None = None) -> Iterable[dict[str, Any]]:
        """Rækker der endnu ikke er indlæst. Læses fra stg.v_*_til_indlaesning."""

    @abstractmethod
    def byg(self, raekke: dict[str, Any]) -> T:
        """GO-række -> kanonisk objekt. Her anvendes kodelisterne."""

    @abstractmethod
    def payload(self, objekt: T) -> dict[str, Any]:
        """Kanonisk objekt -> SBSYS-payload via endpoints.byg_payload()."""

    @abstractmethod
    def send(self, objekt: T, payload: dict[str, Any]) -> tuple[str, str | None]:
        """Udfør kaldet. Returnerer (sbsys_id, sbsys_nummer)."""

    # -- fælles kørsel ------------------------------------------------

    def koer(self, graense: int | None = None) -> Resultat:
        r = Resultat()
        for raekke in self.hent_arbejde(graense):
            r.behandlet += 1
            self._behandl_en(raekke, r)
        log.info("%s: %s", self.objekt_type, r)
        return r

    def behandl_noegle(self, kilde_noegle: str) -> Resultat:
        """Behandl ét objekt — bruges af Automation Server, hvor hvert
        workitem er ét objekt og skal kunne fejle uafhængigt af de andre."""
        r = Resultat()
        raekke = self._hent_en(kilde_noegle)
        if raekke is None:
            log.warning("Ingen række for %s %s", self.objekt_type, kort_noegle(kilde_noegle))
            r.sprunget_over += 1
            return r
        r.behandlet += 1
        self._behandl_en(raekke, r)
        return r

    def _behandl_en(self, raekke: dict[str, Any], r: Resultat) -> None:
        kilde_noegle = str(raekke["kilde_noegle"])
        ref = kort_noegle(kilde_noegle)

        # 1) Byg og validér. Fejl her når aldrig til SBSYS.
        try:
            objekt = self.byg(raekke)
            payload = self.payload(objekt)
        except UdeladtMedVilje as e:
            # En beslutning, ikke en fejl. Intet skrives i id_map: ændres
            # mapningen fra 'ignorer' til 'map', skal objektet med i næste
            # kørsel uden at nogen først skal nulstille det.
            log.debug("%s %s udeladt med vilje: %s", self.objekt_type, ref, e)
            r.sprunget_over += 1
            return
        except ValideringsFejl as e:
            self._afvis(kilde_noegle, e.regel, "blok", str(e), e.felt, e.vaerdi,
                        boelge=self._boelge(raekke))
            self.k.idmap.reserver(
                self.objekt_type, kilde_noegle, "", self.k.run_id, self._boelge(raekke)
            )
            # Bevidst 'failed' og ikke 'manual': en manglende mapning bliver
            # rettet af en fagperson, og så skal objektet med i næste kørsel.
            # 'manual' er terminal og forbeholdt en menneskelig beslutning om
            # at noget skal håndteres uden for konverteringen.
            self.k.idmap.markér_fejlet(self.objekt_type, kilde_noegle, str(e))
            r.afvist += 1
            return
        except FilFejl as e:
            self._fil_fejl(e, kilde_noegle, raekke, r)
            return

        # 2) Idempotens: er objektet allerede i SBSYS?
        h = payload_hash(payload)
        skal, begrundelse = self.k.idmap.skal_indlaeses(self.objekt_type, kilde_noegle, h)
        if not skal:
            log.debug("%s %s springes over: %s", self.objekt_type, ref, begrundelse)
            r.sprunget_over += 1
            return

        self.k.idmap.reserver(
            self.objekt_type, kilde_noegle, h, self.k.run_id, self._boelge(raekke)
        )

        # 3) Dry-run standser her — payload er bygget og valideret, men intet sendes.
        if self.k.settings.dry_run:
            log.info("[DRY-RUN] ville oprette %s %s (%d felter)",
                     self.objekt_type, ref, len(payload))
            if self.k.vis_payload:
                print(formatér_payload(payload, self.k.settings.maskér_persondata))
            r.sprunget_over += 1
            return

        # 4) Send.
        try:
            sbsys_id, sbsys_nummer = self.send(objekt, payload)
        except (AutentifikationsFejl, VaertsnavnFejl, CertifikatFejl):
            # Alle tre er fejl i opsætningen, ikke i dette objekts data. Stop
            # hele kørslen frem for at afvise 40.000 sager én ad gangen.
            raise
        except FilFejl as e:
            # Filen tjekkes inde i send(), fordi den skal verificeres FØR
            # metadata oprettes. En manglende fil er derfor en afvisning som
            # alle andre — ikke en grund til at stoppe resten af bølgen.
            self._fil_fejl(e, kilde_noegle, raekke, r)
            return
        except PermanentFejl as e:
            # Sig HVORFOR med det samme. Uden linjen viste kørslen kun
            # "fejlet=1", og grunden skulle graves frem med konv status.
            log.warning("%s %s afvist af SBSYS: %s", self.objekt_type, ref, maskér(str(e))[:400])
            self.k.idmap.markér_fejlet(self.objekt_type, kilde_noegle, maskér(str(e)))
            self._afvis(kilde_noegle, "sbsys_afviste", "blok", maskér(str(e)),
                        boelge=self._boelge(raekke))
            r.fejlet += 1
            return
        except ForbigaaendeFejl as e:
            # Alle genforsøg er brugt. Objektet forbliver 'pending' og
            # samles op af næste kørsel — ingen manuel oprydning nødvendig.
            log.warning("%s %s fejlede efter alle genforsøg: %s", self.objekt_type, ref,
                        maskér(str(e))[:400])
            self.k.idmap.markér_fejlet(self.objekt_type, kilde_noegle, maskér(str(e)))
            r.fejlet += 1
            return

        self.k.idmap.markér_indlaest(self.objekt_type, kilde_noegle, sbsys_id, sbsys_nummer)
        # Hver oprettelse nævnes. Uden den linje kan man kun se AT noget blev
        # oprettet, ikke HVAD — og så skal man i databasen for at finde den
        # sag man lige har lavet. Nøglen vises forkortet, fordi GO's nøgler
        # kan indeholde CPR; sbsys_id og -nummer er ikke personhenførbare.
        log.info("%s %s oprettet i SBSYS: id=%s nummer=%s",
                 self.objekt_type, ref, sbsys_id, sbsys_nummer or "-")
        self._fil_fundet = True
        self._manglende_filer_i_traek = 0
        r.indlaest += 1

    # -- hjælpere -----------------------------------------------------

    def _fil_fejl(self, e: FilFejl, kilde_noegle: str, raekke: dict[str, Any],
                  r: Resultat) -> None:
        """En fil der ikke kan bruges afviser ét dokument — ikke bølgen.

        Dokumentet markeres 'failed' og ikke 'manual', så det kommer med i
        næste kørsel når filen er lagt på plads. Det er samme reparations-
        sløjfe som ved manglende mapning.
        """
        self._afvis(kilde_noegle, e.regel, "blok", str(e), "fil", e.sti,
                    boelge=self._boelge(raekke))
        self.k.idmap.reserver(
            self.objekt_type, kilde_noegle, "", self.k.run_id, self._boelge(raekke)
        )
        self.k.idmap.markér_fejlet(self.objekt_type, kilde_noegle, str(e))
        r.afvist += 1

        if e.regel != "fil_mangler" or self._fil_fundet:
            return
        self._manglende_filer_i_traek += 1
        if self._manglende_filer_i_traek < FIL_MANGLER_GRAENSE:
            return
        raise KonverteringsFejl(
            f"{self._manglende_filer_i_traek} dokumenter i træk manglede deres fil, "
            f"og ingen fil er fundet i denne kørsel. Det ligner en forkert "
            f"KONV_FIL_ROD ({self.k.filer.rod}) snarere end huller i leverancen. "
            f"Tjek stien, og kør `konv filer scan` for at se status på alle filer."
        )

    def _boelge(self, raekke: dict[str, Any]) -> str | None:
        """Objektets egen bølge, med kørslens som reserve.

        Rækken vinder, fordi den er den faktiske tildeling fra extract-laget.
        Kørslens bølge er kun en afgrænsning af hvad der blev hentet.
        """
        return raekke.get("boelge") or self.k.boelge

    def _hent_en(self, kilde_noegle: str) -> dict[str, Any] | None:
        tabel = {
            Objekttype.SAG: "stg.sag",
            Objekttype.PART: "stg.part",
            Objekttype.DOKUMENT: "stg.dokument",
            Objekttype.NOTAT: "stg.notat",
            Objekttype.INDSATS: "stg.indsats",
        }[self.objekt_type]
        return self.k.db.hent_en(f"SELECT * FROM {tabel} WHERE kilde_noegle = ?", kilde_noegle)

    def _afvis(
        self,
        kilde_noegle: str,
        regel: str,
        alvorlighed: str,
        beskrivelse: str,
        felt: str | None = None,
        vaerdi: str | None = None,
        boelge: str | None = None,
    ) -> None:
        self.k.db.udfoer(
            """
            INSERT INTO konv.afvist
                (run_id, objekt_type, boelge, kilde_noegle, regel, alvorlighed,
                 beskrivelse, felt, vaerdi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self.k.run_id,
            str(self.objekt_type),
            boelge or self.k.boelge,
            kilde_noegle,
            regel,
            alvorlighed,
            maskér(beskrivelse)[:1000],
            felt,
            maskér(vaerdi)[:400] if vaerdi else None,
        )
