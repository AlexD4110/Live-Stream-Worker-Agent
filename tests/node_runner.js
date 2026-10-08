// Loads the dashboard's data + metrics exactly as the browser does, prints results as JSON.
// usage: node tests/node_runner.js <path to data.js> [filter-json]
const fs = require('fs');
const path = require('path');
global.window = {};
require(path.resolve(process.argv[2]));
const Metrics = require('../dashboard/metrics.js');
const D = global.window.GIFTING_DATA;
const filter = process.argv[3] ? JSON.parse(process.argv[3]) : {};
const out = { summary: Metrics.summary(D, filter), findings: Metrics.findings(D) };
console.log(JSON.stringify(out));
