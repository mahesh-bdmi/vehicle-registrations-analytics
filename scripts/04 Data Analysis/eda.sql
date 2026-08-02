-- 1. National EV share by financial year

SELECT financial_year,
		SUM(registrations) 	as total_registrations,
		SUM(CASE WHEN fuel_type = 'electric' THEN registrations END) as ev_registrations,
		SUM(CASE WHEN fuel_type = 'electric' THEN registrations END)*100.0/NULLIF(SUM(registrations),NULL) AS ev_share
FROM fact_registrations
WHERE year >=2017 AND month_number >=4
GROUP BY financial_year
ORDER BY financial_year;

-- 2. EV registrations per 1,000 people, by state, latest year
WITH registrations AS
	(SELECT year,state_id,
		SUM(CASE WHEN fuel_type = 'electric' THEN registrations END) as ev_registrations
	FROM fact_registrations
	WHERE year = 2025
	GROUP BY state_id,year)

SELECT  ds.state_name,
		fr.ev_registrations*1000.0 / NULLIF(fp.population,NULL) AS reg_per_1000
FROM registrations fr
LEFT JOIN fact_population fp ON fr.[year] = fp.[year]  AND fr.state_id = fp.state_id
LEFT JOIN dim_state ds ON fr.state_id = ds.state_id 
ORDER BY reg_per_1000 DESC;

--3. Charging station density (as of latest snapshot)

SELECT ds.state_name,
		SUM(CASE WHEN fuel_type = 'electric' THEN registrations END)*1.0/NULLIF(fcs.total_chargers,NULL) as vehicles_per_charger
FROM fact_registrations fr
LEFT JOIN dim_state ds ON fr.state_id  = ds.state_id
LEFT JOIN fact_charging_stations fcs ON fcs.state_id = fr.state_id
WHERE fr.year <=2025
GROUP BY ds.state_name,fcs.total_chargers
ORDER BY vehicles_per_charger DESC


--4.EV adoption leader over the years

CREATE PROCEDURE dbo.ev_leaders
	@Year INT = NULL
AS
BEGIN
	SELECT  ds.state_name,
			SUM(fr.registrations) AS total_registrations,
			SUM(CASE WHEN fuel_type='electric' THEN fr.registrations ELSE 0 END) as ev_registrations,
			((SUM(CASE WHEN fuel_type='electric' THEN fr.registrations ELSE 0 END)*1.0)/SUM(fr.registrations))*100 as [%ev_share]
	FROM fact_registrations fr
	LEFT JOIN dim_state ds ON ds.state_id = fr.state_id
	WHERE year = @Year
	GROUP BY ds.state_name
	ORDER BY [%ev_share] DESC;		
END;

EXEC ev_leaders @YEAR = 2019


--5. EV adoption by state % YoY

WITH state_ev AS

(SELECT  year,state_id,
		 SUM(CASE WHEN fuel_type='electric' THEN registrations ELSE 0 END)*100.0/NULLIF(SUM(registrations),NULL) as ev_share
FROM fact_registrations 
GROUP BY year,state_id)

SELECT ds.state_name,
		CAST(SUM(CASE WHEN se.year = 2017 THEN se.ev_share END) AS DECIMAL(6,2)) as [2017],
		CAST(SUM(CASE WHEN se.year = 2018 THEN se.ev_share END) AS DECIMAL(6,2)) as [2018],
		CAST(SUM(CASE WHEN se.year = 2019 THEN se.ev_share END) AS DECIMAL(6,2)) as [2019],
		CAST(SUM(CASE WHEN se.year = 2020 THEN se.ev_share END) AS DECIMAL(6,2)) as [2020],
		CAST(SUM(CASE WHEN se.year = 2021 THEN se.ev_share END) AS DECIMAL(6,2)) as [2021],
		CAST(SUM(CASE WHEN se.year = 2022 THEN se.ev_share END) AS DECIMAL(6,2)) as [2022],
		CAST(SUM(CASE WHEN se.year = 2023 THEN se.ev_share END) AS DECIMAL(6,2)) as [2023],
		CAST(SUM(CASE WHEN se.year = 2024 THEN se.ev_share END) AS DECIMAL(6,2)) as [2024],
		CAST(SUM(CASE WHEN se.year = 2025 THEN se.ev_share END) AS DECIMAL(6,2)) as [2025],
		CAST(SUM(CASE WHEN se.year = 2026 THEN se.ev_share END) AS DECIMAL(6,2)) as [2026]
FROM state_ev se
LEFT JOIN dim_state ds ON se.state_id = ds.state_id 
GROUP BY ds.state_name


--6. EV adoption growth by state % YoY

WITH state_ev AS

(SELECT  year,state_id,
		 (SUM(CASE WHEN fuel_type='electric' THEN registrations ELSE 0 END)*100.0)/NULLIF(SUM(registrations),NULL) as ev_share
FROM fact_registrations
GROUP BY year,state_id),

pv_ev_share AS
(SELECT  year,
		state_id,
		ev_share AS cyear_ev_share,
		LAG(ev_share,1,NULL) OVER(PARTITION BY state_id ORDER BY year) AS pyear_ev_share
FROM state_ev),

ev_growth_yoy AS
(SELECT *,
		(cyear_ev_share - pyear_ev_share)*100/NULLIF(pyear_ev_share,0) AS [%ev_growth]
FROM pv_ev_share)

SELECT ds.state_name,
		SUM(CASE WHEN year = 2018 THEN [%ev_growth] END) as [2018],
		SUM(CASE WHEN year = 2019 THEN [%ev_growth] END) as [2019],
		SUM(CASE WHEN year = 2020 THEN [%ev_growth] END) as [2020],
		SUM(CASE WHEN year = 2021 THEN [%ev_growth] END) as [2021],
		SUM(CASE WHEN year = 2022 THEN [%ev_growth] END) as [2022],
		SUM(CASE WHEN year = 2023 THEN [%ev_growth] END) as [2023],
		SUM(CASE WHEN year = 2024 THEN [%ev_growth] END) as [2024],
		SUM(CASE WHEN year = 2025 THEN [%ev_growth] END) as [2025],
		SUM(CASE WHEN year = 2026 THEN [%ev_growth] END) as [2026]
		
FROM ev_growth_yoy egy
LEFT JOIN dim_state ds ON ds.state_id = egy.state_id
GROUP BY ds.state_name


