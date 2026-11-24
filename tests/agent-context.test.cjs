const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("a follow-up receives the previous exchange and the fresh situation", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../web-dispatcher/js/agent.js"), "utf8");
  let received;
  const ctx = {refresh:async () => {}, Api:{aiAgent:async (command,messages) => {
    received = messages; return {kind:"info",actions:[],answer:"Да"};
  }}};
  vm.createContext(ctx);
  const agent = vm.runInContext(source + "\nagentSituation = () => ({аэропорт:'Тест'}); Agent",ctx);
  await agent.run("А сколько до него?", () => {}, [{question:"Какой борт?",answer:"RA-123"}]);
  assert.equal(received[0].role,"user");
  assert.equal(received[0].content,"Какой борт?");
  assert.equal(JSON.parse(received[1].content).answer,"RA-123");
  assert.match(received[2].content,/Тест/);
  assert.match(received[2].content,/сколько до него/);
});
