const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");

test("switching calls restores only their own trail and closing discards it", () => {
  const lines = [];
  const document = {createElementNS: () => ({setAttribute(name, value) { this[name] = value; }})};
  const source = readFileSync(resolve(__dirname, "../web-dispatcher/js/map.js"), "utf8");
  const map = vm.runInNewContext(source + "\nObject.create(AirportMap.prototype)", {document});
  map.projectLatLon = (x, y) => ({x, y});
  map.trails = new Map();
  map.layers = {trails: {replaceChildren() { lines.length = 0; }, append(line) { lines.push(line.points); }}};
  const calls = [
    {id: 1, status: "accepted", assigned_employee_id: 10},
    {id: 2, status: "accepted", assigned_employee_id: 20},
  ];
  function draw(selected, offset) {
    const employees = [10,20].map(id => ({id, lat: id + offset, lon: 0}));
    map.drawCallTrails(calls, employees, selected);
  }
  draw(null, 0);
  draw(null, 5);
  assert.deepEqual(lines, []);
  draw(1, 5);
  assert.deepEqual(lines, ["10,0 15,0"]);
  draw(2, 5);
  assert.deepEqual(lines, ["20,0 25,0"]);
  calls[0].status = "queued";
  draw(2, 10);
  calls[0].status = "accepted";
  draw(1, 15);
  draw(1, 20);
  assert.deepEqual(lines, ["10,0 15,0", "25,0 30,0"]);
  calls[0].status = "closed";
  draw(1, 20);
  assert.deepEqual(lines, []);
  assert.equal(map.trails.has(1), false);
});
