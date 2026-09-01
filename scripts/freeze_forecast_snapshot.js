#!/usr/bin/env node
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const dbStart = html.indexOf('const DB=') + 9;
const dbEnd = html.indexOf(';\n', dbStart);
const DB = JSON.parse(html.slice(dbStart, dbEnd));
const tideMatch = html.match(/forecastTideDeltas=(\[.*?\]);\nfunction forecastTideDelta/s);
if (!tideMatch) throw new Error('Embedded forecast tide series was not found.');
const tideDeltas = JSON.parse(tideMatch[1]);

const startDate = DB.forecast[0].date;
const startSerial = Date.parse(startDate + 'T12:00:00') / 864e5;
const dateAt = index => new Date((startSerial + index) * 864e5).toISOString().slice(0, 10);
const seasonDay = iso => {
  const [, month, day] = iso.split('-').map(Number);
  return Math.round((Date.UTC(2000, month - 1, day) - Date.UTC(2000, 0, 1)) / 864e5);
};
const median = values => {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
};

function features(row) {
  const water = (row.waterTempF - 65) / 10;
  const swell = (row.swellFt - 3) / 3;
  const tide = row.tideDeltaFt / 4;
  const pm = row.period === 'PM' ? 1 : 0;
  const recent = Math.min(20, row.recent12 || 0) / 5;
  return [1, water, water * water, swell, swell * swell, pm, recent, pm * water, tide];
}

function solveRidge(rows, lambda = 12) {
  const count = features(rows[0]).length;
  const matrix = Array.from({length: count}, () => Array(count + 1).fill(0));
  for (const row of rows) {
    const x = features(row);
    for (let i = 0; i < count; i++) {
      matrix[i][count] += x[i] * row.y;
      for (let j = 0; j < count; j++) matrix[i][j] += x[i] * x[j];
    }
  }
  for (let i = 1; i < count; i++) matrix[i][i] += lambda;
  for (let i = 0; i < count; i++) {
    let pivot = i;
    for (let j = i + 1; j < count; j++) if (Math.abs(matrix[j][i]) > Math.abs(matrix[pivot][i])) pivot = j;
    [matrix[i], matrix[pivot]] = [matrix[pivot], matrix[i]];
    const divisor = matrix[i][i] || 1e-9;
    for (let j = i; j <= count; j++) matrix[i][j] /= divisor;
    for (let row = 0; row < count; row++) if (row !== i) {
      const multiplier = matrix[row][i];
      for (let column = i; column <= count; column++) matrix[row][column] -= multiplier * matrix[i][column];
    }
  }
  return matrix.map(row => row[count]);
}

const predict = (model, row) => Math.max(0, features(row).reduce((sum, value, index) => sum + value * model[index], 0));
const weatherByWindow = new Map(DB.analysisRows.map(row => [`${row.date}|${row.period}`, row]));
const queues = new Map();
const trainingRows = [];
for (const trip of [...DB.trips].sort((a, b) => a.date.localeCompare(b.date) || a.period.localeCompare(b.period))) {
  const weather = weatherByWindow.get(`${trip.date}|${trip.period}`);
  if (!weather || !Number.isFinite(weather.waterTempF) || !Number.isFinite(weather.swellFt) || !Number.isFinite(weather.tideDeltaFt)) continue;
  const key = `${trip.boat}|${trip.period}`;
  const queue = queues.get(key) || [];
  const recent12 = queue.length ? queue.reduce((sum, value) => sum + value, 0) / queue.length : DB.boatProfiles[trip.boat].recent12;
  trainingRows.push({boat: trip.boat, date: trip.date, period: trip.period, waterTempF: weather.waterTempF, swellFt: weather.swellFt, tideDeltaFt: weather.tideDeltaFt, recent12, y: Math.min(20, trip.epa)});
  queue.push(Math.min(20, trip.epa));
  if (queue.length > 12) queue.shift();
  queues.set(key, queue);
}

const validationTrain = trainingRows.filter(row => row.date < '2025-01-01');
const validationTest = trainingRows.filter(row => row.date >= '2025-01-01');
const globalHoldoutModel = solveRidge(validationTrain);
const globalModel = solveRidge(trainingRows);
const latestDataDate = DB.trips.reduce((latest, trip) => trip.date > latest ? trip.date : latest, '');
const recentCutoff = new Date(Date.parse(latestDataDate + 'T12:00:00') - 180 * 864e5).toISOString().slice(0, 10);
const boats = Object.keys(DB.boatProfiles).filter(boat => {
  const rows = trainingRows.filter(row => row.boat === boat);
  return rows.length >= 30 && DB.trips.some(trip => trip.boat === boat && trip.date >= recentCutoff);
}).sort();
const models = Object.fromEntries(boats.map(boat => {
  const rows = trainingRows.filter(row => row.boat === boat);
  return [boat, rows.length >= 20 ? solveRidge(rows) : globalModel];
}));
const residualsByBoat = {};
const fleetResiduals = [];
for (const boat of boats) {
  const train = validationTrain.filter(row => row.boat === boat);
  const test = validationTest.filter(row => row.boat === boat);
  const model = train.length >= 20 ? solveRidge(train) : globalHoldoutModel;
  residualsByBoat[boat] = test.map(row => row.y - predict(model, row));
  fleetResiduals.push(...residualsByBoat[boat]);
}
const quantile = (values, q) => {
  const sorted = [...values].sort((a, b) => a - b);
  const position = (sorted.length - 1) * q;
  const index = Math.floor(position), fraction = position - index;
  return sorted[index] + ((sorted[index + 1] ?? sorted[index]) - sorted[index]) * fraction;
};

const liveByDate = new Map(DB.forecast.map(day => [day.date, day]));
const forecastDays = Array.from({length: 28}, (_, index) => {
  const date = dateAt(index);
  const live = liveByDate.get(date);
  if (live) return {date, source: 'live marine forecast', periods: live.periods};
  const target = seasonDay(date);
  const periods = ['AM', 'PM'].map(period => {
    const matching = DB.analysisRows.filter(row => row.period === period && Number.isFinite(row.waterTempF));
    const seasonal = matching.filter(row => {
      const gap = Math.abs(seasonDay(row.date) - target);
      return Math.min(gap, 366 - gap) <= 28;
    });
    const sample = seasonal.length ? seasonal : matching;
    return {period, sstF: median(sample.map(row => row.waterTempF)), seasFt: median(sample.map(row => row.swellFt)) ?? 3};
  });
  return {date, source: 'seasonal weather outlook', periods};
});

const predictions = [];
for (const day of forecastDays) {
  const tideIndex = Math.round(Date.parse(day.date + 'T12:00:00') / 864e5 - startSerial);
  for (const boat of boats) for (const period of day.periods) {
    const profile = DB.boatProfiles[boat];
    const periodProfile = profile.periods[period.period];
    const weight = Math.min(1, periodProfile.n / 12);
    const periodRecent = periodProfile.recent12 ?? profile.recent12;
    const recent12 = weight * periodRecent + (1 - weight) * profile.recent12;
    const tideDeltaFt = tideDeltas[tideIndex][period.period === 'PM' ? 1 : 0];
    const row = {period: period.period, waterTempF: period.sstF, swellFt: period.seasFt ?? 3, tideDeltaFt, recent12};
    const point = predict(models[boat], row), residuals = residualsByBoat[boat].length >= 30 ? residualsByBoat[boat] : fleetResiduals;
    predictions.push({date: day.date, boat, period: period.period, predictedFishPerAngler: +point.toFixed(4), typicalLow: +Math.max(0, point + quantile(residuals, .25)).toFixed(4), typicalHigh: +Math.max(0, point + quantile(residuals, .75)).toFixed(4), planningLow: +Math.max(0, point + quantile(residuals, .1)).toFixed(4), planningHigh: +Math.max(0, point + quantile(residuals, .9)).toFixed(4), waterTempF: +row.waterTempF.toFixed(3), swellFt: +row.swellFt.toFixed(3), tideDeltaFt, weatherSource: day.source});
  }
}

const snapshot = {
  protocol: 'prospective-4-week-holdout-v1',
  frozenAt: new Date().toISOString(),
  trainingDataThrough: latestDataDate,
  validationStart: startDate,
  validationEnd: dateAt(27),
  retrainingAllowedDuringWindow: false,
  featureOrder: ['intercept', 'waterTemp', 'waterTempSquared', 'swell', 'swellSquared', 'PM', 'recent12', 'PMxWaterTemp', 'tideChange'],
  ridgeLambda: 12,
  eligibleBoats: boats,
  predictions
};
const output = process.argv[2] || path.join(root, 'data', 'validation', `frozen_forecasts_${snapshot.validationStart}_${snapshot.validationEnd}.json`);
fs.mkdirSync(path.dirname(output), {recursive: true});
fs.writeFileSync(output, JSON.stringify(snapshot, null, 2) + '\n');
console.log(`${predictions.length} predictions frozen in ${output}`);
