"""Værdimapping trin 1: hvilke GO-stavemåder er samme værdi?"""

from __future__ import annotations

import pytest

from konvertering import cli, vaerdier
from konvertering.vaerdier import DANSKE_KOMMUNER, ryd, saml, stavefejl


def _raekker(liste, data):
    return {r.go_vaerdi: r for r in saml(data)[liste]}


def test_98_kommuner():
    assert len(DANSKE_KOMMUNER) == 98


@pytest.mark.parametrize("raa,renset", [
    ("Børnehaven Hans Broges Vej [ 534]", "Børnehaven Hans Broges Vej [534]"),
    ("Børnehuset Skoven - Væksthuset [400}", "Børnehuset Skoven - Væksthuset [400]"),
    ("Vuggestuen Charlottehøj[148]", "Vuggestuen Charlottehøj [148]"),
    ("Skovslottet (sdt) [65]", "Skovslottet (SDT) [65]"),
    ("Anemonen, d.i.i.", "Anemonen, d.i.i"),
    ("  Mårslet  Skole ", "Mårslet Skole"),
])
def test_sikker_oprydning(raa, renset):
    assert ryd(raa) == renset


def test_store_og_smaa_bogstaver_er_auto():
    r = _raekker("bevillingssted", {"Betalingssted": ["SFO Elev skole [660]"],
                                    "Bevillingssted": ["SFO Elev Skole [660]"]})
    assert {x.foreslaaet_vaerdi for x in r.values()} == {"SFO Elev Skole [660]"}
    assert {x.status for x in r.values()} == {"auto"}


def test_samme_navn_uden_nummer_er_forslag():
    r = _raekker("institution", {"Institutionsnavn": ["2-Kløveren", "2-Kløveren [344]"]})
    assert r["2-Kløveren"].foreslaaet_vaerdi == "2-Kløveren [344]"
    assert r["2-Kløveren"].lis_nr == "344"
    assert r["2-Kløveren"].status == "forslag"


def test_kolonner_til_samme_liste_samles_og_vises():
    r = _raekker("institution", {"Institutionsnavn": ["Liffen"], "SkoleEllerInstitution": ["Liffen"]})
    assert r["Liffen"].go_kolonner == "Institutionsnavn, SkoleEllerInstitution"


def test_tal_foran_der_er_lis_nummeret():
    r = _raekker("bevillingssted", {"Betalingssted": ["158 Møllevang dagtilbud"],
                                    "Bevillingssted": ["Møllevang dagtilbud [158]"]})
    assert r["158 Møllevang dagtilbud"].foreslaaet_vaerdi == "Møllevang dagtilbud [158]"
    assert "= LIS-nummeret" in r["158 Møllevang dagtilbud"].begrundelse


def test_tal_foran_der_ikke_er_lis_nummeret_siges_tydeligt():
    r = _raekker("institution", {"SkoleEllerInstitution": ["407 Skæringhus", "Skæringhus [207]"]})
    assert r["407 Skæringhus"].foreslaaet_vaerdi == "Skæringhus [207]"
    assert "IKKE LIS-nummeret [207]" in r["407 Skæringhus"].begrundelse


def test_alene_med_tal_foran_mister_tallet_men_beder_om_tjek():
    r = _raekker("bevillingssted", {"Bevillingssted": ["1443 Område Horsensvej Puljer"]})
    x = r["1443 Område Horsensvej Puljer"]
    assert x.foreslaaet_vaerdi == "Område Horsensvej Puljer"
    assert x.status == "forslag"
    assert "tjek mod Jakobs liste" in x.begrundelse


def test_forskellige_tal_foran_samles_ikke():
    r = _raekker("institution", {"SkoleEllerInstitution": ["151 Smilehullet", "60 Smilehullet"]})
    assert r["151 Smilehullet"].foreslaaet_vaerdi != r["60 Smilehullet"].foreslaaet_vaerdi
    assert r["151 Smilehullet"].status == "afklar"


def test_to_lis_numre_samles_aldrig():
    r = _raekker("institution", {"SkoleEllerInstitution": [
        "Børnehaven Bjørnbakhus [525]", "Børnehaven Bjørnbakhus (SDT) [50]"]})
    assert len({x.foreslaaet_vaerdi for x in r.values()}) == 2


def test_samme_nummer_forskellige_navne_markeres():
    r = _raekker("institution", {"SkoleEllerInstitution": [
        "VokseVærket [130]", "Vuggestuen Storkereden [130]"]})
    assert {x.status for x in r.values()} == {"afklar"}
    assert "Storkereden" in r["VokseVærket [130]"].begrundelse


def test_sdt_ignoreres_men_siges():
    r = _raekker("institution", {"Institutionsnavn": ["Langenæsen (SDT)", "Langenæsen [501]"]})
    assert r["Langenæsen (SDT)"].foreslaaet_vaerdi == "Langenæsen [501]"
    assert "(SDT) ignoreret" in r["Langenæsen (SDT)"].begrundelse


@pytest.mark.parametrize("a,b,ens", [
    ("børenhuset valnødden", "børnehuset valnødden", True),
    ("område skanderborgvej særlige tilbud", "område skanderborgvej særligt tilbud", True),
    ("chr sogns menigheds børnehave", "chr sogns menighedsbørnehave", True),
    ("harlev dagtilbud", "hasle dagtilbud", False),
    ("ung i århus fritidscenter grenåvej vest", "ung i århus fritidscenter grenåvej øst", False),
    ("vest ungiårhus", "sydvest ungiårhus", False),
    ("vuggestuen trekanten (1)", "vuggestuen trekanten (2)", False),
])
def test_stavefejl(a, b, ens):
    assert bool(stavefejl(a, b)) is ens


def test_to_rigtige_kommuner_samles_ikke():
    r = _raekker("kommune", {"Betalingskommune": ["Skanderborg", "Sønderborg"]})
    assert {x.status for x in r.values()} == {"auto"}


def test_ikke_kommuner_og_kategorier_skal_afklares():
    assert _raekker("kommune", {"Betalingskommune": ["NU"]})["NU"].status == "afklar"
    assert _raekker("institution", {"SkoleEllerInstitution": ["Særl. bh."]})["Særl. bh."].status == "afklar"
    assert _raekker("bevillingssted", {"Betalingssted": ["PPR"]})["PPR"].status == "afklar"


def test_klassetrin_0_er_ikke_en_kategori():
    assert _raekker("klassetrin", {"Klassetrin": ["0", "10"]})["0"].status == "auto"


def test_xxx_og_aendret_skal_afklares():
    r = _raekker("bevillingssted", {"Bevillingssted": ["ÆNDRET - Trøjborg Dagtilbud"]})
    assert r["ÆNDRET - Trøjborg Dagtilbud"].status == "afklar"
    r = _raekker("institution", {"Institutionsnavn": ["XXXBørnehuset Tulshøjvej 5 [366]"]})
    assert r["XXXBørnehuset Tulshøjvej 5 [366]"].status == "afklar"


def test_sagsbehandler_springes_over_og_laeses_ikke(tmp_path):
    import openpyxl

    bog = openpyxl.Workbook()
    bog.active.title = "Klassetrin"
    bog.active.append(["Klassetrin"])
    bog.active.append(["3"])
    ark = bog.create_sheet("Sagsbehandler")
    ark.append(["Sagsbehandler"])
    ark.append(["Fiktiv Medarbejder"])
    sti = tmp_path / "v.xlsx"
    bog.save(sti)

    faner, sprunget_over = vaerdier.laes_xlsx(sti)
    assert set(faner) == {"Klassetrin"}
    assert sprunget_over == ["Sagsbehandler"]
    assert "Fiktiv Medarbejder" not in repr(faner)


def test_cli_ukendt_fane_giver_ren_fejl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vaerdier, "laes_xlsx", lambda sti: ({"Noget": ["x"]}, []))
    fil = tmp_path / "in.xlsx"
    fil.write_bytes(b"")
    assert cli.main(["vaerdier", "saml", str(fil), "--ud", str(tmp_path / "ud")]) == 1
    assert "Ukendte faner: Noget" in capsys.readouterr().err
    assert not (tmp_path / "ud").exists()


def test_ukendt_fane_afvises():
    with pytest.raises(ValueError, match="Ukendte faner"):
        saml({"Noget": ["x"]})


def test_cli_skriver_excel_og_overskriver_ikke(tmp_path, monkeypatch):
    import openpyxl

    monkeypatch.setattr(vaerdier, "laes_xlsx", lambda sti: ({"Klassetrin": ["1", "2"]}, []))
    fil = tmp_path / "in.xlsx"
    fil.write_bytes(b"")
    ud = tmp_path / "ud"
    assert cli.main(["vaerdier", "saml", str(fil), "--ud", str(ud)]) == 0
    bog = openpyxl.load_workbook(ud / "gennemsyn.xlsx")
    ark = bog["klassetrin"]
    raekker = list(ark.iter_rows(values_only=True))
    assert raekker[0][:4] == ("liste", "go_vaerdi", "go_kolonner", "foreslaaet_vaerdi")
    assert [r[1] for r in raekker[1:]] == ["1", "2"]
    assert ark.freeze_panes == "A2"

    foer = (ud / "gennemsyn.xlsx").read_bytes()
    assert cli.main(["vaerdier", "saml", str(fil), "--ud", str(ud)]) == 1
    assert (ud / "gennemsyn.xlsx").read_bytes() == foer
