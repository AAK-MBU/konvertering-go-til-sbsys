/*
  998_gofake_testmiljoe.sql — TESTMILJØETS id'er til de falske GO-værdier i
  sql/998_gofake_kilde.sql.

  Samme princip som 999_testdata_testmiljoe.sql: id'erne gælder kun i
  SBSYS' testmiljø og står derfor her, ikke i CSV-filerne i mapping-mappen,
  som også bruges mod produktion.

  Kør EFTER extract (extract lægger nye GO-værdier i kodelisten med tom
  SBSYS-værdi; denne fil udfylder dem):

      uv run konv db koer sql\998_gofake_testmiljoe.sql --var behandler=20 --var skabelon_aba=<id>

  skabelon_aba: id'et på "PPRsag - ABA" i testmiljøet. Find det i
  rapporter\skabeloner-fra-sbsys.csv (konv opslag skabeloner). Uden den
  får ABA-sagen ingen skabelon og afvises med manglende_mapping — en
  tydelig fejl frem for et gæt.

  Id'er fra testmiljøet 16-18/11-2026:
    skabelon "PPRsag - psykolog" = 2, afdeling "PPR" = 5,
    status Åben = 6 / Lukket = 3, servicekontoen = bruger 20,
    BU-kataloget: paragraf 4 "." -> foranstaltning 4 "PPR" -> tilbud 3 "ABA" / 1 "Psykolog".
*/

SET NOCOUNT ON;

MERGE map.kodeliste AS t
USING (VALUES
    ('sagsskabelon',   N'ABA',      NULLIF(N'$(skabelon_aba|)', N''),   N'PPRsag - ABA'),
    ('sagsskabelon',   N'Psykolog', N'$(skabelon_psykolog|2)',          N'PPRsag - psykolog'),
    ('afdeling',       N'PPR',      N'$(afdeling|5)',                   N'PPR'),
    ('sagsstatus',     N'Åben',     N'$(status_aaben|6)',               N'Åben'),
    /* Testmiljøet har ingen "Passiv" (produktionens mål for Lukket) */
    ('sagsstatus',     N'Lukket',   N'$(status_lukket|3)',              N'Lukket'),
    /* Sagens behandler: servicekontoen. Skabelonerne beskytter sagen og
       kræver en behandler. */
    ('bruger',         N'az99999',  NULLIF(N'$(behandler|)', N''),      N'servicekontoen'),
    ('paragraf',       N'ABA',      N'$(paragraf|4)',                   N'.'),
    ('foranstaltning', N'ABA',      N'$(foranstaltning|4)',             N'PPR'),
    ('tilbud',         N'ABA',      N'$(tilbud_aba|3)',                 N'ABA'),
    ('paragraf',       N'Psykolog', N'$(paragraf|4)',                   N'.'),
    ('foranstaltning', N'Psykolog', N'$(foranstaltning|4)',             N'PPR'),
    ('tilbud',         N'Psykolog', N'$(tilbud_psykolog|1)',            N'Psykolog'),
    /* Tilbudsfelter: testmiljøet kalder Psykologs "Indsats"-felt
       "Indstillingsårsag" (produktion: "Indsats"). Navnet, ikke id'et,
       er det der mappes — se src/konvertering/tilbudsfelter.py. */
    ('tilbudsfelt',    N'Psykolog|Indstillingsaarsag', N'Indstillingsårsag', N'Indstillingsårsag'),
    /* ... og ABA's "ABA gruppe" hedder "ABA gruppe1" i testmiljøet. */
    ('tilbudsfelt',    N'ABA|ABAGruppe',               N'ABA gruppe1',       N'ABA gruppe1')
) AS s (liste, go_vaerdi, sbsys_vaerdi, sbsys_tekst)
ON t.liste = s.liste AND t.go_vaerdi = s.go_vaerdi
WHEN MATCHED THEN UPDATE SET
    sbsys_vaerdi = s.sbsys_vaerdi,
    sbsys_tekst  = s.sbsys_tekst,
    handling     = 'map',
    kommentar    = N'TESTMILJØ-id til falske GO-data — sql/998_gofake_testmiljoe.sql'
WHEN NOT MATCHED THEN
    INSERT (liste, go_vaerdi, sbsys_vaerdi, sbsys_tekst, handling, kommentar)
    VALUES (s.liste, s.go_vaerdi, s.sbsys_vaerdi, s.sbsys_tekst, 'map',
            N'TESTMILJØ-id til falske GO-data — sql/998_gofake_testmiljoe.sql');

/* Hvad mangler stadig? Tom = alt de falske sager og indsatser skal bruge,
   er mappet. */
SELECT k.liste, k.go_vaerdi, k.handling
FROM   map.kodeliste AS k
WHERE  k.liste IN ('sagsskabelon', 'afdeling', 'sagsstatus', 'bruger', 'kle',
                   'paragraf', 'foranstaltning', 'tilbud')
  AND  k.go_vaerdi IN (N'ABA', N'Psykolog', N'PPR', N'Åben', N'Lukket',
                       N'az99999', N'17.01.05')
  AND  k.handling = 'map'
  AND  NULLIF(k.sbsys_vaerdi, N'') IS NULL;
