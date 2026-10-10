import { useEffect, useRef } from "react";
import {
  GeoJSONSource,
  LngLatBounds,
  Map as MapLibreMap,
  NavigationControl,
  Popup,
  setWorkerUrl,
  type FilterSpecification,
  type StyleSpecification,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import type { Municipality } from "./api";

setWorkerUrl(workerUrl);

interface MapViewProps {
  municipalities: Municipality[];
  routeUrl: string;
  comparisonUrl: string;
  visibleAgents: number[];
  onMunicipalitySelect: (municipality: Municipality | null) => void;
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

export function MapView({ municipalities, routeUrl, comparisonUrl, visibleAgents, onMunicipalitySelect }: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const latestMunicipalities = useRef(municipalities);
  const latestAgents = useRef(visibleAgents);
  const latestRouteUrl = useRef(routeUrl);
  const latestComparisonUrl = useRef(comparisonUrl);
  const onSelect = useRef(onMunicipalitySelect);
  const hasInitialFit = useRef(false);

  useEffect(() => { latestMunicipalities.current = municipalities; }, [municipalities]);
  useEffect(() => { latestAgents.current = visibleAgents; }, [visibleAgents]);
  useEffect(() => { latestRouteUrl.current = routeUrl; }, [routeUrl]);
  useEffect(() => { latestComparisonUrl.current = comparisonUrl; }, [comparisonUrl]);
  useEffect(() => { onSelect.current = onMunicipalitySelect; }, [onMunicipalitySelect]);

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
      map.addSource("municipalities", { type: "geojson", data: municipalityGeoJson(latestMunicipalities.current) });
      map.addLayer({
        id: "municipality-points",
        type: "circle",
        source: "municipalities",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 3.2, 8, 5.5, 12, 7],
          "circle-color": "#f8f5ec",
          "circle-stroke-color": "#366c61",
          "circle-stroke-width": 1.8,
        },
      });
      map.addSource("routes", { type: "geojson", data: municipalityGeoJson([]) });
      map.addSource("comparison-routes", { type: "geojson", data: municipalityGeoJson([]) });
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
        filter: agentFilter(latestAgents.current),
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["match", ["get", "agent_id"], 1, "#346b9b", 2, "#4a8058", 3, "#cf762e", "#777777"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 4, 2.5, 8, 4.2, 12, 6],
          "line-opacity": 0.88,
        },
      });
      const routeSource = map.getSource("routes") as GeoJSONSource;
      if (latestRouteUrl.current) routeSource.setData(latestRouteUrl.current);
      const comparisonSource = map.getSource("comparison-routes") as GeoJSONSource;
      if (latestComparisonUrl.current) comparisonSource.setData(latestComparisonUrl.current);

      map.on("click", "municipality-points", (event) => {
        const feature = event.features?.[0];
        if (!feature?.properties) return;
        const code = Number(feature.properties.ibge_code);
        const municipality = latestMunicipalities.current.find((item) => item.ibge_code === code);
        if (!municipality) return;
        onSelect.current(municipality);
        const content = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = municipality.name;
        const codeLabel = document.createElement("span");
        codeLabel.textContent = `IBGE ${municipality.ibge_code}`;
        content.append(name, codeLabel);
        new Popup({ closeButton: false, closeOnClick: true }).setLngLat(event.lngLat).setDOMContent(content).addTo(map);
      });
      map.on("mouseenter", "municipality-points", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "municipality-points", () => { map.getCanvas().style.cursor = ""; });
      fitMunicipalities(map, latestMunicipalities.current);
    });

    const resizeObserver = new ResizeObserver(() => map.resize());
    resizeObserver.observe(containerRef.current);
    return () => {
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
    map.setFilter("comparison-agent-routes", agentFilter(visibleAgents));
  }, [visibleAgents]);

  useEffect(() => {
    const map = mapRef.current;
    const source = map?.getSource("routes") as GeoJSONSource | undefined;
    if (!source || !map?.isStyleLoaded()) return;
    source.setData(routeUrl || municipalityGeoJson([]));
  }, [routeUrl]);

  useEffect(() => {
    const map = mapRef.current;
    const source = map?.getSource("comparison-routes") as GeoJSONSource | undefined;
    if (!source || !map?.isStyleLoaded()) return;
    source.setData(comparisonUrl || municipalityGeoJson([]));
  }, [comparisonUrl]);

  return <div className="map-root" ref={containerRef} role="application" aria-label="Mapa interativo de Mato Grosso" />;
}

function agentFilter(agentIds: number[]): FilterSpecification {
  return agentIds.length ? ["in", "agent_id", ...agentIds] : ["==", "agent_id", -1];
}

function fitMunicipalities(map: MapLibreMap, municipalities: Municipality[]) {
  if (!municipalities.length) return;
  const bounds = new LngLatBounds();
  municipalities.forEach((municipality) => bounds.extend([municipality.longitude, municipality.latitude]));
  map.fitBounds(bounds, { padding: 60, maxZoom: 6.5, duration: 500 });
}
