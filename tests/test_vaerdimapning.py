"""Værdimapping trin 2 (src/konvertering/vaerdimapning.py)."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import openpyxl

from konvertering import vaerdimapning as vm
from konvertering.cli import main

HOVED = ["liste", "go_kolonner", "go_vaerdi", "foreslaaet_vaerdi", "lis_nr", "status",
         "godkendt", "rettet_vaerdi", "kommentar", "begrundelse"]


def _gennemsyn(sti: Path, raekker: list[list]) -> Path:
    bog = openpyxl.Workbook()
    ark = bog.active
    ark.title = "bevillingssted"
    ark.append(HOVED)
    for r in raekker:
        ark.append(r)
    bog.save(sti)
    return sti


def _r(go, forslag, status, godkendt=None, rettet=None, kolonner="Betalingssted"):
    return ["bevillingssted", kolonner, go, forslag, None, status, godkendt, rettet, None, ""]


SBSYS = {"bevillingssted": [
    {"id": 424, "vaerdiMulighed": "Mårslet Skole [693]", "aktiv": True},
    {"id": 353, "vaerdiMulighed": "Børnehaven Svend-Åge (SDT) [29]", "aktiv": True},
    {"id": 500, "vaerdiMulighed": "Hasle Skole [600]", "aktiv": True},
]}


def test_endelig_vaerdi_rettet_slaar_godkendt_og_auto(tmp_path):
    valg = vm.laes_gennemsyn(_gennemsyn(tmp_path / "g.xlsx", [
        _r("a", "A", "auto"),
        _r("b", "B", "forslag", godkendt="x"),
        _r("c", "C", "forslag"),                              # ikke godkendt
        _r("d", "D", "afklar", rettet="Rettet [1]"),
        _r("e", "E", "auto", godkendt="nej"),                 # afvist auto
    ]))
    assert [(v.go_vaerdi, v.endelig, v.kilde) for v in valg] == [
        ("a", "A", "auto"), ("b", "B", "godkendt"), ("c", None, ""),
        ("d", "Rettet [1]", "rettet"), ("e", None, "")]


def test_kun_eksakt_navn_i_sbsys_er_klar():
    valg = [vm.Valg("bevillingssted", "mårslet skole [693]", ["Betalingssted"],
                    "Mårslet  skole [693]", "auto"),
            vm.Valg("bevillingssted", "Svend Åge", ["Betalingssted"],
                    "Børnehaven Svend Åge (SDT) [29]", "godkendt"),
            vm.Valg("bevillingssted", "Hasle skole", ["Betalingssted"], "Hasle Skole", "auto")]
    vm.byg(valg, SBSYS)
    assert (valg[0].status, valg[0].sbsys_id) == (vm.KLAR, 424)
    assert valg[1].status == vm.MANGLER_I_SBSYS
    assert "Børnehaven Svend-Åge (SDT) [29]" in valg[1].bemaerkning
    assert valg[2].status == vm.MANGLER_I_SBSYS
    assert "Hasle Skole [600]" in valg[2].bemaerkning


def test_liste_uden_sbsys_fil_er_ikke_tjekket():
    v = vm.Valg("kommune", "Aarhus", ["Betalingskommune"], "Aarhus", "auto")
    vm.byg([v], SBSYS)
    assert v.status == vm.IKKE_TJEKKET


def test_tjek_finder_nummerkonflikter():
    valg = [vm.Valg("institution", "VokseVærket [130]", ["X"], "VokseVærket [130]", "rettet"),
            vm.Valg("institution", "Storkereden [130]", ["X"], "Vuggestuen Storkereden [130]",
                    "godkendt"),
            vm.Valg("institution", "DII Vestermarken [1998]", ["X"],
                    "Børnehuset Udsigten [1988]", "rettet")]
    problemer = [t["problem"] for t in vm.byg(valg, {}).tjek]
    assert any("[130] har 2" in p for p in problemer)
    assert any("GO har [1998]" in p for p in problemer)


def test_en_raekke_pr_go_kolonne_og_kun_klar():
    valg = [vm.Valg("bevillingssted", "Mårslet skole", ["Betalingssted", "Bevillingssted"],
                    "Mårslet Skole [693]", "auto"),
            vm.Valg("bevillingssted", "Hasle skole", ["Betalingssted"], "Hasle Skole", "auto")]
    res = vm.byg(valg, SBSYS)
    raekker = vm.mapping_raekker(res, "g.xlsx", date(2026, 11, 24))
    assert [(r["go_vaerdi"], r["sbsys_vaerdi"]) for r in raekker] == [
        ("Betalingssted|Mårslet skole", "Mårslet Skole [693]"),
        ("Bevillingssted|Mårslet skole", "Mårslet Skole [693]")]


def test_konflikt_med_nuvaerende_mapping():
    raekker = [{"go_vaerdi": "Betalingssted|x", "sbsys_vaerdi": "Ny"},
               {"go_vaerdi": "Betalingssted|y", "sbsys_vaerdi": "Samme"}]
    k = vm.konflikter_med_mapping(raekker, {"Betalingssted|x": "Gammel",
                                            "Betalingssted|y": "samme"})
    assert [r["go_vaerdi"] for r in k] == ["Betalingssted|x"]


def test_cli_skriver_xlsx_og_csv(tmp_path):
    g = _gennemsyn(tmp_path / "dv-gennemsyn.xlsx", [_r("Mårslet skole", "Mårslet Skole [693]", "auto")])
    (tmp_path / "liste.json").write_text(json.dumps(SBSYS["bevillingssted"]), encoding="utf-8")
    assert main(["vaerdier", "mapning", str(g),
                 "--sbsys", f"bevillingssted={tmp_path / 'liste.json'}"]) == 0
    assert (tmp_path / "dv-mapning.xlsx").exists()
    with (tmp_path / "dv-tilbudsfeltvaerdi.csv").open(encoding="utf-8") as f:
        raekker = list(csv.DictReader(f, delimiter=";"))
    assert raekker[0]["go_vaerdi"] == "Betalingssted|Mårslet skole"
    assert raekker[0]["sbsys_vaerdi"] == "Mårslet Skole [693]"


def test_cli_afviser_ukendt_liste(tmp_path):
    g = _gennemsyn(tmp_path / "g.xlsx", [])
    assert main(["vaerdier", "mapning", str(g), "--sbsys", "noget=x.json"]) == 2


def test_flet_erstatter_paa_plads_og_roerer_ikke_andre_linjer(tmp_path):
    sti = tmp_path / "tilbudsfeltvaerdi.csv"
    sti.write_text(
        "liste;go_vaerdi;go_tekst;sbsys_vaerdi;sbsys_tekst;handling;kommentar;godkendt_af;godkendt_dato\n"
        'tilbudsfeltvaerdi;"Type|a; b";a; b;X;X;map;citeret;;\n'
        "tilbudsfeltvaerdi;Betalingssted|65 Skovslottet (SDT);65;"
        "Skovslottet (SDT);Skovslottet (SDT);map;gammel;;\n"
        "tilbudsfeltvaerdi;Indstillingsaarsag|x;x;Y;Y;map;urort;;\n",
        encoding="utf-8")
    foer = sti.read_text(encoding="utf-8").splitlines()
    ny = {k: "" for k in vm.MAPPING_KOLONNER} | {
        "liste": "tilbudsfeltvaerdi", "handling": "map"}
    erstattet, tilfoejet = vm.flet(sti, [
        ny | {"go_vaerdi": "Betalingssted|65 Skovslottet (SDT)",
              "sbsys_vaerdi": "Skovslottet (SDT) [65]"},
        ny | {"go_vaerdi": "Betalingssted|Ny", "sbsys_vaerdi": "Ny [1]"}])
    efter = sti.read_text(encoding="utf-8").splitlines()
    assert erstattet == ["Betalingssted|65 Skovslottet (SDT)"]
    assert tilfoejet == ["Betalingssted|Ny"]
    assert efter[:2] == foer[:2] and efter[3] == foer[3]
    assert "Skovslottet (SDT) [65]" in efter[2]
    assert efter[4].startswith("tilbudsfeltvaerdi;Betalingssted|Ny;")


def test_kolonner_for_bevillingssted():
    assert vm.kolonner_for("bevillingssted") == {"Betalingssted", "Bevillingssted"}
