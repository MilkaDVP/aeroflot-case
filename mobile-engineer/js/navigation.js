/* Маршрут на телефоне: граф и разрешённые виды передвижения из того же аэропорта. */
class LocalNavigation {
  async load(icao, onGraph) {
    onGraph(null);
    const key = CONFIG.apiBase + "/graph/" + icao;
    this.key = key;
    this.signature = null;
    this.nodes = null;
    this.path = null;
    const cached = await OfflineStore.get(key);
    if (cached && this.key === key) { this.index(cached); onGraph(cached); }
    try {
      const graph = await Api.airportGraph(icao);
      if (key !== this.key) return;
      this.index(graph);
      onGraph(graph);
      await OfflineStore.set(key, graph);
    } catch { /* Сохранённая схема остаётся доступной. */ }
  }

  index(graph) {
    this.nodes = new Map(graph.nodes.map(p => [p.id, {...p, links: []}]));
    for (const e of graph.edges) {
      const a = this.nodes.get(e.from_id), b = this.nodes.get(e.to_id);
      if (a && b) {
        // Изгибы дороги — в градусах и в порядке движения.
        const bends = this.bends(e.points, graph.ref_point);
        a.links.push([b,e,bends]); b.links.push([a,e,[...bends].reverse()]);
      }
    }
    this.path = null;
  }

  bends(points, ref) {
    if (!ref) return [];
    const metres = Math.PI / 180 * 6371008.8;
    return (points || []).map(([x, y]) => ({id:null, lat:ref.lat - y / metres,
      lon:ref.lon + x / (metres * Math.cos(ref.lat * Math.PI / 180))}));
  }

  route(payload, pos) {
    const original = payload?.route_points || [];
    if (!pos || !original.length || !this.nodes) return original;
    const job = payload.call;
    const signature = JSON.stringify([job?.id, payload.instruction, original.map(p => p.id || [p.lat,p.lon]), job?.route_stops, job?.waypoints, payload.vehicle?.pickup]);
    if (signature !== this.signature) {
      this.signature = signature;
      this.stops = (payload.instruction ? [] : job?.route_stops || []).filter(s =>
        !(job.waypoints || []).some(w => w.node_id === s.node_id && w.done_at) &&
        !(s.kind === "warehouse" && job.tools_taken_at)).slice();
      if (!payload.instruction) {
        // Некоторые обязательные точки не требуют посадки или другой остановки.
        const indexOnRoute = id => original.findIndex(p => p.id === id);
        for (const waypoint of job?.waypoints || []) {
          if (waypoint.done_at || this.stops.some(s => s.node_id === waypoint.node_id)) continue;
          const next = this.stops.findIndex(s => indexOnRoute(s.node_id) > indexOnRoute(waypoint.node_id));
          this.stops.splice(next < 0 ? this.stops.length : next, 0, waypoint);
        }
      }
      this.driving = payload.vehicle && !payload.vehicle.pickup;
      if (payload.instruction) this.driving = payload.instruction.startsWith("Верните спецтранспорт");
      this.path = null;
    }
    while (this.stops.length) {
      const stop = this.stops[0], node = this.nodes.get(stop.node_id);
      if (!node || haversineM(pos.lat,pos.lon,node.lat,node.lon)>25) break;
      if (stop.kind === "board") this.driving = true;
      if (stop.kind === "alight") this.driving = false;
      this.stops.shift(); this.path = null;
    }
    const goal = original[original.length-1];
    const goals = this.stops.map(s => ({node:this.nodes.get(s.node_id),kind:s.kind}));
    goals.push({node:this.nodes.get(goal.id) || this.nearest(goal,this.driving)});
    const start = this.nearest(pos,this.driving);
    if (!start) return original;
    if (!this.path || this.start !== start.id) {
      let cursor = start, driving = this.driving;
      const path = [cursor];
      for (const g of goals) {
        if (!g.node) return original;
        const leg = this.shortest(cursor,g.node,driving);
        if (!leg) return original;
        path.push(...leg.slice(1));
        cursor = g.node;
        if (g.kind === "board") driving = true;
        if (g.kind === "alight") driving = false;
      }
      this.path = [...path,goal]; this.start = start.id;
    }
    return [pos,...this.path];
  }

  nearest(pos, driving) {
    let nearest, best = Infinity;
    for (const p of this.nodes.values()) {
      const dist = haversineM(pos.lat,pos.lon,p.lat,p.lon);
      if (dist < best && p.links.some(([,e]) => driving ? e.vehicle_allowed : e.walk_allowed !== false)) {
        nearest=p; best=dist;
      }
    }
    return nearest;
  }

  shortest(start, goal, driving) {
    const queue = [start], dist = new Map([[start,0]]), previous = new Map();
    while (queue.length) {
      queue.sort((a,b) => dist.get(b)-dist.get(a));
      const p = queue.pop();
      if (p === goal) {
        const path = [p];
        for (let at = p; previous.has(at);) {
          const [from, bends] = previous.get(at);
          path.push(...[...bends].reverse(), from);
          at = from;
        }
        return path.reverse();
      }
      for (const [next,e,bends] of p.links) {
        if (driving ? !e.vehicle_allowed : e.walk_allowed === false) continue;
        // Как на сервере: быстрее, а не короче — с ограничениями скорости.
        const own = driving ? 20 : 5;
        const cost = dist.get(p)+e.distance_m/Math.min(own, e.speed_kmh || own);
        if (cost >= (dist.get(next) ?? Infinity)) continue;
        dist.set(next,cost); previous.set(next,[p,bends]);
        if (!queue.includes(next)) queue.push(next);
      }
    }
    return null;
  }
}
const localNavigation = new LocalNavigation();
