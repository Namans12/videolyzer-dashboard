import type { DevicesDocument, ScoreRecord } from "./types";
import { NO_RECORDS_TEXT } from "./messages";
import { groupPlayback, PARTIAL_TEXT, recordName } from "./verdict";
import { DeviceCard } from "./DeviceCard";

export function ScoreResults({ records, devices }: { records: ScoreRecord[]; devices: DevicesDocument | null }) {
  if (records.length === 0) return <p>{NO_RECORDS_TEXT}</p>;
  return <div>{records.map(rec => <section className="suite-record" key={rec.id}><h3>{recordName(rec)}</h3><p className="suite-muted">{rec.source.path}</p>
    {rec.source.partial && <><p className="suite-partial">{PARTIAL_TEXT}</p><ul>{(rec.source.caveats ?? []).map((text, index) => <li key={index}>{text}</li>)}</ul></>}
    {groupPlayback(rec, devices).map(group => <DeviceCard key={group.deviceId} group={group} />)}
  </section>)}</div>;
}
