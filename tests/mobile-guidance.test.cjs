const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");

function setup() {
  const elements = new Map(), spoken = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {setAttribute() {}});
    return elements.get(id);
  }
  const context = vm.createContext({
    document: {getElementById: element},
    localStorage: {getItem() {}, setItem() {}},
    window: {speechSynthesis: {speak: m => spoken.push(m.text), cancel() {}}, SpeechSynthesisUtterance: true},
    SpeechSynthesisUtterance: class {constructor(text) { this.text = text; }},
  });
  for (const file of ["geo", "guidance"])
    vm.runInContext(readFileSync(resolve(__dirname, "../mobile-engineer/js", file + ".js"), "utf8"), context);
  return {context, spoken, element};
}

const a = {id:"a", lat:55, lon:37}, b = {...a, id:"b", lat:55.001};
const c = {...b, id:"c", lon:37.001};
const job = {call:{id:1, status:"accepted"}};

test("distance to the next turn follows the current segment", () => {
  const {context} = setup();
  const cue = context.nextManeuver(job, [a,b,c], {...a,lat:55.0005});
  assert.equal(cue.action, "Поверните направо");
  assert.ok(cue.metres > 54 && cue.metres < 57);
  assert.equal(context.nextManeuver(job, [a,b,{...c,lon:36.999}], a).action, "Поверните налево");
  assert.equal(context.nextManeuver(job, [a,b,c], {...b,lon:37.0005}).action, "Подойдите к борту");
});

test("transport and tool stops take priority over turns, returns have their own destination", () => {
  const {context} = setup();
  for (const [kind, action] of [["board","Сядьте в транспорт"], ["warehouse","Заберите инструмент"], ["alight","Выйдите из транспорта"]]) {
    const payload = {call:{...job.call, route_stops:[{node_id:"b",kind}]}, vehicle:{pickup:true}};
    assert.equal(context.nextManeuver(payload, [a,b,c], a).action, action);
  }
  assert.equal(context.nextManeuver({instruction:"Верните спецтранспорт на исходную стоянку"}, [a,b], a).action, "Верните спецтранспорт на исходную стоянку");
  assert.equal(context.nextManeuver({call:{status:"arrived"}}, [a,b], a), null);
});

test("speech announces approach once per phase and respects mute", () => {
  const {context, spoken, element} = setup();
  const cue = context.nextManeuver(job, [a,b,c], a);
  context.updateGuidance(cue);
  assert.equal(spoken.length, 0);
  element("nav-voice").onclick();
  for (const metres of [90,49,51,49,10,11]) context.updateGuidance({...cue, metres});
  assert.equal(spoken.length, 3);
  assert.equal(spoken.at(-1), "Поверните направо");
  element("nav-voice").onclick();
  context.updateGuidance({...cue,key:"new"});
  assert.equal(spoken.length, 3);
});

test("a later crossing cannot skip a pending waypoint; travelled metres disappear inside an edge", () => {
  const {context} = setup();
  const payload = {call:{...job.call, waypoints:[{node_id:"b", name:"Склад 2"}]}};
  const halfway = {...a, lat:55.0005};
  const crossing = [a,b,c,halfway];
  const remainder = context.remainingLine(payload, crossing, halfway);
  assert.ok(Math.abs(remainder[0].lat-halfway.lat)<1e-8);
  assert.equal(remainder[1].id, "b");
  assert.equal(context.nextManeuver(payload,crossing,halfway).action, "Пройдите точку Склад 2");
});

test("outbound and return share a road: an implicit stop bounds positioning until completed", () => {
  const {context} = setup();
  const payload = {call:{...job.call,route_stops:[{kind:"warehouse",node_id:"b"},{final:true,node_id:"c"}]}};
  const route = [a,b,a,c];
  assert.equal(context.currentLegEnd(payload,route),1);
  assert.equal(context.remainingLine(payload,route.slice(0,2),a).at(-1).id,"b");
  payload.call.tools_taken_at = "done";
  assert.equal(context.currentLegEnd(payload,[b,a,c]),-1);
});
