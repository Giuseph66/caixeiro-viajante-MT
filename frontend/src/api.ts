export type JobType = "tsp" | "mtsp" | "overlap";
export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted";

export interface Municipality {
  ibge_code: number;
  name: string;
  longitude: number;
  latitude: number;
}

export interface Dataset {
  dataset_id: string;
  title: string;
  available: boolean;
  manifest_sha256: string;
}

export interface Experiment {
  id: string;
  job_type: JobType;
  status: JobStatus;
  parameters: Record<string, unknown>;
  progress_percent: number;
  progress_stage: string | null;
  result_available: boolean;
  result_summary: Record<string, unknown> | null;
  error_summary: string | null;
  log_tail: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ScenarioMetrics {
  scenario: string;
  status?: string;
  objective?: string;
  unit?: string;
  objective_value?: number | null;
  total_distance_m?: number | null;
  time_max_s?: number | null;
  best_by_policy?: Record<string, unknown>;
  metrics_by_policy?: Record<string, unknown>;
}

export interface ExperimentMetrics {
  run_id: string;
  job_type: JobType;
  algorithm_version: string | null;
  seed: number | null;
  data_manifest_sha256: string | null;
  scenario_count: number;
  scenarios: ScenarioMetrics[];
}

export interface ExperimentRequest {
  job_type: JobType;
  time_limit_s: number;
  workers: number;
  variants: number;
  seed: number;
  lambdas: number[];
}

const API_ROOT = import.meta.env.VITE_API_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `A API respondeu ${response.status}.`);
  }
  return response.json() as Promise<T>;
}

export function getMunicipalities() {
  return request<Municipality[]>("/api/municipalities");
}

export function getDatasets() {
  return request<Dataset[]>("/api/datasets/status");
}

export function getConfigurations() {
  return request<Record<string, unknown>>("/api/experiments/configurations");
}

export function listExperiments() {
  return request<Experiment[]>("/api/experiments?limit=20");
}

export function createExperiment(payload: ExperimentRequest) {
  return request<Experiment>("/api/experiments", { method: "POST", body: JSON.stringify(payload) });
}

export function cancelExperiment(experimentId: string) {
  return request<Experiment>(`/api/experiments/${experimentId}/cancel`, { method: "POST" });
}

export function getExperimentMetrics(experimentId: string) {
  return request<ExperimentMetrics>(`/api/experiments/${experimentId}/metrics`);
}

export function getGeojsonUrl(
  experimentId: string,
  scenario: string,
  policy: "P1" | "P2" | "P3",
  lambdaValue: number,
) {
  const query = new URLSearchParams({ scenario, policy });
  if (policy === "P2") query.set("lambda", String(lambdaValue));
  return `${API_ROOT}/api/experiments/${experimentId}/geojson?${query.toString()}`;
}

export function getExportUrl(experimentId: string) {
  return `${API_ROOT}/api/experiments/${experimentId}/export`;
}
