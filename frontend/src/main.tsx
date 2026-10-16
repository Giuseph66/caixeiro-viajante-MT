import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  cancelExperiment,
  createExperiment,
  getExportUrl,
  getConfigurations,
  getDatasets,
  getExperimentMetrics,
  getGeojsonUrl,
  getMemoryBudget,
  getPreviewUrl,
  getMunicipalities,
  listExperiments,
  type Dataset,
  type Experiment,
  type ExperimentMetrics,
  type JobType,
  type MemoryBudget,
  type Municipality,
  type RepetitionPolicy,
} from "./api";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

const AGENT_COLORS = ["#346b9b", "#4a8058", "#cf762e"];
const REPETITION_OPTIONS: { value: RepetitionPolicy; label: string; description: string }[] = [
  { value: "allow", label: "Permitir", description: "Aceita passar novamente pelo mesmo trecho, sem penalidade extra." },
  { value: "reduce", label: "Reduzir", description: "Acrescenta custo aos quilômetros repetidos para preferir rotas com menos repetição." },
  { value: "forbid", label: "Proibir", description: "Só entrega rotas em que cada agente usa cada trecho físico uma única vez." },
];
const MapView = lazy(() => import("./map-view").then(({ MapView: mapView }) => ({ default: mapView })));

function App() {
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [comparisonId, setComparisonId] = useState("");
  const [metrics, setMetrics] = useState<ExperimentMetrics | null>(null);
  const [comparisonMetrics, setComparisonMetrics] = useState<ExperimentMetrics | null>(null);
  const [metricsExperimentId, setMetricsExperimentId] = useState<string | null>(null);
  const [comparisonMetricsExperimentId, setComparisonMetricsExperimentId] = useState<string | null>(null);
  const [metricsRetryToken, setMetricsRetryToken] = useState(0);
  const [scenario, setScenario] = useState("");
  const [comparisonScenario, setComparisonScenario] = useState("");
  const [apiState, setApiState] = useState<"loading" | "ready" | "offline">("loading");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [agentCount, setAgentCount] = useState(2);
  const [objective, setObjective] = useState<"distance" | "time">("distance");
  const [originCode, setOriginCode] = useState<number | null>(null);
  const [destinationCode, setDestinationCode] = useState<number | null>(null);
  const [selectionTarget, setSelectionTarget] = useState<"origin" | "destination" | null>(null);
  const [overlapPolicy, setOverlapPolicy] = useState<"P1" | "P2" | "P3">("P1");
  const [repetitionPolicy, setRepetitionPolicy] = useState<RepetitionPolicy>("reduce");
  const [memoryBudget, setMemoryBudget] = useState<MemoryBudget | null>(null);
  const [approximateGeometry, setApproximateGeometry] = useState(false);
  const [routeGeometryLoaded, setRouteGeometryLoaded] = useState(false);
  const [previewTick, setPreviewTick] = useState(0);
  const [timeLimit, setTimeLimit] = useState(60);
  const [workers, setWorkers] = useState(8);
  const [variants, setVariants] = useState(8);
  const [policy, setPolicy] = useState<"P1" | "P2" | "P3">("P1");
  const [lambdaValue, setLambdaValue] = useState(1);
  const [visibleAgents, setVisibleAgents] = useState([1, 2, 3]);
  const [selectedMunicipality, setSelectedMunicipality] = useState<Municipality | null>(null);
  const [draftChanged, setDraftChanged] = useState(false);
  const draftTouched = useRef(false);
  const apiWasOffline = useRef(false);
  const initializedSelection = useRef(false);
  const jobType: JobType = agentCount === 1 ? "tsp" : overlapPolicy === "P1" ? "mtsp" : "overlap";

  const selectedExperiment = experiments.find((experiment) => experiment.id === selectedId) ?? null;
  const comparisonExperiment = experiments.find((experiment) => experiment.id === comparisonId) ?? null;
  const hasActiveExperiment = experiments.some((experiment) =>
    experiment.status === "queued" || experiment.status === "running",
  );
  const isSelectedRunActive = selectedExperiment?.status === "queued" || selectedExperiment?.status === "running";

  function markDraftChanged() {
    draftTouched.current = true;
    setDraftChanged(true);
  }

  function syncConfigurationFromExperiment(experiment: Experiment) {
    const parameters = experiment.parameters;
    const count = Number(parameters.vehicle_count ?? (experiment.job_type === "tsp" ? 1 : 2));
    const safeCount = Math.max(1, Math.min(3, count));
    setAgentCount(safeCount);
    setObjective(parameters.objective === "time" ? "time" : "distance");
    setOriginCode(typeof parameters.origin_ibge_code === "number" ? parameters.origin_ibge_code : null);
    setDestinationCode(safeCount === 1 && typeof parameters.destination_ibge_code === "number" ? parameters.destination_ibge_code : null);
    setOverlapPolicy(parameters.overlap_policy === "P2" || parameters.overlap_policy === "P3" ? parameters.overlap_policy : "P1");
    setRepetitionPolicy(parameters.repetition_policy === "reduce" || parameters.repetition_policy === "forbid" ? parameters.repetition_policy : "allow");
    setTimeLimit(Number(parameters.time_limit_s ?? 60));
    setWorkers(Number(parameters.workers ?? 8));
    setVariants(Number(parameters.variants ?? 8));
    setPolicy(parameters.overlap_policy === "P2" || parameters.overlap_policy === "P3" ? parameters.overlap_policy : "P1");
    const lambdas = Array.isArray(parameters.lambdas)
      ? parameters.lambdas.map(Number).filter(Number.isFinite)
      : [];
    setLambdaValue(lambdas.includes(1) ? 1 : lambdas[0] ?? 1);
    setSelectionTarget(null);
    setDraftChanged(false);
    draftTouched.current = false;
  }

  function selectExperiment(experiment: Experiment) {
    setSelectedId(experiment.id);
    syncConfigurationFromExperiment(experiment);
  }

  function handleAgentCountChange(count: number) {
    if (agentCount === count) return;
    markDraftChanged();
    setAgentCount(count);
    if (count > 1) {
      setDestinationCode(null);
      if (selectionTarget === "destination") setSelectionTarget(null);
    }
  }

  const refreshExperiments = useCallback(async () => {
    try {
      const items = await listExperiments();
      setExperiments(items);
      setApiState("ready");
      if (apiWasOffline.current) {
        apiWasOffline.current = false;
        setError("");
      }
      if (!initializedSelection.current && items.length > 0) {
        initializedSelection.current = true;
        if (!draftTouched.current) syncConfigurationFromExperiment(items[0]);
      }
      setSelectedId((current) => current ?? items[0]?.id ?? null);
    } catch (requestError) {
      apiWasOffline.current = true;
      setApiState("offline");
      setError(errorMessage(requestError));
    }
  }, []);

  useEffect(() => {
    let live = true;
    Promise.allSettled([getMunicipalities(), getDatasets(), getConfigurations(), getMemoryBudget()]).then(
      ([municipalityResult, datasetResult, _configurationResult, memoryResult]) => {
        if (!live) return;
        if (municipalityResult.status === "fulfilled") setMunicipalities(municipalityResult.value);
        if (datasetResult.status === "fulfilled") setDatasets(datasetResult.value);
        if (memoryResult.status === "fulfilled") setMemoryBudget(memoryResult.value);
      },
    );
    void refreshExperiments();
    return () => { live = false; };
  }, [refreshExperiments]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      getMemoryBudget().then(setMemoryBudget).catch(() => undefined);
    }, hasActiveExperiment ? 5000 : 15000);
    return () => window.clearInterval(interval);
  }, [hasActiveExperiment]);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshExperiments(), hasActiveExperiment ? 2000 : 8000);
    return () => window.clearInterval(interval);
  }, [hasActiveExperiment, refreshExperiments]);

  useEffect(() => {
    if (!isSelectedRunActive) return;
    const interval = window.setInterval(() => setPreviewTick((tick) => tick + 1), 1200);
    return () => window.clearInterval(interval);
  }, [isSelectedRunActive, selectedExperiment?.id]);

  useEffect(() => {
    if (!selectedExperiment || selectedExperiment.status !== "succeeded") {
      setMetrics(null);
      setMetricsExperimentId(null);
      setScenario("");
      return;
    }
    let live = true;
    getExperimentMetrics(selectedExperiment.id)
      .then((result) => {
        if (!live) return;
        setMetrics(result);
        setMetricsExperimentId(selectedExperiment.id);
        setError("");
        setScenario((current) => result.scenarios.some((item) => item.scenario === current)
          ? current
          : result.scenarios[0]?.scenario ?? "",
        );
      })
      .catch((requestError) => { if (live) setError(errorMessage(requestError)); });
    return () => { live = false; };
  }, [metricsRetryToken, selectedExperiment?.id, selectedExperiment?.status]);

  useEffect(() => {
    if (!comparisonExperiment || comparisonExperiment.status !== "succeeded") {
      setComparisonMetrics(null);
      setComparisonMetricsExperimentId(null);
      setComparisonScenario("");
      return;
    }
    let live = true;
    getExperimentMetrics(comparisonExperiment.id)
      .then((result) => {
        if (!live) return;
        setComparisonMetrics(result);
        setComparisonMetricsExperimentId(comparisonExperiment.id);
        setComparisonScenario((current) => result.scenarios.some((item) => item.scenario === current)
          ? current
          : result.scenarios[0]?.scenario ?? "",
        );
      })
      .catch((requestError) => { if (live) setError(errorMessage(requestError)); });
    return () => { live = false; };
  }, [comparisonExperiment?.id, comparisonExperiment?.status]);

  useEffect(() => {
    if (comparisonId && comparisonId === selectedId) setComparisonId("");
  }, [comparisonId, selectedId]);

  const selectedRunMetrics = metricsExperimentId === selectedExperiment?.id ? metrics : null;
  const selectedMetrics = selectedRunMetrics?.scenarios.find((item) => item.scenario === scenario) ?? null;
  const comparisonRunMetrics = comparisonMetricsExperimentId === comparisonExperiment?.id ? comparisonMetrics : null;
  const selectedComparisonMetrics = comparisonRunMetrics?.scenarios.find((item) => item.scenario === comparisonScenario) ?? null;
  const selectedPolicySummary = policySummary(selectedMetrics, policy, lambdaValue);
  const noSelectedPolicyRoutes = isPolicyUnavailable(selectedMetrics, policy, lambdaValue);
  const noSelectedPolicyMessage = noSelectedPolicyRoutes && selectedExperiment?.parameters.repetition_policy === "forbid"
    ? "Nenhuma rota que cumpra as regras escolhidas foi encontrada nesta busca. Acessos únicos podem exigir repetir trechos; tente Reduzir em vez de Proibir."
    : selectedMetrics?.status === "not_found" || selectedMetrics?.status === "infeasible_proven"
    ? "Nenhuma rota viável foi encontrada nesta busca."
    : !selectedPolicySummary
    ? policy === "P2" ? "Esta penalização não foi calculada nesta execução." : "Esta política não foi calculada nesta execução."
    : policy === "P3"
      ? "Nenhuma combinação sem trechos compartilhados foi encontrada entre as rotas candidatas desta busca."
      : policy === "P2"
        ? "Nenhuma combinação viável foi encontrada para esta penalização."
        : "Nenhuma combinação viável foi encontrada para esta política.";
  const routeUrl = useMemo(() => {
    if (draftChanged && !isSelectedRunActive) return "";
    if (!selectedExperiment || selectedExperiment.status !== "succeeded" || metricsExperimentId !== selectedExperiment.id || !scenario || noSelectedPolicyRoutes) return "";
    return getGeojsonUrl(selectedExperiment.id, scenario, policy, lambdaValue);
  }, [draftChanged, isSelectedRunActive, lambdaValue, metricsExperimentId, noSelectedPolicyRoutes, policy, scenario, selectedExperiment?.id, selectedExperiment?.status]);
  const previewUrl = useMemo(() => {
    if (!selectedExperiment) return "";
    const showCandidate = isSelectedRunActive || (
      selectedExperiment.status === "succeeded" && !noSelectedPolicyRoutes && !draftChanged && Boolean(scenario) && !routeGeometryLoaded
    );
    if (!showCandidate) return "";
    return getPreviewUrl(selectedExperiment.id, previewTick);
  }, [draftChanged, isSelectedRunActive, noSelectedPolicyRoutes, previewTick, routeGeometryLoaded, scenario, selectedExperiment?.id, selectedExperiment?.status]);
  const comparisonRouteUrl = useMemo(() => {
    if (draftChanged && !isSelectedRunActive) return "";
    if (!comparisonExperiment || comparisonExperiment.status !== "succeeded" || comparisonMetricsExperimentId !== comparisonExperiment.id || !comparisonScenario) return "";
    if (isPolicyUnavailable(selectedComparisonMetrics, policy, lambdaValue)) return "";
    return getGeojsonUrl(comparisonExperiment.id, comparisonScenario, policy, lambdaValue);
  }, [comparisonExperiment?.id, comparisonExperiment?.job_type, comparisonExperiment?.status, comparisonMetricsExperimentId, comparisonScenario, draftChanged, isSelectedRunActive, lambdaValue, policy, selectedComparisonMetrics]);
  const comparisons = experiments.filter((experiment) => experiment.id !== selectedId && experiment.status === "succeeded");
  const availableDatasets = datasets.filter((dataset) => dataset.available).length;
  const isOverlap = selectedExperiment?.job_type === "overlap";
  const displayedAgentCount = draftChanged && !isSelectedRunActive
    ? agentCount
    : Number(selectedExperiment?.parameters.vehicle_count ?? (selectedExperiment?.job_type === "tsp" ? 1 : 2));
  const legendAgents = Array.from({ length: Math.max(1, Math.min(3, displayedAgentCount)) }, (_, index) => index + 1);
  const showDraftMap = draftChanged && !isSelectedRunActive;
  const draftObjectiveLabel = objective === "distance" ? "menor distância" : "menor tempo";
  const draftOriginLabel = originCode === null ? "início otimizado" : municipalityName(originCode, municipalities);
  const draftDestinationLabel = destinationCode === null ? "" : ` · final ${municipalityName(destinationCode, municipalities)}`;
  const topStatus = apiState === "ready"
    ? hasActiveExperiment ? "Processando" : "API conectada"
    : apiState === "loading" ? "Conectando" : "API indisponível";

  async function startExperiment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    setActionError("");
    try {
      const latestBudget = await getMemoryBudget();
      setMemoryBudget(latestBudget);
      if (!latestBudget.can_start) {
        setActionError(
          `Há ${formatGigabytes(latestBudget.available_memory_bytes)} de RAM disponível; ` +
          "o cálculo exige pelo menos 1 GB dentro do limite seguro.",
        );
        return;
      }
      const chosenPolicy = agentCount === 1 ? "P1" : overlapPolicy;
      const chosenJobType: JobType = agentCount === 1
        ? "tsp"
        : chosenPolicy === "P1" ? "mtsp" : "overlap";
      const experiment = await createExperiment({
        job_type: chosenJobType,
        objective,
        vehicle_count: agentCount,
        origin_ibge_code: originCode,
        destination_ibge_code: agentCount === 1 ? destinationCode : null,
        overlap_policy: chosenPolicy,
        repetition_policy: repetitionPolicy,
        time_limit_s: timeLimit,
        workers: chosenJobType === "overlap" ? workers : 1,
        variants,
        seed: 20261008,
        lambdas: [0, 0.25, 0.5, 1, 2],
      });
      setExperiments((current) => [experiment, ...current.filter((item) => item.id !== experiment.id)]);
      setSelectedId(experiment.id);
      setPolicy(chosenPolicy);
      setComparisonId("");
      setMetrics(null);
      setScenario("");
      setDraftChanged(false);
      draftTouched.current = false;
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  function handleMapMunicipalitySelect(municipality: Municipality | null) {
    setSelectedMunicipality(municipality);
    if (!municipality || !selectionTarget) return;
    if (selectionTarget === "destination" && agentCount !== 1) {
      setSelectionTarget(null);
      return;
    }
    if (selectionTarget === "destination" && municipality.ibge_code === originCode) {
      setActionError("A primeira e a última cidade precisam ser diferentes.");
      setSelectionTarget(null);
      return;
    }
    setActionError("");
    if (selectionTarget === "origin" && municipality.ibge_code !== originCode) {
      markDraftChanged();
      setOriginCode(municipality.ibge_code);
      if (municipality.ibge_code === destinationCode) setDestinationCode(null);
    }
    if (selectionTarget === "destination" && municipality.ibge_code !== destinationCode) {
      markDraftChanged();
      setDestinationCode(municipality.ibge_code);
    }
    setSelectionTarget(null);
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
          <div className="section-heading"><span className="section-index">01</span><div><h2>Monte sua rota</h2><p>Defina equipes, cidades e objetivo</p></div></div>
          <form className="experiment-form" onSubmit={startExperiment}>
            <fieldset className="setup-group">
              <legend>Quantas equipes vão visitar?</legend>
              <div className="agent-choice-list">
                {[1, 2, 3].map((count) => <button key={count} type="button" className={agentCount === count ? "agent-choice is-active" : "agent-choice"} aria-pressed={agentCount === count} onClick={() => handleAgentCountChange(count)}>
                  <strong>{count}</strong><span>{count === 1 ? "equipe" : "equipes"}</span>
                </button>)}
              </div>
              <p className="field-note">{agentCount === 1 ? "Uma equipe percorre todas as cidades em uma rota aberta." : "As equipes saem da mesma cidade e dividem a visita às cidades."}</p>
            </fieldset>

            <label className="field-label field-row-top" htmlFor="objective">Priorizar</label>
            <select id="objective" value={objective} onChange={(event) => { const value = event.target.value as "distance" | "time"; if (value !== objective) markDraftChanged(); setObjective(value); }}>
              <option value="distance">Menor distância total</option>
              <option value="time">Menor tempo de conclusão</option>
            </select>

            <div className="stop-field field-row-top">
              <div className="stop-heading"><label className="field-label" htmlFor="origin-city">Primeira cidade</label><button type="button" className={selectionTarget === "origin" ? "map-pick-button is-active" : "map-pick-button"} onClick={() => setSelectionTarget(selectionTarget === "origin" ? null : "origin")}>{selectionTarget === "origin" ? "Clique no mapa…" : "Escolher no mapa"}</button></div>
              <select id="origin-city" value={originCode ?? ""} onChange={(event) => { const code = event.target.value ? Number(event.target.value) : null; if (code !== originCode) markDraftChanged(); setOriginCode(code); if (code === destinationCode) setDestinationCode(null); }}>
                <option value="">Otimizar: encontrar a melhor cidade inicial</option>
                {municipalities.map((municipality) => <option key={municipality.ibge_code} value={municipality.ibge_code}>{municipality.name}</option>)}
              </select>
              {originCode !== null && <button type="button" className="text-button" onClick={() => { markDraftChanged(); setOriginCode(null); }}>Usar início automático</button>}
            </div>

            {agentCount === 1 ? <div className="stop-field field-row-top">
              <div className="stop-heading"><label className="field-label" htmlFor="destination-city">Última cidade</label><button type="button" className={selectionTarget === "destination" ? "map-pick-button is-active" : "map-pick-button"} onClick={() => setSelectionTarget(selectionTarget === "destination" ? null : "destination")}>{selectionTarget === "destination" ? "Clique no mapa…" : "Escolher no mapa"}</button></div>
              <select id="destination-city" value={destinationCode ?? ""} onChange={(event) => { const code = event.target.value ? Number(event.target.value) : null; if (code !== destinationCode) markDraftChanged(); setDestinationCode(code); }}>
                <option value="">Otimizar: escolher a melhor cidade final</option>
                {municipalities.map((municipality) => <option key={municipality.ibge_code} value={municipality.ibge_code} disabled={municipality.ibge_code === originCode}>{municipality.name}</option>)}
              </select>
              {destinationCode !== null && <button type="button" className="text-button" onClick={() => { markDraftChanged(); setDestinationCode(null); }}>Usar final automático</button>}
            </div> : <p className="field-note endpoint-note">Cada equipe termina na própria última cidade. Um destino único só se aplica à rota de uma equipe.</p>}

            <fieldset className="setup-group policy-group">
              <legend>Repetir trechos na própria rota</legend>
              <p className="field-note policy-intro">Vale para cada agente, inclusive quando há só 1. Voltar pelo mesmo trecho no sentido contrário também conta como repetição.</p>
              <div className="policy-options">
                {REPETITION_OPTIONS.map((option) => <button key={option.value} type="button" className={repetitionPolicy === option.value ? "policy-option is-active" : "policy-option"} aria-pressed={repetitionPolicy === option.value} onClick={() => { if (repetitionPolicy !== option.value) markDraftChanged(); setRepetitionPolicy(option.value); }}><strong>{option.label}</strong><span>{option.description}</span></button>)}
              </div>
              {repetitionPolicy === "reduce" && <p className="field-note">Custo extra: 1 metro por metro repetido. Ao priorizar tempo, a penalidade é convertida a 60 km/h.</p>}
              {repetitionPolicy === "forbid" && <p className="field-note">Cidades com acesso único podem exigir ida e volta. Se a busca não encontrar rota sem repetição, nenhum trajeto que viole a regra será entregue. A busca usa os caminhos rodoviários já calculados entre cidades.</p>}
            </fieldset>

            <fieldset className={`setup-group policy-group${agentCount === 1 ? " is-disabled" : ""}`}>
              <legend>Compartilhar trechos entre equipes</legend>
              <p className="field-note policy-intro">Defina o que a busca faz quando equipes passam pela mesma estrada.</p>
              {agentCount === 1 && <p className="field-note policy-single-agent" role="note">Com 1 agente, não há outra equipe para compartilhar estradas. As opções ficam inativas e não mudam a rota.</p>}
              <div className="policy-options">
                <button type="button" className={agentCount > 1 && overlapPolicy === "P1" ? "policy-option is-active" : "policy-option"} aria-pressed={agentCount > 1 && overlapPolicy === "P1"} disabled={agentCount === 1} onClick={() => { if (overlapPolicy !== "P1") markDraftChanged(); setOverlapPolicy("P1"); }}><strong>Permitir</strong><span>Equipes podem usar a mesma estrada, sem custo extra.</span></button>
                <button type="button" className={agentCount > 1 && overlapPolicy === "P2" ? "policy-option is-active" : "policy-option"} aria-pressed={agentCount > 1 && overlapPolicy === "P2"} disabled={agentCount === 1} onClick={() => { if (overlapPolicy !== "P2") markDraftChanged(); setOverlapPolicy("P2"); }}><strong>Reduzir</strong><span>Penaliza quilômetros compartilhados para tentar reduzi-los.</span></button>
                <button type="button" className={agentCount > 1 && overlapPolicy === "P3" ? "policy-option is-active" : "policy-option"} aria-pressed={agentCount > 1 && overlapPolicy === "P3"} disabled={agentCount === 1} onClick={() => { if (overlapPolicy !== "P3") markDraftChanged(); setOverlapPolicy("P3"); }}><strong>Evitar</strong><span>Busca rotas sem trechos comuns; pode não encontrar uma combinação.</span></button>
              </div>
              {agentCount > 1 && overlapPolicy === "P2" && <p className="field-note">A busca compara cinco intensidades de penalidade. Depois, escolha uma no mapa para ver as rotas.</p>}
              {agentCount > 1 && overlapPolicy === "P3" && <p className="field-note">A busca usa as rotas candidatas geradas; não encontrar uma combinação aqui não prova que ela seja impossível na rede inteira.</p>}
            </fieldset>

            <div className="field-row field-row-top"><label className="field-label" htmlFor="time-limit">Tempo de busca</label><span className="field-value">{timeLimit}<small> s</small></span></div>
            <input id="time-limit" type="range" min="10" max="300" step="10" value={timeLimit} onChange={(event) => { markDraftChanged(); setTimeLimit(Number(event.target.value)); }} />
            <div className="range-ends"><span>10 s</span><span>300 s</span></div>

            {jobType === "overlap" && <>
              <div className="field-row field-row-top workers-row"><label className="field-label" htmlFor="variants">Alternativas comparadas</label><span className="field-value">{variants}<small> / 8</small></span></div>
              <input id="variants" type="range" min="2" max="8" step="1" value={variants} onChange={(event) => { markDraftChanged(); setVariants(Number(event.target.value)); }} />
              <div className="range-ends"><span>2 alternativas</span><span>8 alternativas</span></div>
              <div className="field-row field-row-top workers-row"><label className="field-label" htmlFor="workers">Processos simultâneos</label><span className="field-value">{workers}<small> máx.</small></span></div>
              <input id="workers" type="range" min="1" max="8" step="1" value={workers} onChange={(event) => { markDraftChanged(); setWorkers(Number(event.target.value)); }} />
              <div className="range-ends"><span>1 processo</span><span>até 8; ajustado pela RAM</span></div>
            </>}

            <div className={agentCount === 1 ? "sticky-form-actions" : "form-actions"}>
              <div className="memory-note"><span className="memory-icon" aria-hidden="true">⌁</span><span><strong>{memoryBudget ? `${formatGigabytes(memoryBudget.available_memory_bytes)} de RAM disponível` : "Verificando a memória…"}</strong><small>{memoryBudget ? `Estimativa: até ${formatGigabytes(memoryBudget.budget_bytes)} · 80% livre · teto de 25 GB; revalida ao iniciar` : "A verificação acontece antes de iniciar"}</small></span></div>
              <button className="primary-button" type="submit" disabled={submitting || apiState !== "ready" || hasActiveExperiment || !memoryBudget?.can_start}>
                <span className="button-symbol" aria-hidden="true">＋</span>{submitting ? "Preparando busca…" : hasActiveExperiment ? "Busca em execução" : "Buscar rota"}<span className="button-arrow" aria-hidden="true">↗</span>
              </button>
            </div>
            <p className="form-footnote">O pontilhado mostra a melhor rota encontrada durante a busca. O resultado é a melhor solução viável no tempo definido; não garante o ótimo global. Rotas antigas sem trechos OSM são estimativas.</p>
          </form>
        </aside>

        <section className="map-panel" aria-label="Mapa dos municípios e rotas">
          <div className="map-toolbar">
            <div className="map-title"><span className="map-crosshair" aria-hidden="true">⌖</span><div><h2>{showDraftMap ? `${agentCount} ${agentCount === 1 ? "equipe" : "equipes"} · ${draftObjectiveLabel}` : selectedMetrics ? scenarioLabel(scenario, municipalities) : "Mapa de Mato Grosso"}</h2><p>{!showDraftMap && approximateGeometry ? "Estimativa entre sedes · sem geometria das vias OSM" : showDraftMap ? `Partida: ${draftOriginLabel}${draftDestinationLabel} · sem cálculo` : selectedExperiment ? `${statusLabel(selectedExperiment.status)} · limite IBGE 2022` : "142 sedes municipais · limite IBGE 2022"}</p></div></div>
            <div className="map-tools">
              {!showDraftMap && isOverlap && selectedExperiment?.status === "succeeded" && <div className="policy-picker" aria-label="Política de sobreposição">
                <select value={policy} onChange={(event) => setPolicy(event.target.value as "P1" | "P2" | "P3")} aria-label="Política"><option value="P1">Permitir (P1)</option><option value="P2">Reduzir (P2)</option><option value="P3">Evitar (P3)</option></select>
                {policy === "P2" && <select value={lambdaValue} onChange={(event) => setLambdaValue(Number(event.target.value))} aria-label="Penalização lambda"><option value="0">λ 0</option><option value="0.25">λ 0,25</option><option value="0.5">λ 0,5</option><option value="1">λ 1</option><option value="2">λ 2</option></select>}
              </div>}
              {selectedRunMetrics && selectedRunMetrics.scenarios.length > 0 && !showDraftMap && <select className="scenario-picker" value={scenario} onChange={(event) => setScenario(event.target.value)} aria-label="Cenário exibido" title={scenarioLabel(scenario, municipalities)}>
                {selectedRunMetrics.scenarios.map((item) => <option key={item.scenario} value={item.scenario}>{scenarioLabel(item.scenario, municipalities)}</option>)}
              </select>}
            </div>
          </div>
          <div className="map-canvas-wrap">
            <Suspense fallback={<div className="map-loading">Carregando motor cartográfico…</div>}>
              <MapView municipalities={municipalities} routeUrl={routeUrl} comparisonUrl={comparisonRouteUrl} previewUrl={previewUrl} selectedOriginIbgeCode={originCode} selectedDestinationIbgeCode={destinationCode} visibleAgents={visibleAgents} onMunicipalitySelect={handleMapMunicipalitySelect} onRouteGeometryChange={(approximate, loaded) => { setApproximateGeometry(approximate); setRouteGeometryLoaded(loaded); }} />
            </Suspense>
            {selectionTarget && <div className="map-pick-prompt"><span className="pick-pulse" />Clique em uma sede municipal para definir {selectionTarget === "origin" ? "a primeira cidade" : "a última cidade"}<button type="button" onClick={() => setSelectionTarget(null)}>Cancelar</button></div>}
            {!previewUrl && (!routeUrl || noSelectedPolicyRoutes) && <div className="map-empty-note"><span className="empty-route-mark" aria-hidden="true">⌁</span><span>{showDraftMap ? "Configuração alterada. Inicie uma busca para ver essa rota no mapa." : noSelectedPolicyRoutes ? noSelectedPolicyMessage : selectedExperiment?.status === "running" || selectedExperiment?.status === "queued" ? "Preparando matriz e primeira solução candidata…" : selectedExperiment ? "Selecione uma execução concluída para exibir as rotas." : "Escolha cidades e equipes para desenhar uma rota neste mapa."}</span></div>}
            {previewUrl && <div className="map-preview-note"><span className="preview-dash" />{selectedExperiment?.status === "succeeded" ? "Cálculo concluído · ordem candidata pontilhada enquanto as vias OSM carregam" : "Busca em andamento · linhas pontilhadas mostram a melhor ordem encontrada"}</div>}
            <div className="map-key" aria-label="Legenda do mapa"><span><i className="key-seat" />Sede municipal</span><span><i className="key-state" />Limite estadual · IBGE 2022</span><span><i className="key-municipal" />Divisas municipais · IBGE 2025</span><span><i className="key-road-federal" />Rodovia federal · IBGE BC250</span><span><i className="key-road-state" />Rodovia estadual · IBGE BC250</span><span><i className="key-road-other" />Demais vias · IBGE BC250</span><span><i className="key-river" />Rio · IBGE BC250</span>{((routeUrl && !noSelectedPolicyRoutes) || previewUrl) && <><span><i className="key-start" />Partida</span><span><i className="key-end" />Chegada</span></>}{comparisonRouteUrl && <span><i className="key-comparison" />Comparação</span>}{previewUrl && <span><i className="key-candidate" />Candidata</span>}</div>
            {selectedMunicipality && <div className="municipality-chip"><span className="chip-dot" />{selectedMunicipality.name}<small>IBGE {selectedMunicipality.ibge_code}</small><button type="button" onClick={() => setSelectedMunicipality(null)} aria-label="Fechar município">×</button></div>}
          </div>
          <div className="map-footer">
            <div className="agent-legend" aria-label="Agentes visíveis">{legendAgents.map((agentId) => <button key={agentId} type="button" className={visibleAgents.includes(agentId) ? "legend-agent is-visible" : "legend-agent"} onClick={() => toggleAgent(agentId)} aria-pressed={visibleAgents.includes(agentId)}><span style={{ backgroundColor: AGENT_COLORS[agentId - 1] }} />Agente {agentId}</button>)}</div>
            <span className="map-scale">Pontilhado: busca · traço longo: estimativa · traço curto: comparação · contínuo: vias OSM</span>
          </div>
        </section>

        <aside className="activity-rail">
          <div className="section-heading"><span className="section-index">02</span><div><h2>Execuções</h2><p>Fila local · atualização automática</p></div></div>
          {error && <div className="inline-error" role="alert">{error}{selectedExperiment?.status === "succeeded" && metricsExperimentId !== selectedExperiment.id && <button type="button" className="text-button" onClick={() => setMetricsRetryToken((value) => value + 1)}>Tentar novamente</button>}</div>}
          {actionError && <div className="inline-error" role="alert">{actionError}</div>}
          {selectedExperiment && <section className="selected-run" aria-label="Progresso da execução selecionada">
            <div className="run-title-line"><span className={`status-marker status-${selectedExperiment.status}`} /><strong>{jobTitle(selectedExperiment.job_type, selectedExperiment.parameters)}</strong><span className="run-percent">{selectedExperiment.progress_percent}%</span></div>
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
              {comparisons.map((experiment) => <option key={experiment.id} value={experiment.id}>{jobTitle(experiment.job_type, experiment.parameters)} · {formatDate(experiment.created_at)}</option>)}
            </select>
            {comparisonRunMetrics && <select className="compare-scenario-picker" value={comparisonScenario} onChange={(event) => setComparisonScenario(event.target.value)} aria-label="Cenário da execução comparada">
              {comparisonRunMetrics.scenarios.map((item) => <option key={item.scenario} value={item.scenario}>{item.scenario}</option>)}
            </select>}
            <a className="export-link" href={getExportUrl(selectedExperiment.id)}>Exportar resultado JSON <span aria-hidden="true">↗</span></a>
          </div>}
          <div className="activity-list-heading"><span>HISTÓRICO RECENTE</span><span>{experiments.length.toString().padStart(2, "0")}</span></div>
          <div className="experiment-list">
            {experiments.length === 0 ? <div className="empty-list"><span className="empty-list-icon">↗</span><strong>Nenhuma execução ainda</strong><p>O primeiro lote aparecerá aqui com status, progresso e horário.</p></div> : experiments.map((experiment) => <button key={experiment.id} type="button" className={experiment.id === selectedId ? "experiment-row is-selected" : "experiment-row"} onClick={() => selectExperiment(experiment)} title={jobTitle(experiment.job_type, experiment.parameters)}>
              <span className={`status-marker status-${experiment.status}`} /><span className="experiment-row-main"><strong>{jobTitle(experiment.job_type, experiment.parameters)}</strong><small>{formatDate(experiment.created_at)}</small></span><span className="experiment-row-state">{experiment.status === "queued" || experiment.status === "running" ? `${experiment.progress_percent}%` : statusLabel(experiment.status)}</span>
            </button>)}
          </div>
          {selectedMetrics && !showDraftMap && <div className="comparison-table">
            <div className="comparison-table-head"><span>MEDIDA</span><span>SELECIONADO</span>{selectedComparisonMetrics && <span>COMPARAÇÃO</span>}</div>
            <div className="comparison-table-row"><span>Custo da busca</span><strong>{formatMetric(scenarioObjectiveValue(selectedMetrics, policy, lambdaValue), metricUnit(selectedMetrics))}</strong>{selectedComparisonMetrics && <strong>{formatMetric(scenarioObjectiveValue(selectedComparisonMetrics, policy, lambdaValue), metricUnit(selectedComparisonMetrics))}</strong>}</div>
            <div className="comparison-table-row"><span>Distância total</span><strong>{formatMetric(scenarioDistanceValue(selectedMetrics, policy, lambdaValue), "m")}</strong>{selectedComparisonMetrics && <strong>{formatMetric(scenarioDistanceValue(selectedComparisonMetrics, policy, lambdaValue), "m")}</strong>}</div>
            <div className="comparison-table-row"><span>Tempo máx.</span><strong title={formatDuration(scenarioTimeMax(selectedMetrics, policy, lambdaValue))}>{formatDuration(scenarioTimeMax(selectedMetrics, policy, lambdaValue))}</strong>{selectedComparisonMetrics && <strong title={formatDuration(scenarioTimeMax(selectedComparisonMetrics, policy, lambdaValue))}>{formatDuration(scenarioTimeMax(selectedComparisonMetrics, policy, lambdaValue))}</strong>}</div>
            <div className="comparison-table-row"><span>Repetição própria</span><strong>{formatRepeatedDistance(scenarioRepeatedValue(selectedMetrics, policy, lambdaValue))}</strong>{selectedComparisonMetrics && <strong>{formatRepeatedDistance(scenarioRepeatedValue(selectedComparisonMetrics, policy, lambdaValue))}</strong>}</div>
            <div className="comparison-table-row"><span title="Distância compartilhada entre equipes">Entre equipes</span><strong>{formatMetric(scenarioOverlapValue(selectedMetrics, policy, lambdaValue), "m")}</strong>{selectedComparisonMetrics && <strong>{formatMetric(scenarioOverlapValue(selectedComparisonMetrics, policy, lambdaValue), "m")}</strong>}</div>
          </div>}
        </aside>
      </div>
      <footer className="page-footer"><span>Dados de sedes: IBGE <span className="footer-divider">/</span> malha: OpenStreetMap</span><span>Rotas abertas · origem comum · 1–3 agentes</span></footer>
    </main>
  );
}

function jobTitle(jobType: JobType, parameters?: Record<string, unknown>) {
  const count = Number(parameters?.vehicle_count ?? (jobType === "tsp" ? 1 : 2));
  const repetition = parameters?.repetition_policy === "reduce" ? " · reduz repetição" : parameters?.repetition_policy === "forbid" ? " · sem repetição" : "";
  if (jobType === "tsp") return `1 equipe · rota aberta${repetition}`;
  const policy = parameters?.overlap_policy === "P2" || parameters?.overlap_policy === "P3"
    ? parameters.overlap_policy
    : "P1";
  const policyLabel = policy === "P1" ? "permite compartilhamento" : policy === "P2" ? "reduz trechos comuns" : "evita trechos comuns";
  return `${count} equipes · ${policy} ${policyLabel}${repetition}`;
}

function scenarioLabel(scenario: string, municipalities: Municipality[]) {
  const match = scenario.match(/^(O1-fixed-(\d+)|O1-fixed-cuiaba|O2-variable-origin)(?:-to-(\d+))?(?:-k([23]))?-(distance|time)$/);
  if (!match) return scenario;
  const origin = match[1] === "O2-variable-origin"
    ? "início otimizado"
    : municipalityName(Number(match[2] ?? 5103403), municipalities);
  const destination = match[3] ? ` → ${municipalityName(Number(match[3]), municipalities)}` : "";
  const agents = Number(match[4] ?? 1);
  const metric = match[5] === "distance" ? "distância" : "tempo";
  return `${origin}${destination} · ${agents} ${agents === 1 ? "equipe" : "equipes"} · ${metric}`;
}

function municipalityName(code: number, municipalities: Municipality[]) {
  return municipalities.find((municipality) => municipality.ibge_code === code)?.name ?? `IBGE ${code}`;
}

function formatGigabytes(bytes: number) {
  return `${(bytes / 1_000_000_000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} GB`;
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
  const total = Math.round(value);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days) return `${days} d ${hours} h ${minutes} min`;
  if (hours) return `${hours} h ${minutes} min`;
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

function scenarioRepeatedValue(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics.repeated_distance_m ?? policySummary(metrics, policy, lambdaValue)?.repeated_distance_m;
  return typeof value === "number" ? value : null;
}

function scenarioDistanceValue(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics.total_distance_m ?? policySummary(metrics, policy, lambdaValue)?.total_distance_m;
  return typeof value === "number" ? value : null;
}

function formatRepeatedDistance(value: number | null) {
  return value === null ? "—" : value < 1000 ? `${Math.round(value)} m` : `${(value / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} km`;
}

function scenarioTimeMax(metrics: ExperimentMetrics["scenarios"][number], policy: "P1" | "P2" | "P3", lambdaValue: number) {
  const value = metrics.time_max_s ?? policySummary(metrics, policy, lambdaValue)?.time_max_s;
  return typeof value === "number" ? value : null;
}

function metricUnit(metrics: ExperimentMetrics["scenarios"][number]) {
  return metrics.unit ?? (metrics.objective === "distance" ? "m" : metrics.objective === "time" ? "s" : undefined);
}

function isPolicyUnavailable(metrics: ExperimentMetrics["scenarios"][number] | null, policy: "P1" | "P2" | "P3", lambdaValue: number) {
  if (metrics?.status === "not_found" || metrics?.status === "infeasible_proven") return true;
  if (!metrics?.best_by_policy) return false;
  const summary = policySummary(metrics, policy, lambdaValue);
  const candidates = summary?.candidate_ids;
  return summary?.status === "not_found" || !Array.isArray(candidates) || candidates.length === 0;
}

const rootContainer = document.getElementById("root");
if (!rootContainer) throw new Error("Elemento #root não encontrado.");

const hot = import.meta.hot;
const root = hot?.data.root ?? createRoot(rootContainer);
if (hot) hot.data.root = root;
root.render(<App />);
