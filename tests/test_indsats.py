"""Tests af indsatser (foranstaltninger).

Tre beslutninger fra 09-11-2026 former loaderen, og det er dem testene
her holder fast i:

* **Økonomi er ikke en del af konverteringen.** Kaldet tager vederlag,
  tillægspris og engangstillæg — vi sender dem ikke.
* **GO-nøglen må ikke skrives ind i SBSYS.** Så findes ADR-0001's
  sikkerhedsnet ikke, og genkendelsen må hvile på en naturlig nøgle:
  sag + foranstaltning + tilbud + startdato.
* **Mapningen kommer senere.** Indtil da skal indsatsen afvises pænt, ikke
  sendes med huller i.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from konvertering.loaders.indsats import ENHEDSTYPER, IndsatsLoader
from konvertering.models import Indsats
from konvertering.sbsys.endpoints import Endpoints
from konvertering.sbsys.errors import ValideringsFejl


def _loader(**kontekst: Any) -> IndsatsLoader:
    k = SimpleNamespace(endpoints=Endpoints(), **{"bu_klient": None, **kontekst})
    return IndsatsLoader(k)


def _indsats(**felter: Any) -> Indsats:
    standard = {
        "kilde_noegle": "TEST-INDSATS-001",
        "sag_noegle": "TEST-SAG-001",
        "paragraf": "1",
        "foranstaltning": "2",
        "tilbud": "3",
        "periode_start": datetime(2023, 2, 1),
    }
    return Indsats(**{**standard, **felter})


# --- økonomi holdes ude ---------------------------------------------

def test_oekonomifelter_sendes_ikke():
    """Kaldet tager dem, men økonomi er ikke en del af konverteringen."""
    krop = _loader().payload(_indsats(antal_enheder=12, enhedstype="Time"))

    for felt in ("antalVederlag", "tillaegsPrisPerEnhed", "engangsTillaeg", "antalDoegn"):
        assert felt not in krop, f"{felt} er økonomi og hører ikke med"


def test_periode_og_omfang_sendes():
    krop = _loader().payload(
        _indsats(periode_slut=datetime(2023, 12, 31), antal_enheder=12, enhedstype="Time")
    )
    assert krop["periodeStart"].startswith("2023-02-01")
    assert krop["periodeSlut"].startswith("2023-12-31")
    assert krop["antalEnheder"] == 12
    assert krop["antalEnhederEnhedsType"] == "Time"


def test_aaben_indsats_har_ingen_slutdato():
    """En løbende indsats skal IKKE have en opfundet slutdato."""
    assert "periodeSlut" not in _loader().payload(_indsats())


def test_id_er_sendes_som_tal():
    """.NET-enden er int32. "1" er ikke 1."""
    krop = _loader().payload(_indsats())
    assert krop["paragrafId"] == 1
    assert krop["foranstaltningId"] == 2
    assert krop["tilbudId"] == 3


# --- validering ------------------------------------------------------

def test_indsats_uden_startdato_afvises():
    """En indsats uden periode er ikke en indsats."""
    with pytest.raises(ValideringsFejl) as e:
        _loader().payload(_indsats(periode_start=None))
    assert "periode" in str(e.value).lower()


def test_enheder_uden_enhedstype_afvises():
    """12 hvad? SBSYS' enum tager kun de seks værdier, og at gætte på
    "timer" vs "Time" ville give en 400 langt senere."""
    with pytest.raises(ValideringsFejl) as e:
        _loader().payload(_indsats(antal_enheder=12, enhedstype=None))
    assert "enhedstype" in str(e.value).lower()


@pytest.mark.parametrize("enhed", ENHEDSTYPER)
def test_sbsys_egne_enhedstyper_accepteres(enhed):
    assert _loader().payload(_indsats(antal_enheder=1, enhedstype=enhed))[
        "antalEnhederEnhedsType"
    ] == enhed


def test_uden_bu_base_url_afvises_med_en_forklaring():
    """Ikke en AttributeError: Børn og Unge-API'et er en anden tjeneste,
    og det skal fejlen sige."""
    loader = _loader(idmap=SimpleNamespace(sbsys_id=lambda *_: "42"))
    with pytest.raises(ValideringsFejl) as e:
        loader.send(_indsats(), {"paragrafId": 1})
    assert "SBSYS_BU_BASE_URL" in str(e.value)


# --- naturlig nøgle i stedet for ekstern -----------------------------
#
# GO-nøglen må ikke skrives ind i SBSYS, så en indsats genkendes på sag,
# foranstaltning, tilbud og startdato. Uden den kontrol ville et tabt svar
# give en dublet ved næste kørsel.

def _med_soegesvar(traef: list[dict]) -> IndsatsLoader:
    kaldte: list[str] = []

    class Klient:
        def kald(self, _m, sti, **_kw):
            kaldte.append(sti)
            return SimpleNamespace(status=200, krop=traef, korrelations_id="x")

    loader = _loader(bu_klient=Klient())
    loader.kaldte = kaldte  # type: ignore[attr-defined]
    return loader


_KROP = {"sagId": 42, "paragrafId": 1, "foranstaltningId": 2, "tilbudId": 3,
         "periodeStart": "2023-02-01T00:00:00"}


def test_identisk_indsats_genkendes():
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) == "77"


def test_anden_startdato_er_en_anden_indsats():
    """Samme tilbud i to perioder er to indsatser, ikke én."""
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2024-01-15T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) is None


def test_flere_ens_traef_giver_ikke_et_gaet():
    """To ens indsatser: at vælge den ene ville hægte alt efterfølgende på
    den forkerte. Bedre at lade den fejle."""
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    assert _med_soegesvar([r, {**r, "id": 78}])._find_identisk(_KROP) is None


def test_opslaget_afgraenses_til_sagen():
    """Sagens egen liste frem for en søgning: ét id vi har i forvejen, og
    ingen kriterienavne at gætte på."""
    loader = _med_soegesvar([])
    loader._find_identisk(_KROP)
    assert loader.kaldte[0] == "/bu_api/sag/42/sag_foranstaltninger"  # type: ignore[attr-defined]


def test_periodestart_taeller_ogsaa_som_startdato():
    """Oprettelsen hedder periodeStart, og udlæsningen kan hedde det samme.
    Et felt der ikke genkendes, ville give en dublet ved hver kørsel."""
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "periodeStart": "2023-02-01T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) == "77"


# --- registrering ----------------------------------------------------

def test_indsats_er_en_objekttype_man_kan_koere():
    from konvertering.loaders import LOADERS

    assert "indsats" in LOADERS
    assert LOADERS["indsats"].arbejdsliste == "stg.v_indsatser_til_indlaesning"


def test_stierne_peger_paa_bu_api():
    e = Endpoints()
    assert e.operation("opret_indsats").sti == "/bu_api/sag_foranstaltning"
    assert e.operation("soeg_indsats").sti == "/bu_api/sag_foranstaltning/search"
    assert e.operation("hent_katalog").sti == "/bu_api/foranstaltning/katalog"
    assert e.operation("hent_sag_indsatser", sag_id=7).sti == (
        "/bu_api/sag/7/sag_foranstaltninger")
    # Livstegnet ligger under /api/, ikke /bu_api/ — en stavefejl her ville
    # få selvtesten til at melde BU-værten død.
    assert e.operation("bu_livstegn").sti == "/api/health"


# --- kataloget er mapningskilden -------------------------------------

def test_kataloget_foldes_ud_til_tripler():
    """Kataloget kommer som et træ, men det er tripler fagpersonen skal
    vælge imellem — og tripler kaldet kræver."""
    from konvertering.cli import _fold_katalog

    traeet = [{
        "id": 11, "navn": "SEL 11.3",
        "foranstaltninger": [{
            "id": 22, "navn": "Psykologsamtaler",
            "tilbuds": [{"id": 33, "navn": "PPR intern"}, {"id": 34, "navn": "Ekstern"}],
        }],
    }]

    flade = _fold_katalog(traeet)

    assert len(flade) == 2
    assert flade[0] == {
        "paragrafId": 11, "paragraf": "SEL 11.3",
        "foranstaltningId": 22, "foranstaltning": "Psykologsamtaler",
        "tilbudId": 33, "tilbud": "PPR intern",
    }


def test_foranstaltning_uden_tilbud_forsvinder_ikke():
    """Ellers mangler den i den liste fagpersonen skal tage stilling ud fra."""
    from konvertering.cli import _fold_katalog

    flade = _fold_katalog([{"id": 1, "navn": "P", "foranstaltninger": [{"id": 2, "navn": "F"}]}])
    assert len(flade) == 1
    assert flade[0]["tilbudId"] is None
