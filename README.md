# BiteCast

BiteCast is a standalone, interactive browser for Dolphin half-day sportfishing results from Fisherman's Landing in San Diego.

The site covers every available SanDiegoFishReports Dolphin `1/2 Day AM` and `1/2 Day PM` record from January 1, 2024 through August 30, 2026. Kept and released fish remain separate in the underlying data, while the main encounter metric includes both.

## Open the site

Open `index.html` directly in a modern browser. The complete dataset, charts, filters, and forecast interface are embedded in that file, so the site works without a server or external JavaScript dependencies.

For local development, you can also serve the repository directory with any static web server.

## Publish with GitHub Pages

1. Create a GitHub repository and push this directory to its default branch.
2. In the repository, open **Settings → Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Select the default branch and the `/ (root)` folder, then save.

GitHub Pages will publish `index.html` as the site homepage.

## Data and methodology

- Primary fish-count source: [SanDiegoFishReports daily boat archive](https://www.sandiegofishreports.com/dock_totals/boats.php)
- Boat and landing: Dolphin, Fisherman's Landing
- Included trips: half-day AM and half-day PM only
- Encounter metric: kept fish + released fish
- Missing trip reports are treated as missing observations, not zero catch
- Exact duplicate source rows are removed; distinct same-period reports remain separate trips
- Historical coastal weather: NOAA NDBC station LJAC1
- Historical nearshore waves: NOAA NDBC station 46235
- Forecast marine inputs: NWS SGX marine forecast gridpoint 53,12

The forecast is weather-first. Its model uses wind, gusts, pressure and pressure change, air and water temperature, three-day water-temperature change, wave height, dominant and average periods, wave direction, AM/PM, and recent encounter-state measures. It does not use month, season, day-of-year, or year as a predictor.

Validation is rolling-origin and strictly chronological. Before each 2025–2026 fishing day, the model is refit using every earlier trip; all trips on the forecast day remain unseen. Current validation covers 781 unseen trips with MAE 2.06 encounters/angler, RMSE 2.88, and correlation 0.32; 60% are within ±2.0 encounters/angler. The site labels this confidence honestly.

Each forecast includes two asymmetric ranges derived from signed `actual − forecast` residuals. The dark typical band uses the 25th–75th percentiles; the light 80% planning band uses the 10th–90th percentiles. Calibration uses the latest 250 prior residuals for AM or PM, falls back to pooled residuals when needed, and clamps displayed lower bounds at zero. Validation ranges are constructed only from errors available before the trip being evaluated.

## Current dataset

- 1,197 distinct trips
- 681 fishing days
- 38,740 anglers
- 148,803 total encounters
- 28,539 released fish included in encounters

## Repository layout

```text
BiteCast/
├── index.html   # Complete deployable site and embedded dataset
└── README.md    # Project and hosting documentation
```

## Attribution

Fish counts belong to their respective source publishers. The fish marks used in BiteCast are original inline SVG illustrations and do not reuse FishDatabase artwork.
