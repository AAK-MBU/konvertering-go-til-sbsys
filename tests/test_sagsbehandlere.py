"""Sagsbehandlere -> AZ-ident. Kun opdigtede personer."""

from __future__ import annotations

from datetime import datetime

import openpyxl
import pytest

from konvertering import sagsbehandlere as sb
from konvertering.cli import main

BRUGERE = [
    # IDENT, NAVN, STARTDATO, SLUTDATO, AKTIV
    ("AZ11111", "Anna Testesen", datetime(2015, 1, 1), datetime(2019, 12, 31), "0"),
    ("AZ11111", "Anna Prøve Testesen", datetime(2020, 1, 1), None, "1"),   # navneskift
    ("AZ22222", "Bo Eksempel", datetime(2010, 1, 1), datetime(2018, 6, 30), "Nej"),
    ("AZ33333", "Jens Hansen", datetime(2012, 1, 1), None, "Ja"),
    ("AZ44444", "Jens Hansen", datetime(2016, 1, 1), None, "Ja"),           # navnebror
    ("az55555", "Mette Kirstine Fiktiv", datetime(2018, 1, 1), None, 1),
    ("AZ66666", "Sørensen, Lise", datetime(2018, 1, 1), None, True),
]


def _bog(sti, fane, hoved, raekker):
    bog = openpyxl.Workbook()
    ark = bog.active
    ark.title = fane
    ark.append(hoved)
    for r in raekker:
        ark.append(list(r))
    bog.save(sti)
    return sti


@pytest.fixture
def personer(tmp_path):
    return sb.laes_brugerliste(_bog(tmp_path / "b.xlsx", "Ark1", list(sb.BRUGER_KOLONNER), BRUGERE))


def test_brugerlisten_samles_pr_az(personer):
    assert set(personer) == {"az11111", "az22222", "az33333", "az44444", "az55555", "az66666"}
    anna = personer["az11111"]
    assert anna.navne == {"Anna Testesen", "Anna Prøve Testesen"}
    assert anna.seneste_navn == "Anna Prøve Testesen"
    assert anna.aktiv and anna.slutdato is None
    bo = personer["az22222"]
    assert not bo.aktiv and bo.slutdato == datetime(2018, 6, 30).date()


@pytest.mark.parametrize("go, az, status", [
    ("AZ22222", "az22222", sb.AUTO),
    ("ADM\\az22222", "az22222", sb.AUTO),
    ("Bo Eksempel (az22222)", "az22222", sb.AUTO),
    ("Bo Eksempl (AZ22222)", "az22222", sb.AUTO),         # stavefejl i navnet: AZ vinder
    ("Anna Testesen (az11111)", "az11111", sb.AUTO),       # gammelt navn
    ("Bo Hansen (az22222)", "az22222", sb.FORSLAG),        # efternavn passer ikke
    ("Jens Hansen (az22222)", "az22222", sb.AFKLAR),       # navnet er en ANDEN person
    ("Kaj Kælenavn (az22222)", "az22222", sb.FORSLAG),     # navnet passer til ingen
    ("AZ99999", "", sb.AFKLAR),                         # AZ ikke i listen
    ("anna  testesen", "az11111", sb.AUTO),             # gammelt navn, store/små
    ("Anna Prøve Testesen", "az11111", sb.AUTO),
    ("Jens Hansen", "", sb.AFKLAR),                     # navnebrødre
    ("Mette Fiktiv", "az55555", sb.FORSLAG),            # mellemnavn mangler
    ("Mette Kirstine Fiktive", "az55555", sb.FORSLAG),  # stavefejl
    ("Lise Sørensen", "az66666", sb.AUTO),              # "Efternavn, Fornavn" i listen
    ("MKF", "", sb.AFKLAR),                             # initialer
    ("Helt Ukendt Person", "", sb.AFKLAR),
])
def test_klassificering(personer, go, az, status):
    r = sb.Register(personer).klassificer(go)
    assert (r.foreslaaet_az, r.status) == (az, status), r.begrundelse


def test_navnebroedre_viser_kandidater(personer):
    r = sb.Register(personer).klassificer("Jens Hansen")
    assert len(r.kandidater) == 2 and all("Jens Hansen" in k for k in r.kandidater)


def test_endelig_az():
    assert sb.endelig_az({"rettet_vaerdi": "AZ12345", "foreslaaet_az": "az1"}) == "az12345"
    assert sb.endelig_az({"foreslaaet_az": "az1", "godkendt": "x", "status": "forslag"}) == "az1"
    assert sb.endelig_az({"foreslaaet_az": "az1", "status": "forslag"}) is None
    assert sb.endelig_az({"foreslaaet_az": "az1", "status": "auto"}) == "az1"
    assert sb.endelig_az({"foreslaaet_az": "az1", "status": "auto", "godkendt": "nej"}) is None


def _koer(tmp_path, ud, *ekstra):
    go = _bog(tmp_path / "distinkte_vaerdier.xlsx", "Sagsbehandler", ["Sagsbehandler"],
              [("Mette Fiktiv",), ("Jens Hansen",), ("AZ22222",), ("Mette Fiktiv",)])
    bl = _bog(tmp_path / "brugerliste.xlsx", "Ark1", list(sb.BRUGER_KOLONNER), BRUGERE)
    return main(["vaerdier", "sagsbehandlere", str(go), "--brugerliste", str(bl),
                 "--ud", str(ud), *ekstra])


def test_cli_skriver_gennemsyn_og_ingen_navne_i_terminalen(tmp_path, capsys):
    assert _koer(tmp_path, tmp_path) == 0
    ud = capsys.readouterr().out
    for navn in ("Mette", "Jens", "Hansen", "Eksempel", "Fiktiv"):
        assert navn not in ud
    raekker = sb.laes_gennemsyn(tmp_path / "sagsbehandlere-gennemsyn.xlsx")
    assert len(raekker) == 3                                      # dubletter fjernet
    assert {r["go_vaerdi"]: r["status"] for r in raekker}["AZ22222"] == sb.AUTO


def test_cli_naegter_at_skrive_i_repoet(tmp_path):
    assert _koer(tmp_path, sb.Path(__file__).resolve().parents[1] / "rapporter") == 2


def test_overfoer_bevarer_afgoerelser(tmp_path):
    assert _koer(tmp_path, tmp_path) == 0
    sti = tmp_path / "sagsbehandlere-gennemsyn.xlsx"
    bog = openpyxl.load_workbook(sti)
    ark = bog.active
    hoved = [c.value for c in ark[1]]
    for r in ark.iter_rows(min_row=2):
        if r[hoved.index("go_vaerdi")].value == "Jens Hansen":
            r[hoved.index("rettet_vaerdi")].value = "AZ44444"
        if r[hoved.index("go_vaerdi")].value == "Mette Fiktiv":
            r[hoved.index("godkendt")].value = "x"
    runde1 = tmp_path / "runde1.xlsx"
    bog.save(runde1)
    foer = {r["go_vaerdi"]: sb.endelig_az(r) for r in sb.laes_gennemsyn(runde1)}
    assert _koer(tmp_path, tmp_path, "--tidligere", str(runde1), "--overskriv") == 0
    efter = {r["go_vaerdi"]: sb.endelig_az(r) for r in sb.laes_gennemsyn(sti)}
    assert foer == efter == {"Mette Fiktiv": "az55555", "Jens Hansen": "az44444",
                             "AZ22222": "az22222"}


def _register(*brugere):
    personer = {}
    for az, navn in brugere:
        p = personer.setdefault(az, sb.Person(az))
        p.navne.add(navn)
        p.seneste_navn = navn
    return sb.Register(personer)


def test_flest_faelles_navne_vinder_blandt_navnebroedre():
    reg = _register(("az00001", "Ane Bodil Cirkel Dahl"),
                    ("az00002", "Ane Xenia Cirkel Dahl"),   # 3 af 4 - den rigtige
                    ("az00003", "Ane Dahl"),
                    ("az00004", "Ane Yrsa Dahl"))
    r = reg.klassificer("Ane Bodil Cirkel Dahl")
    assert (r.foreslaaet_az, r.status) == ("az00001", sb.AUTO)           # eksakt
    r = reg.klassificer("Ane Birte Cirkel Dahl")
    assert (r.foreslaaet_az, r.status) == ("", sb.AFKLAR)                # to deler 3
    reg = _register(("az00002", "Ane Xenia Cirkel Dahl"), ("az00003", "Ane Dahl"),
                    ("az00004", "Ane Yrsa Dahl"))
    r = reg.klassificer("Ane Birte Cirkel Dahl")
    assert (r.foreslaaet_az, r.status) == ("az00002", sb.FORSLAG)
    assert len(r.kandidater) == 3 and "az00002" in r.kandidater[0]


def test_kun_for_og_efternavn_til_faelles_forbliver_afklar():
    reg = _register(("az00003", "Ane Dahl"), ("az00004", "Ane Yrsa Dahl"))
    assert reg.klassificer("Ane Birte Dahl").status == sb.AFKLAR
