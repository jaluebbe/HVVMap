// Plan/live pairing shared by the departure developer pages.

// Live destination covers the plan destination: it contains it, or the
// train runs on from Blankenese to Wedel.
function extendsDestination(planned, live) {
    return (live || '').includes(planned) ||
        (planned === 'Blankenese' && live === 'Wedel');
}

// A row has exactly one planned departure time. A live departure joins
// a plan departure only with the same planned minute and the same
// planned platform (an unknown platform does not conflict); otherwise
// it starts a row of its own. Among equals the same direction id wins.
// Exception S1 (wing trains): a remaining pair within one minute
// matches if platform (known) and destination are identical; the row
// then carries the live planned time. A matched S1 plan departure also
// takes a second live departure with the same known platform and the
// same minute as its live partner whose destination contains the plan
// destination (or is Wedel for a plan Blankenese): the plan cells span
// both rows.
function pair(planDeps, liveDeps, line) {
    const free = new Set(liveDeps.keys());
    const livePlatform = l => l.planned_platform ?? l.platform;
    const exact = (p, l) => l.at === p.at &&
        (!p.platform || !livePlatform(l) || p.platform === livePlatform(l));
    const nearS1 = (p, l) => line === 'S1' && Math.abs(l.at - p.at) <= 1 &&
        !!p.platform && p.platform === livePlatform(l) && p.direction === l.direction;
    const take = (p, match) => {
        let best = null;
        for (const i of free) {
            const l = liveDeps[i];
            if (!match(p, l)) continue;
            if (best === null || (l.direction_id === p.direction_id &&
                liveDeps[best].direction_id !== p.direction_id)) best = i;
        }
        if (best !== null) free.delete(best);
        return best;
    };
    const rows = planDeps.map(p => {
        const i = take(p, exact);
        return { plan: p, live: i === null ? null : liveDeps[i] };
    });
    for (const row of rows) {
        if (row.live) continue;
        const i = take(row.plan, nearS1);
        if (i !== null) row.live = liveDeps[i];
    }
    const groups = rows.map(row => {
        row.at = (row.live ?? row.plan).at;
        const group = [row];
        const platform = row.plan?.platform;
        if (line === 'S1' && row.live && platform) {
            let extra = null;
            for (const i of free) {
                const l = liveDeps[i];
                if (livePlatform(l) !== platform || l.at !== row.live.at ||
                    !(extendsDestination(row.plan.direction, l.direction))) continue;
                extra = i;
                break;
            }
            if (extra !== null) {
                free.delete(extra);
                row.span = 2;
                group.push({ plan: row.plan, live: liveDeps[extra], at: liveDeps[extra].at, cont: true });
            }
        }
        return group;
    });
    for (const i of free) groups.push([{ plan: null, live: liveDeps[i], at: liveDeps[i].at }]);
    return groups.sort((a, b) => a[0].at - b[0].at).flat();
}
