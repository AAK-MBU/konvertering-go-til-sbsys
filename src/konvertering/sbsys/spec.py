"""Læs SBSYS' egen OpenAPI-beskrivelse i stedet for at gætte.

SBSYS udstiller et dokument pr. API-version under ``{base_url}/docs/{version}``.
Det er den eneste kilde der ikke er et gæt, og det besvarer på minutter
spørgsmål der ellers kræver et møde:

* findes der overhovedet et felt til en ekstern nøgle, og hvad hedder det?
  (spørgsmål A1/A17 — hele idempotensen hviler på svaret)
* hvad hedder stierne i JERES version, og hvilke metoder tager de?
* hvilke felter er obligatoriske ved oprettelse? (A7)

Tre dokumentformer er i omløb, og de gemmer modellerne forskellige steder:
Swagger 2.0 under ``definitions``, OpenAPI 3 under ``components.schemas``,
og OpenAPI 3 med modellerne inline i ``paths``. De to første håndteres her;
den tredje giver færre svar, og så er det specifikationen der skal læses
i hånden.

Idéen — og stiskabelonerne — er lånt fra kommunens egen SBSYS-klient
(AAK-MBU/api-sbsys-client), som gør det samme for sin modelgenerering.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .client import SbsysKlient

log = logging.getLogger(__name__)

STI_SKABELONER = (
    # SBSYS' egen web-API
    "/docs/{version}",
    "/swagger/docs/{version}",
    "/swagger/{version}/swagger.json",
    # Børn og Unge-API'et er en SELVSTÆNDIG tjeneste med sit eget dokument:
    # stierne dér er snake_case (bu_api/sag_foranstaltning), hvilket er et
    # andet framework end resten af SBSYS. Derfor også de gængse layouts
    # for sådan et. Prøves med --praefiks /boernogungeapi.
    "/openapi.json",
    "/swagger.json",
    "/schema/",
    "/swagger/?format=openapi",
    "/api-docs",
    "/v3/api-docs",
    # ASP.NET Cores standard (Swashbuckle). Bekraeftet 09-11-2026: det er
    # HER Boern og Unge-API'ets dokument ligger. Med literal v1, fordi
    # "{version}" med default-vaerdien gav /swagger/default/swagger.json.
    "/swagger/v1/swagger.json",
    # Med skraastreg til sidst: nogle frameworks ruter kun den form.
    "/docs/",
    "/swagger/",
    "/api/schema/",
    "/redoc/",
)
"""Layouts der prøves, i rækkefølge. Den første der svarer, vinder."""

METODER = ("get", "post", "put", "patch", "delete")

# Mønstre der kan bære en ekstern nøgle. Bevidst brede: det er bedre at
# se ti kandidater og kassere ni end at overse den ene der findes.
# "Id" alene er udeladt — ellers drukner svaret i SagsTypeId og BehandlerId.
_NOEGLEMOENSTRE = (
    "ekstern",
    "external",
    "fremmed",
    "foreign",
    "andenpart",
    "anden_part",
    "kilde",
    "source",
    "legacy",
    "reference",
    "referencenummer",
    "noegle",
    "nøgle",
    "key",
    "import",
    "konvert",
    "migration",
    "migrer",
    "oprindelig",
    "original",
    "gammel",
    "esdh",
    "getorganized",
    "sharepoint",
    "udvidet",      # SBSYS har "UdvidetEgenskab"-mønstre i nogle versioner
    "egenskab",
    "kendetegn",
    "stikord",
    "fritekst",
)


class SpecFejl(Exception):
    """Dokumentet kunne ikke hentes eller læses."""


def hent(klient: SbsysKlient, version: str = "default",
         praefiks: str = "") -> tuple[dict[str, Any], str]:
    """Hent ét specifikationsdokument.

    Args:
        klient: Vores egen SBSYS-klient — så token, TLS og kaldelog er med.
        version: Dokumentnavn, fx "default" eller "v10".
        praefiks: Sti foran skabelonerne, når dokumentet hører til en anden
            tjeneste på samme vært. Børn og Unge-API'et ligger under
            ``/boernogungeapi`` og har sit eget dokument — derfor gav
            "Foranstaltning" nul hits i SBSYS' eget.

    Returns:
        Dokumentet og den sti det blev fundet på.

    Raises:
        SpecFejl: Hvis ingen af stiskabelonerne svarede med et dokument.
    """
    noter: list[str] = []
    praefiks = "/" + praefiks.strip("/") if praefiks.strip("/") else ""
    for skabelon in STI_SKABELONER:
        sti = praefiks + skabelon.format(version=version)
        svar = klient.kald(
            "GET", sti, operation="hent_spec",
            forventede_status=(200, 301, 302, 400, 401, 403, 404, 406, 500),
        )
        if svar.status == 200 and isinstance(svar.krop, dict):
            return svar.krop, sti
        # Kroppen med: en 400 betyder at stien FINDES, men at kaldet ikke
        # var som tjenesten ville have det — og hvad der mangler, står
        # næsten altid i svaret. Uden den er en 400 en blindgyde.
        noter.append(f"{sti} -> HTTP {svar.status}{_vink(svar.krop)}")
    raise SpecFejl(
        f"Ingen OpenAPI-beskrivelse fundet for version '{version}'. Prøvet:\n  "
        + "\n  ".join(noter)
        + "\nPrøv en anden version (--version v10), et andet præfiks "
          "(--praefiks /boernogungeapi), eller spørg SBSYS-driften hvor "
          "beskrivelsen ligger."
    )


def _vink(krop: Any) -> str:
    """Kort uddrag af svarkroppen, til fejlbeskeden."""
    if not krop:
        return ""
    # Et ikke-JSON-svar kommer som bytes. En HTML-fejlside siger intet
    # brugbart, så den koges ned til det der står i <title>.
    if isinstance(krop, bytes):
        raa = krop.decode("utf-8", "replace")
        if "<html" in raa.lower():
            titel = re.search(r"<title[^>]*>(.*?)</title>", raa, re.IGNORECASE | re.DOTALL)
            raa = titel.group(1) if titel else "(HTML-side)"
        krop = raa
    tekst = str(krop).strip().replace("\n", " ")
    return f"  {tekst[:160]}" if tekst else ""


def operationer(dokument: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Fladgør dokumentet til én række pr. operation: (metode, sti, tekst)."""
    raekker: list[tuple[str, str, str]] = []
    for sti, punkt in (dokument.get("paths") or {}).items():
        if not isinstance(punkt, dict):
            continue
        for metode in METODER:
            op = punkt.get(metode)
            if not isinstance(op, dict):
                continue
            tekst = (
                op.get("summary")
                or op.get("operationId")
                or (op.get("description") or "").split("\n")[0]
            )
            raekker.append((metode.upper(), sti, tekst[:70]))
    return sorted(raekker, key=lambda r: (r[1], r[0]))


def skemaer(dokument: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Modellerne, uanset om dokumentet er Swagger 2.0 eller OpenAPI 3."""
    if "definitions" in dokument:
        return dict(dokument["definitions"] or {})
    return dict((dokument.get("components") or {}).get("schemas") or {})


def felter(skema: dict[str, Any]) -> dict[str, Any]:
    return dict(skema.get("properties") or {})


def noeglekandidater(dokument: dict[str, Any], kun: str = "") -> list[tuple[str, str, str]]:
    """Felter der kunne bære vores eksterne nøgle.

    Args:
        dokument: Den hentede specifikation.
        kun: Vis kun modeller hvis navn indeholder denne tekst, fx "sag".

    Returns:
        Tripler af (modelnavn, feltnavn, type), sorteret.
    """
    fundet: list[tuple[str, str, str]] = []
    for navn, skema in skemaer(dokument).items():
        if kun and kun.lower() not in navn.lower():
            continue
        for felt, beskrivelse in felter(skema).items():
            lavt = felt.lower()
            if not any(m in lavt for m in _NOEGLEMOENSTRE):
                continue
            type_ = str((beskrivelse or {}).get("type") or "?")
            fundet.append((navn, felt, type_))
    return sorted(set(fundet))


def paakraevede_felter(dokument: dict[str, Any], model: str) -> list[str]:
    """Hvad SBSYS kræver udfyldt på en model (spørgsmål A7)."""
    for navn, skema in skemaer(dokument).items():
        if navn.lower() == model.lower() or navn.lower().endswith(f".{model.lower()}"):
            return sorted(str(f) for f in (skema.get("required") or []))
    return []


def find_modeller(dokument: dict[str, Any], tekst: str) -> list[str]:
    """Modelnavne der indeholder teksten. Til at finde ud af hvad de hedder."""
    noegle = tekst.lower()
    return sorted(n for n in skemaer(dokument) if noegle in n.lower())


_SOEGBAR = re.compile(r"search|soeg|søg|criteria|query", re.IGNORECASE)
_SKRIVBAR = re.compile(r"opret|create|input|update|rediger|post|journaliser", re.IGNORECASE)


def rolle(modelnavn: str) -> str:
    """Er modellen noget man SKRIVER eller noget man SØGER med?

    Det er hele spørgsmålet bag A1. Et felt man kan skrive men ikke søge på
    er værdiløst som ekstern nøgle — man kan gemme nøglen og aldrig finde
    den igen. Omvendt er et søgefelt uden et sted at skrive værdien lige så
    ubrugeligt. Begge dele skal findes, og helst på samme navn.
    """
    if _SOEGBAR.search(modelnavn):
        return "søg"
    if _SKRIVBAR.search(modelnavn):
        return "skriv"
    return "læs"


def felt_forekomster(dokument: dict[str, Any], navn: str) -> list[tuple[str, str, str, str]]:
    """Hvor optræder et feltnavn — og i hvilken rolle?

    Args:
        dokument: Den hentede specifikation.
        navn: Feltnavn eller del af et, fx "EksternSagId". Store og små
            bogstaver er ligegyldige, for SBSYS er ikke konsekvent:
            ``EksternId`` på én model er ``Eksternid`` på den næste.

    Returns:
        Firlinger af (rolle, modelnavn, feltnavn, type), sorteret med
        skrive- og søgemodellerne først.
    """
    noegle = navn.lower()
    fundet: list[tuple[str, str, str, str]] = []
    for model, skema in skemaer(dokument).items():
        for felt, beskrivelse in felter(skema).items():
            if noegle not in felt.lower():
                continue
            fundet.append((rolle(model), model, felt, str((beskrivelse or {}).get("type") or "?")))
    orden = {"skriv": 0, "søg": 1, "læs": 2}
    return sorted(set(fundet), key=lambda r: (orden[r[0]], r[1]))


def soegemodeller(dokument: dict[str, Any]) -> list[str]:
    """Modeller der ligner søgekriterier.

    Et felt er kun brugbart som ekstern nøgle hvis det kan SØGES på — ellers
    kan vi skrive nøglen ind, men aldrig finde den igen. Søgemodellen er
    derfor det sted svaret på A1 i virkeligheden står.
    """
    return sorted(n for n in skemaer(dokument) if _SOEGBAR.search(n))
