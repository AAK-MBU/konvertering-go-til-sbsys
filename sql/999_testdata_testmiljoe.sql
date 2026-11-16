/*
  999_testdata_testmiljoe.sql — id'er fra SBSYS' TESTMILJØ til de FALSKE testdata.

  KUN til fase 2: falske testdata (sql/999_testdata.sql) ind i SBSYS'
  testmiljø. Rører udelukkende de falske GO-værdier (PPR-sag, PPR Nord,
  ab1234, Active, Closed, Notat) — aldrig de rigtige fra CSV-filerne i mapping-mappen.

  Id'erne er hentet med `konv opslag` mod testmiljøet 16-11-2026. De gælder
  IKKE nødvendigvis i produktion, og derfor står de her og ikke i
  CSV-filerne i mapping-mappen, som også bruges mod produktion.

  Koer i denne raekkefoelge, efter en nulstilling:
      uv run konv mapping load
      uv run konv db koer sql\999_testdata.sql --var cpr1=<CPR der findes i testmiljoeet>
      uv run konv db koer sql\999_testdata_testmiljoe.sql

  Hvert id kan overstyres:  --var skabelon=21 --var afdeling=27 …
*/

SET NOCOUNT ON;

MERGE map.kodeliste AS t
USING (VALUES
    /* PPRsag - psykolog. Samme valg som den tidligere udvikler. */
    ('sagsskabelon',     N'PPR-sag',  N'$(skabelon|2)',    N'PPRsag - psykolog'),
    /* "PPR" (MBU\Pædagogik organisation og sundhed). Kontoen skal have
       rettigheder dér — ellers svarer SBSYS 403 paa oprettelsen. */
    ('afdeling',         N'PPR Nord', N'$(afdeling|5)',    N'PPR'),
    /* Statusser i testmiljøet: 6 = Åben, 3 = Lukket (kræver kommentar). */
    ('sagsstatus',       N'Active',   N'$(status_aaben|6)',  N'Åben'),
    ('sagsstatus',       N'Closed',   N'$(status_lukket|3)', N'Lukket'),
    ('dokumentkategori', N'Notat',    N'$(dokumentart|5)', N'Notat')
) AS s (liste, go_vaerdi, sbsys_vaerdi, sbsys_tekst)
ON t.liste = s.liste AND t.go_vaerdi = s.go_vaerdi
WHEN MATCHED THEN UPDATE SET
    sbsys_vaerdi = s.sbsys_vaerdi,
    sbsys_tekst  = s.sbsys_tekst,
    handling     = 'map',
    kommentar    = N'TESTMILJØ-id til falske testdata — sql/999_testdata_testmiljoe.sql'
WHEN NOT MATCHED THEN
    INSERT (liste, go_vaerdi, sbsys_vaerdi, sbsys_tekst, handling, kommentar)
    VALUES (s.liste, s.go_vaerdi, s.sbsys_vaerdi, s.sbsys_tekst, 'map',
            N'TESTMILJØ-id til falske testdata — sql/999_testdata_testmiljoe.sql');

/* Sagsbehandleren: den falske 'ab1234' har intet id i testmiljøet. Uden en
   vaerdi sendes SagsBehandlerID slet ikke, og SBSYS vælger selv — som
   regel den der opretter, altså servicekontoen. Ikke blokerende. */
UPDATE map.kodeliste
SET    sbsys_vaerdi = NULL,
       kommentar    = N'Bevidst tom i testmiljøet — SBSYS vælger sagsbehandler'
WHERE  liste = 'bruger' AND go_vaerdi = N'ab1234';
