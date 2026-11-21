/*
  104_foranstaltning_profil.sql — hvilke kolonner i Foranstaltningsdata er
  udfyldt, pr. foranstaltningstype (Indholdstype)?

  Kun laesning. Viser INGEN vaerdier fra felterne — kun kolonnenavne,
  typer og optaellinger. Indholdstype er mappetypen (Psykolog, ABA ...),
  ikke persondata. Kig alligevel resultatet igennem, foer det sendes videre.

  Koeres i SSMS mod serveren med GOStaging.

  Baggrund: konverteringsarket (TIL_PPR_Konverteringsark_foranstaltningsdata,
  17-11-2026) bruger visningsnavne som "Skole og institution" og "ABA gruppe".
  Databasens kolonner kan hedde noget andet (SkoleOgInstitution ...). Uden
  de rigtige navne kan tilbudsfelterne ikke mappes.

  Svarer paa:
    1. Hvilke kolonner har tabellen, og hvilken type har de?
    2. Hvor mange foranstaltninger er der pr. type?
    3. Hvilke kolonner er udfyldt for hvilke typer — og hvor mange
       forskellige vaerdier har de (lille tal = vaerdiliste, stort = fritekst)?
*/

SET NOCOUNT ON;

/* --- 1. Kolonnerne --------------------------------------------------- */
SELECT  c.ORDINAL_POSITION         AS nr,
        c.COLUMN_NAME              AS kolonne,
        c.DATA_TYPE                AS datatype,
        c.CHARACTER_MAXIMUM_LENGTH AS maks_laengde
FROM    GOStaging.INFORMATION_SCHEMA.COLUMNS AS c
WHERE   c.TABLE_SCHEMA = 'dbo'
  AND   c.TABLE_NAME   = 'Foranstaltningsdata'
ORDER BY c.ORDINAL_POSITION;

/* --- 2. Antal pr. type ----------------------------------------------- */
SELECT  COALESCE(f.[Indholdstype], N'(tom)') AS indholdstype,
        COUNT(*)                             AS antal,
        COUNT(DISTINCT f.[SagsId])           AS antal_sager
FROM    GOStaging.dbo.Foranstaltningsdata AS f
GROUP BY f.[Indholdstype]
ORDER BY antal DESC;

/* --- 3. Udfyldte kolonner pr. type -----------------------------------
   Bygges dynamisk: én SELECT pr. kolonne, samlet med UNION ALL. En
   vaerdi taeller som udfyldt, naar den ikke er NULL og ikke kun er
   mellemrum. Kolonner der aldrig er udfyldt for en type, vises ikke.
   Id- og noeglekolonner springes over — de er altid udfyldt og siger
   intet om tilbudsfelterne. */
DECLARE @sql NVARCHAR(MAX) = N'';

SELECT @sql = @sql
    + CASE WHEN @sql = N'' THEN N'' ELSE N' UNION ALL ' END
    + N'SELECT COALESCE([Indholdstype], N''(tom)'') AS indholdstype, '
    + N'N' + QUOTENAME(c.COLUMN_NAME, '''') + N' AS kolonne, '
    + CONVERT(NVARCHAR(10), c.ORDINAL_POSITION) + N' AS nr, '
    + N'COUNT(*) AS antal_raekker, '
    + N'COUNT(NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(400), '
    +     QUOTENAME(c.COLUMN_NAME) + N'))), N'''')) AS udfyldt, '
    + N'COUNT(DISTINCT NULLIF(LTRIM(RTRIM(CONVERT(NVARCHAR(400), '
    +     QUOTENAME(c.COLUMN_NAME) + N'))), N'''')) AS forskellige '
    + N'FROM GOStaging.dbo.Foranstaltningsdata GROUP BY [Indholdstype]'
FROM GOStaging.INFORMATION_SCHEMA.COLUMNS AS c
WHERE c.TABLE_SCHEMA = 'dbo'
  AND c.TABLE_NAME   = 'Foranstaltningsdata'
  AND c.COLUMN_NAME NOT IN (N'ID', N'SagsId', N'Indholdstype')
  AND c.DATA_TYPE NOT IN (N'image', N'varbinary', N'binary', N'timestamp')
ORDER BY c.ORDINAL_POSITION;

SET @sql = N'SELECT indholdstype, kolonne, antal_raekker, udfyldt, forskellige '
         + N'FROM (' + @sql + N') AS x WHERE udfyldt > 0 '
         + N'ORDER BY indholdstype, nr;';

EXEC sys.sp_executesql @sql;
