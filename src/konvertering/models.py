"""Den kanoniske model.

Hverken GO's eller SBSYS' datamodel. Et neutralt mellemlag, så:

* extract-laget må ændre sig frit uden at røre loaders
* SBSYS-klienten må ændre sig frit uden at røre extract
* validering sker ét sted, tidligt, med en forståelig fejlbesked

Alle modeller bærer ``kilde_noegle`` — den nøgle idempotensen hviler på.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Objekttype(StrEnum):
    SAG = "sag"
    PART = "part"
    DOKUMENT = "dokument"
    NOTAT = "notat"
    INDSATS = "indsats"


class Retning(StrEnum):
    INDGAAENDE = "indgaaende"
    UDGAAENDE = "udgaaende"
    INTERNT = "internt"


class PartType(StrEnum):
    PERSON = "person"
    VIRKSOMHED = "virksomhed"
    FRI = "fri"


class KanoniskObjekt(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kilde_noegle: str = Field(min_length=1, max_length=200)
    raa: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def objekttype(self) -> Objekttype:  # pragma: no cover - overskrives
        raise NotImplementedError


class Sag(KanoniskObjekt):
    titel: str = Field(min_length=1, max_length=500)
    go_sagsnummer: str | None = None
    beskrivelse: str | None = None
    kle: str | None = None            # SBSYS-værdi, allerede mappet
    facet: str | None = None
    sagstype: str | None = None
    # Sager oprettes FRA en skabelon: KLE, sagstype og afdeling kommer fra
    # den, ikke fra felterne nedenfor. Blokerende — uden den ingen sag.
    skabelon: str | None = None
    status: str | None = None
    ansvarlig: str | None = None      # SBSYS-bruger-id, allerede mappet
    afdeling: str | None = None
    oprettet_dato: datetime | None = None
    afsluttet_dato: datetime | None = None
    fortrolighed: str | None = None
    bevaring: str | None = None
    primaer_part_noegle: str | None = None
    # Uden part sendes sagen uden part (emnesager); ellers afvises den.
    part_valgfri: bool = False

    @property
    def objekttype(self) -> Objekttype:
        return Objekttype.SAG

    @field_validator("titel")
    @classmethod
    def _titel_ikke_kun_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("titel må ikke være tom")
        return v

    @field_validator("afsluttet_dato")
    @classmethod
    def _afslutning_efter_oprettelse(cls, v: datetime | None, info) -> datetime | None:
        oprettet = info.data.get("oprettet_dato")
        if v and oprettet and v < oprettet:
            raise ValueError("afsluttet_dato ligger før oprettet_dato")
        return v


class Part(KanoniskObjekt):
    part_type: PartType
    navn: str | None = None
    cpr: str | None = None
    cvr: str | None = None
    adresse: str | None = None
    postnr: str | None = None
    bynavn: str | None = None
    email: str | None = None
    telefon: str | None = None

    @property
    def objekttype(self) -> Objekttype:
        return Objekttype.PART

    @field_validator("cpr")
    @classmethod
    def _cpr_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        renset = v.replace("-", "").strip()
        if len(renset) != 10 or not renset.isdigit():
            raise ValueError("cpr skal være 10 cifre")
        return renset

    @field_validator("cvr")
    @classmethod
    def _cvr_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        renset = v.strip()
        if len(renset) != 8 or not renset.isdigit():
            raise ValueError("cvr skal være 8 cifre")
        return renset


class Dokument(KanoniskObjekt):
    sag_noegle: str = Field(min_length=1, max_length=200)
    titel: str = Field(min_length=1, max_length=500)
    kategori: str | None = None
    dokument_dato: datetime | None = None
    registreret: datetime | None = None
    forfatter: str | None = None
    retning: Retning = Retning.INTERNT
    version: int | None = None
    # GO's Journaliseret = 0. Et dokument paa en sag i SBSYS er enten
    # journaliseret eller en kladde, og en kladde maa ikke journaliseres:
    # det aendrer dokumentets status i en boernesag.
    er_kladde: bool = False
    # Kun kladder: GO's oprettelse som tekst i Beskrivelse, fordi en kladde
    # ikke kan bære dato, opretter eller behandler (26-11-2026).
    beskrivelse: str | None = None
    fil_sti: str | None = None
    fil_navn: str | None = None
    fil_stoerrelse: int | None = None
    fil_sha256: str | None = None
    mime: str | None = None

    @property
    def objekttype(self) -> Objekttype:
        return Objekttype.DOKUMENT


class Indsats(KanoniskObjekt):
    """En foranstaltning/indsats på en sag.

    Økonomifelterne fra GO (vederlag, tillæg, enhedspris) er bevidst IKKE
    med: økonomi er ikke en del af konverteringen, besluttet 09-11-2026.
    Perioden og omfanget er — en indsats uden sin rigtige periode er ikke
    en indsats.
    """

    sag_noegle: str = Field(min_length=1, max_length=200)
    # Alle tre skal være mappet, før SBSYS kan oprette indsatsen.
    paragraf: str | None = None
    foranstaltning: str | None = None
    tilbud: str | None = None
    periode_start: datetime | None = None
    periode_slut: datetime | None = None
    antal_enheder: int | None = None
    enhedstype: str | None = None
    udslagsgivende_forhold: str | None = None
    # Foranstaltningsmappens type (GO's Indholdstype) og rækkens øvrige
    # kolonner — kilden til tilbudsfelterne, se tilbudsfelter.py.
    mappetype: str | None = None
    go_felter: dict[str, Any] = Field(default_factory=dict)

    @property
    def objekttype(self) -> Objekttype:
        return Objekttype.INDSATS


class Notat(KanoniskObjekt):
    sag_noegle: str = Field(min_length=1, max_length=200)
    titel: str | None = None
    tekst: str | None = None
    forfatter: str | None = None
    dato: datetime | None = None
    notat_type: str | None = None

    @property
    def objekttype(self) -> Objekttype:
        return Objekttype.NOTAT
