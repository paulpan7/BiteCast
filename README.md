# BiteCast

BiteCast is a standalone browser for San Diego-area half-day sportfishing results and weather-first AM/PM forecasts.

The historical site includes every available SanDiegoFishReports `1/2 Day AM` and `1/2 Day PM` row from January 1, 2024 through August 31, 2026. It covers 27 boats at five landings in San Diego and Oceanside. Kept and released fish remain separate in the embedded data, while the main encounter metric includes both.

The Forecast tab includes a boat dropdown synchronized with the Historical boat filter. Dolphin uses the validated model. Other boats use a clearly labeled provisional transfer of the Dolphin weather pattern shifted to the selected boat's recent 12-trip AM/PM encounter level; those transfers are not independently validated. Directly below the evidence header, a seven-day timeline compares rounded light-AM and dark-PM encounter-per-angler bars side by side for the selected boat.

## Open or publish the site

Open `index.html` directly in a modern browser. The complete dataset, charts, filters, and forecast interface are embedded in that file, so the site works without a server or external JavaScript dependencies.

To publish with GitHub Pages:

1. Push this directory to a GitHub repository.
2. Open **Settings → Pages** in that repository.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Select the default branch and the `/ (root)` folder, then save.

## Historical coverage

- Primary fish-count source: [SanDiegoFishReports daily boat archive](https://www.sandiegofishreports.com/dock_totals/boats.php)
- Included trips: half-day AM and half-day PM only
- 5,279 distinct trips across 947 fishing days
- 27 boats and five landings
- 149,868 anglers
- 588,931 total encounters, including 120,615 released fish
- Missing reports are treated as missing observations, not zero catch
- Exact duplicate source rows are removed; distinct same-period reports remain separate trips

Historical filters include species, boat, landing, year, AM/PM, and date range. Species is a dropdown populated from the embedded archive.

The default **Trending** tab provides selectable 7-, 30-, 60-, and 90-day views. It ranks the eight most-reported species for the selected window and draws a daily light-AM/dark-PM bar chart for each. A second chart ranks the ten highest-producing boats with AM and PM shown separately on the same scale. All totals include kept and released encounters; missing reports remain missing rather than confirmed zero-catch days.

## Forecast and validation

- Validated forecast boat: Dolphin, Fisherman's Landing
- Provisional selectable forecasts: every other boat with reported half-day history
- Historical coastal weather: NOAA NDBC station LJAC1
- Historical nearshore waves: NOAA NDBC station 46235
- Seven-day inputs: NWS SGX marine forecast gridpoint 53,12

The weather-first model uses wind, gusts, pressure and pressure change, air and water temperature, three-day water-temperature change, wave height, dominant and average periods, wave direction, AM/PM, and recent Dolphin encounter-state measures. It does not use month, season, day-of-year, or year as a predictor.

Validation is rolling-origin and strictly chronological. Before each 2025–2026 Dolphin fishing day, the model is refit using every earlier Dolphin trip; all trips on the forecast day remain unseen. Current validation covers 781 unseen trips with MAE 2.06 encounters per angler, RMSE 2.88, and correlation 0.32; 60% are within ±2.0 encounters per angler.

The Dolphin forecast includes two asymmetric ranges derived from signed `actual − forecast` residuals. The dark typical band uses the 25th–75th percentiles; the light planning band uses the 10th–90th percentiles. Calibration uses the latest 250 prior residuals for AM or PM, falls back to pooled residuals when needed, and clamps displayed lower bounds at zero. Other boats inherit shifted Dolphin bands as provisional planning guides, not calibrated boat-specific confidence intervals.

## Keeping BiteCast current

`index.html` is static, so it does not update by itself. The recommended production setup is a scheduled GitHub Actions refresh once daily after dock totals normally settle:

1. Re-scrape at least the latest 14 SanDiegoFishReports days, not only yesterday. This captures late reports and corrections.
2. Merge using a stable fingerprint of date, boat, landing, period, anglers, and species counts. Replace corrected rows instead of simply appending them.
3. Refresh completed NOAA observations and the current NWS seven-day forecast grid.
4. Recompute each boat/period's recent encounter state, retrain, and rerun rolling-origin validation.
5. Rebuild `index.html` and publish only if quality checks pass.

Recommended publishing guardrails:

- The source request completed without archive-page errors.
- The historical trip count did not unexpectedly shrink.
- The latest fish report and weather timestamps are recent.
- No forecast is published for a boat without enough prior AM/PM trips.
- Validation ranges are calibrated only from errors known before each test trip.
- A material drop in model accuracy, interval coverage, or input coverage blocks deployment and preserves the last good site.

For a boat-specific model, use a rolling training window only if walk-forward testing shows it beats the expanding-history model. A practical comparison is expanding history versus the latest 90, 180, and 365 days; select the policy by chronological validation, never by fit on the same trips used for training.

## Repository layout

```text
BiteCast/
├── index.html   # Complete deployable site and embedded dataset
└── README.md    # Hosting, source, validation, and refresh policy
```

## Attribution

Fish counts belong to their respective source publishers. The fish marks used in BiteCast are original inline SVG illustrations and do not reuse FishDatabase artwork.
