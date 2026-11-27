"""Tests af den form SBSYS faktisk vil have.

Stierne og kroppene her stammer fra AAK-MBU/api-sbsys-client, kommunens egen
delte SBSYS-klient, hvor sagsopslag og -søgning er verificeret mod
testmiljøet. Indtil da var vores egne stier gæt, og hver eneste af dem var
forkert: /api/version findes ikke, søgningen er en POST, og dokumenter
oprettes ikke i to kald.

Testene her holder fast i det vi nu VED, så ingen kommer til at gætte igen.
"""

from __future__ import annotations

import inspect

import pytest

from konvertering.sbsys.endpoints import (
    STANDARD_OPERATIONER,
    Endpoints,
    ManglendeOperation,
)


def test_livstegn_rammer_ikke_api_version():
    """/api/version gav 404 mod det rigtige testmiljø. Statuslisten er det
    billigste kald der faktisk findes."""
    op = Endpoints().operation("hent_version")
    assert op.sti != "/api/version"
    assert op.sti == "/api/sag/sagStatusList"


def test_soegning_er_en_post():
    """En GET på /api/sag/search gav 405."""
    assert Endpoints().operation("soeg_sag").metode == "POST"


def test_soegekrop_har_paging():
    """Uden PageNumber/PageSize afviser SBSYS kaldet."""
    krop = Endpoints().soegningskrop({"EksternNoegle": "GO:X"})
    assert krop["EksternNoegle"] == "GO:X"
    assert krop["PageNumber"] == 1
    assert krop["PageSize"] == 100


def test_kriterier_ligger_i_roden_som_standard():
    """Kommunens klient sender dem i roden, og den form er verificeret."""
    assert "Query" not in Endpoints().soegningskrop({"A": 1})


def test_indpakning_kan_slaas_til_uden_kodeaendring():
    """Kræver en anden API-version et omsluttende objekt, skal det kunne
    sættes i endpoints.toml."""
    e = Endpoints(soegning={"indpak": "Query"})
    krop = e.soegningskrop({"A": 1})
    assert krop["Query"] == {"A": 1}
    assert krop["PageNumber"] == 1, "paging hører i roden, ikke inde i indpakningen"


@pytest.mark.parametrize(
    "svar,forventet",
    [
        ({"Results": [{"Id": 1}], "TotalNumberOfResults": 1}, 1),
        ([{"Id": 1}, {"Id": 2}], 2),
        ({"Results": None}, 0),
        ({}, 0),
        (None, 0),
    ],
)
def test_traef_laeses_uanset_svarform(svar, forventet):
    assert len(Endpoints().soegningstraef(svar)) == forventet


def test_sag_oprettes_fra_skabelon():
    """Der findes ikke et felt-for-felt POST /api/sag. Det ændrer
    sagsmodellen — se A15."""
    op = Endpoints().operation("opret_sag")
    assert op.sti == "/api/sag/template"


def test_sag_opdateres_med_put_af_hele_sagen():
    """SBSYS har ingen PATCH. Hent sagen, læg ændringen ovenpå, send retur."""
    assert Endpoints().operation("opdater_sag", sag_id="1").metode == "PUT"


def test_dokument_journaliseres_i_eet_kald():
    """To kald (opret dokument, upload fil) findes ikke i SBSYS."""
    e = Endpoints()
    assert e.operation("journaliser").sti == "/api/dokument/journaliser"
    with pytest.raises(ManglendeOperation):
        e.operation("upload_fil")


def test_dokumentloaderen_sender_fil_og_metadata_sammen():
    """Værn mod at nogen deler kaldet op igen."""
    import inspect

    from konvertering.loaders import dokument

    kilde = inspect.getsource(dokument)
    assert "journaliser" in kilde
    assert "upload_fil" not in kilde
    assert 'data={"json"' in kilde, "metadata skal med som JSON-streng i formfeltet"


# --- eksempelfilen skal kunne læses ---------------------------------
#
# En operation hed "journalisér". TOML tillader kun A-Za-z0-9_- i en bar
# nøgle, så [operationer.journalisér] fik tomllib til at kaste — og
# fejlen kom først når nogen kopierede eksempelfilen og kørte en rigtig
# indlæsning. Den slags skal fanges her.

def test_eksempelfilen_er_gyldig_toml():
    import tomllib
    from pathlib import Path

    tekst = Path("sbsys/endpoints.example.toml").read_text(encoding="utf-8")
    tomllib.loads(tekst)


def test_eksempelfilen_kan_indlaeses_som_endpoints(tmp_path):
    import shutil
    from pathlib import Path

    kopi = tmp_path / "endpoints.toml"
    shutil.copy(Path("sbsys/endpoints.example.toml"), kopi)
    e = Endpoints.fra_fil(kopi)

    assert e.operation("journaliser").sti == "/api/dokument/journaliser"
    assert e.felt("dokument", "ekstern_noegle") == "Eksternid"


@pytest.mark.parametrize("navn", sorted(STANDARD_OPERATIONER))
def test_operationsnavne_kan_skrives_i_toml(navn):
    """Ethvert operationsnavn skal kunne stå som [operationer.<navn>]."""
    import re
    import tomllib

    assert re.fullmatch(r"[A-Za-z0-9_-]+", navn), (
        f"'{navn}' kan ikke være en bar TOML-nøgle — undgå æøå og accenter"
    )
    tomllib.loads(f'[operationer.{navn}]\nmetode = "GET"\nsti = "/x"')


# --- indlejrede felter ----------------------------------------------
#
# SBSYS peger på flere ting ved reference frem for ved værdi. Vi sendte
# afdelingen som en flad værdi, og SBSYS svarede
# "Kunne ikke finde ansættelsessted med ekstern ID ''" — den ville have
# {"Ansaettelsessted": {"ExternalKey": "..."}}.
#
# Hvilken form det er, skal kunne rettes i endpoints.toml, ikke i koden.

def test_punktum_giver_indlejret_objekt():
    e = Endpoints(felter={"sag": {"afdeling": "Ansaettelsessted.ExternalKey"}})
    payload = e.byg_payload("sag", {"afdeling": "AFD-4711"})
    assert payload["Ansaettelsessted"] == {"ExternalKey": "AFD-4711"}


def test_flere_felter_deler_samme_objekt():
    e = Endpoints(felter={"sag": {
        "afdeling": "Ansaettelsessted.ExternalKey",
        "afdeling_id": "Ansaettelsessted.AnsaettelsesstedId",
    }})
    payload = e.byg_payload("sag", {"afdeling": "AFD-1", "afdeling_id": 7})
    assert payload["Ansaettelsessted"] == {"ExternalKey": "AFD-1", "AnsaettelsesstedId": 7}


def test_flade_felter_er_uaendrede():
    payload = Endpoints().byg_payload("sag", {"titel": "Sag"})
    assert payload == {"SagsTitel": "Sag"}


def test_none_udelades_ogsaa_i_indlejrede_felter():
    """Et tomt Ansaettelsessted-objekt er værre end ingen: SBSYS svarer
    "kunne ikke finde ansættelsessted med ekstern ID ''"."""
    assert Endpoints().byg_payload("sag", {"afdeling": None}) == {}


def test_modstrid_mellem_vaerdi_og_objekt_fanges():
    e = Endpoints(felter={"sag": {"a": "X", "b": "X.Y"}})
    with pytest.raises(ManglendeOperation):
        e.byg_payload("sag", {"a": 1, "b": 2})


def test_id_sendes_som_tal_ikke_streng():
    """Kodelisterne er CSV, saa alt derfra er tekst — men SBSYS' id-felter
    er heltal, og "4711" afvises hvor 4711 accepteres."""
    payload = Endpoints().byg_payload("sag", {"afdeling": "4711"})
    # Ansaettelsessted er en BaseUuidDto {Id, Uuid, Navn} paa CreateSagFromSkabelonDto
    assert payload["Ansaettelsessted"] == {"Id": 4711}


def test_kun_rene_cifre_i_id_felter_omsaettes():
    """Et sagsnummer som 17.01.05 er ikke et tal, og må ikke røres."""
    e = Endpoints(felter={"sag": {"kle": "SagsNummerId"}})
    assert e.byg_payload("sag", {"kle": "17.01.05"}) == {"SagsNummerId": "17.01.05"}


def test_almindelige_felter_omsaettes_ikke():
    assert Endpoints().byg_payload("sag", {"titel": "2024"}) == {"SagsTitel": "2024"}


def test_opslag_lander_ikke_i_mapping():
    """`konv mapping load` indlæser hver .csv i mapping/ som en kodeliste.
    En opslagsfil dér ville blive slugt med forkerte kolonner."""
    import inspect

    from konvertering import cli

    kilde = inspect.getsource(cli._opslag)
    assert 'Path("rapporter")' in kilde
    assert 'Path("mapping")' not in kilde


# --- parter ----------------------------------------------------------
#
# "Skabelonen kræver at der medsendes parter". SBSYS vil have sit eget
# PartId, ikke et CPR-nummer, så opslaget er første skridt i enhver
# sagsoprettelse.

def test_parten_peges_paa_med_sbsys_eget_id():
    e = Endpoints()
    assert e.felt("part", "part_id") == "PartId"
    assert e.felt("part", "part_type") == "PartType"


def test_sagen_har_baade_primaer_part_og_partsliste():
    e = Endpoints()
    assert e.felt("sag", "primaer_part") == "PrimaryPart"
    assert e.felt("sag", "parter") == "Parts"


def test_sagsloaderen_slaar_parten_op_foer_oprettelse():
    """Værn mod at nogen sender et CPR-nummer direkte i payloaden."""
    import inspect

    from konvertering.loaders import sag

    kilde = inspect.getsource(sag)
    assert "_part_reference" in kilde
    assert "soeg_person" in kilde
    assert "_part_cache" in kilde, "samme borger må ikke slås op tusindvis af gange"


def test_sag_uden_part_afvises_og_stopper_ikke_boelgen():
    """En emnesag uden borger er spørgsmål A16 — den skal afvises enkeltvis."""
    import inspect

    from konvertering.loaders import sag

    kilde = inspect.getsource(sag.SagLoader._part_reference)
    assert "ValideringsFejl" in kilde
    assert "mangler_part" in kilde


# --- journalnotater ---------------------------------------------------

def test_notatfelter_matcher_snitfladen():
    e = Endpoints()
    assert e.felt("notat", "sag_id") == "SagID"
    assert e.felt("notat", "titel") == "Overskrift"
    assert e.felt("notat", "tekst") == "Note"
    assert e.felt("notat", "dato") == "KontaktTidspunkt"


def test_notattekst_escapes_som_standard():
    """SBSYS renderer notefeltet som HTML. Et "<" i en adresse ville ellers
    blive til begyndelsen paa et tag, og notatet ser forkert ud."""
    from types import SimpleNamespace

    from konvertering.loaders.notat import NotatLoader

    loader = NotatLoader(SimpleNamespace(endpoints=Endpoints()))
    tekst = loader._tekst(SimpleNamespace(tekst="Bor <hos> mor\nog far"))
    assert "&lt;hos&gt;" in tekst
    assert "<br>" in tekst, "linjeskift skal bevares, ellers er notatet ulæseligt"


def test_notattekst_windows_linjeskift_giver_rent_br():
    """GO har \\r\\n. Dry-run 17-11-2026 gav '\\r<br>' — \\r blev hængende."""
    from types import SimpleNamespace

    from konvertering.loaders.notat import NotatLoader

    loader = NotatLoader(SimpleNamespace(endpoints=Endpoints()))
    assert loader._tekst(SimpleNamespace(tekst="a\r\nb\rc\nd")) == "a<br>b<br>c<br>d"


def test_notattekst_kan_vaere_html_allerede():
    from types import SimpleNamespace

    from konvertering.loaders.notat import NotatLoader

    e = Endpoints(soegning={"notat_er_html": True})
    loader = NotatLoader(SimpleNamespace(endpoints=e))
    assert loader._tekst(SimpleNamespace(tekst="<p>Hej</p>")) == "<p>Hej</p>"


# --- GO-nøglen skrives IKKE ind i SBSYS ------------------------------
#
# Besluttet 11-11-2026. Koblingen mellem GO og SBSYS lever udelukkende i
# konv.id_map. Feltnavnene står stadig i endpoints.py, fordi de er en
# egenskab ved snitfladen — men de må ikke udfyldes. En payload der
# alligevel bærer nøglen, ville skrive vores interne id ind i en
# børnesag, og det er ikke til at trække tilbage bagefter.

def _kode(fn) -> str:
    """Kildeteksten uden kommentarer. Kommentarerne FORTÆLLER om nøglen —
    det er koden der ikke må skrive den."""
    linjer = [
        linje for linje in inspect.getsource(fn).splitlines()
        if not linje.lstrip().startswith("#")
    ]
    return "\n".join(linjer)


def test_sag_payload_baerer_ikke_go_noeglen():
    from konvertering.loaders import sag

    kilde = _kode(sag.SagLoader.payload)
    assert "ekstern_noegle" not in kilde
    assert "GO:" not in kilde


def test_dokument_payload_baerer_ikke_go_noeglen():
    from konvertering.loaders import dokument

    kilde = _kode(dokument.DokumentLoader.payload)
    assert "ekstern_noegle" not in kilde
    assert "GO:" not in kilde


def test_ingen_loader_skriver_go_praefikset():
    """Den brede kontrol: dukker 'GO:' op i en payload igen, er det et
    tilbagefald til ADR-0001's gamle form."""
    from pathlib import Path

    for sti in Path("src/konvertering/loaders").glob("*.py"):
        tekst = sti.read_text(encoding="utf-8")
        for nr, linje in enumerate(tekst.splitlines(), 1):
            if linje.lstrip().startswith("#"):
                continue
            assert 'f"GO:' not in linje, f"{sti.name}:{nr} skriver GO-nøglen ind i SBSYS"


def test_sagen_genfindes_paa_naturlig_noegle():
    """Uden et nøglefelt er part + titel det eneste tilbage — og den
    søgning SKAL afgrænses af parten, ellers rammer den hele SBSYS."""
    from konvertering.loaders import sag

    kilde = inspect.getsource(sag.SagLoader._find_identisk)
    assert "primaer_part" in kilde
    assert "titel" in kilde
    assert "len(traef) == 1" in kilde, "flere træf må ikke give et gæt"
