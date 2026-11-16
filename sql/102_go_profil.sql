/*
  102_go_profil.sql — afklaringer i GOStaging, FØR extract og filopslag skrives om.

  Kun laesning. Viser ingen navne, CPR-numre, sagstitler eller filnavne —
  kun optaellinger, filendelser og id-agtige kolonner. Kig alligevel
  resultatet igennem, foer det sendes videre.

      uv run konv db koer sql/102_go_profil.sql --raekker 0
      (eller koer filen i SSMS)

  Svarer paa:
    1. Hvilken kolonne giver mappenavnet paa fileshare'et?
    2. Staar filendelsen i databasen?
    3. Ligger journalark og workflows OGSAA i Documents (dobbeltkonvertering)?
    4. Hvilke datoformater staar i Foranstaltningsdata.Fra/Til?
    5. Er Indholdstype den rigtige mapningsnoegle for indsatser?
    6. Kolliderer den sammensatte indsatsnoegle (B14)?
    7. Status, omraader, parter og grupper — til mapningen
*/

SET NOCOUNT ON;

/* Sagsnumre maskeres i alt hvad scriptet viser:
       PPR-2025-123456-001  ->  PPR-2025-XXXXXX-001
   Moenstret er tre bogstaver, aarstal, seks cifre og bindestreg, saa ELM,
   BOR og EMN fanges ogsaa. De seks cifre starter 9 tegn inde i et traef.
   Optaellingerne i 1a sammenligner de RIGTIGE vaerdier; kun visningen
   maskeres. Begge sider maskeres ens, saa to vaerdier stadig kan ses at
   vaere ens. */
DECLARE @sagsnr NVARCHAR(100) =
    N'%[A-Z][A-Z][A-Z]-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-%';

/* --- 0. Er noeglerne entydige? ----------------------------------------
   Cases' primaernoegle er ID, ikke GOSagsID — entydigheden er ikke
   haandhaevet. Optraeder en GOSagsID to gange, fejler extract paa
   stg.sag's primaernoegle, og joins nedenfor taeller dobbelt. Skal vaere 0. */
SELECT
    '0. Entydighed'                                                     AS afsnit,
    (SELECT COUNT(*) FROM (SELECT GOSagsID FROM GOStaging.dbo.Cases
                           GROUP BY GOSagsID HAVING COUNT(*) > 1) AS x) AS dublerede_gosagsid,
    (SELECT COUNT(*) FROM (SELECT GOSagsDBID FROM GOStaging.dbo.Cases
                           WHERE GOSagsDBID IS NOT NULL
                           GROUP BY GOSagsDBID HAVING COUNT(*) > 1) AS x) AS dublerede_gosagsdbid,
    (SELECT COUNT(*) FROM GOStaging.dbo.Documents AS d
     WHERE NOT EXISTS (SELECT 1 FROM GOStaging.dbo.DocumentSHA AS s
                       WHERE s.DocumentId = d.DokumentID))              AS dokumenter_uden_sha_raekke;

/* --- 1a. Mappenavn: hvilken kolonne matcher Mappestruktur? ---------- */
SELECT
    '1a. Mappenavn'                                                     AS afsnit,
    COUNT(*)                                                            AS dokumenter,
    SUM(CASE WHEN d.SagsId IS NULL THEN 1 ELSE 0 END)                   AS uden_sagsid,
    SUM(CASE WHEN d.SagsId IS NOT NULL AND c.GOSagsID IS NULL
             THEN 1 ELSE 0 END)                                         AS sag_findes_ikke,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(d.Mappestruktur)), N'') IS NULL
             THEN 1 ELSE 0 END)                                         AS uden_mappestruktur,
    SUM(CASE WHEN d.Mappestruktur = c.GOSagsDBID THEN 1 ELSE 0 END)     AS mappe_er_gosagsdbid,
    SUM(CASE WHEN d.Mappestruktur LIKE c.GOSagsDBID + N'[_]%'
             THEN 1 ELSE 0 END)                                         AS mappe_er_gosagsdbid_overloeb,
    SUM(CASE WHEN d.Mappestruktur = c.Sagsnavn THEN 1 ELSE 0 END)       AS mappe_er_sagsnavn,
    SUM(CASE WHEN d.Mappestruktur = c.GOSagsID THEN 1 ELSE 0 END)       AS mappe_er_guid,
    SUM(CASE WHEN d.Mappestruktur LIKE N'%\%' OR d.Mappestruktur LIKE N'%/%'
             THEN 1 ELSE 0 END)                                         AS mappe_med_skraastreg,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(c.GOSagsDBID)), N'') IS NULL
              AND c.GOSagsID IS NOT NULL THEN 1 ELSE 0 END)             AS sag_uden_gosagsdbid
FROM   GOStaging.dbo.Documents AS d
LEFT   JOIN GOStaging.dbo.Cases AS c ON c.GOSagsID = d.SagsId;

/* --- 1b. Fem eksempler: kun id-agtige kolonner og filendelsen ------- */
SELECT TOP 5
    '1b. Eksempler'                                                     AS afsnit,
    c.Organisering,
    db2.v                                                               AS GOSagsDBID,
    ms2.v                                                               AS Mappestruktur,
    d.DokumentID,
    CASE WHEN CHARINDEX(N'.', REVERSE(d.Filnavn)) > 0
         THEN RIGHT(d.Filnavn, CHARINDEX(N'.', REVERSE(d.Filnavn)) - 1)
    END                                                                 AS endelse
FROM   GOStaging.dbo.Documents AS d
LEFT   JOIN GOStaging.dbo.Cases AS c ON c.GOSagsID = d.SagsId
/* Maskering i to trin, saa ogsaa et andet sagsnummer i samme vaerdi fanges */
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, c.GOSagsDBID) > 0
                              THEN STUFF(c.GOSagsDBID, PATINDEX(@sagsnr, c.GOSagsDBID) + 9, 6, N'XXXXXX')
                              ELSE c.GOSagsDBID END) AS db1
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, db1.v) > 0
                              THEN STUFF(db1.v, PATINDEX(@sagsnr, db1.v) + 9, 6, N'XXXXXX')
                              ELSE db1.v END) AS db2
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, d.Mappestruktur) > 0
                              THEN STUFF(d.Mappestruktur, PATINDEX(@sagsnr, d.Mappestruktur) + 9, 6, N'XXXXXX')
                              ELSE d.Mappestruktur END) AS ms1
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, ms1.v) > 0
                              THEN STUFF(ms1.v, PATINDEX(@sagsnr, ms1.v) + 9, 6, N'XXXXXX')
                              ELSE ms1.v END) AS ms2
ORDER  BY d.DokumentID;

/* --- 2. Filendelser --------------------------------------------------- */
SELECT
    '2. Filendelser'                                                    AS afsnit,
    LOWER(CASE WHEN CHARINDEX(N'.', REVERSE(d.Filnavn)) > 0
               THEN RIGHT(d.Filnavn, CHARINDEX(N'.', REVERSE(d.Filnavn)) - 1)
               ELSE N'(ingen)' END)                                     AS endelse,
    COUNT(*)                                                            AS antal,
    SUM(CASE WHEN d.Journaliseret = 0 THEN 1 ELSE 0 END)                AS heraf_kladder
FROM   GOStaging.dbo.Documents AS d
GROUP  BY LOWER(CASE WHEN CHARINDEX(N'.', REVERSE(d.Filnavn)) > 0
                     THEN RIGHT(d.Filnavn, CHARINDEX(N'.', REVERSE(d.Filnavn)) - 1)
                     ELSE N'(ingen)' END)
ORDER  BY antal DESC;

/* --- 3a. Journalark og workflows: hvordan ser de ud? ------------------ */
/* Alle tre kolonner kan indeholde sagsnummeret — filnavnet er fx
   <SagsID>_GO_Workflows.txt, og titlen "Journalark – <sagsid> – konverteret".
   Hver kolonne maskeres i to trin. */
SELECT x.afsnit,
       id2.v AS DokumentID, mp2.v AS ScpFolder, ti2.v AS DokumentTitel
FROM (
    SELECT '3a. JournalNotes' AS afsnit, DokumentID, ScpFolder, DokumentTitel
    FROM   GOStaging.dbo.JournalNotes
    UNION ALL
    SELECT '3a. GOWorkflows', DokumentID, ScpFolder, DokumentTitel
    FROM   GOStaging.dbo.GOWorkflows
) AS x
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, x.DokumentID) > 0
                              THEN STUFF(x.DokumentID, PATINDEX(@sagsnr, x.DokumentID) + 9, 6, N'XXXXXX')
                              ELSE x.DokumentID END) AS id1
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, id1.v) > 0
                              THEN STUFF(id1.v, PATINDEX(@sagsnr, id1.v) + 9, 6, N'XXXXXX')
                              ELSE id1.v END) AS id2
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, x.ScpFolder) > 0
                              THEN STUFF(x.ScpFolder, PATINDEX(@sagsnr, x.ScpFolder) + 9, 6, N'XXXXXX')
                              ELSE x.ScpFolder END) AS mp1
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, mp1.v) > 0
                              THEN STUFF(mp1.v, PATINDEX(@sagsnr, mp1.v) + 9, 6, N'XXXXXX')
                              ELSE mp1.v END) AS mp2
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, x.DokumentTitel) > 0
                              THEN STUFF(x.DokumentTitel, PATINDEX(@sagsnr, x.DokumentTitel) + 9, 6, N'XXXXXX')
                              ELSE x.DokumentTitel END) AS ti1
CROSS  APPLY (SELECT v = CASE WHEN PATINDEX(@sagsnr, ti1.v) > 0
                              THEN STUFF(ti1.v, PATINDEX(@sagsnr, ti1.v) + 9, 6, N'XXXXXX')
                              ELSE ti1.v END) AS ti2;

/* --- 3b. Ligger de ogsaa i Documents? (saa konverteres de to gange) --- */
SELECT
    '3b. Dobbeltregistrering'                                           AS afsnit,
    (SELECT COUNT(*) FROM GOStaging.dbo.JournalNotes AS n
     WHERE EXISTS (SELECT 1 FROM GOStaging.dbo.Documents AS d
                   WHERE d.SagsId = n.SagsId
                     AND (d.Filnavn = n.DokumentID OR d.DokumentTitel = n.DokumentTitel)))
                                                                        AS journalark_ogsaa_i_documents,
    (SELECT COUNT(*) FROM GOStaging.dbo.GOWorkflows AS w
     WHERE EXISTS (SELECT 1 FROM GOStaging.dbo.Documents AS d
                   WHERE d.SagsId = w.SagsId
                     AND (d.Filnavn = w.DokumentID OR d.DokumentTitel = w.DokumentTitel)))
                                                                        AS workflows_ogsaa_i_documents,
    (SELECT COUNT(*) FROM GOStaging.dbo.Documents
     WHERE DokumentTitel LIKE N'Journalark%' OR Filnavn LIKE N'%GO[_]Workflows%')
                                                                        AS documents_der_ligner_journalark_eller_workflow;

/* --- 4. Datoformater i Foranstaltningsdata ---------------------------
   Cifre erstattes af 9, saa formatet kan ses uden vaerdierne.
   '99-99-9999' er tvetydigt: TRY_CONVERT laeser det efter serverens
   sprog, og med us_english bliver 01-02-2023 til 2. januar. */
SELECT
    '4. Datoformater'                                                   AS afsnit,
    x.kolonne,
    x.moenster,
    COUNT(*)                                                            AS antal,
    SUM(CASE WHEN x.vaerdi IS NOT NULL AND TRY_CONVERT(DATE, x.vaerdi) IS NULL
             THEN 1 ELSE 0 END)                                         AS kan_ikke_tolkes
FROM (
    SELECT 'Fra' AS kolonne, f.Fra AS vaerdi,
           ISNULL(TRANSLATE(f.Fra, N'0123456789', N'9999999999'), N'(NULL)') AS moenster
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
    UNION ALL
    SELECT 'Til', f.Til,
           ISNULL(TRANSLATE(f.Til, N'0123456789', N'9999999999'), N'(NULL)')
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
    UNION ALL
    SELECT 'Visiteret', f.Visiteret,
           ISNULL(TRANSLATE(f.Visiteret, N'0123456789', N'9999999999'), N'(NULL)')
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
) AS x
GROUP  BY x.kolonne, x.moenster
ORDER  BY x.kolonne, antal DESC;

SELECT '4b. Serverens sprog' AS afsnit, @@LANGUAGE AS sprog,
       (SELECT dateformat FROM sys.syslanguages WHERE name = @@LANGUAGE) AS datoformat;

/* --- 5. Indsatser pr. Indholdstype: hvad er udfyldt? ----------------- */
SELECT
    '5. Indholdstype'                                                   AS afsnit,
    CASE WHEN PATINDEX(@sagsnr, f.Indholdstype) > 0
         THEN STUFF(f.Indholdstype, PATINDEX(@sagsnr, f.Indholdstype) + 9, 6, N'XXXXXX')
         ELSE f.Indholdstype END                                         AS Indholdstype,
    COUNT(*)                                                            AS raekker,
    COUNT(DISTINCT f.SagsId)                                            AS sager,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(f.Ydelse)), N'') IS NOT NULL THEN 1 ELSE 0 END)         AS med_ydelse,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(f.Bevillingstype)), N'') IS NOT NULL THEN 1 ELSE 0 END) AS med_bevillingstype,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(f.Fra)), N'') IS NULL THEN 1 ELSE 0 END)                AS uden_fra,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(f.Timer)), N'') IS NOT NULL THEN 1 ELSE 0 END)          AS med_timer,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(f.Visiteret)), N'') IS NOT NULL THEN 1 ELSE 0 END)      AS med_visiteret,
    SUM(CASE WHEN s.findes IS NULL THEN 1 ELSE 0 END)                   AS sag_findes_ikke
FROM   GOStaging.dbo.Foranstaltningsdata AS f
/* OUTER APPLY frem for EXISTS: SQL Server tillader ikke en underforespoergsel
   inde i SUM(). TOP 1 saa en dubleret GOSagsID ikke tæller raekken to gange. */
OUTER  APPLY (SELECT TOP 1 1 AS findes FROM GOStaging.dbo.Cases AS c
              WHERE c.GOSagsID = f.SagsId) AS s
GROUP  BY f.Indholdstype
ORDER  BY raekker DESC;

/* --- 6. B14: kolliderer den sammensatte noegle? ---------------------- */
SELECT
    '6. Noeglekollisioner'                                              AS afsnit,
    COUNT(*)                                                            AS noegler_med_flere_raekker,
    ISNULL(SUM(n), 0)                                                   AS raekker_der_ville_blive_udeladt
FROM (
    SELECT COUNT(*) AS n
    FROM   GOStaging.dbo.Foranstaltningsdata AS f
    GROUP  BY f.SagsId, f.Indholdstype, f.Ydelse, TRY_CONVERT(DATE, f.Fra)
    HAVING COUNT(*) > 1
) AS k;

/* --- 7a. Sager pr. omraade og status ---------------------------------- */
SELECT
    '7a. Sager'                                                         AS afsnit,
    c.Organisering,
    CASE WHEN PATINDEX(@sagsnr, c.Sagsstatus) > 0
         THEN STUFF(c.Sagsstatus, PATINDEX(@sagsnr, c.Sagsstatus) + 9, 6, N'XXXXXX')
         ELSE c.Sagsstatus END                                         AS Sagsstatus,
    COUNT(*)                                                            AS sager,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(c.CPR)), N'') IS NULL THEN 1 ELSE 0 END)        AS uden_cpr,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(c.Gruppe)), N'') IS NOT NULL THEN 1 ELSE 0 END) AS med_gruppe,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(c.Sagsprofil)), N'') IS NULL THEN 1 ELSE 0 END) AS uden_sagsprofil,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(c.KLE)), N'') IS NULL THEN 1 ELSE 0 END)        AS uden_kle,
    SUM(CASE WHEN c.StartDato IS NULL THEN 1 ELSE 0 END)                             AS uden_startdato
FROM   GOStaging.dbo.Cases AS c
GROUP  BY c.Organisering, c.Sagsstatus
ORDER  BY c.Organisering, sager DESC;

/* --- 7b. Hvad afgoer skabelonen? Sagsprofil pr. omraade ------------- */
SELECT
    '7b. Sagsprofil'                                                    AS afsnit,
    c.Organisering,
    CASE WHEN PATINDEX(@sagsnr, c.Sagsprofil) > 0
         THEN STUFF(c.Sagsprofil, PATINDEX(@sagsnr, c.Sagsprofil) + 9, 6, N'XXXXXX')
         ELSE c.Sagsprofil END                                         AS Sagsprofil,
    COUNT(*)                                                            AS sager
FROM   GOStaging.dbo.Cases AS c
GROUP  BY c.Organisering, c.Sagsprofil
ORDER  BY c.Organisering, sager DESC;

/* --- 7c. Parter -------------------------------------------------------- */
SELECT
    '7c. Parter'                                                        AS afsnit,
    CASE WHEN PATINDEX(@sagsnr, k.Rolle) > 0
         THEN STUFF(k.Rolle, PATINDEX(@sagsnr, k.Rolle) + 9, 6, N'XXXXXX')
         ELSE k.Rolle END                                         AS Rolle,
    COUNT(*)                                                            AS kontakter,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(k.CPR)), N'') IS NULL THEN 1 ELSE 0 END) AS uden_cpr,
    SUM(CASE WHEN NULLIF(LTRIM(RTRIM(k.CVR)), N'') IS NOT NULL THEN 1 ELSE 0 END) AS med_cvr,
    SUM(CASE WHEN k.Adressebeskyttelse = 1 THEN 1 ELSE 0 END)           AS adressebeskyttede,
    SUM(CASE WHEN s.findes IS NULL THEN 1 ELSE 0 END)                   AS sag_findes_ikke
FROM   GOStaging.dbo.Contacts AS k
OUTER  APPLY (SELECT TOP 1 1 AS findes FROM GOStaging.dbo.Cases AS c
              WHERE c.GOSagsID = k.SagsId) AS s
GROUP  BY k.Rolle
ORDER  BY kontakter DESC;
