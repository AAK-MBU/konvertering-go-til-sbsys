"""Tilbudsfelter (src/konvertering/tilbudsfelter.py).

Felter, typer og lister herunder er klippet fra de rigtige svar 18-11-2026:
Psykolog i testmiljøet (indsats 67) og ABA i produktion, hvor Timer blev
skiftet fra heltal til decimaltal og den gamle type deaktiveret.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from konvertering.mapping import Kodelister, Opslag
from konvertering.sbsys.endpoints import Endpoints
from konvertering.tilbudsfelter import (
    TilbudsfeltKatalog,
    byg_feltvaerdier,
    dato,
    eksterne_kandidater,
    timer,
)

TYPER = [
    {"id": 1, "navn": "Visiteret", "aktiv": True, "vaerdiType": "Datetime"},
    {"id": 2, "navn": "Sagsbehandler", "aktiv": True, "vaerdiType": "Predefined",
     "tilbudFeltVaerdiListeId": 103},
    {"id": 5, "navn": "Timer", "aktiv": False, "vaerdiType": "Int"},       # gammel
    {"id": 11, "navn": "Indsats", "aktiv": True, "vaerdiType": "Predefined",
     "tilbudFeltVaerdiListeId": 104},
    {"id": 25, "navn": "Timer", "aktiv": True, "vaerdiType": "Decimal"},   # ny
]
FELTER = {
    1: [  # "Psykolog"
        {"id": 3, "tilbudFeltTypeId": 1, "aktiv": True},
        {"id": 12, "tilbudFeltTypeId": 2, "aktiv": True},
        {"id": 13, "tilbudFeltTypeId": 11, "aktiv": True},
    ],
    7: [  # "ABA" — både den gamle og den nye Timer hænger på
        {"id": 5, "tilbudFeltTypeId": 5, "aktiv": True},
        {"id": 55, "tilbudFeltTypeId": 25, "aktiv": True},
    ],
}
MULIGHEDER = {
    104: [{"id": 613, "vaerdiMulighed": "Indstilling", "aktiv": True},
          {"id": 615, "vaerdiMulighed": "Følgeskab", "aktiv": True}],
    103: [{"id": 608, "vaerdiMulighed": "fornavn efternavn", "aktiv": True}],
}


class Klient:
    def __init__(self, eksterne: list[dict] | None = None) -> None:
        self.kald_liste: list[str] = []
        self._eksterne = eksterne or []

    def kald(self, _metode, sti, **_kw):
        self.kald_liste.append(sti)
        if sti == "/bu_api/tilbudfelttype":
            krop: Any = TYPER
        elif sti.startswith("/bu_api/tilbud/"):
            krop = FELTER[int(sti.split("/")[3])]
        elif sti.startswith("/bu_api/tilbudfeltvaerdiliste/"):
            krop = MULIGHEDER[int(sti.split("/")[3])]
        elif sti == "/bu_api/externalreference/tilbudfeltvaerdilistemulighed":
            krop = self._eksterne
        else:
            raise AssertionError(sti)
        return SimpleNamespace(status=200, krop=krop)


def _kodelister(**ekstra: str) -> Kodelister:
    poster = {
        ("tilbudsfelt", "Psykolog|Visiteret"): Opslag("Visiteret", "map"),
        ("tilbudsfelt", "Psykolog|Sagsbehandler"): Opslag("Sagsbehandler", "map"),
        ("tilbudsfelt", "Psykolog|Indstillingsaarsag"): Opslag("Indsats", "map"),
        ("tilbudsfelt", "Psykolog|Aktivitet"): Opslag("Aktivitet", "map"),
        ("tilbudsfelt", "ABA|Timer"): Opslag("Timer", "map"),
        ("tilbudsfeltvaerdi", "Indstillingsaarsag|xxxTilflytter"): Opslag("Indstilling", "map"),
    }
    poster.update({("tilbudsfelt", k): Opslag(v, "map") for k, v in ekstra.items()})
    return Kodelister(poster)


def _byg(type_: str, tilbud: int, go: dict, klient: Klient | None = None, **kodelister):
    katalog = TilbudsfeltKatalog(klient or Klient(), Endpoints())
    return byg_feltvaerdier(katalog, _kodelister(**kodelister), type_, tilbud, go)


# --- formatet testmiljøet godtog (indsats 67) -----------------------------

def test_psykolog_som_indsats_67():
    u = _byg("Psykolog", 1, {"Visiteret": "20-05-2024", "Indstillingsaarsag": "Indstilling"})
    assert u.felt_vaerdier == [
        {"tilbudFeltId": 13, "tilbudFeltVaerdiListeMulighedId": 613},
        {"tilbudFeltId": 3, "vaerdi": "2024-05-20T00:00:00"},
    ]
    assert not u.advarsler


def test_go_vaerdi_oversaettes_foer_listeopslaget():
    """Arkets "Nej"-værdi xxxTilflytter -> Indstilling."""
    u = _byg("Psykolog", 1, {"Indstillingsaarsag": "xxxTilflytter"})
    assert u.felt_vaerdier == [{"tilbudFeltId": 13, "tilbudFeltVaerdiListeMulighedId": 613}]


def test_tomme_go_felter_sendes_ikke():
    u = _byg("Psykolog", 1, {"Visiteret": "  ", "Sagsbehandler": None})
    assert u.felt_vaerdier == [] and u.advarsler == []


def test_kolonner_der_ikke_er_mappet_til_typen_ignoreres():
    """ABAGruppe på en Psykolog-række er ikke et Psykolog-felt."""
    assert _byg("Psykolog", 1, {"ABAGruppe": "Fase 1 (3 år)"}).felt_vaerdier == []


# --- det der ikke kan placeres: udelades, afviser aldrig ------------------

def test_ukendt_listevaerdi_udelades_med_advarsel():
    u = _byg("Psykolog", 1, {"Indstillingsaarsag": "Findes ikke"})
    assert u.felt_vaerdier == []
    assert "valgmulighed" in u.advarsler[0].besked
    assert u.advarsler[0].vaerdi == "Findes ikke"


def test_felt_der_ikke_findes_paa_tilbuddet_udelades():
    """Fx et Bortfalder-felt som endnu ikke er oprettet i SBSYS."""
    u = _byg("Psykolog", 1, {"Aktivitet": "Test"})
    assert u.felt_vaerdier == []
    assert "Aktivitet" in u.advarsler[0].besked


def test_sagsbehandlerens_vaerdi_logges_aldrig():
    u = _byg("Psykolog", 1, {"Sagsbehandler": "Fornavn Efternavn (az12345)"})
    assert u.felt_vaerdier == []
    assert u.advarsler[0].vaerdi is None


def test_sagsbehandler_matches_paa_ekstern_noegle():
    """DWH-listerne: AZ-identen er nøglen (Jakob, 18-11-2026)."""
    klient = Klient(eksterne=[{"tilbudFeltVaerdiListeMulighedId": 608, "externalKey": "AZ12345"}])
    u = _byg("Psykolog", 1, {"Sagsbehandler": "Fornavn Efternavn (az12345)"}, klient)
    assert u.felt_vaerdier == [{"tilbudFeltId": 12, "tilbudFeltVaerdiListeMulighedId": 608}]


# --- kun aktive typer: Timer i produktion ---------------------------------

def test_den_deaktiverede_timer_type_bruges_ikke():
    u = _byg("ABA", 7, {"Timer": "7:30"})
    assert u.felt_vaerdier == [{"tilbudFeltId": 55, "vaerdi": "7.5"}]


# --- omsætning ------------------------------------------------------------

@pytest.mark.parametrize("go, forventet", [
    ("7:30", Decimal("7.5")), ("4:45", Decimal("4.75")), ("22:00", Decimal("22")),
    ("0:00", Decimal("0")), ("1:75", Decimal("1.75")), ("3.45", Decimal("3.45")),
    ("22", Decimal("22")), ("2,5", Decimal("2.5")),
])
def test_timer(go, forventet):
    assert timer(go) == forventet


def test_timer_der_ikke_er_et_tal():
    assert timer("cirka 5") is None


@pytest.mark.parametrize("go", ["20-05-2024", "2024-05-20"])
def test_dato(go):
    assert dato(go) == "2024-05-20T00:00:00"


def test_katalog_hentes_kun_én_gang():
    klient = Klient()
    katalog = TilbudsfeltKatalog(klient, Endpoints())
    for _ in range(3):
        byg_feltvaerdier(katalog, _kodelister(), "Psykolog", 1,
                         {"Visiteret": "20-05-2024", "Indstillingsaarsag": "Indstilling"})
    assert klient.kald_liste.count("/bu_api/tilbudfelttype") == 1
    assert klient.kald_liste.count("/bu_api/tilbud/1/tilbudfelt") == 1


def test_indsatsens_payload_faar_feltvaerdier_fra_raa_json():
    import json

    from konvertering.loaders.indsats import IndsatsLoader

    kodelister = _kodelister()
    kodelister._poster.update({(liste, "Psykolog"): Opslag("1", "map")
                               for liste in ("paragraf", "foranstaltning", "tilbud")})
    loader = IndsatsLoader(SimpleNamespace(
        endpoints=Endpoints(), bu_klient=Klient(), kodelister=kodelister))
    raekke = {"kilde_noegle": "K", "sag_noegle": "S", "indholdstype_go": "Psykolog",
              "periode_start": None,
              "raa_json": json.dumps({"SagsId": "S", "Indholdstype": "Psykolog",
                                      "Visiteret": "20-05-2024",
                                      "Indstillingsaarsag": "Findes ikke"})}
    krop = loader.payload(loader.byg(raekke))
    assert krop["feltVaerdier"] == [{"tilbudFeltId": 3, "vaerdi": "2024-05-20T00:00:00"}]
    # Advarslen gemmes til send(), der skriver den i konv.afvist efter oprettelsen
    assert [a.kolonne for a in loader._advarsler["K"]] == ["Indstillingsaarsag"]


# --- ingen gæt på institutionsnumre --------------------------------------

@pytest.mark.parametrize("go", ["Dagplejen Harlev [495]", "158 Møllevang dagtilbud",
                                "Harlev Dagtilbud"])
def test_institutionsnumre_bruges_ikke_som_noegle(go):
    """Bevidst fravalgt 18-11-2026: tallet er ikke sikkert listens nøgle,
    og et forkert match ville lande lydløst i SBSYS."""
    assert not eksterne_kandidater(go)


def test_az_ident_er_eneste_eksterne_noegle():
    assert eksterne_kandidater("Fornavn Efternavn (AZ12345)") == {"az12345"}


# --- SBSYS tjekker ikke at valgmuligheden hører til feltets liste -------
#
# Testmiljøet 18-11-2026 (indsats 72): valgmulighed 613 fra Indstillings-
# årsag-listen i ABA's Institution-felt gav HTTP 200, og feltet stod
# ødelagt-tomt i brugerfladen. SBSYS beskytter os ikke — koden skal.

def test_valgmuligheden_kommer_altid_fra_feltets_egen_liste():
    """Selv når teksten findes i en ANDEN liste, må dens id ikke bruges."""
    MULIGHEDER[202] = [{"id": 700, "vaerdiMulighed": "Møllevang Dagtilbud", "aktiv": True}]
    TYPER.append({"id": 31, "navn": "Institution", "aktiv": True, "vaerdiType": "Predefined",
                  "tilbudFeltVaerdiListeId": 202})
    FELTER[10] = [{"id": 16, "tilbudFeltTypeId": 31, "aktiv": True}]
    try:
        # "Indstilling" findes i liste 104 (id 613), men ikke i feltets liste 202
        u = _byg("ABA", 10, {"SkoleEllerInstitution": "Indstilling"},
                 **{"ABA|SkoleEllerInstitution": "Institution"})
        assert u.felt_vaerdier == []
        u = _byg("ABA", 10, {"SkoleEllerInstitution": "Møllevang Dagtilbud"},
                 **{"ABA|SkoleEllerInstitution": "Institution"})
        assert u.felt_vaerdier == [{"tilbudFeltId": 16, "tilbudFeltVaerdiListeMulighedId": 700}]
    finally:
        MULIGHEDER.pop(202)
        TYPER.pop()
        FELTER.pop(10)


# --- handling = ignorer: feltet tomt, uden advarsel -----------------------

def test_ignoreret_vaerdi_giver_tomt_felt_uden_advarsel():
    """Betalingssted "PPR" findes ikke i den nye opsætning (20-11-2026)."""
    kodelister = _kodelister()
    kodelister._poster[("tilbudsfeltvaerdi", "Indstillingsaarsag|PPR")] = Opslag(None, "ignorer")
    katalog = TilbudsfeltKatalog(Klient(), Endpoints())
    u = byg_feltvaerdier(katalog, kodelister, "Psykolog", 1, {"Indstillingsaarsag": "PPR"})
    assert u.felt_vaerdier == [] and u.advarsler == []
