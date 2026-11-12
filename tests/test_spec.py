"""Tests af opslag i SBSYS' egen OpenAPI-beskrivelse.

Beskrivelsen er den eneste kilde der ikke er et gæt. Den besvarer A1 —
findes der et felt vi kan skrive GO-nøglen i, og kan der søges på det — og
det spørgsmål har indtil nu krævet et møde.

To dokumentformer er i omløb og gemmer modellerne forskellige steder.
Begge skal virke, for vi ved ikke hvilken jeres version udstiller.
"""

from __future__ import annotations

import httpx
import pytest

from konvertering.config import SbsysIndstillinger
from konvertering.sbsys import spec
from konvertering.sbsys.client import SbsysKlient

SWAGGER2 = {
    "swagger": "2.0",
    "paths": {
        "/api/sag/{id}": {"get": {"summary": "Hent sag"}},
        "/api/sag/search": {"post": {"summary": "Søg sager"}},
    },
    "definitions": {
        "Dto.Sag.SagDto": {
            "required": ["SagsTitel", "SkabelonId"],
            "properties": {
                "SagsTitel": {"type": "string"},
                "SkabelonId": {"type": "integer"},
                "BehandlerId": {"type": "integer"},
                "EksternNoegle": {"type": "string"},
            },
        },
        "Dto.Sag.SagSearchDto": {
            "properties": {"SagsTitel": {"type": "string"}, "Stikord": {"type": "string"}}
        },
    },
}

OPENAPI3 = {
    "openapi": "3.0.1",
    "paths": {"/api/sag": {"get": {"operationId": "GetSag"}}},
    "components": {
        "schemas": {"SagDto": {"properties": {"KildeReference": {"type": "string"}}}}
    },
}


def _klient(handler) -> SbsysKlient:
    return SbsysKlient(
        SbsysIndstillinger(base_url="https://sbsys", statisk_token="t",
                           maks_forsoeg=1, kald_pr_sekund=1000),
        transport=httpx.MockTransport(handler),
    )


def test_dokumentet_hentes_fra_foerste_sti_der_svarer():
    kaldte: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        kaldte.append(req.url.path)
        if req.url.path == "/docs/default":
            return httpx.Response(200, json=SWAGGER2)
        return httpx.Response(404)

    dokument, sti = spec.hent(_klient(handler))

    assert sti == "/docs/default"
    assert kaldte == ["/docs/default"], "skal ikke prøve flere når den første svarer"
    assert "definitions" in dokument


def test_der_proeves_flere_layouts():
    """SBSYS-installationer lægger dokumentet forskellige steder."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/swagger/docs/default":
            return httpx.Response(200, json=SWAGGER2)
        return httpx.Response(404)

    _, sti = spec.hent(_klient(handler))
    assert sti == "/swagger/docs/default"


def test_intet_dokument_siger_hvad_der_blev_proevet():
    """Ellers står man med "virker ikke" og ingen vej videre."""
    klient = _klient(lambda _r: httpx.Response(404))

    with pytest.raises(spec.SpecFejl) as e:
        spec.hent(klient, "v10")

    besked = str(e.value)
    assert "/docs/v10" in besked
    assert "--version" in besked


@pytest.mark.parametrize("dokument,antal", [(SWAGGER2, 2), (OPENAPI3, 1)])
def test_skemaer_findes_i_begge_dokumentformer(dokument, antal):
    assert len(spec.skemaer(dokument)) == antal


def test_operationer_fladgoeres():
    raekker = spec.operationer(SWAGGER2)
    assert ("GET", "/api/sag/{id}", "Hent sag") in raekker
    assert ("POST", "/api/sag/search", "Søg sager") in raekker


def test_noeglekandidater_finder_det_felt_hele_adr_0001_hviler_paa():
    kandidater = spec.noeglekandidater(SWAGGER2)
    felter = {f for _, f, _ in kandidater}
    assert "EksternNoegle" in felter


def test_noeglekandidater_drukner_ikke_i_almindelige_id_felter():
    """"Id" alene er udeladt af mønstrene — ellers er hver eneste model
    en kandidat, og listen er ubrugelig."""
    felter = {f for _, f, _ in spec.noeglekandidater(SWAGGER2)}
    assert "BehandlerId" not in felter
    assert "SkabelonId" not in felter


def test_noeglekandidater_virker_ogsaa_i_openapi3():
    assert ("SagDto", "KildeReference", "string") in spec.noeglekandidater(OPENAPI3)


def test_kandidater_kan_afgraenses_til_én_model():
    assert spec.noeglekandidater(SWAGGER2, kun="SagSearch") == [
        ("Dto.Sag.SagSearchDto", "Stikord", "string")
    ]


def test_paakraevede_felter_besvarer_a7():
    """Hvilke felter er obligatoriske ved oprettelse — og dermed hvilke
    mapninger der er blokerende."""
    assert spec.paakraevede_felter(SWAGGER2, "SagDto") == ["SagsTitel", "SkabelonId"]


def test_paakraevede_felter_matcher_punkterede_navne():
    """SBSYS navngiver modeller Dto.Sag.SagDto. Man skriver "SagDto"."""
    assert spec.paakraevede_felter(SWAGGER2, "sagdto")


def test_soegemodeller_er_der_svaret_paa_a1_staar():
    """Et felt er kun brugbart som ekstern nøgle hvis der kan SØGES på det.
    Kan man skrive nøglen ind, men ikke finde den igen, er den værdiløs."""
    assert spec.soegemodeller(SWAGGER2) == ["Dto.Sag.SagSearchDto"]


# --- kan feltet både skrives og søges? ------------------------------
#
# Det er hele spørgsmålet bag A1. Et felt man kan skrive men ikke søge på
# er værdiløst som ekstern nøgle: man gemmer nøglen og finder den aldrig
# igen. SBSYS er desuden inkonsekvent med store bogstaver — EksternId på
# én model er Eksternid på den næste — så opslaget må ikke skelne.

SPEC_MED_ROLLER = {
    "swagger": "2.0",
    "definitions": {
        "ApiServer.Dto.Dokument.JournaliserDokumentInputDtoV10": {
            "properties": {"Eksternid": {"type": "string"}}
        },
        "ApiServer.Dto.Dokument.DokumentSearchDtoV29": {
            "properties": {"EksternId": {"type": "string"}}
        },
        "ApiServer.Dto.Dokument.DokumentDtoV15": {
            "properties": {"EksternId": {"type": "string"}}
        },
        "ApiServer.Dto.Soeg.Sag.SagSearchCriteria": {
            "properties": {"EksternSagId": {"type": "string"}}
        },
    },
}


def test_rollen_udledes_af_modelnavnet():
    assert spec.rolle("JournaliserDokumentInputDtoV10") == "skriv"
    assert spec.rolle("DokumentSearchDtoV29") == "søg"
    assert spec.rolle("SagSearchCriteria") == "søg"
    assert spec.rolle("SagSearchQueryV29") == "søg"
    assert spec.rolle("DokumentDtoV15") == "læs"


def test_felt_findes_uanset_store_bogstaver():
    """Eksternid og EksternId er samme felt for et menneske."""
    raekker = spec.felt_forekomster(SPEC_MED_ROLLER, "eksternid")
    felter_ = {f for _, _, f, _ in raekker}
    assert felter_ == {"Eksternid", "EksternId"}


def test_skrive_og_soegemodeller_staar_foerst():
    """Læsemodellerne er de mindst interessante — de skal ikke ligge øverst."""
    roller = [r for r, *_ in spec.felt_forekomster(SPEC_MED_ROLLER, "eksternid")]
    assert roller.index("skriv") < roller.index("søg") < roller.index("læs")


def test_dokumentets_eksterne_noegle_kan_baade_skrives_og_soeges():
    """Det er svaret på A1 for dokumenter — bekræftet mod Aarhus' testmiljø
    02-11-2026: Eksternid på journaliseringen, EksternId i søgningen."""
    roller = {r for r, *_ in spec.felt_forekomster(SPEC_MED_ROLLER, "eksternid")}
    assert {"skriv", "søg"} <= roller


def test_sagens_eksterne_noegle_kan_soeges():
    raekker = spec.felt_forekomster(SPEC_MED_ROLLER, "EksternSagId")
    assert [r for r, *_ in raekker] == ["søg"]


def test_ukendt_felt_giver_tom_liste():
    assert spec.felt_forekomster(SPEC_MED_ROLLER, "FindesIkke") == []


# --- Børn og Unge-API'et er en anden tjeneste ------------------------
#
# Stien boernogungeapi/bu_api/sag_foranstaltning er snake_case, hvor resten
# af SBSYS er camelCase. Det er et andet framework med sit eget
# OpenAPI-dokument — og derfor gav "Foranstaltning" nul hits i SBSYS' eget.

def test_praefiks_laegges_foran_stierne():
    kaldte: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        kaldte.append(req.url.path)
        if req.url.path == "/boernogungeapi/openapi.json":
            return httpx.Response(200, json=SWAGGER2)
        return httpx.Response(404)

    _, sti = spec.hent(_klient(handler), praefiks="/boernogungeapi")

    assert sti == "/boernogungeapi/openapi.json"
    assert all(p.startswith("/boernogungeapi/") for p in kaldte)


def test_praefiks_taaler_manglende_skraastreg():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/bu/docs/default":
            return httpx.Response(200, json=SWAGGER2)
        return httpx.Response(404)

    _, sti = spec.hent(_klient(handler), praefiks="bu/")
    assert sti == "/bu/docs/default"


def test_der_proeves_ogsaa_andre_frameworks_layouts():
    """Et snake_case-API er sjældent samme stak som resten af SBSYS."""
    assert "/openapi.json" in spec.STI_SKABELONER
    assert "/swagger.json" in spec.STI_SKABELONER


def test_fejlbeskeden_naevner_praefiks():
    with pytest.raises(spec.SpecFejl) as e:
        spec.hent(_klient(lambda _r: httpx.Response(404)))
    assert "--praefiks" in str(e.value)


# --- en 400 skal sige hvad der mangler -------------------------------
#
# Boern og Unge-API'et svarede 400 paa /openapi.json. En 400 betyder at
# stien FINDES, men at kaldet ikke var som tjenesten ville have det — og
# hvad der mangler staar naesten altid i svaret. Uden kroppen er det en
# blindgyde.

def test_svarkroppen_kommer_med_i_fejlbeskeden():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "Parameter 'format' mangler"})

    with pytest.raises(spec.SpecFejl) as e:
        spec.hent(_klient(handler))

    assert "format" in str(e.value)


def test_html_fejlside_koges_ned_til_titlen():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, html="<html><head><title>Bad Request</title></head><body>…</body></html>"
        )

    with pytest.raises(spec.SpecFejl) as e:
        spec.hent(_klient(handler))

    besked = str(e.value)
    assert "Bad Request" in besked
    assert "<body>" not in besked, "en hel HTML-side i en fejlbesked hjælper ingen"


def test_aspnet_core_standardstien_proeves():
    """Boern og Unge-API'ets dokument ligger paa /swagger/v1/swagger.json —
    ASP.NET Cores standard. Med skabelonen "{version}" og default-vaerdien
    ramte vi /swagger/default/swagger.json og fandt den aldrig."""
    assert "/swagger/v1/swagger.json" in spec.STI_SKABELONER
