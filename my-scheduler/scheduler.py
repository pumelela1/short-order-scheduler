"""Short Order scheduler - version 6 (experimental: v5 plus a 30-tick lookahead).

Policy:
  * Rail order = value per tick of cook time. Each order is worth
    1 + 0.3 * (patience / span): serving anything scores completion, and
    losing an order whose customer would sit a long time relative to how
    little it needs (a salad, a banquet) wrecks slowdown and fairness far
    more than losing an espresso or a roast. Divide by the work it still
    needs and serve the best ratio first. With every order worth the same
    this is exactly shortest-job-first; the patience term is what tilts it.
  * Orders a cook has already worked on get a fixed bonus, so a dish back
    from the oven is finished before its customer gives up.
  * Never start an order that cannot finish before its customer leaves.
  * Station-aware: only assign an order whose station has a free place, and
    spend places as we assign so nothing is ever refused.
  * Preempt only when a waiting order is tiny next to what a cook is doing.
  * Kitchen with no oven steps: nothing calls us, so set an alarm and rotate
    cooks on short slices, touching never-started orders first.
  * When durations are hidden, estimate every step from the real durations of
    served orders of the same recipe, step by step (better than one mean total).
"""

import time as _time
from kitchen import Decision, Scheduler

LOOKAHEAD_TICKS = 30      # how far each candidate is simulated
LOOKAHEAD_CANDIDATES = 4  # how many of the top-ranked orders are compared
WAIT_PENALTY = 0.02       # weight on simulated waiting (slowdown proxy)
LOOKAHEAD_BUDGET = 0.004  # seconds per decision the lookahead may spend (limit is 0.050)
LOOKAHEAD_MAX_ORDERS = 60 # skip the lookahead in a very crowded kitchen

PATIENCE_WEIGHT = 0.3    # how much a long-suffering customer's slowdown counts
STARTED_BONUS = 6.0      # value added to an order a cook has already worked on
PREEMPT_FRACTION = 0.1   # waiting job must be < 10% of the running job's work
PREEMPT_FRACTION_ONE_COOK = 0.25  # with a single cook, waiting hurts more
CHEAP_SWITCH = 1          # when a switch costs this little or less...
TOUCH_MIN_WORK = 8        # ...a cook on a job this big is taken off to give a new order its first tick
FAIR_TARGET = 1.5         # in a crowded kitchen, let a quick order wait until...
CROWDED_PER_COOK = 15     # ...its slowdown would reach 1.5 (crowded = 15+ ready per cook)
LONG_ORDER_WORK = 80      # an order needing this much cook time or more...
LONG_ORDER_PREEMPT = 0.3  # ...may be interrupted for jobs under 30% of its work
ROTATE_SLICE = 2         # ticks of work per turn in an oven-free kitchen (1 breaks it)


class MyScheduler(Scheduler):
    name = "my_scheduler"
    version = "11"

    def reset(self, seed):
        self._oven_free = None
        self._seen = set()
        self._totals = {}      # (recipe, step index) -> [sum of durations, count]
        self._cache = {}

    # -- learning step durations (only matters when they are hidden) ---------

    def _learn(self, obs):
        for h in obs.history:
            if h.id in self._seen:
                continue
            self._seen.add(h.id)
            for i, (_label, _kind, duration) in enumerate(h.steps):
                if duration is None:
                    continue
                t = self._totals.setdefault((h.recipe, i), [0.0, 0])
                t[0] += duration
                t[1] += 1

    def _step_mean(self, obs, recipe, index, kind):
        t = self._totals.get((recipe, index))
        if t and t[1]:
            return t[0] / t[1]
        menu = obs.recipes.get(recipe)
        if menu is not None and index < len(menu.steps) and menu.steps[index][2]:
            lo, hi = menu.steps[index][2]
            return (lo + hi) / 2.0
        return 8.0 if kind == "work" else 30.0

    def _parts(self, obs, order):
        """(cook time left, oven time left) for an order."""
        if order.id in self._cache:
            return self._cache[order.id]
        work = oven = 0.0
        if obs.kitchen.known_durations:
            work = float(order.work_remaining or 0)
            oven = sum(s.remaining for s in order.steps_left
                       if s.is_wait and s.remaining is not None)
        else:
            done_before = 0
            for i, step in enumerate(order.steps):
                if i < order.step:
                    if step.is_work and step.duration is not None:
                        done_before += step.duration
                    continue
                if step.remaining is not None:
                    left = step.remaining
                else:
                    left = self._step_mean(obs, order.recipe, i, step.kind)
                    if i == order.step and step.is_work:
                        left = max(1.0, left - max(0, order.work_done - done_before))
                if step.is_work:
                    work += left
                else:
                    oven += left
        self._cache[order.id] = (work, oven)
        return work, oven

    def _work(self, obs, order):
        return self._parts(obs, order)[0]

    def _oven(self, obs, order):
        return self._parts(obs, order)[1]

    # -- small helpers -------------------------------------------------------

    def _kitchen_has_no_oven(self, obs):
        if self._oven_free is None:
            self._oven_free = all(
                kind != "wait"
                for recipe in obs.recipes.values()
                for (_label, kind, _ticks) in recipe.steps
            )
        return self._oven_free

    def _can_finish(self, obs, order):
        needed = obs.kitchen.switch_cost + self._work(obs, order) + self._oven(obs, order)
        return needed <= order.time_left

    def _priority(self, obs, order):
        """v7 ranking, plus: when the rail is crowded, an order that would come
        out far faster than everyone else's (slowdown well under 1) waits its
        turn. Fairness is Jain's index over slowdowns, so evening them out
        pays more than the tiny slowdown it adds."""
        key = self._value_priority(obs, order)
        if len(obs.ready) >= CROWDED_PER_COOK * obs.kitchen.cores:
            work = self._work(obs, order)
            oven = self._oven(obs, order)
            span = max(work + oven + order.work_done, obs.kitchen.slowdown_bound)
            projected = (obs.time - order.arrival) + obs.kitchen.switch_cost + work + oven
            if projected < FAIR_TARGET * span:
                key += 1e6
        return key

    def _value_priority(self, obs, order):
        """Lower sorts first: minus (value of serving it / cook time it needs)."""
        work = self._work(obs, order)
        span = max(work + self._oven(obs, order), obs.kitchen.slowdown_bound)
        patience = order.deadline - order.arrival
        value = 1.0 + PATIENCE_WEIGHT * patience / span
        if order.started is not None:
            # Work already sunk into it is wasted if the customer walks out.
            value += STARTED_BONUS
        return -value / max(work, 0.5)

    @staticmethod
    def _place_for(candidate, current, free):
        """Is there room for `candidate` if `current` is taken off its cook?"""
        station = candidate.station
        if station is None:
            return True
        freed = 1 if current.station == station else 0
        return free.get(station, 0) + freed > 0

    @staticmethod
    def _take_place(candidate, current, free):
        if candidate.station is not None and candidate.station != current.station:
            free[candidate.station] -= 1

    # -- the policy ----------------------------------------------------------

    def schedule(self, obs):
        self._cache = {}
        self._learn(obs)
        decision = Decision()

        rail = [o for o in obs.ready if self._can_finish(obs, o)]
        rail.sort(key=lambda o: self._priority(obs, o))

        # 1. Fill idle cooks, counting station places as we spend them.
        free = obs.free_stations()
        use_la = not self._kitchen_has_no_oven(obs)
        t0 = _time.perf_counter()
        sim = None
        for core in obs.idle_cores:
            feas = [o for o in rail if o.station is None or free.get(o.station, 0) > 0]
            if not feas:
                continue
            choice = feas[0]
            K = LOOKAHEAD_CANDIDATES
            if (use_la and len(feas) > 1 and len(obs.orders) <= LOOKAHEAD_MAX_ORDERS
                    and _time.perf_counter() - t0 < LOOKAHEAD_BUDGET):
                try:
                    snap = self._snapshot(obs, decision)
                    best = None
                    for cand in feas[:K]:
                        if _time.perf_counter() - t0 > LOOKAHEAD_BUDGET:
                            best = None      # out of time: keep the plain choice
                            break
                        j = self._rollout(obs, snap, core.id, cand.id)
                        if best is None or j > best[0] + 1e-9:
                            best = (j, cand)
                    if best is not None:
                        choice = best[1]
                except Exception:
                    choice = feas[0]         # never let the lookahead crash a decision
            decision.assign(core, choice.id)
            if choice.station is not None:
                free[choice.station] -= 1
            rail.remove(choice)

        # 1b. Cheap switching: start every new order at once (response time).
        if obs.kitchen.switch_cost <= CHEAP_SWITCH and not self._kitchen_has_no_oven(obs):
            self._touch_new_orders(obs, decision, rail, free)

        # 2. Take cooks off orders only when it clearly pays.
        if self._kitchen_has_no_oven(obs):
            self._rotate(obs, decision, rail, free)
        elif obs.kitchen.switch_cost > CHEAP_SWITCH:
            # (with cheap switching, touching new orders already does this job)
            self._preempt_for_tiny_jobs(obs, decision, rail, free)

        return decision.annotate(
            text=f"{len(obs.ready)} on the rail, value per tick",
            queue=[o.id for o in rail],
        )

    # -- lookahead -------------------------------------------------------------

    def _snapshot(self, obs, decision):
        """Plain-data copy of the kitchen for simulation."""
        sc = obs.kitchen.switch_cost
        orders = {}
        for o in obs.orders:
            steps = []
            for i, st in enumerate(o.steps):
                if i < o.step:
                    continue
                if st.remaining is not None:
                    d = st.remaining
                elif st.duration is not None:
                    d = st.duration
                else:
                    d = self._step_mean(obs, o.recipe, i, st.kind)
                    if i == o.step and st.is_work:
                        d = max(1.0, d - 0)  # rough
                steps.append([st.kind == "work", st.station, max(1, int(round(d)))])
            work, oven = self._parts(obs, o)
            span = max(work + oven + o.work_done, obs.kitchen.slowdown_bound)
            orders[o.id] = {
                "steps": steps, "state": o.state, "dl": o.deadline, "arr": o.arrival,
                "started": o.started is not None, "span": span, "core": o.core,
            }
        cores = []
        for c in obs.cores:
            oid = c.order
            if c.id in decision.assignments:
                oid = decision.assignments[c.id]
                cores.append([c.id, oid, sc])
            else:
                cores.append([c.id, oid, c.switch_remaining if oid is not None else 0])
        cap = {st.name: st.capacity for st in obs.stations}
        return orders, cores, cap

    def _rollout(self, obs, snap, core_id, cand_id):
        orders0, cores0, cap = snap
        orders = {k: {**v, "steps": [list(x) for x in v["steps"]]} for k, v in orders0.items()}
        cores = [list(c) for c in cores0]
        sc = obs.kitchen.switch_cost
        now = obs.time
        H = LOOKAHEAD_TICKS
        MU = WAIT_PENALTY
        pw = PATIENCE_WEIGHT; sb = STARTED_BONUS
        for c in cores:
            if c[0] == core_id:
                c[1] = cand_id; c[2] = sc
                orders[cand_id]["state"] = "running"
        used = {k: 0 for k in cap}
        for c in cores:
            if c[1] is not None:
                o = orders.get(c[1])
                if o is None:
                    c[1] = None; continue
                o["state"] = "running"
                st = o["steps"][0][1] if o["steps"] else None
                if st is not None and st in used:
                    used[st] += 1
        served = 0; aband = 0; pen = 0.0

        def key(o):
            work = sum(x[2] for x in o["steps"] if x[0])
            oven = sum(x[2] for x in o["steps"] if not x[0])
            span = max(work + oven, obs.kitchen.slowdown_bound)
            v = 1.0 + pw * (o["dl"] - o["arr"]) / span
            if o["started"]:
                v += sb
            return -v / max(work, 0.5)

        for t in range(now, now + H):
            # abandon
            for oid, o in orders.items():
                if o["state"] in ("served", "gone"):
                    continue
                if t >= o["dl"]:
                    if o["state"] == "running":
                        for c in cores:
                            if c[1] == oid:
                                st = o["steps"][0][1] if o["steps"] else None
                                if st in used: used[st] -= 1
                                c[1] = None; c[2] = 0
                    o["state"] = "gone"; aband += 1
            # fill idle cores (base policy) - skip t==now (decision already made)
            if t > now:
                idle = [c for c in cores if c[1] is None]
                if idle:
                    ready = [(key(o), oid) for oid, o in orders.items() if o["state"] == "ready"]
                    ready.sort()
                    for c in idle:
                        for i, (_, oid) in enumerate(ready):
                            o = orders[oid]
                            need = sc + sum(x[2] for x in o["steps"])
                            if need > o["dl"] - t:
                                continue
                            st = o["steps"][0][1]
                            if st is not None and used.get(st, 0) >= cap.get(st, 99):
                                continue
                            c[1] = oid; c[2] = sc; o["state"] = "running"
                            if st is not None: used[st] = used.get(st, 0) + 1
                            ready.pop(i)
                            break
            # waiting penalty
            for o in orders.values():
                if o["state"] == "ready":
                    pen += 1.0 / o["span"]
            # progress ovens
            for o in orders.values():
                if o["state"] == "blocked":
                    stp = o["steps"][0]
                    stp[2] -= 1
                    if stp[2] <= 0:
                        o["steps"].pop(0)
                        if not o["steps"]:
                            o["state"] = "served"; served += 1
                        else:
                            o["state"] = "ready"
            # cooks
            for c in cores:
                if c[1] is None:
                    continue
                if c[2] > 0:
                    c[2] -= 1
                    continue
                o = orders[c[1]]
                o["started"] = True
                stp = o["steps"][0]
                stp[2] -= 1
                if stp[2] > 0:
                    continue
                old_st = stp[1]
                o["steps"].pop(0)
                if not o["steps"]:
                    o["state"] = "served"; served += 1
                    if old_st in used: used[old_st] -= 1
                    c[1] = None
                    continue
                nxt = o["steps"][0]
                if not nxt[0]:
                    o["state"] = "blocked"
                    if old_st in used: used[old_st] -= 1
                    c[1] = None
                    continue
                if nxt[1] != old_st:
                    if old_st in used: used[old_st] -= 1
                    if nxt[1] is not None and used.get(nxt[1], 0) >= cap.get(nxt[1], 99):
                        o["state"] = "ready"; c[1] = None
                    elif nxt[1] is not None:
                        used[nxt[1]] = used.get(nxt[1], 0) + 1
        return served - aband - MU * pen

    def _touch_new_orders(self, obs, decision, rail, free):
        """When a switch costs about nothing, the only thing a customer measures
        before food arrives is how long before anyone starts on it. Take a cook
        off a big job it has already begun and give a never-started order its
        first tick; the big job resumes a tick or two later."""
        fresh = sorted((o for o in rail if o.started is None),
                       key=lambda o: o.time_left)   # most impatient customer first
        if not fresh:
            return
        cores = [c for c in obs.working_cores
                 if c.id not in decision.assignments and c.running_for >= 1]
        cores.sort(key=lambda c: -self._work(obs, obs.order_on(c)))
        for core in cores:
            current = obs.order_on(core)
            if current is None or current.started is None:
                continue
            if self._work(obs, current) < TOUCH_MIN_WORK:
                continue
            for cand in fresh:
                if not self._place_for(cand, current, free):
                    continue
                decision.assign(core, cand.id)
                self._take_place(cand, current, free)
                fresh.remove(cand)
                rail.remove(cand)
                break

    def _preempt_for_tiny_jobs(self, obs, decision, rail, free):
        switch = obs.kitchen.switch_cost
        cores = [c for c in obs.working_cores if c.id not in decision.assignments]
        cores.sort(key=lambda c: -self._work(obs, obs.order_on(c)))
        for core in cores:
            current = obs.order_on(core)
            if current is None:
                continue
            fraction = (PREEMPT_FRACTION_ONE_COOK if obs.kitchen.cores == 1
                        else PREEMPT_FRACTION)
            if self._work(obs, current) >= LONG_ORDER_WORK:
                fraction = max(fraction, LONG_ORDER_PREEMPT)
            limit = fraction * self._work(obs, current)
            for i, cand in enumerate(rail):
                if self._work(obs, cand) + 2 * switch >= limit:
                    continue
                if not self._place_for(cand, current, free):
                    continue
                decision.assign(core, cand.id)
                self._take_place(cand, current, free)
                rail.pop(i)
                break

    def _rotate(self, obs, decision, rail, free):
        waiting = sorted(rail, key=lambda o: (o.started is not None, self._work(obs, o)))
        cores = [c for c in obs.working_cores
                 if c.id not in decision.assignments and c.running_for >= ROTATE_SLICE]
        cores.sort(key=lambda c: -self._work(obs, obs.order_on(c)))
        for core in cores:
            if not waiting:
                break
            current = obs.order_on(core)
            if current is None:
                continue
            cand = waiting[0]
            if cand.started is not None and self._work(obs, cand) >= self._work(obs, current):
                continue
            if not self._place_for(cand, current, free):
                continue
            decision.assign(core, cand.id)
            self._take_place(cand, current, free)
            waiting.pop(0)
        # Wake up again whenever anyone is cooking - including cooks we only
        # just assigned, or nobody would look at the rail until a dish is done.
        if waiting and (obs.working_cores or decision.assignments):
            decision.wake_in(ROTATE_SLICE)
