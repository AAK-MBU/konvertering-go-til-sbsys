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
            # GO-værdierne har id, så gennemsynets afgørelse gælder (ikke tekstreglen).
            vm.Valg("bevillingssted", "Svend Åge [29]", ["Betalingssted"],
                    "Børnehaven Svend Åge (SDT) [29]", "godkendt"),
            vm.Valg("bevillingssted", "Hasle skole [600]", ["Betalingssted"], "Hasle Skole",
                    "auto")]
    vm.byg(valg, SBSYS)
    assert (valg[0].status, valg[0].sbsys_id) == (vm.KLAR, 424)
    # Samme nummer [29], andet navn -> SBSYS' valgmulighed bruges (27-11-2026).
    assert (valg[1].status, valg[1].sbsys_id) == (vm.KLAR, 353)
    assert valg[1].sbsys_navn == "Børnehaven Svend-Åge (SDT) [29]"
    assert "Samme nummer, andet navn" in valg[1].bemaerkning
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
            vm.Valg("bevillingssted", "Hasle skole [600]", ["Betalingssted"], "Hasle Skole",
                    "auto")]
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
    erstattet, tilfoejet, _ = vm.flet(sti, [
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


def test_noegletyper_som_i_konverterings_pdfen():
    assert vm.mulighed_noegle("institution", "Hobitten [352]") == ("lisid_352", vm.LISID)
    assert vm.mulighed_noegle("institution", "Leonardoskolen Aarhus [instnr 281847]") == (
        "instreg_instnr_281847", vm.INSTNR)
    assert vm.mulighed_noegle("kommune", "Odense", {"odense": "461"}) == ("461", vm.KOMMUNEKODE)
    noegle, type_ = vm.mulighed_noegle("institution", "Markblomsten")
    assert type_ == vm.KONVERTERING and noegle.startswith("konvertering_")


def test_konvertering_noegle_er_den_samme_hver_gang():
    # Fast værdi: ændrer den sig, er alle tidligere genererede nøgler ugyldige.
    assert vm.konvertering_noegle("institution", "Markblomsten") == \
        "konvertering_" + str(__import__("uuid").uuid5(
            vm.NAVNERUM, "institution|markblomsten"))
    assert vm.konvertering_noegle("institution", " markblomsten ") == \
        vm.konvertering_noegle("institution", "Markblomsten")
    assert vm.konvertering_noegle("kommune", "Markblomsten") != \
        vm.konvertering_noegle("institution", "Markblomsten")
    assert str(vm.NAVNERUM) == "d6e5a33e-4914-4e6a-8f9a-a9f5ca30cc0d"


def test_tabellen_bygges_ens_to_gange(tmp_path):
    g = _gennemsyn(tmp_path / "g.xlsx", [
        _r("Mårslet skole", "Mårslet Skole [693]", "auto", kolonner="Betalingssted, Bevillingssted"),
        _r("FU VEST", "FU VEST", "auto"),
        _r("Ukendt sted", "?", "afklar")])

    def koer():
        return vm.vaerdilistemuligheder(vm.byg(vm.laes_gennemsyn(g), SBSYS))

    foerste, anden = koer(), koer()
    assert foerste == anden
    pr_vaerdi = {(m["go_feltnavn"], m["go_feltvaerdi"]): m for m in foerste}
    assert pr_vaerdi[("Bevillingssted", "Mårslet skole")]["vaerdimulighed_external_key"] == "lisid_693"
    assert pr_vaerdi[("Betalingssted", "FU VEST")]["noegletype"] == vm.KONVERTERING
    ukendt = pr_vaerdi[("Betalingssted", "Ukendt sted")]
    assert ukendt["vaerdimulighed_navn"] == "Ukendt sted"
    assert ukendt["status"] == vm.MANGLER_I_SBSYS                # konvertering_: LIS opretter


def test_kommunekoder_fra_danmarks_statistik():
    koder = vm.laes_kommunekoder()
    assert len(koder) == 99
    assert koder["aarhus"] == "751" and koder["odense"] == "461" and koder["københavn"] == "101"
    assert vm.mulighed_noegle("kommune", "Aarhus", koder) == ("751", vm.KOMMUNEKODE)


def test_vaerdiliste_noegler_fra_lis():
    assert vm.VAERDILISTE_NOEGLE == {
        "bevillingssted": "v_bevillingssted_ppr", "institution": "v_skole_el_institution_ppr",
        "klassetrin": "v_klassetrin", "kommune": "v_kommune"}


# --- Tekstreglen: GO-værdier uden id ------------------------------------------

PROD_BEV = [
    {"id": 1, "vaerdiMulighed": "Mårslet Skole [693]", "aktiv": True},
    {"id": 2, "vaerdiMulighed": "Åbyhøj dagtilbud [438]", "aktiv": True},
    {"id": 3, "vaerdiMulighed": "Klubben Liffen [1682]", "aktiv": True},
    {"id": 4, "vaerdiMulighed": "Møllevang dagtilbud [158]", "aktiv": True},
    {"id": 5, "vaerdiMulighed": "Rudolf Steiner-Skolen i Aarhus [instnr 751062]", "aktiv": True},
    {"id": 6, "vaerdiMulighed": "Børnehaven Kløverparken (SDT) [51]", "aktiv": True},
]


def _v(go, endelig, kilde="rettet"):
    return vm.Valg("bevillingssted", go, ["Betalingssted"], endelig, kilde)


def test_tekstregel():
    valg = [
        _v("Mårslet  skole", None, ""),                             # b. træf, store/små
        _v("Åbyhøj Dagtilbud", "Noget andet [1]"),                   # b. træf slår gennemsynet
        _v("Arvepr. Carolines Børneasyl", "Arveprinsesse Carolines Børneasyl [503]"),  # c.
        _v("Liffen", "Klubben Liffen [1682]"),                       # c. selv om navnet findes
        _v("Tranbjerg skole", "Tranbjergskolen [725]"),              # b. uden træf: gennemsynet
        _v("Aarhus International School S/I", None, ""),            # b. uden træf/afgørelse
        _v("Rudolf Steiner-Skolen i Århus",
           "Rudolf Steiner-Skolen i Aarhus [instnr 751062]"),        # a. privatskole
        _v("158 Møllevang dagtilbud", "Møllevang dagtilbud [158]"),  # tal foran = id
        _v("Hobitten [352}", "Hobitten [352]"),                      # id, forkert klamme
    ]
    vm.byg(valg, {"bevillingssted": PROD_BEV})
    r = {v.go_vaerdi: v for v in valg}
    assert (r["Mårslet  skole"].status, r["Mårslet  skole"].sbsys_navn) == (
        vm.KLAR, "Mårslet Skole [693]")
    assert r["Åbyhøj Dagtilbud"].sbsys_navn == "Åbyhøj dagtilbud [438]"
    for go in ("Arvepr. Carolines Børneasyl", "Liffen"):
        assert r[go].status == vm.INGEN_TEKSTMATCH and r[go].endelig is None, go
    assert r["Tranbjerg skole"].endelig == "Tranbjergskolen [725]"
    assert r["Aarhus International School S/I"].status == vm.UAFKLARET
    assert r["Rudolf Steiner-Skolen i Århus"].status == vm.KLAR
    assert r["158 Møllevang dagtilbud"].status == vm.KLAR
    assert r["Hobitten [352}"].endelig == "Hobitten [352]"       # urørt af reglen


def test_tekstregel_giver_konvertering_noegle_med_go_navnet():
    valg = [_v("Arvepr. Carolines Børneasyl", "Arveprinsesse Carolines Børneasyl [503]")]
    m = vm.vaerdilistemuligheder(vm.byg(valg, {"bevillingssted": PROD_BEV}))[0]
    assert m["vaerdimulighed_navn"] == "Arvepr. Carolines Børneasyl"
    assert m["vaerdimulighed_external_key"] == vm.konvertering_noegle(
        "bevillingssted", "Arvepr. Carolines Børneasyl")


def test_tekstregel_kraever_prod_listen():
    v = _v("Liffen", "Klubben Liffen [1682]")
    vm.byg([v], {})
    assert v.endelig == "Klubben Liffen [1682]"


def test_har_go_id():
    assert vm.har_go_id("Hobitten [352]") and vm.har_go_id("X [instnr 281847]")
    assert vm.har_go_id("158 Møllevang dagtilbud") and vm.har_go_id("Hobitten [400}")
    assert not vm.har_go_id("Liffen") and not vm.har_go_id("Skole 2000")


def test_flet_fjerner_kun_map_raekker(tmp_path):
    sti = tmp_path / "tilbudsfeltvaerdi.csv"
    sti.write_text(
        "liste;go_vaerdi;go_tekst;sbsys_vaerdi;sbsys_tekst;handling;kommentar;godkendt_af;godkendt_dato\n"
        "tilbudsfeltvaerdi;Betalingssted|Liffen;Liffen;Klubben Liffen [1682];x;map;;;\n"
        "tilbudsfeltvaerdi;Betalingssted|PPR;PPR;;;ignorer;;;\n"
        "tilbudsfeltvaerdi;Betalingssted|Behold;Behold;B [1];B [1];map;;;\n",
        encoding="utf-8")
    _, _, fjernet = vm.flet(sti, [], {"Betalingssted|Liffen", "Betalingssted|PPR"})
    tekst = sti.read_text(encoding="utf-8")
    assert fjernet == ["Betalingssted|Liffen"]
    assert "Liffen" not in tekst and "Betalingssted|PPR" in tekst and "Behold" in tekst


def test_noegletabellen_har_kun_tre_statusser():
    assert {vm.tabel_status(s) for s in (vm.KLAR, vm.MANGLER_I_SBSYS, vm.INGEN_TEKSTMATCH,
                                         vm.UAFKLARET, vm.IKKE_TJEKKET)} == {
        "klar", "mangler_i_sbsys", "uafklaret"}
    assert vm.tabel_status(vm.INGEN_TEKSTMATCH) == "mangler_i_sbsys"


def test_samme_nummer_andet_navn_er_klar_ogsaa_instnr():
    sbsys = {"institution": [
        {"id": 997, "vaerdiMulighed": "Spiren [178]", "aktiv": True},
        {"id": 1, "vaerdiMulighed": "Risskov Efterskole [instnr 280307]", "aktiv": True},
    ]}
    valg = [vm.Valg("institution", "Spiren, Hjortshøj [178]", ["Institution"],
                    "Spiren, Hjortshøj [178]", "auto"),
            vm.Valg("institution", "Spiren [178]", ["Institution"], "Spiren [178]", "auto"),
            vm.Valg("institution", "Risskov efterskole [instnr 280307]", ["Institution"],
                    "Risskov Efterskole, Risskov [instnr 280307]", "auto")]
    res = vm.byg(valg, sbsys)
    assert [(v.status, v.sbsys_id) for v in valg] == [(vm.KLAR, 997), (vm.KLAR, 997),
                                                       (vm.KLAR, 1)]
    # Begge Spiren-stavemåder ender på samme valgmulighed -> intet at tjekke.
    assert not [t for t in res.tjek if "[178]" in t["problem"]]
    raekker = vm.mapping_raekker(res, "g.xlsx")
    assert {r["sbsys_vaerdi"] for r in raekker} == {"Spiren [178]",
                                                    "Risskov Efterskole [instnr 280307]"}
    noegler = {(m["go_feltvaerdi"], m["vaerdimulighed_external_key"], m["status"])
               for m in vm.vaerdilistemuligheder(res)}
    assert ("Spiren, Hjortshøj [178]", "lisid_178", vm.KLAR) in noegler


def test_samme_nummer_flere_aktive_eller_kun_inaktiv_er_ikke_klar():
    sbsys = {"institution": [
        {"id": 1, "vaerdiMulighed": "Spiren [178]", "aktiv": True},
        {"id": 2, "vaerdiMulighed": "Spiren Nord [178]", "aktiv": True},
        {"id": 3, "vaerdiMulighed": "Gammel [55]", "aktiv": False},
    ]}
    valg = [vm.Valg("institution", "Spiren, Hjortshøj [178]", ["Institution"],
                    "Spiren, Hjortshøj [178]", "auto"),
            vm.Valg("institution", "Ny [55]", ["Institution"], "Ny [55]", "auto")]
    vm.byg(valg, sbsys)
    assert [v.status for v in valg] == [vm.MANGLER_I_SBSYS, vm.MANGLER_I_SBSYS]
    assert "2 aktive" in valg[0].bemaerkning
    assert "0 aktive" in valg[1].bemaerkning


def test_gem_i_db_erstatter_alt_i_en_transaktion():
    from contextlib import contextmanager

    class Markoer:
        def __init__(self):
            self.kald = []

        def execute(self, sql, *p):
            self.kald.append((sql, p))

    class Db:
        markoer = Markoer()
        transaktioner = 0

        @contextmanager
        def transaktion(self):
            Db.transaktioner += 1
            yield self.markoer

    m = {"liste": "institution", "go_feltnavn": "SkoleEllerInstitution",
         "go_feltvaerdi": "Spiren, Hjortshøj [178]",
         "vaerdiliste_external_key": "v_skole_el_institution_ppr",
         "vaerdimulighed_external_key": "lisid_178", "vaerdimulighed_navn": "Spiren [178]",
         "noegletype": "lisid", "status": "klar", "kommentar": ""}
    assert vm.gem_i_db(Db(), [m, m | {"go_feltvaerdi": "Spiren [178]"}], "g.xlsx") == 2
    kald = Db.markoer.kald
    assert Db.transaktioner == 1
    assert kald[0] == ("DELETE FROM map.vaerdilistemulighed WHERE liste IN (?)",
                       ("institution",)), "kun egne lister erstattes"
    assert [k[0].startswith("INSERT INTO map.vaerdilistemulighed") for k in kald[1:]] == [True] * 2
    # kommentar "" -> NULL, kilde til sidst
    assert kald[1][1][-2:] == (None, "g.xlsx")


def test_noegletabellen_findes_i_map_skemaet():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "sql" / "020_map_schema.sql").read_text(
        encoding="utf-8")
    assert "CREATE TABLE map.vaerdilistemulighed" in sql
    for kolonne in vm.MULIGHED_KOLONNER:
        assert f"    {kolonne} " in sql, kolonne
    assert "map.v_vaerdilistemuligheder_til_lis" in sql


def test_tekstregel_sdt_er_dagtilbud():
    """(SDT) = selvejende dagtilbud: navnet slås op i prod (LIS 27-11-2026)."""
    valg = [_v("Børnehaven Kløverparken (sdt)", None, ""),         # b. træf
            _v("Børnehaven Svend Åge (Sdt)", None, ""),            # b. uden træf
            _v("Bisdtrup Klub", None, "")]                        # 'sdt' inde i et ord: c.
    vm.byg(valg, {"bevillingssted": PROD_BEV})
    assert (valg[0].status, valg[0].sbsys_id) == (vm.KLAR, 6)
    assert valg[1].status == vm.UAFKLARET
    assert valg[2].status == vm.INGEN_TEKSTMATCH
