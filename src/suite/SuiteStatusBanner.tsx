import { SUITE_DISABLED_TEXT, suiteReady, versionBanner } from "./messages";
import type { VersionState } from "./messages";

export function SuiteStatusBanner({ state, checking, onRecheck }: { state: VersionState; checking: boolean; onRecheck: () => void }) {
  const { tone, text, detail } = versionBanner(state);
  return <div role="status" className={"suite-banner suite-banner-" + tone}>
    <p>{text}</p>{detail && <p className="suite-muted">{detail}</p>}
    {state.kind !== "loading" && !suiteReady(state) && <p>{SUITE_DISABLED_TEXT}</p>}
    <button type="button" disabled={checking} onClick={onRecheck}>Check again</button>
  </div>;
}
