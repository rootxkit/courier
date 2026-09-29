// P1-12. A self-hosted OpenStreetMap extract (Protomaps basemap) read from one
// PMTiles file with HTTP range requests: no tile server, no API key, no third
// party per request. The same approach as api/static/map.html.
import { layers, namedFlavor } from "@protomaps/basemaps";
import type { StyleSpecification } from "maplibre-gl";
import type { Lang } from "../i18n";

export const BASEMAP_URL = "/basemap/basemap.pmtiles";

export interface BasemapInfo {
  bounds: [[number, number], [number, number]];
  // From /basemap/SOURCE.json, when present: an offline map has no other way
  // to say it is stale.
  osmDataAsOf: string | null;
}

function attribution(info: BasemapInfo): string {
  const date = info.osmDataAsOf ? ` · ${info.osmDataAsOf.slice(0, 10)}` : "";
  return (
    '<a href="https://www.openstreetmap.org/copyright" target="_blank">© OpenStreetMap contributors</a>' +
    `${date} · <a href="https://protomaps.com" target="_blank">Protomaps</a>`
  );
}

export function basemapStyle(
  info: BasemapInfo | null,
  lang: Lang,
  dark: boolean,
): StyleSpecification {
  if (!info) {
    return {
      version: 8,
      sources: {},
      layers: [
        {
          id: "background",
          type: "background",
          paint: { "background-color": dark ? "#1e1e1e" : "#e8e8e8" },
        },
      ],
    };
  }
  const flavor = dark ? "dark" : "light";
  let styleLayers = layers("protomaps", namedFlavor(flavor), { lang: "en" });
  // The basemap library has no Georgian. In Georgia OSM's plain `name` is the
  // Georgian name and the extract has no `name:ka`, so in `ka` every label
  // that shows a name shows that. Road shields (`ref`) are left alone.
  if (lang === "ka") {
    styleLayers = styleLayers.map((layer) => {
      if (layer.type !== "symbol") return layer;
      const field = layer.layout?.["text-field"];
      if (!field || !JSON.stringify(field).includes('"name')) return layer;
      return {
        ...layer,
        layout: {
          ...layer.layout,
          "text-field": ["coalesce", ["get", "name:ka"], ["get", "name"]],
        },
      };
    });
  }
  return {
    version: 8,
    glyphs: `${location.origin}/basemap/fonts/{fontstack}/{range}.pbf`,
    sprite: `${location.origin}/basemap/sprites/v4/${flavor}`,
    sources: {
      protomaps: {
        type: "vector",
        url: `pmtiles://${location.origin}${BASEMAP_URL}`,
        attribution: attribution(info),
      },
    },
    layers: styleLayers,
  };
}
