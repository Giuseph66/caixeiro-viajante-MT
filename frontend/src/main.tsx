import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  cancelExperiment,
  createExperiment,
  getExportUrl,
  getConfigurations,
  getDatasets,
  getExperimentMetrics,
  getGeojsonUrl,
  getMunicipalities,
  listExperiments,
  type Dataset,
  type Experiment,
  type ExperimentMetrics,
  type JobType,
  type Municipality,
} from "./api";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

const AGENT_COLORS = ["#346b9b", "#4a8058", "#cf762e"];
const MapView = lazy(() => import("./map-view").then(({ MapView: mapView }) => ({ default: mapView })));

function App() {
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [comparisonId, setComparisonId] = useState("");
  const [metrics, setMetrics] = useState<ExperimentMetrics | null>(null);
  const [comparisonMetrics, setComparisonMetrics] = useState<ExperimentMetrics | null>(null);
  const [scenario, setScenario] = useState("");
  const [comparisonScenario, setComparisonScenario] = useState("");
  const [apiState, setApiState] = useState<"loading" | "ready" | "offline">("loading");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [jobType, setJobType] = useState<JobType>("mtsp");
  const [timeLimit, setTimeLimit] = useState(60);
  const [workers, setWorkers] = useState(8);
  const [variants, setVariants] = useState(8);
  const [policy, setPolicy] = useState<"P1" | "P2" | "P3">("P1");
  const [lambdaValue, setLambdaValue] = useState(1);
  const [visibleAgents, setVisibleAgents] = useState([1, 2, 3]);
  const [selectedMunicipality, setSelectedMunicipality] = useState<Municipality | null>(null);

  const selectedExperiment = experiments.find((experiment) => experiment.id === selectedId) ?? null;
  const comparisonExperiment = experiments.find((experiment) => experiment.id === comparisonId) ?? null;
  const hasActiveExperiment = experiments.some((experiment) =>
    experiment.status === "queued" || experiment.status === "running",
  );

  const refreshExperiments = useCallback(async () => {
    try {
      const items = await listExperiments();
      setExperiments(items);
      setApiState("ready");
      setError("");
      setSelectedId((current) => current ?? items[0]?.id ?? null);
    } catch (requestError) {
      setApiState("offline");
      setError(errorMessage(requestError));
    }
  }, []);

  useEffect(() => {
    let live = true;
    Promise.allSettled([getMunicipalities(), getDatasets(), getConfigurations()]).then(
      ([municipalityResult, datasetResult]) => {
        if (!live) return;
        if (municipalityResult.status === "fulfilled") setMunicipalities(municipalityResult.value);
        if (datasetResult.status === "fulfilled") setDatasets(datasetResult.value);
      },
    );
    void refreshExperiments();
    return () => { live = false; };
  }, [refreshExperiments]);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshExperiments(), hasActiveExperiment ? 2000 : 8000);
    return () => window.clearInterval(interval);
  }, [hasActiveExperiment, refreshExperiments]);

  useEffect(() => {
    if (!selectedExperiment || selectedExperiment.status !== "succeeded") {
      setMetrics(null);
      setScenario("");
      return;
    }
    let live = true;
    getExperimentMetrics(selectedExperiment.id)
      .then((result) => {
        if (!live) return;
        setMetrics(result);
        setScenario((current) => result.scenarios.some((item) => item.scenario === current)
          ? current
          : result.scenarios[0]?.scenario ?? "",
        );
      })
      .catch((requestError) => { if (live) setError(errorMessage(requestError)); });
    return () => { live = false; };
  }, [selectedExperiment?.id, selectedExperiment?.status]);

  useEffect(() => {
    if (!comparisonExperiment || comparisonExperiment.status !== "succeeded") {
      setComparisonMetrics(null);
      setComparisonScenario("");
      return;
    }
    let live = true;
    getExperimentMetrics(comparisonExperiment.id)
      .then((result) => {
        if (!live) return;
        setComparisonMetrics(result);
        setComparisonScenario((current) => result.scenarios.some((item) => item.scenario === current)
          ? current
          : result.scenarios.find((item) => item.scenario === scenario)?.scenario ?? result.scenarios[0]?.scenario ?? "",
        );
      })
      .catch((requestError) => { if (live) setError(errorMessage(requestError)); });
    return () => { live = false; };
  }, [comparisonExperiment?.id, comparisonExperiment?.status]);

  useEffect(() => {
    if (comparisonId && comparisonId === selectedId) setComparisonId("");
  }, [comparisonId, selectedId]);

  const routeUrl = useMemo(() => {
    if (!selectedExperiment || selectedExperiment.status !== "succeeded" || !scenario) return "";
    return getGeojsonUrl(selectedExperiment.id, scenario, policy, lambdaValue);
  }, [lambdaValue, policy, scenario, selectedExperiment?.id, selectedExperiment?.status]);
  const comparisonRouteUrl = useMemo(() => {
    if (!comparisonExperiment || comparisonExperiment.status !== "succeeded" || !comparisonScenario) return "";
    return getGeojsonUrl(comparisonExperiment.id, comparisonScenario, policy, lambdaValue);
  }, [comparisonExperiment?.id, comparisonExperiment?.status, comparisonScenario, lambdaValue, policy]);
  const selectedMetrics = metrics?.scenarios.find((item) => item.scenario === scenario) ?? null;
  const selectedComparisonMetrics = comparisonMetrics?.scenarios.find((item) => item.scenario === comparisonScenario) ?? null;
  const comparisons = experiments.filter((experiment) => experiment.id !== selectedId && experiment.status === "succeeded");
  const availableDatasets = datasets.filter((dataset) => dataset.available).length;
  const isOverlap = selectedExperiment?.job_type === "overlap";
  const noSelectedPolicyRoutes = isOverlap && isPolicyUnavailable(selectedMetrics, policy, lambdaValue);
  const topStatus = apiState === "ready"
    ? hasActiveExperiment ? "Processando" : "API conectada"
    : apiState === "loading" ? "Conectando" : "API indisponível";

  async function startExperiment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const experiment = await createExperiment({
        job_type: jobType,
        time_limit_s: timeLimit,
        workers,
        variants,
        seed: 20261008,
        lambdas: [0, 0.25, 0.5, 1, 2],
      });
      setExperiments((current) => [experiment, ...current.filter((item) => item.id !== experiment.id)]);
      setSelectedId(experiment.id);
      setComparisonId("");
      setMetrics(null);
      setScenario("");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  async function stopExperiment(experimentId: string) {
    try {
      const updated = await cancelExperiment(experimentId);
      setExperiments((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  }

  function toggleAgent(agentId: number) {
    setVisibleAgents((current) => current.includes(agentId)
      ? current.filter((item) => item !== agentId)
      : [...current, agentId].sort(),
    );
  }

  return (
    <main className="workspace">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="Rotas MT, início">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span><strong>rotas<span className="brand-dot">.</span>mt</strong><small>laboratório de roteamento</small></span>
        </a>
        <div className="topbar-context">
          <span className="context-kicker">ESTUDO DE CAMPO</span>
          <strong>Mato Grosso <span>·</span> rede rodoviária OSM</strong>
        </div>
        <div className={`connection-state connection-${apiState}`}><span className="connection-light" />{topStatus}</div>
      </header>

      <div className="page-heading">
        <div>
          <p className="eyebrow">Pesquisa operacional <span>/</span> MT</p>
          <h1>Rotas multiagente</h1>
          <p className="page-subtitle">Planeje cenários e acompanhe a cobertura das 142 sedes municipais.</p>
        </div>
        <div className="data-stamp">
          <span className="stamp-icon">142</span>
          <span><strong>{municipalities.length || "—"} sedes</strong><small>{availableDatasets || "—"} conjuntos disponíveis</small></span>
        </div>
      </div>

      <div className="dashboard-grid">
        <aside className="control-rail">
          <div className="section-heading"><span className="section-index">01</span><div><h2>Novo experimento</h2><p>Configure um lote de cenários</p></div></div>
          <form className="experiment-form" onSubmit={startExperiment}>
            <label className="field-label" htmlFor="job-type">Família de algoritmo</label>
            <select id="job-type" value={jobType} onChange={(event) => setJobType(event.target.value as JobType)}>
              <option value="tsp">Open TSP · um agente</option>
              <option value="mtsp">Open mTSP · dois ou três agentes</option>
              <option value="overlap">Sobreposição · P1, P2 e P3</option>
            </select>

            <div className="field-row field-row-top"><label className="field-label" htmlFor="time-limit">Limite por cenário</label><span className="field-value">{timeLimit}<small> s</small></span></div>
            <input id="time-limit" type="range" min="10" max="300" step="10" value={timeLimit} onChange={(event) => setTimeLimit(Number(event.target.value))} />
            <div className="range-ends"><span>10 s</span><span>300 s</span></div>

            {jobType === "tsp" ? <p className="field-note workers-row">D4 usa um processo de otimização para a rota de um agente.</p> : <>
              <div className="field-row field-row-top workers-row"><label className="field-label" htmlFor="workers">Processos paralelos</label><span className="field-value">{workers}<small> / 8</small></span></div>
              <input id="workers" type="range" min="1" max="8" step="1" value={workers} onChange={(event) => setWorkers(Number(event.target.value))} />
              <div className="range-ends"><span>1 processo</span><span>até 8 processos</span></div>
            </>}

            {jobType === "overlap" && <>
              <div className="field-row field-row-top workers-row"><label className="field-label" htmlFor="variants">Variantes de rota</label><span className="field-value">{variants}<small> / 8</small></span></div>
              <input id="variants" type="range" min="1" max="8" step="1" value={variants} onChange={(event) => setVariants(Number(event.target.value))} />
              <div className="range-ends"><span>1 variante</span><span>8 variantes</span></div>
              <p className="field-note">P2 avalia λ = 0, 0,25, 0,5, 1 e 2. P3 informa quando não encontra uma combinação viável no conjunto de candidatos.</p>
            </>}

            <div className="memory-note"><span className="memory-icon" aria-hidden="true">⌁</span><span><strong>Limite de memória: 25 GB</strong><small>Aplicado ao processo e aos runners</small></span></div>
            <button className="primary-button" type="submit" disabled={submitting || apiState !== "ready" || hasActiveExperiment}>
              <span className="button-symbol" aria-hidden="true">＋</span>{submitting ? "Enfileirando…" : hasActiveExperiment ? "Lote em execução" : "Iniciar lote"}<span className="button-arrow" aria-hidden="true">↗</span>
            </button>
            <p className="form-footnote">Os runners comparam os cenários previstos para D4, D5 ou D6. Selecione um cenário concluído para inspecioná-lo no mapa.</p>
          </form>
        </aside>

        <section className="map-panel" aria-label="Mapa dos municípios e rotas">
          <div className="map-toolbar">
            <div className="map-title"><span className="map-crosshair" aria-hidden="true">⌖</span><div><h2>{scenario || "Mapa de Mato Grosso"}</h2><p>{selectedExperiment ? statusLabel(selectedExperiment.status) : "142 sedes municipais · visualização rodoviária"}</p></div></div>
            <div className="map-tools">
              {isOverlap && selectedExperiment?.status === "succeeded" && <div className="policy-picker" aria-label="Política de sobreposição">
                <select value={policy} onChange={(event) => setPolicy(event.target.value as "P1" | "P2" | "P3")} aria-label="Política"><option value="P1">P1 livre</option><option value="P2">P2 penalizada</option><option value="P3">P3 proibida</option></select>
                {policy === "P2" && <select value={lambdaValue} onChange={(event) => setLambdaValue(Number(event.target.value))} aria-label="Penalização lambda"><option value="0">λ 0</option><option value="0.25">λ 0,25</option><option value="0.5">λ 0,5</option><option value="1">λ 1</option><option value="2">λ 2</option></select>}
              </div>}
              {metrics && metrics.scenarios.length > 0 && <select className="scenario-picker" value={scenario} onChange={(event) => setScenario(event.target.value)} aria-label="Cenário exibido">
                {metrics.scenarios.map((item) => <option key={item.scenario} value={item.scenario}>{item.scenario}</option>)}
              </select>}
            </div>
          </div>
          <div className="map-canvas-wrap">
            <Suspense fallback={<div className="map-loading">Carregando motor cartográfico…</div>}>
              <MapView municipalities={municipalities} routeUrl={routeUrl} comparisonUrl={comparisonRouteUrl} visibleAgents={visibleAgents} onMunicipalitySelect={setSelectedMunicipality} />
            </Suspense>
            {(!routeUrl || noSelectedPolicyRoutes) && <div className="map-empty-note"><span className="empty-route-mark" aria-hidden="true">⌁</span><span>{noSelectedPolicyRoutes ? "Nenhuma rota viável encontrada neste conjunto de candidatos; isso não prova inviabilidade global." : selectedExperiment ? "Selecione um experimento concluído para exibir as rotas." : "As sedes estão prontas. Inicie um lote para desenhar as trajetórias sobre a malha rodoviária."}</span></div>}
            {selectedMunicipality && <div className="municipality-chip"><span className="chip-dot" />{selectedMunicipality.name}<small>IBGE {selectedMunicipality.ibge_code}</small><button type="button" onClick={() => setSelectedMunicipality(null)} aria-label="Fechar município">×</button></div>}
          </div>
          <div className="map-footer">
            <div className="agent-legend" aria-label="Agentes visíveis">{[1, 2, 3].map((agentId) => <button key={agentId} type="button" className={visibleAgents.includes(agentId) ? "legend-agent is-visible" : "legend-agent"} onClick={() => toggleAgent(agentId)} aria-pressed={visibleAgents.includes(agentId)}><span style={{ backgroundColor: AGENT_COLORS[agentId - 1] }} />Agente {agentId}</button>)}</div>
            <span className="map-scale">Linha tracejada: comparação · segmentos do grafo OSM</span>
          </div>
        </section>

        <aside className="activity-rail">
          <div className="section-heading"><span className="section-index">02</span><div><h2>Execuções</h2><p>Fila local · atualização automática</p></div></div>
          {error && <div className="inline-error" role="alert">{error}</div>}
          {selectedExperiment && <section className="selected-run" aria-label="Progresso da execução selecionada">
            <div className="run-title-line"><span className={`status-marker status-${selectedExperiment.status}`} /><strong>{jobTitle(selectedExperiment.job_type)}</strong><span className="run-percent">{selectedExperiment.progress_percent}%</span></div>
            <div className="progress-track" role="progressbar" aria-label="Progresso da execução" aria-valuenow={selectedExperiment.progress_percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${selectedExperiment.progress_percent}%` }} /></div>
            <div className="progress-caption"><span>{selectedExperiment.progress_stage || statusLabel(selectedExperiment.status)}</span><span>{statusLabel(selectedExperiment.status)}</span></div>
            {(selectedExperiment.status === "queued" || selectedExperiment.status === "running") && <button className="cancel-button" type="button" onClick={() => void stopExperiment(selectedExperiment.id)}>Interromper execução</button>}
            {selectedExperiment.error_summary && <p className="run-error">{selectedExperiment.error_summary}</p>}
            {selectedExperiment.log_tail && <details className="run-log"><summary>Últimas mensagens do runner</summary><pre>{selectedExperiment.log_tail}</pre></details>}
          </section>}
          {selectedExperiment?.status === "succeeded" && <div className="compare-block">
            <label className="field-label" htmlFor="compare-run">Comparar com</label>
            <select id="compare-run" value={comparisonId} onChange={(event) => setComparisonId(event.target.value)}>
              <option value="">Sem comparação</option>
              {comparisons.map((experiment) => <option key={experiment.id} value={experiment.id}>{jobTitle(experiment.job_type)} · {formatDate(experiment.created_at)}</option>)}
            </select>
            {comparisonMetrics && <select className="compare-scenario-picker" value={comparisonScenario} onChange={(event) => setComparisonScenario(event.target.value)} aria-label="Cenário da execução comparada">
              {comparisonMetrics.scenarios.map((item) => <option key={item.scenario} value={item.scenario}>{item.scenario}</option>)}
            </select>}
            <a className="export-link" href={getExportUrl(selectedExperiment.id)}>Exportar resultado JSON <span aria-hidden="true">↗</span></a>
          </div>}
          <div className="activity-list-heading"><span>HISTÓRICO RECENTE</span><span>{experiments.length.toString().padStart(2, "0")}</span></div>
          <div className="experiment-list">
            {experiments.length === 0 ? <div className="empty-list"><span className="empty-list-icon">↗</span><strong>Nenhuma execução ainda</strong><p>O primeiro lote aparecerá aqui com status, progresso e horário.</p></div> : experiments.map((experiment) => <button key={experiment.id} type="button" className={experiment.id === selectedId ? "experiment-row is-selected" : "experiment-row"} onClick={() => setSelectedId(experiment.id)}>
              <span className={`status-marker status-${experiment.status}`} /><span className="experiment-row-main"><strong>{jobTitle(experiment.job_type)}</strong><small>{formatDate(experiment.created_at)}</small></span><span className="experiment-row-state">{experiment.status === "queued" || experiment.status === "running" ? `${experiment.progress_percent}%` : statusLabel(experiment.status)}</span>
            </button>)}
          </div>
          {selectedMetrics && <div className="comparison-table">
            <div className="comparison-table-head"><span>MEDIDA</span><span>SELECIONADO</span>{selectedComparisonMetrics && <span>COMPARAÇÃO</span>}</div>
            <div className="comparison-table-row"><span>Objetivo</span><strong>{formatMetric(scenarioObjectiveValue(selectedMetrics, policy, lambdaValue), metricUnit(selectedMetrics))}</strong>{selectedComparisonMetrics && <strong>{formatMetric(scenarioObjectiveValue(selectedComparisonMetrics, policy, lambdaValue), metricUnit(selectedComparisonMetrics))}</strong>}</div>
            <div className="comparison-table-row"><span>Tempo máx.</span><strong>{formatDuration(scenarioTimeMax(selectedMetrics, policy, lambdaValue))}</strong>{selectedComparisonMetrics && <strong>{formatDuration(scenarioTimeMax(selectedComparisonMetrics, policy, lambdaValue))}</strong>}</div>
            <div className="comparison-table-row"><span>Sobreposição</span><strong>{formatMetric(scenarioOverlapValue(selectedMetrics, policy, lambdaValue), "m")}</strong>{selectedComparisonMetrics && <strong>{formatMetric(scenarioOverlapValue(selectedComparisonMetrics, policy, lambdaValue), "m")}</strong>}</div>
          </div>}
        </aside>
      </div>
      <footer className="page-footer"><span>Dados de sedes: IBGE <span className="footer-divider">/</span> malha: OpenStreetMap</span><span>Rotas abertas · origem comum · 1–3 agentes</span></footer>
    </main>
  );
}

function jobTitle(jobType: JobType) {
  return jobType === "tsp" ? "Open TSP · D4" : jobType === "mtsp" ? "Open mTSP · D5" : "Sobreposição · D6";
}

function statusLabel(status: Experiment["status"]) {
  const labels: Record<Experiment["status"], string> = { queued: "Na fila", running: "Em execução", succeeded: "Concluído", failed: "Falhou", interrupted: "Interrompido" };
  return labels[status];
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function formatDuration(value?: number | null) {
  if (value === undefined || value === null) return "—";
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return minutes ? `${minutes} min ${seconds} s` : `${seconds} s`;
}

function formatMetric(value?: number | null, unit?: string) {
  if (value === undefined || value === null) return "—";
  if (unit === "m") return `${(value / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 0 })} km`;
  if (unit === "s") return formatDuration(value);
  return value.toLocaleString("pt-BR", { maximumFractionDigits: 1 });
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Falha ao comunicar com a API.";
}

function policySummary(metrics: ExperimentMetrics["scenarios"][number] | null, policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics?.metrics_by_policy?.[policy] ?? metrics?.best_by_policy?.[policy];
  if (Array.isArray(value)) {
    const match = value.find((item) => {
      if (!item || typeof item !== "object") return false;
      return Number((item as Record<string, unknown>).lambda) === lambdaValue;
    });
    return match && typeof match === "object" ? match as Record<string, unknown> : null;
  }
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function scenarioObjectiveValue(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics.objective_value ?? policySummary(metrics, policy, lambdaValue)?.objective_value;
  return typeof value === "number" ? value : null;
}

function scenarioOverlapValue(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = policySummary(metrics, policy, lambdaValue)?.overlap_distance_m;
  return typeof value === "number" ? value : null;
}

function scenarioTimeMax(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics.time_max_s ?? policySummary(metrics, policy, lambdaValue)?.time_max_s;
  return typeof value === "number" ? value : null;
}

function metricUnit(metrics: ExperimentMetrics["scenarios"][number]) {
  return metrics.unit ?? (metrics.objective === "distance" ? "m" : metrics.objective === "time" ? "s" : undefined);
}

function isPolicyUnavailable(metrics: ExperimentMetrics["scenarios"][number] | null, policy: "P1" | "P2" | "P3", lambdaValue: number) {
  if (!metrics?.best_by_policy) return false;
  const summary = policySummary(metrics, policy, lambdaValue);
  const candidates = summary?.candidate_ids;
  return summary?.status === "not_found" || !Array.isArray(candidates) || candidates.length === 0;
}

createRoot(document.getElementById("root")!).render(<App />);
