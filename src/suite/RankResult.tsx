import type { DevicesDocument, RankDocument } from "./types";
import { formatCreditText } from "./verdict";
import { VerdictBadge } from "./VerdictBadge";

export function RankResult({ doc, devices }: { doc: RankDocument; devices: DevicesDocument | null }) {
  const order = (devices?.devices ?? []).flatMap(d => d.players.map(p => `${d.id}/${p.id}`));
  const keys = Object.keys(doc.devices).sort((a, b) => {
    const ai = order.indexOf(a);
    const bi = order.indexOf(b);
    return (ai < 0 ? order.length : ai) - (bi < 0 ? order.length : bi) || a.localeCompare(b);
  });
  return <div>
    <h3>Picture quality (device-neutral)</h3>
    <table className="suite-table">
      <thead>
        <tr>
          {["#", "File", "Quality", "Pairs compared"].map(h => <th key={h}>{h}</th>)}
        </tr>
      </thead>

      <tbody>
        {doc.quality.map(row => <tr key={row.id}>
          <td>{row.rank}</td>
          <td>{row.label}</td>
          <td>{row.quality_score?.toFixed(1) ?? "not compared"}</td>
          <td>{row.games}</td>
        </tr>)}
      </tbody>
    </table>

    {doc.same_picture_groups?.filter(g => g.length > 1).map((g, i) => <p key={i}>
      Same picture: {g.join(" = ")}
    </p>)}

    {doc.head_to_head_conflicts?.map((c, i) => <p key={i}>
      Note: {c.winner} beat {c.ranked_above} head to head but ranks below it overall
      (quality scores are averages over all pairs of this set).
    </p>)}

    {!!doc.stale_reports?.length
      && <p>
        Ignored {doc.stale_reports.length} stale report(s) (made from files that have changed since,
        or by an older comparison engine): {doc.stale_reports.map(
          r => `${r.name1} vs ${r.name2} (${r.reason})`
        ).join(", ")}
      </p>}

    {!!doc.missing_pairs?.length
      && <p>
        {doc.missing_pairs.length} pair(s) have no comparison report yet.</p>}

    {keys.map(key => {
      const d = doc.devices[key];
      const [deviceId, playerId] = key.split("/");
      const device = devices?.devices.find(v => v.id === deviceId);
      const player = device?.players.find(v => v.id === playerId);
      const heading = device && player ? `${device.label} - ${player.label}` : key;
      const whys = [...new Set(d.ranking.map(r => r.why).filter((v): v is string => v != null))];
      return <section className="suite-rank" aria-label={heading} key={key}>
        <h4>{heading}</h4>

        <p>
          {d.quality_basis === "device"
            ? "Quality basis: adjusted for this player (format credit it cannot show was removed)."
            : "Quality basis: device-neutral."}</p>

        <table className="suite-table">
          <thead>
            <tr>
              {["#", "File", "Device score", "Quality", "Playback", "Verdict", "You will see"].map(h => <th key={h}>
                {h}
              </th>)}
            </tr>
          </thead>
          <tbody>
            {d.ranking.map(row => <tr key={row.id}>
              <td>{row.device_rank}</td>
              <td>{row.label}</td>
              <td>{row.device_score?.toFixed(1) ?? "-"}</td>
              <td>{row.quality_score?.toFixed(1) ?? "not compared"}</td>
              <td>{Math.round(row.playback_score)}</td>
              <td>
                <VerdictBadge verdict={row.verdict} evidence={row.evidence} />
              </td>
              <td>{row.effective_hdr ?? "-"}</td>
            </tr>)}
          </tbody>
        </table>
        {whys.map(why => <p key={why}>Why: {why}</p>)}

        {d.will_not_play.map(row => <p key={row.id}>
          Will not play: {row.label}: {
            (Array.isArray(row.reasons) ? row.reasons[0] : undefined) ?? "see the issues list"
          } <VerdictBadge verdict={row.verdict} evidence={row.evidence} />
        </p>)}

        {d.format_credit_removed?.map((v, i) => <p key={`${i}-${v.a}-${v.b}`}>{formatCreditText(v)}</p>)}

        {d.crossing_note && <p>Crossing point: {d.crossing_note}</p>}

        {d.not_compared.map(row => <p key={row.id}>
          Not compared (no picture data): {row.label} (playback {
            Math.round(row.playback_score)
          }) <VerdictBadge verdict={row.verdict} evidence={row.evidence} />
        </p>)}
      </section>;
    })}
  </div>;
}
