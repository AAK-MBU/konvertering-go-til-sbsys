"""Tests af kommandolinjens egne hjælpere."""

from __future__ import annotations

import pytest

from konvertering.cli import (
    _indlaes_env_fil,
    _parser,
    _sniff_kodning,
    erstat_variabler,
    paakraevede_variabler,
)
from konvertering.config import KonfigurationsFejl


def test_variabel_erstattes():
    sql, mangler = erstat_variabler("SELECT $(run_id) AS x", ["run_id=42"])
    assert sql == "SELECT 42 AS x"
    assert mangler == []


def test_flere_forekomster_erstattes():
    sql, _ = erstat_variabler("$(b) og $(b) og $(a)", ["b=2", "a=1"])
    assert sql == "2 og 2 og 1"


def test_uerstattet_variabel_rapporteres():
    """Et glemt $(run_id) ender ellers som tekst i en INSERT — enten en
    syntaksfejl eller en afstemningsrække med vrøvl i."""
    _, mangler = erstat_variabler("SELECT $(run_id), $(boelge)", ["run_id=1"])
    assert mangler == ["boelge"]


def test_tom_vaerdi_er_tilladt():
    """`--var boelge=` skal kunne bruges til at køre på tværs af bølger."""
    sql, mangler = erstat_variabler("WHERE b = '$(boelge)'", ["boelge="])
    assert sql == "WHERE b = ''"
    assert mangler == []


def test_standardvaerdi_bruges_naar_var_mangler():
    """Testdataens CPR skal kunne køres uændret, men også kunne tage et
    rigtigt nummer uden at nogen redigerer i filen."""
    sql, mangler = erstat_variabler("cpr = '$(cpr1|0101011234)'", [])
    assert sql == "cpr = '0101011234'"
    assert mangler == []


def test_var_vinder_over_standardvaerdien():
    sql, _ = erstat_variabler("cpr = '$(cpr1|0101011234)'", ["cpr1=2512489996"])
    assert sql == "cpr = '2512489996'"


def test_tom_standardvaerdi_er_tilladt():
    sql, mangler = erstat_variabler("WHERE b = '$(boelge|)'", [])
    assert sql == "WHERE b = ''"
    assert mangler == []


def test_variabel_uden_standard_rapporteres_stadig():
    _, mangler = erstat_variabler("$(run_id) og $(cpr1|x)", [])
    assert mangler == ["run_id"]


def test_forkert_formateret_var_afvises():
    with pytest.raises(KonfigurationsFejl):
        erstat_variabler("SELECT 1", ["run_id"])


def test_db_koer_parses():
    args = _parser().parse_args(
        ["db", "koer", "sql/900_afstemning.sql", "--var", "run_id=7", "--var", "boelge=ppr"]
    )
    assert args.db_kommando == "koer"
    # as_posix(): på Windows bliver stien sql\900_afstemning.sql
    assert args.fil.as_posix() == "sql/900_afstemning.sql"
    assert args.var == ["run_id=7", "boelge=ppr"]


def test_db_init_parses_stadig():
    assert _parser().parse_args(["db", "init"]).db_kommando == "init"


# --- diagnostik når .env mangler -----------------------------------
#
# "Miljøvariablen KONV_DB_SERVER mangler" er en blindgyde uden et hint om
# hvorvidt .env overhovedet blev fundet. Disse tests holder hintet skarpt.

def test_env_fil_findes_og_laeses(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KONV_DB_SERVER=fra-fil\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KONV_DB_SERVER", raising=False)

    status = _indlaes_env_fil()

    assert ".env læst fra" in status
    import os
    assert os.environ["KONV_DB_SERVER"] == "fra-fil"


def test_env_txt_fangen(tmp_path, monkeypatch):
    """Notepad gemmer som .env.txt medmindre man vælger "Alle filer"."""
    (tmp_path / ".env.txt").write_text("KONV_DB_SERVER=x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert ".env.txt" in _indlaes_env_fil()


def test_kun_eksempelfil_giver_kopier_hint(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("KONV_DB_SERVER=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert "copy .env.example .env" in _indlaes_env_fil()


def test_ingen_env_fil_overhovedet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "Ingen .env fundet" in _indlaes_env_fil()


def test_satte_variabler_vinder_over_env_fil(tmp_path, monkeypatch):
    """En glemt .env må ikke kunne overskrive et bevidst sat KONV_MILJOE=prod."""
    (tmp_path / ".env").write_text("KONV_MILJOE=test\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KONV_MILJOE", "prod")

    _indlaes_env_fil()

    import os
    assert os.environ["KONV_MILJOE"] == "prod"


# --- tegnkodning ----------------------------------------------------
#
# PowerShell 5.1's Out-File skriver UTF-16LE som standard. Læses sådan en
# fil som UTF-8, bliver første nøgle til volapyk, og variablen "mangler"
# selv om den tydeligvis står i filen.

@pytest.mark.parametrize(
    "kodning,forventet",
    [
        ("utf-8", "utf-8"),
        ("utf-8-sig", "utf-8-sig"),
        ("utf-16-le", "utf-16-le"),
        ("utf-16-be", "utf-16-be"),
    ],
)
def test_kodning_genkendes(tmp_path, kodning, forventet):
    sti = tmp_path / ".env"
    sti.write_bytes("KONV_DB_SERVER=x\n".encode(kodning))
    assert _sniff_kodning(sti) == forventet


def test_utf16_env_fil_laeses(tmp_path, monkeypatch):
    sti = tmp_path / ".env"
    sti.write_bytes("KONV_DB_SERVER=fra-utf16\n".encode("utf-16-le"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KONV_DB_SERVER", raising=False)

    status = _indlaes_env_fil()

    import os
    assert os.environ["KONV_DB_SERVER"] == "fra-utf16"
    assert "UTF-16" in status


# --- linjer dotenv ikke kan tolke -----------------------------------
#
# Dotenv springer en uforståelig linje over og skriver en advarsel i
# logningen. Følgen — "Miljøvariablen KONV_FIL_ROD mangler" — kommer et
# helt andet sted fra. To beskeder der ikke ligner hinanden, og man leder
# efter en variabel der står tydeligt i filen.

def test_utolkelig_linje_naevnes_i_diagnosen(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "KONV_DB_SERVER=x\ndette er ikke en tildeling\nKONV_FIL_ROD=/data\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    status = _indlaes_env_fil()

    assert "linje 2" in status
    assert "IKKE tolkes" in status


def test_ren_env_fil_giver_ingen_advarsel(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KONV_DB_SERVER=x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert "IKKE tolkes" not in _indlaes_env_fil()


def test_paakraevede_variabler_afhaenger_af_auth(monkeypatch):
    monkeypatch.delenv("KONV_DB_TRUSTED", raising=False)
    assert "KONV_DB_BRUGER" in paakraevede_variabler()

    monkeypatch.setenv("KONV_DB_TRUSTED", "1")
    assert "KONV_DB_BRUGER" not in paakraevede_variabler()
    assert "KONV_DB_SERVER" in paakraevede_variabler()


# --- --live skal virke begge steder ----------------------------------
#
# argparse kræver som udgangspunkt at globale flag står FØR underkommandoen.
# `konv load sag --live` fejlede derfor med "unrecognized arguments: --live",
# mens `konv --live load sag` virkede. Det er ikke til at gætte, og --live er
# det vigtigste flag i hele værktøjet.

@pytest.mark.parametrize(
    "argv",
    [
        ["load", "sag", "--boelge", "ppr", "--live"],
        ["--live", "load", "sag", "--boelge", "ppr"],
        ["load", "sag", "--live", "--boelge", "ppr"],
        ["--live", "load", "sag", "--live"],
    ],
)
def test_live_accepteres_i_begge_positioner(argv):
    assert _parser().parse_args(argv).live is True


def test_uden_live_er_det_dry_run():
    assert _parser().parse_args(["load", "sag", "--boelge", "ppr"]).live is False


def test_live_paa_underkommando_overskriver_ikke_global():
    """SUPPRESS-defaulten: uden den ville underkommandoens False vinde."""
    assert _parser().parse_args(["--live", "load", "sag"]).live is True


# --- konv kald -------------------------------------------------------
#
# Boern og Unge-API'et har et sag_foranstaltning-endpoint, men ingen
# OpenAPI-beskrivelse vi kunne finde. Saa er tjenesten selv eneste kilde,
# og OPTIONS er det foerste at proeve: mange API'er svarer med hvilke
# metoder stien tager, nogle med hele feltlisten.

def test_skrivende_metoder_kraever_jatak():
    args = _parser().parse_args(["kald", "POST", "/bu_api/sag_foranstaltning"])
    assert args.jatak is False


def test_options_er_blandt_de_sikre_metoder():
    from konvertering.cli import _SIKRE_METODER

    assert {"GET", "HEAD", "OPTIONS"} == _SIKRE_METODER
    assert "POST" not in _SIKRE_METODER, "en POST kan oprette noget"


def test_kald_parses_med_base_og_krop():
    args = _parser().parse_args(
        ["kald", "OPTIONS", "/bu_api/sag_foranstaltning",
         "--base", "https://bu/api", "--krop", "krop.json", "--jatak"]
    )
    assert args.metode == "OPTIONS"
    assert args.sti == "/bu_api/sag_foranstaltning"
    assert args.base == "https://bu/api"
    assert str(args.krop) == "krop.json"


# --- hver underkommando skal dispatches ------------------------------
#
# `mapping` havde et `else`, der fangede alt andet end "load" og koerte
# "mangler". `mapping forbered` koerte derfor den forkerte kommando og
# svarede venligt "0 uafklarede vaerdier" -- det vaerste svar en forkert
# kommando kan give, for det ligner et resultat.

def test_alle_mapping_underkommandoer_naevnes_i_dispatchen():
    import inspect

    from konvertering import cli

    parser = _parser()
    [mapping] = [
        a for a in parser._subparsers._group_actions[0].choices["mapping"]._actions
        if hasattr(a, "choices") and a.choices
    ]
    kilde = inspect.getsource(cli._udfoer)
    for navn in mapping.choices:
        assert f'"{navn}"' in kilde, f"mapping {navn} dispatches ikke ved navn"


def test_forbered_parses():
    args = _parser().parse_args(["mapping", "forbered", "--liste", "tilbud"])
    assert args.mapping_kommando == "forbered"
    assert args.liste == "tilbud"


# --- et rapportscript skal svare -------------------------------------
#
# `konv db koer sql/910_overblik.sql` koerte hele filen og viste
# INGENTING: koer_script hentede aldrig raekkerne. Et script hvis eneste
# formaal er at svare paa et spoergsmaal, skal svare.

def test_db_koer_henter_resultatsaet():
    import inspect

    from konvertering import cli

    kilde = inspect.getsource(cli._udfoer)
    assert "vis_resultater=True" in kilde


def test_tomme_kolonner_udelades(capsys):
    """Et overblik med tyve kolonner, hvoraf fjorten er NULL, er ikke et
    overblik."""
    from konvertering.cli import _udskriv_tabel

    _udskriv_tabel([{"navn": "Sag", "fejl": None}, {"navn": "Sag2", "fejl": None}])
    ud = capsys.readouterr().out
    assert "navn" in ud
    assert "fejl" not in ud


def test_graensen_siger_til_naar_der_er_mere(capsys):
    from konvertering.cli import _udskriv_tabel

    _udskriv_tabel([{"n": i} for i in range(10)], graense=3)
    ud = capsys.readouterr().out
    assert "7 rækker mere" in ud
    assert "(10 rækker)" in ud


def test_intet_resultatsaet_giver_ingen_stoej(capsys):
    from konvertering.cli import _udskriv_tabel

    _udskriv_tabel([])
    assert capsys.readouterr().out == ""


# --- filscanningens resultat skal faktisk lande ------------------------
#
# 16-11-2026: `filer scan` meldte "ok: 3, mangler: 1", men fil_status stod
# NULL på alle fire dokumenter. UPDATE'en kørte med fast_executemany og
# matchede ingenting — uden fejl. Dokumentet uden fil kom derfor med på
# arbejdslisten og blev kun stoppet af kontrollen lige før afsendelse.

class _ScanDb:
    def __init__(self, beroert: int | None = None) -> None:
        self.beroert = beroert
        self.kald: list[dict] = []

    def udfoer_mange(self, sql, raekker, **kw):
        self.kald.append({"sql": sql, "raekker": raekker, **kw})
        return len(raekker) if self.beroert is None else self.beroert


def test_filstatus_skrives_ikke_med_fast_executemany():
    from konvertering.cli import _gem_filstatus

    db = _ScanDb()
    _gem_filstatus(db, [("ok", 1, None, "text/plain", "D1")])
    assert db.kald[0].get("hurtig", False) is False


def test_filstatus_der_ikke_lander_stopper_scanningen():
    from konvertering.cli import _gem_filstatus
    from konvertering.sbsys.errors import KonverteringsFejl

    db = _ScanDb(beroert=0)
    with pytest.raises(KonverteringsFejl) as e:
        _gem_filstatus(db, [("ok", 1, None, "text/plain", "D1"),
                            ("mangler", None, None, None, "D2")])
    assert "0 af 2" in str(e.value)


def test_filstatus_der_lander_er_stille():
    from konvertering.cli import _gem_filstatus

    _gem_filstatus(_ScanDb(), [("ok", 1, None, "text/plain", "D1")])


def test_udfoer_mange_er_sikker_som_standard():
    """Den hurtige tilstand har to gange matchet ingenting uden fejl. Den
    skal vælges bevidst, ikke være det man får af sig selv."""
    import inspect

    from konvertering.db import Database

    assert inspect.signature(Database.udfoer_mange).parameters["hurtig"].default is False


def test_kald_har_en_bu_genvej():
    """`konv kald GET /bu_api/... --bu` bruger SBSYS_BU_BASE_URL fra .env,
    så adressen ikke skal skrives (og kopieres forkert) i hvert kald."""
    from konvertering.cli import _parser

    args = _parser().parse_args(["kald", "GET", "/bu_api/paragraf", "--bu"])
    assert args.bu is True


def test_kald_kan_skrive_hele_svaret_til_en_fil():
    """Skærmen klipper ved 20.000 tegn; en værdiliste er længere."""
    from konvertering.cli import _parser

    args = _parser().parse_args(["kald", "GET", "/bu_api/x", "--bu", "--ud", "liste.json"])
    assert str(args.ud) == "liste.json"


# --- TEST_-variabler fra .env (sql/997_udvalg_kilde.sql) ------------------

def test_test_variabel_hentes_fra_miljoeet(monkeypatch):
    monkeypatch.setenv("TEST_ABA_SAGS_ID", "PPR-X")
    sql, mangler = erstat_variabler("WHERE id = N'$(TEST_ABA_SAGS_ID)'", [])
    assert sql == "WHERE id = N'PPR-X'" and not mangler


def test_kun_test_variabler_hentes_fra_miljoeet(monkeypatch):
    """Et script må aldrig kunne trække et kodeord ind i SQL'en."""
    monkeypatch.setenv("SBSYS_PASSWORD", "hemmeligt")
    sql, mangler = erstat_variabler("$(SBSYS_PASSWORD)", [])
    assert "hemmeligt" not in sql and mangler == ["SBSYS_PASSWORD"]


def test_apostrof_i_miljoevaerdi_kan_ikke_bryde_ud_af_strengen(monkeypatch):
    monkeypatch.setenv("TEST_X", "a'; DROP TABLE x;--")
    sql, _ = erstat_variabler("N'$(TEST_X)'", [])
    assert sql == "N'a''; DROP TABLE x;--'"


def test_var_paa_kommandolinjen_vinder_over_miljoeet(monkeypatch):
    monkeypatch.setenv("TEST_X", "fra-env")
    assert erstat_variabler("$(TEST_X)", ["TEST_X=fra-var"])[0] == "fra-var"


def test_udvalg_scriptet_laeser_sagen_fra_env_og_kopierer_alle_syv_tabeller():
    from pathlib import Path
    sql = Path("sql/997_udvalg_kilde.sql").read_text(encoding="utf-8")
    # Cases, Contacts, Foranstaltningsdata, Documents, JournalNotes, GOWorkflows
    # (DocumentSHA følger Documents). Ingen tabel oprettes tom længere.
    assert sql.count("$(TEST_ABA_SAGS_ID)") == 6
    assert "TOP (0)" not in sql
    for tabel in ("Cases", "Contacts", "Documents", "DocumentSHA",
                  "Foranstaltningsdata", "GOWorkflows", "JournalNotes"):
        assert f"INTO udvalg.{tabel}" in sql
