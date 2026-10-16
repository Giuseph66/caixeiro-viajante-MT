import { useEffect, useRef } from "react";
import {
  GeoJSONSource,
  LngLatBounds,
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  setWorkerUrl,
  type ExpressionSpecification,
  type FilterSpecification,
  type StyleSpecification,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import type { Municipality } from "./api";

setWorkerUrl(workerUrl);

// Official extent of frontend/public/mt-boundary.geojson: [west, south] / [east, north].
const MT_BOUNDARY_BBOX: [[number, number], [number, number]] = [
  [-61.6333, -18.0416],
  [-50.2248, -7.349],
];

interface MapViewProps {
  municipalities: Municipality[];
  routeUrl: string;
  comparisonUrl: string;
  previewUrl: string;
  selectedOriginIbgeCode: number | null;
  selectedDestinationIbgeCode: number | null;
  visibleAgents: number[];
  onMunicipalitySelect: (municipality: Municipality | null) => void;
  onRouteGeometryChange: (approximate: boolean, loaded: boolean) => void;
}

function municipalityGeoJson(municipalities: Municipality[]) {
  return {
    type: "FeatureCollection" as const,
    features: municipalities.map((municipality) => ({
      type: "Feature" as const,
      geometry: { type: "Point" as const, coordinates: [municipality.longitude, municipality.latitude] },
      properties: { ibge_code: municipality.ibge_code, name: municipality.name },
    })),
  };
}

export function MapView({
  municipalities,
  routeUrl,
  comparisonUrl,
  previewUrl,
  selectedOriginIbgeCode,
  selectedDestinationIbgeCode,
  visibleAgents,
  onMunicipalitySelect,
  onRouteGeometryChange,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const latestMunicipalities = useRef(municipalities);
  const latestAgents = useRef(visibleAgents);
  const latestRouteUrl = useRef(routeUrl);
  const latestComparisonUrl = useRef(comparisonUrl);
  const latestPreviewUrl = useRef(previewUrl);
  const latestOrigin = useRef(selectedOriginIbgeCode);
  const latestDestination = useRef(selectedDestinationIbgeCode);
  const onSelect = useRef(onMunicipalitySelect);
  const onGeometryChange = useRef(onRouteGeometryChange);
  const hasInitialFit = useRef(false);
  const routeRequestId = useRef(0);
  const comparisonRequestId = useRef(0);
  const previewRequestId = useRef(0);

  useEffect(() => { latestMunicipalities.current = municipalities; }, [municipalities]);
  useEffect(() => { latestAgents.current = visibleAgents; }, [visibleAgents]);
  useEffect(() => { latestRouteUrl.current = routeUrl; }, [routeUrl]);
  useEffect(() => { latestComparisonUrl.current = comparisonUrl; }, [comparisonUrl]);
  useEffect(() => { latestPreviewUrl.current = previewUrl; }, [previewUrl]);
  useEffect(() => { latestOrigin.current = selectedOriginIbgeCode; }, [selectedOriginIbgeCode]);
  useEffect(() => { latestDestination.current = selectedDestinationIbgeCode; }, [selectedDestinationIbgeCode]);
  useEffect(() => { onSelect.current = onMunicipalitySelect; }, [onMunicipalitySelect]);
  useEffect(() => { onGeometryChange.current = onRouteGeometryChange; }, [onRouteGeometryChange]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const style: StyleSpecification = {
      version: 8,
      sources: {},
      layers: [{ id: "background", type: "background", paint: { "background-color": "#edeae1" } }],
    };
    const map = new MapLibreMap({
      container: containerRef.current,
      style,
      center: [-55.7, -12.7],
      zoom: 4.5,
      minZoom: 3,
      maxZoom: 14,
      attributionControl: false,
      pitchWithRotate: false,
      dragRotate: false,
    });
    map.addControl(new NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("state-boundary", { type: "geojson", data: "/mt-boundary.geojson" });
      map.addLayer({
        id: "state-boundary-fill",
        type: "fill",
        source: "state-boundary",
        paint: { "fill-color": "#e3e9dc", "fill-opacity": 0.78 },
      });
      map.addSource("municipality-boundaries", {
        type: "geojson",
        data: "/municipality-boundaries.geojson",
      });
      map.addLayer({
        id: "municipality-boundaries",
        type: "line",
        source: "municipality-boundaries",
        layout: { "line-join": "round" },
        paint: {
          "line-color": "#8e9c8f",
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.75, 8, 1.25, 12, 1.6],
          "line-opacity": 0.9,
        },
      });
      // Malha rodoviária do IBGE (BC250), sempre visível sob as rotas.
      map.addSource("ibge-rivers", { type: "geojson", data: IBGE_RIVERS_URL });
      map.addLayer({
        id: "ibge-rivers",
        type: "line",
        source: "ibge-rivers",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#6f9db5",
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.4, 8, 0.8, 12, 1.6],
          "line-opacity": ["case", ["get", "permanente"], 0.7, 0.4],
        },
      });
      map.addSource("ibge-roads", { type: "geojson", data: IBGE_ROADS_URL });
      for (const layer of IBGE_ROAD_LAYERS) {
        map.addLayer({
          id: layer.id,
          type: "line",
          source: "ibge-roads",
          filter: ["==", ["get", "categoria"], layer.categoria],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": layer.color,
            "line-width": ["interpolate", ["linear"], ["zoom"], 4, layer.width * 0.5, 8, layer.width, 12, layer.width * 1.8],
            "line-opacity": 0.85,
            ...(layer.dash ? { "line-dasharray": layer.dash } : {}),
          },
        });
      }
      // Camadas invisíveis e largas: dão área de alcance ao mouse sobre linhas finas.
      for (const [id, source, width] of [["ibge-rivers-hit", "ibge-rivers", 10], ["ibge-roads-hit", "ibge-roads", 12]] as const) {
        map.addLayer({ id, type: "line", source, paint: { "line-width": width, "line-opacity": 0 } });
      }
      map.addSource("route-candidate", { type: "geojson", data: municipalityGeoJson([]) });
      map.addSource("selection-points", { type: "geojson", data: municipalityGeoJson([]) });
      map.addLayer({
        id: "state-boundary-outline",
        type: "line",
        source: "state-boundary",
        layout: { "line-join": "round" },
        paint: {
          "line-color": "#748c78",
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 1.2, 8, 2],
          "line-opacity": 0.9,
        },
      });
      map.addSource("municipalities", { type: "geojson", data: municipalityGeoJson(latestMunicipalities.current) });
      map.addLayer({
        id: "municipality-points",
        type: "circle",
        source: "municipalities",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 3.6, 8, 6, 12, 7.5],
          "circle-color": "#fbfaf5",
          "circle-stroke-color": "#244a43",
          "circle-stroke-width": 2,
        },
      });
      map.addLayer({
        id: "municipality-hit-target",
        type: "circle",
        source: "municipalities",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 9, 8, 11, 12, 13],
          "circle-color": "#fbfaf5",
          "circle-opacity": 0.01,
        },
      });
      map.addSource("routes", { type: "geojson", data: municipalityGeoJson([]) });
      map.addSource("comparison-routes", { type: "geojson", data: municipalityGeoJson([]) });
      map.addSource("route-ends", { type: "geojson", data: municipalityGeoJson([]) });
      map.addLayer({
        id: "candidate-routes",
        type: "line",
        source: "route-candidate",
        filter: agentFilter(latestAgents.current),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": AGENT_COLOR_EXPRESSION,
          "line-width": 2.8,
          "line-dasharray": [1, 1.5],
          "line-opacity": 0.78,
        },
      });
      map.addLayer({
        id: "selected-municipalities",
        type: "circle",
        source: "selection-points",
        paint: {
          "circle-radius": 10,
          "circle-color": ["match", ["get", "role"], "origin", "#cf762e", "#fbfaf5"],
          "circle-stroke-color": ["match", ["get", "role"], "origin", "#20342e", "#326157"],
          "circle-stroke-width": 3,
        },
      });
      map.addLayer({
        id: "agent-routes-casing",
        type: "line",
        source: "routes",
        filter: agentFilter(latestAgents.current),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#fbfaf5",
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 5, 8, 7.5, 12, 10],
          "line-opacity": 0.85,
        },
      });
      map.addLayer({
        id: "comparison-agent-routes",
        type: "line",
        source: "comparison-routes",
        filter: agentFilter(latestAgents.current),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["match", ["get", "agent_id"], 1, "#346b9b", 2, "#4a8058", 3, "#cf762e", "#777777"],
          "line-width": 2,
          "line-dasharray": [2, 1.5],
          "line-opacity": 0.54,
        },
      });
      map.addLayer({
        id: "agent-routes",
        type: "line",
        source: "routes",
        filter: ["all", agentFilter(latestAgents.current), ["!=", "geometry_approximate", true]] as FilterSpecification,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["match", ["get", "agent_id"], 1, "#346b9b", 2, "#4a8058", 3, "#cf762e", "#777777"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 2.5, 8, 4.2, 12, 6],
          "line-opacity": 0.95,
        },
      });
      map.addLayer({
        id: "approximate-agent-routes",
        type: "line",
        source: "routes",
        filter: ["all", agentFilter(latestAgents.current), ["==", "geometry_approximate", true]] as FilterSpecification,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["match", ["get", "agent_id"], 1, "#346b9b", 2, "#4a8058", 3, "#cf762e", "#777777"],
          "line-width": 3,
          "line-dasharray": [4, 3],
          "line-opacity": 0.95,
        },
      });
      // Partida laranja e chegada vazada mantêm a legenda consistente entre equipes.
      map.addLayer({
        id: "route-end-points",
        type: "circle",
        source: "route-ends",
        filter: roleAgentFilter("end", latestAgents.current),
        paint: {
          "circle-radius": 8,
          "circle-color": "#fbfaf5",
          "circle-stroke-color": "#326157",
          "circle-stroke-width": 4,
        },
      });
      map.addLayer({
        id: "route-start-points",
        type: "circle",
        source: "route-ends",
        filter: roleAgentFilter("start", latestAgents.current),
        paint: {
          "circle-radius": 9,
          "circle-color": "#cf762e",
          "circle-stroke-color": "#20342e",
          "circle-stroke-width": 2.5,
        },
      });
      map.moveLayer("municipality-points");
      map.moveLayer("route-end-points");
      map.moveLayer("route-start-points");
      map.moveLayer("selected-municipalities");
      map.moveLayer("municipality-hit-target");
      void loadRoutes(map, latestRouteUrl.current, nextRouteRequest(routeRequestId), onGeometryChange.current, Boolean(latestPreviewUrl.current));
      void loadComparison(map, latestComparisonUrl.current, nextRouteRequest(comparisonRequestId));
      void loadPreview(map, latestPreviewUrl.current, nextRouteRequest(previewRequestId));
      updateSelectedStops(
        map,
        latestMunicipalities.current,
        latestOrigin.current,
        latestDestination.current,
      );

      map.on("click", "municipality-hit-target", (event) => {
        const feature = event.features?.[0];
        if (!feature?.properties) return;
        const code = Number(feature.properties.ibge_code);
        const municipality = latestMunicipalities.current.find((item) => item.ibge_code === code);
        if (!municipality) return;
        onSelect.current(municipality);
      });
      map.on("mouseenter", "municipality-hit-target", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "municipality-hit-target", () => { map.getCanvas().style.cursor = ""; });
      const tooltip = new Popup({ closeButton: false, closeOnClick: false, offset: 12, className: "map-tooltip" });
      map.on("mousemove", (event) => {
        const overSeat = map.queryRenderedFeatures(event.point, { layers: ["municipality-hit-target"] }).length > 0;
        const [hit] = overSeat ? [] : map.queryRenderedFeatures(event.point, { layers: ["ibge-roads-hit", "ibge-rivers-hit"] });
        const label = hit ? featureTooltip(hit.layer.id, hit.properties) : null;
        if (!label) { tooltip.remove(); return; }
        tooltip.setLngLat(event.lngLat).setText(label).addTo(map);
      });
      map.on("mouseout", () => tooltip.remove());
      fitMunicipalities(map, latestMunicipalities.current);
      disposeLabels = attachMunicipalityLabels(map, () => latestMunicipalities.current);
    });

    let disposeLabels: (() => void) | undefined;
    const resizeObserver = new ResizeObserver(() => map.resize());
    resizeObserver.observe(containerRef.current);
    return () => {
      disposeLabels?.();
      routeRequestId.current += 1;
      comparisonRequestId.current += 1;
      previewRequestId.current += 1;
      resizeObserver.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded() || !map.getSource("municipalities")) return;
    (map.getSource("municipalities") as GeoJSONSource).setData(municipalityGeoJson(municipalities));
    if (!hasInitialFit.current && municipalities.length) {
      fitMunicipalities(map, municipalities);
      hasInitialFit.current = true;
    }
  }, [municipalities]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded() || !map.getLayer("agent-routes")) return;
    map.setFilter("agent-routes", agentFilter(visibleAgents));
    map.setFilter("agent-routes-casing", agentFilter(visibleAgents));
    map.setFilter("approximate-agent-routes", ["all", agentFilter(visibleAgents), ["==", "geometry_approximate", true]] as FilterSpecification);
    map.setFilter("candidate-routes", agentFilter(visibleAgents));
    map.setFilter("route-start-points", roleAgentFilter("start", visibleAgents));
    map.setFilter("route-end-points", roleAgentFilter("end", visibleAgents));
    map.setFilter("comparison-agent-routes", agentFilter(visibleAgents));
  }, [visibleAgents]);

  useEffect(() => {
    // A lista chega de forma assíncrona e pode carregar depois do estilo do mapa.
    const map = mapRef.current;
    const source = map?.getSource("municipalities") as GeoJSONSource | undefined;
    if (!source) return;
    source.setData(municipalityGeoJson(municipalities));
  }, [municipalities]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded() || !map.getSource("selection-points")) return;
    updateSelectedStops(map, municipalities, selectedOriginIbgeCode, selectedDestinationIbgeCode);
  }, [municipalities, selectedOriginIbgeCode, selectedDestinationIbgeCode]);

  useEffect(() => {
    // Antes do load do mapa as sources não existem; o handler de load faz a carga inicial.
    const map = mapRef.current;
    if (!map?.getSource("routes")) return;
    void loadRoutes(map, routeUrl, nextRouteRequest(routeRequestId), onGeometryChange.current, Boolean(latestPreviewUrl.current));
  }, [routeUrl]);

  useEffect(() => {
    // Antes do load a source não existe; o handler de load faz a carga inicial.
    const map = mapRef.current;
    if (!map?.getSource("comparison-routes")) return;
    void loadComparison(map, comparisonUrl, nextRouteRequest(comparisonRequestId));
  }, [comparisonUrl]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("route-candidate")) return;
    void loadPreview(map, previewUrl, nextRouteRequest(previewRequestId));
  }, [previewUrl]);

  return <div className="map-root" ref={containerRef} role="application" aria-label="Mapa interativo de Mato Grosso" />;
}

const IBGE_ROADS_URL = "/ibge-rodovias-mt.geojson";
const IBGE_RIVERS_URL = "/ibge-rios-mt.geojson";

const ROAD_CATEGORY_LABEL: Record<string, string> = {
  federal: "Rodovia federal",
  estadual: "Rodovia estadual",
  outra: "Via municipal ou vicinal",
};

// A BC250 só traz a sigla (BR-163, MT-130) em parte dos trechos; os demais saem pelo tipo.
function featureTooltip(layerId: string, properties: Record<string, unknown> | null): string | null {
  if (!properties) return null;
  if (layerId === "ibge-rivers-hit") {
    return typeof properties.nome === "string" ? properties.nome : null;
  }
  const category = ROAD_CATEGORY_LABEL[String(properties.categoria)] ?? "Via";
  const surface = properties.pavimentada === true || properties.pavimentada === "true" ? "pavimentada" : "sem pavimentação";
  return typeof properties.sigla === "string" ? `${properties.sigla} · ${category} · ${surface}` : `${category} · ${surface}`;
}

// Do mais largo ao mais fino: federais por cima das estaduais e das demais vias.
const IBGE_ROAD_LAYERS = [
  { id: "ibge-roads-other", categoria: "outra", color: "#a4906d", width: 0.7, dash: undefined },
  { id: "ibge-roads-state", categoria: "estadual", color: "#b8873f", width: 1.1, dash: undefined },
  { id: "ibge-roads-federal", categoria: "federal", color: "#8a4b2d", width: 1.6, dash: undefined },
] as const;

const AGENT_COLOR_EXPRESSION: ExpressionSpecification = [
  "match", ["get", "agent_id"], 1, "#346b9b", 2, "#4a8058", 3, "#cf762e", "#777777",
];

function nextRouteRequest(counter: { current: number }) {
  const id = ++counter.current;
  return () => counter.current === id;
}

type RouteCollection = {
  features?: Array<{ geometry?: { type?: string; coordinates?: number[][] }; properties?: { agent_id?: number; geometry_approximate?: boolean } | null }>;
};

function updateSelectedStops(
  map: MapLibreMap,
  municipalities: Municipality[],
  originCode: number | null,
  destinationCode: number | null,
) {
  const source = map.getSource("selection-points") as GeoJSONSource | undefined;
  if (!source) return;
  const features = [
    { code: originCode, role: "origin" },
    { code: destinationCode, role: "destination" },
  ].flatMap(({ code, role }) => {
    const municipality = municipalities.find((item) => item.ibge_code === code);
    if (!municipality) return [];
    return [{
      type: "Feature" as const,
      geometry: {
        type: "Point" as const,
        coordinates: [municipality.longitude, municipality.latitude],
      },
      properties: { role, ibge_code: municipality.ibge_code },
    }];
  });
  source.setData({ type: "FeatureCollection", features } as unknown as GeoJSON.FeatureCollection);
}

// Busca única: "routes" recebe as linhas; "route-ends" recebe primeiro/último vértice de cada LineString.
async function loadRoutes(
  map: MapLibreMap,
  url: string,
  isCurrent: () => boolean,
  onGeometryChange: (approximate: boolean, loaded: boolean) => void,
  preservePreviewEndpoints: boolean,
) {
  const routes = map.getSource("routes") as GeoJSONSource | undefined;
  const ends = map.getSource("route-ends") as GeoJSONSource | undefined;
  if (!routes || !ends) return;
  const empty = municipalityGeoJson([]);
  const clear = () => { routes.setData(empty); ends.setData(empty); };
  if (!url) { clear(); onGeometryChange(false, false); return; }
  routes.setData(empty);
  if (!preservePreviewEndpoints) ends.setData(empty);
  onGeometryChange(false, false);
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    const payload = await response.json() as RouteCollection;
    if (!isCurrent()) return;
    const endpoints = (payload.features ?? []).flatMap((feature) => {
      const coordinates = feature.geometry?.type === "LineString" ? feature.geometry.coordinates ?? [] : [];
      if (coordinates.length < 2) return [];
      const agent_id = feature.properties?.agent_id ?? 0;
      return [
        { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: coordinates[0] }, properties: { role: "start", agent_id } },
        { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: coordinates[coordinates.length - 1] }, properties: { role: "end", agent_id } },
      ];
    });
    routes.setData(payload as unknown as GeoJSON.FeatureCollection);
    ends.setData({ type: "FeatureCollection", features: endpoints } as unknown as GeoJSON.FeatureCollection);
    onGeometryChange((payload.features ?? []).some((feature) => feature.properties?.geometry_approximate === true), true);
  } catch {
    if (isCurrent()) {
      clear();
      onGeometryChange(false, false);
    }
  }
}

async function loadComparison(map: MapLibreMap, url: string, isCurrent: () => boolean) {
  const source = map.getSource("comparison-routes") as GeoJSONSource | undefined;
  if (!source) return;
  const clear = () => source.setData(municipalityGeoJson([]));
  if (!url) { clear(); return; }
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    const payload = await response.json() as GeoJSON.FeatureCollection;
    if (!isCurrent()) return;
    source.setData(payload);
  } catch {
    if (isCurrent()) clear();
  }
}

async function loadPreview(map: MapLibreMap, url: string, isCurrent: () => boolean) {
  const source = map.getSource("route-candidate") as GeoJSONSource | undefined;
  const ends = map.getSource("route-ends") as GeoJSONSource | undefined;
  if (!source) return;
  const clear = () => source.setData(municipalityGeoJson([]));
  if (!url) { clear(); return; }
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    const payload = await response.json() as GeoJSON.FeatureCollection;
    if (isCurrent()) {
      source.setData(payload);
      if (ends) {
        const endpoints = (payload.features ?? []).flatMap((feature) => {
          const coordinates = feature.geometry?.type === "LineString" ? feature.geometry.coordinates ?? [] : [];
          if (coordinates.length < 2) return [];
          const agent_id = Number(feature.properties?.agent_id ?? 1);
          return [
            { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: coordinates[0] }, properties: { role: "start", agent_id } },
            { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: coordinates[coordinates.length - 1] }, properties: { role: "end", agent_id } },
          ];
        });
        ends.setData({ type: "FeatureCollection", features: endpoints } as unknown as GeoJSON.FeatureCollection);
      }
    }
  } catch {
    if (isCurrent()) clear();
  }
}

function agentFilter(agentIds: number[]): FilterSpecification {
  return agentIds.length ? ["in", "agent_id", ...agentIds] : ["==", "agent_id", -1];
}

function roleAgentFilter(role: "start" | "end", agentIds: number[]): FilterSpecification {
  return ["all", ["==", ["get", "role"], role], ["in", ["get", "agent_id"], ["literal", agentIds]]];
}

const LABEL_MIN_ZOOM = 7;
const LABEL_HEIGHT = 14;
const LABEL_CHAR_WIDTH = 6;

// Style local sem glyphs (offline): rótulos são divs HTML posicionados sobre o canvas,
// com colisão gulosa em pixels para evitar sobreposição.
function attachMunicipalityLabels(map: MapLibreMap, getMunicipalities: () => Municipality[]) {
  const layer = document.createElement("div");
  layer.setAttribute("aria-hidden", "true");
  Object.assign(layer.style, { position: "absolute", inset: "0", overflow: "hidden", pointerEvents: "none" });
  map.getCanvasContainer().appendChild(layer);
  const elements = new Map<number, HTMLDivElement>();
  let frame = 0;

  const update = () => {
    frame = 0;
    const zoom = map.getZoom();
    const visible = new Set<number>();
    if (zoom >= LABEL_MIN_ZOOM) {
      const { clientWidth: width, clientHeight: height } = map.getCanvas();
      const fontSize = zoom >= 9 ? 12 : 11;
      const scale = fontSize / 11;
      const placed: Array<[number, number, number, number]> = [];
      const sorted = [...getMunicipalities()].sort((a, b) => a.name.length - b.name.length || a.ibge_code - b.ibge_code);
      for (const municipality of sorted) {
        const point = map.project([municipality.longitude, municipality.latitude]);
        const w = municipality.name.length * LABEL_CHAR_WIDTH * scale + 6;
        const h = LABEL_HEIGHT * scale;
        const x = point.x + 10;
        const y = point.y - h / 2;
        if (x < 0 || x + w > width || y < 0 || y + h > height) continue;
        if (placed.some(([px, py, pw, ph]) => x < px + pw && x + w > px && y < py + ph && y + h > py)) continue;
        placed.push([x, y, w, h]);
        let element = elements.get(municipality.ibge_code);
        if (!element) {
          element = document.createElement("div");
          element.textContent = municipality.name;
          Object.assign(element.style, {
            position: "absolute",
            left: "0",
            top: "0",
            whiteSpace: "nowrap",
            lineHeight: `${LABEL_HEIGHT}px`,
            fontWeight: "600",
            color: "#2f4540",
            textShadow: "0 0 3px #edeae1, 0 0 3px #edeae1, 0 0 2px #edeae1, 0 0 2px #edeae1",
            willChange: "transform",
          });
          layer.appendChild(element);
          elements.set(municipality.ibge_code, element);
        }
        element.style.fontSize = `${fontSize}px`;
        element.style.transform = `translate(${x}px, ${y}px)`;
        element.style.display = "";
        visible.add(municipality.ibge_code);
      }
    }
    elements.forEach((element, code) => { if (!visible.has(code)) element.style.display = "none"; });
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };

  map.on("move", schedule);
  map.on("resize", schedule);
  schedule();
  return () => {
    map.off("move", schedule);
    map.off("resize", schedule);
    if (frame) cancelAnimationFrame(frame);
    layer.remove();
  };
}

function fitMunicipalities(map: MapLibreMap, municipalities: Municipality[]) {
  if (!municipalities.length) return;
  const bounds = new LngLatBounds(MT_BOUNDARY_BBOX[0], MT_BOUNDARY_BBOX[1]);
  municipalities.forEach((municipality) => bounds.extend([municipality.longitude, municipality.latitude]));
  map.fitBounds(bounds, {
    padding: { top: 56, right: 56, bottom: 56, left: 56 },
    maxZoom: 7.2,
    duration: 500,
  });
}
