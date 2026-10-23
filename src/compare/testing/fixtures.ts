// The only door to the real unified-run HTTP fixtures in tests. Every accessor returns a deep clone of bytes that the
// actual Dashboard routes/workers serialized (tests/test_comparison_routes.py and tests/test_comparison_magnets.py with
// PLAN4B_REGEN_FIXTURES=1); nothing here is a hand-made near-match.
import publicSnapshotsJson from "../../../tests/fixtures/comparison/public_snapshots_http.json";
import localQuickBothJson from "../../../tests/fixtures/comparison/local_quick_both.json";
import localFullBothJson from "../../../tests/fixtures/comparison/local_full_both.json";
import localAlternativesJson from "../../../tests/fixtures/comparison/local_just_player_alternatives.json";
import failedPairsJson from "../../../tests/fixtures/comparison/failed_pairs.json";
import changedSettingsJson from "../../../tests/fixtures/comparison/changed_settings.json";
import magnetQuickOverallJson from "../../../tests/fixtures/comparison/magnet_quick_overall.json";
import magnetQuickDeviceJson from "../../../tests/fixtures/comparison/magnet_quick_device.json";
import magnetSelectionJson from "../../../tests/fixtures/comparison/magnet_selection_required.json";
import magnetPartialFailureJson from "../../../tests/fixtures/comparison/magnet_partial_failure.json";

export type Json = Record<string, unknown>;

function clone(value: unknown): Json {
  return JSON.parse(JSON.stringify(value)) as Json;
}

/** `{list, detail, plan_job, run_job}`: GET /comparison/runs, GET /comparison/runs/{id} and both GET /job/{id}. */
export const publicSnapshotsHttp = (): Json => clone(publicSnapshotsJson);
/** GET /comparison/runs/{id} detail envelopes `{run, result}`. */
export const localQuickBoth = (): Json => clone(localQuickBothJson);
export const localFullBoth = (): Json => clone(localFullBothJson);
export const localJustPlayerAlternatives = (): Json => clone(localAlternativesJson);
export const magnetQuickOverall = (): Json => clone(magnetQuickOverallJson);
export const magnetQuickDevice = (): Json => clone(magnetQuickDeviceJson);
export const magnetSelectionRequired = (): Json => clone(magnetSelectionJson);
export const magnetPartialFailure = (): Json => clone(magnetPartialFailureJson);
/** `{job, run}`: the finished GET /job/{id} answer and the GET /comparison/runs/{id} detail. */
export const failedPairs = (): Json => clone(failedPairsJson);
export const changedSettings = (): Json => clone(changedSettingsJson);

/** Every detail envelope (`{run, result}`) the real backend emitted, by fixture name. */
export function allDetails(): Array<[string, Json]> {
  return [
    ["public_snapshots_http.detail", publicSnapshotsHttp().detail as Json],
    ["local_quick_both", localQuickBoth()],
    ["local_full_both", localFullBoth()],
    ["local_just_player_alternatives", localJustPlayerAlternatives()],
    ["failed_pairs", failedPairs().run as Json],
    ["changed_settings", changedSettings().run as Json],
    ["magnet_quick_overall", magnetQuickOverall()],
    ["magnet_quick_device", magnetQuickDevice()],
    ["magnet_selection_required", magnetSelectionRequired()],
    ["magnet_partial_failure", magnetPartialFailure()],
  ];
}
