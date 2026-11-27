"""Værn om extract-laget (sql/100_extract_skabelon.sql).

Extract kan ikke køres uden GOStaging, men to ting i det er bekræftet mod
testdumpet 16-11-2026, og de må ikke glide tilbage:

* Foranstaltningsdata.Fra/Til er tekst i formatet dd-mm-aaaa, og serveren
  er us_english (mdy). Uden en eksplicit stil bliver 05-03-2021 lydløst
  til 3. maj — en forkert periode på en indsats, ikke en fejl.
* Filstien er <SagsId>/<DokumentID>.<endelse>. Mappestruktur er tom for
  alle dokumenter, og ScpFolder har '/GO_Files/' foran, som ville give
  ...\\GO_Files\\GO_Files\\... med roden sat til sharet.
"""

from __future__ import annotations

import re
from pathlib import Path

EXTRACT = Path(__file__).resolve().parents[1] / "sql" / "100_extract_skabelon.sql"


def _kode() -> str:
    """Extract uden kommentarer — det er koden, ikke forklaringen, der tæller."""
    tekst = EXTRACT.read_text(encoding="utf-8")
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", tekst)


def test_fra_og_til_konverteres_med_dansk_datoformat():
    # (?:[^()]|\(\d+\)) tillader DATETIME2(3) inde i kaldet. Uden det så
    # testen kun nogle af konverteringerne og overså periodekolonnerne.
    # To er periode_start og periode_slut. (Den gamle indsatsnøgle havde
    # tre mere; fingeraftrykket 17-11-2026 bruger teksten som den står.)
    konverteringer = re.findall(
        r"TRY_CONVERT\((?:[^()]|\(\d+\))*?\[(?:Fra|Til)\](?:[^()]|\(\d+\))*?\)", _kode())
    assert len(konverteringer) >= 2, (
        f"fandt kun {len(konverteringer)} konverteringer af Fra/Til — er mønsteret ændret?")
    uden_stil = [k for k in konverteringer if not re.search(r",\s*105\s*\)$", k)]
    assert not uden_stil, (
        f"Fra/Til konverteres uden stil 105 (dd-mm-aaaa): {uden_stil}. Serveren "
        f"er mdy, så dag og måned byttes lydløst om."
    )


def test_dokumentstien_bygges_af_sagsid_og_dokumentid():
    kode = _kode()
    assert "d.[SagsId] + N'/' + CONVERT(NVARCHAR(20), d.[DokumentID])" in kode
    assert "[Mappestruktur]" not in kode, "Mappestruktur er tom i dumpet og må ikke indgå i stien"


def _sammenstoedende_strenge(sql: str) -> list[str]:
    """Linjer der slutter med en streng, efterfulgt af en linje der starter
    med en. Python og C sætter dem sammen; T-SQL giver Msg 102."""
    uden = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), sql, flags=re.DOTALL)
    linjer = [linje.split("--")[0] for linje in uden.splitlines()]
    return [
        a.strip() for a, b in zip(linjer, linjer[1:], strict=False)
        if re.search(r"'\s*$", a) and re.match(r"\s*N?'", b)
    ]


def test_ingen_sammenstoedende_strenge_i_sql():
    """Extract fejlede med Msg 102 i SSMS 16-11-2026: en RAISERROR-besked
    var delt over tre linjer som tre strenge uden +."""
    fund = {
        p.name: s
        for p in sorted(EXTRACT.parent.glob("*.sql"))
        if (s := _sammenstoedende_strenge(p.read_text(encoding="utf-8")))
    }
    assert not fund, f"strenge uden + imellem: {fund}"


def test_sammenstoed_genkendes():
    """Holder tjekket ovenfor i live."""
    assert _sammenstoedende_strenge("RAISERROR(\n  'a '\n  'b', 16, 1);")
    assert not _sammenstoedende_strenge("SET @x = N'a '\n  + N'b';")


def test_journalark_bruger_ikke_scpfolder():
    kode = _kode()
    assert "n.[SagsId] + N'/' + n.[DokumentID]" in kode
    assert "[ScpFolder]" not in kode


def test_workflows_konverteres_ikke():
    """Omgjort 26-11-2026: GOWorkflows skal ikke med (SBSYS har intet
    workflow-begreb). Extract må ikke læse tabellen."""
    assert "[GOWorkflows]" not in _kode()
    assert "'WF:' +" not in _kode()


# --- kommentarer skal være lukkede ----------------------------------------
#
# T-SQL tillader INDLEJREDE blokkommentarer: et "/*" inde i en kommentar
# åbner en ny. Skriver man "mapping/*.csv" i en kommentar, er filen ét langt,
# uafsluttet kommentarfelt — "Missing end comment mark '*/'" (16-11-2026,
# en SQL-fil til testmiljøets id'er, nu mapping/testmiljoe/). Python ser intet forkert ved det.

def _kommentar_ubalance(tekst: str) -> str | None:
    """None hvis balanceret. Strenge og --linjer tæller ikke med."""
    i, dybde, i_streng = 0, 0, False
    while i < len(tekst):
        c, par = tekst[i], tekst[i:i + 2]
        if dybde == 0 and not i_streng and par == "--":
            slut = tekst.find("\n", i)
            i = len(tekst) if slut < 0 else slut
            continue
        if dybde == 0 and c == "'":
            i_streng = not i_streng
            i += 1
            continue
        if not i_streng and par == "/*":
            dybde += 1
            i += 2
            continue
        if not i_streng and par == "*/":
            if dybde == 0:
                return "*/ uden et /* foran"
            dybde -= 1
            i += 2
            continue
        i += 1
    return f"{dybde} kommentar(er) åbnet men aldrig lukket" if dybde else None


def test_alle_sql_kommentarer_er_lukkede():
    fejl = {
        p.name: u for p in sorted(EXTRACT.parent.glob("*.sql"))
        if (u := _kommentar_ubalance(p.read_text(encoding="utf-8")))
    }
    assert not fejl, f"SQL Server afviser: {fejl}. Et '/*' inde i en kommentar åbner en ny."


def test_indlejret_kommentar_fanges():
    assert _kommentar_ubalance("/* se mapping/*.csv */ SELECT 1;")
    assert not _kommentar_ubalance("/* se mapping\\*.csv */ SELECT '/*';")


# --- indsatsnøglen ---------------------------------------------------------
#
# Testdumpet 17-11-2026: 63 rækker delte den gamle nøgle (sag + type + ydelse
# + fra) med en anden og blev udeladt. 16 var ens i ALLE felter.

def test_indsatsnoeglen_er_fingeraftryk_af_hele_raekken():
    kode = _kode()
    assert "HASHBYTES('SHA2_256'" in kode
    assert "ROW_NUMBER() OVER" in kode and "PARTITION BY k.[SagsId], k.fingeraftryk" in kode
    # Kolonner der adskiller rækker, som den gamle nøgle ikke så:
    for kolonne in ("Til", "Visiteret", "Sagsbehandler", "Timer", "ABAGruppe"):
        assert f"COALESCE(N'1' + f.[{kolonne}], N'0')" in kode, kolonne


def test_indsatsnoeglen_bruger_ikke_concat_ws():
    """CONCAT_WS kræver SQL Server 2017; versionen kendes ikke."""
    assert "CONCAT_WS" not in _kode()


def test_kolliderende_indsatser_udelades_ikke_laengere():
    """Den gamle extract tog kun nøgler med COUNT(*) = 1 og smed resten."""
    assert "HAVING COUNT(*) = 1" not in _kode()


def test_tripellet_slaas_op_paa_mappetypen():
    kode = _kode()
    for liste in ("paragraf", "foranstaltning", "tilbud"):
        assert re.search(rf"'{liste}',\s+indholdstype_go\s+FROM stg\.indsats", kode), liste


def test_noegletjekket_bruger_samme_udtryk_som_extract():
    """sql/106 er en kopi af nøgleudtrykket. Glider de fra hinanden, tester
    tjekket noget andet end det extract gør."""
    def blok(tekst: str) -> str:
        return tekst[tekst.index(";WITH kandidater AS ("):tekst.index("nummereret AS (")]
    tjek = (EXTRACT.parent / "106_indsats_noegle_tjek.sql").read_text(encoding="utf-8")
    assert blok(tjek) == blok(EXTRACT.read_text(encoding="utf-8"))


# --- sorteringsregler (collation) -----------------------------------------
#
# GOStaging og SBSYS_KONV har forskellige collations. En sammenligning
# mellem en GOStaging-kolonne og en stg-kolonne giver Msg 468 uden
# COLLATE. Fundet 18-11-2026 ved simulering mod sql/998_gofake_kilde.sql.

def test_gostaging_mod_stg_sammenligninger_har_collate():
    kode = _kode()
    # Alle opslag i stg der sammenlignes med noget der IKKE er en stg-kolonne
    # (et alias med [Kolonne] fra GOStaging, eller r.* afledt af GOStaging).
    sammenligninger = re.findall(
        r"FROM stg\.\w+ \w+ WHERE \w+\.kilde_noegle = (\w+\.(?:\[\w+\]|\w+))([^)]*)\)", kode)
    # 7 efter at workflows blev taget ud 26-11-2026 (de havde to).
    assert len(sammenligninger) >= 7, f"fandt kun {len(sammenligninger)} — er mønsteret ændret?"
    uden = [f"{venstre}{resten}" for venstre, resten in sammenligninger
            if "COLLATE DATABASE_DEFAULT" not in resten]
    assert not uden, f"mangler COLLATE DATABASE_DEFAULT: {uden}"


def test_sag_part_har_én_raekke_pr_sag_og_part():
    """Barnet står både på Cases (primær) og i Contacts — PK-brud på den
    første rigtige sag 20-11-2026. Den primære rolle skal vinde."""
    kode = _kode()
    blok = kode[kode.index("INSERT INTO stg.sag_part"):kode.index("DELETE FROM stg.dokument")]
    assert "SELECT DISTINCT" not in blok
    assert "PARTITION BY r.sag_noegle, r.part_noegle" in blok
    assert "ORDER BY r.primaer DESC" in blok
    assert "r.nr = 1" in blok
