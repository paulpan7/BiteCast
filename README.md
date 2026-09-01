# BiteCast

BiteCast is a standalone browser for San Diego-area half-day sportfishing results and weather-first AM/PM forecasts.

The historical site includes usable SanDiegoFishReports `1/2 Day AM` and `1/2 Day PM` rows from January 1, 2017 through August 30, 2026. It covers 46 named boats at four San Diego-area landings. Kept and released fish remain separate in the embedded data, while the main encounter metric includes both. One malformed 2018 source row with a blank boat identity is excluded. Oceanside Sea Center and its associated boats are also excluded because their reported encounter rates were judged unreliable for comparison and model training.

The former 7-day Forecast view is archived from the navigation for now, while its model, data, and interface remain in the page for future reuse. Every boat uses a chronologically validated weather model: boats with sufficient history receive an independent fit, while sparse-history boats use the validated shared fleet fit.

The BiteCast tab ranks up to five active, sufficiently sampled boats and recommends each boat's better AM or PM half-day. Users can filter Charter and Party boats; Charter is assigned when fewer than 10 anglers were reported on more than half of that boat's historical trips. The species selector is limited to the 16 highest-volume species. Three traditional monthly heatmap calendars appear side by side, each with a month title and Monday-through-Sunday columns; hovering a day shows its best modeled encounter rate. Calendar colors use five numeric encounter bands held fixed across the full six-month outlook, with narrower bands at the low end for useful separation. Selecting a day updates a detailed color-coded chart with the point estimate, typical 50% range, and wider planning 80% range. Arrows move three months at a time across a six-month outlook. Days 1–7 use the live NWS marine forecast, while later dates use NOAA seasonal median water temperature and swell near that calendar date as model inputs. Every date uses NOAA CO-OPS San Diego station 9410170 hourly tide predictions summarized as the total tide swing in the matching AM or PM half-day. The later weather component is labeled as a seasonal-weather outlook rather than a live weather forecast.

## Open or publish the site

Open `index.html` directly in a modern browser. The complete dataset, charts, filters, and forecast interface are embedded in that file, so the site works without a server or external JavaScript dependencies.

The interface adapts to phone and tablet screens: filters stack into one column, calendar months stack vertically, navigation remains swipeable, and wide charts scroll inside their cards without widening the page.

To publish with GitHub Pages:

1. Push this directory to a GitHub repository.
2. Open **Settings → Pages** in that repository.
3. Under **Build and deployment**, choose **Deploy from a branch**.
4. Select the default branch and the `/ (root)` folder, then save.

## Historical coverage

- Primary fish-count source: [SanDiegoFishReports daily boat archive](https://www.sandiegofishreports.com/dock_totals/boats.php)
- Included trips: half-day AM and half-day PM only
- 17,496 distinct trips across 3,411 fishing days
- 46 named boats and four landings
- 486,296 anglers
- 1,620,012 total encounters, including 234,299 released fish
- Missing reports are treated as missing observations, not zero catch
- Exact duplicate source rows are removed; distinct same-period reports remain separate trips

Historical filters include species, boat, landing, year, AM/PM, and date range. Species is a dropdown populated from the embedded archive.

The default **Trending** tab provides selectable 7-, 30-, 60-, 90-day, and current-year YTD views. It ranks the 8 most-reported species for the selected window and draws light-AM/dark-PM bars using daily bins for 7 and 30 days, weekly average-daily bins for 60 and 90 days, and monthly average-daily bins for YTD. Longer-bin averages divide by AM/PM dates with reports, so missing report days are not counted as zero. Each selected window recalculates its chart limit from the visible bin maximum with 20% headroom. The card header still shows the full encounter total for the selected range. A second chart ranks the 10 highest-producing boats with AM and PM shown separately on the same scale. All totals include kept and released encounters; missing reports remain missing rather than confirmed zero-catch days.

The **Analysis** tab compares catch with observed weather and signed half-day tide change. Its species selector contains the 10 highest-volume species in the archive. A shared weather/tide selector controls both a catch-condition time series and a fish-count scatter plot for water temperature, air temperature, pressure, swell height, or tide change. Tide change is the NOAA-predicted level at the end of the selected half-day minus its starting level: positive is rising and negative is falling. Its histogram uses a zero-centered axis and shows an explicit `+` on positive ticks. Missing condition observations remain visible as gaps rather than being joined by a straight line. Date-range and AM/PM filters update both plots together. Single-year views always span January 1 through December 31 so month ticks align exactly when comparing years. The fitted time chart automatically uses monthly ticks for broad ranges and weekly ticks for shorter windows. Its Fit, zoom-in, zoom-out, and Window zoom controls support centered zooming or drag-to-select box zoom without leaving the page. The scatter plot reports Pearson correlation `r`, linear `R²`, direction, and the number of matched date/AM-PM observations. Catch is aggregated across boats before joining weather so a single condition window is not duplicated once per boat. Correlation is descriptive and may include seasonal effects; it is not evidence that weather or tide caused the catch.

## Forecast and validation

- Validated forecasts: every boat with reported half-day history
- Sparse-history policy: use the chronologically validated shared fleet model
- Historical coastal weather: NOAA NDBC station LJAC1
- Fallback swell and sea temperature: NOAA NDBC station 46225
- Signed half-day predicted tide change: NOAA CO-OPS San Diego station 9410170
- Historical nearshore waves: NOAA NDBC station 46235
- Seven-day inputs: NWS SGX marine forecast gridpoint 53,12

The per-boat model uses sea-surface temperature, swell height, signed NOAA half-day tide change, AM/PM, and that boat's recent 12-trip catch state. Tide change is the predicted level at the end of the half-day minus the level at its start: positive is rising and negative is falling. The model does not use month, season, day-of-year, or year as a predictor. Inputs are scaled and their coefficients are learned from training trips; they are not assigned fixed percentage weights. Ridge regularization with λ = 12 shrinks weak or noisy effects toward zero, and fish per angler is capped at 20 during fitting so isolated extreme reports cannot dominate the forecast. In the chronological fleet test, the signed tide term has low influence: MAE is 2.0529 and RMSE is 2.7142 fish per angler, versus 2.0502 and 2.7112 without tide.

Validation is strictly chronological. The validation fit uses 2018–2024 weather-matched trips, then faces 2025–2026 trips that were never used for fitting. Boats with at least 20 matched training trips use a boat-specific validation fit. Other boats use the shared fleet fit. The page computes and displays fleet holdout MAE, RMSE, within-±2 coverage, and interval coverage from the embedded data when it loads. LJAC1 observations extend through the full archive; station 46235 wave history begins in 2018, so 2017 contributes to catch history but not the swell-based model fit.

In the page's plain-language accuracy guide, **holdout** means later trips set aside as a closed-book test, **MAE** is the average forecast miss in fish per angler, **RMSE** is an error score that penalizes large misses more heavily, and **within ±2 fish** is the percentage of forecasts within 2 fish per angler of the actual result.

Forecasts include two asymmetric ranges derived from signed `actual − forecast` holdout residuals. The dark typical band uses the 25th–75th percentiles; the light planning band uses the 10th–90th percentiles. A boat's own residuals are used after 30 unseen trips; otherwise the validated fleet residual distribution supplies the range. Displayed lower bounds are clamped at zero.

## Model freeze and prospective validation

The BiteCast model and embedded training data are frozen at reports through August 30, 2026. They must remain unchanged until the user explicitly requests model updates to resume. `data/validation/MODEL_FROZEN.json` is the repository guard; `scripts/extend_history.py` refuses any operation that would modify `index.html` while that marker exists. Source pages may still be collected with `--download-only`, and prospective validation may read them without merging them into the site or retraining.

Forecasts for September 1–28, 2026 are preserved in `data/validation/frozen_forecasts_2026-09-01_2026-09-28.json`. After the window closes, run:

```bash
python3 scripts/validate_frozen_forecast.py data/validation/frozen_forecasts_2026-09-01_2026-09-28.json
```

The comparison aggregates multiple same-boat/period reports by anglers, then reports MAE, RMSE, signed bias, angler-weighted MAE, within-1 and within-2 accuracy, and 50%/80% interval coverage. It writes a separate validation report and never changes the frozen model.

When model updates are explicitly resumed, the refresh process should:

1. Re-scrape at least the latest 14 SanDiegoFishReports days, not only yesterday. This captures late reports and corrections.
2. Merge using a stable fingerprint of date, boat, landing, period, anglers, and species counts. Replace corrected rows instead of simply appending them.
3. Refresh completed NOAA observations and the current NWS seven-day forecast grid.
4. Recompute each boat/period's recent catch state, retrain, and rerun chronological validation.
5. Rebuild `index.html` and publish only if quality checks pass.

Recommended publishing guardrails:

- The source request completed without archive-page errors.
- The historical trip count did not unexpectedly shrink.
- The latest fish report and weather timestamps are recent.
- Sparse-history boats visibly identify their use of the shared fleet model.
- Validation ranges are calibrated only from held-out errors.
- A material drop in model accuracy, interval coverage, or input coverage blocks deployment and preserves the last good site.

For a boat-specific model, use a rolling training window only if walk-forward testing shows it beats the expanding-history model. A practical comparison is expanding history versus the latest 90, 180, and 365 days; select the policy by chronological validation, never by fit on the same trips used for training.

## Repository layout

```text
BiteCast/
├── index.html                 # Complete deployable site and embedded dataset
├── scripts/extend_history.py # Resumable historical fish/NOAA importer
├── scripts/freeze_forecast_snapshot.js # Creates an immutable forecast baseline
├── scripts/validate_frozen_forecast.py # Scores later reports without retraining
├── data/validation/           # Freeze marker, forecast snapshot, and reports
└── README.md                  # Hosting, source, validation, and refresh policy
```

## Attribution

Fish counts belong to their respective source publishers. The fish marks used in BiteCast are original inline SVG illustrations and do not reuse FishDatabase artwork.
