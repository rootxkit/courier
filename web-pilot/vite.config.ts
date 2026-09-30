// Built into dist/ and served by the API at /app (api/app.py). In `npm run dev`
// the API, its pages and the console feed are proxied, so everything is
// same-origin, as it will be behind the TLS front (P0-09). The dev server
// answers /config.json itself, pointing the page at the proxied feed.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = "http://127.0.0.1:8010";
const feed = "ws://127.0.0.1:8000";

export default defineConfig({
  base: "/app/",
  plugins: [
    react(),
    {
      name: "dev-feed-config",
      configureServer(server) {
        server.middlewares.use("/config.json", (_request, response) => {
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ console_feed_url: "/ws/telemetry" }));
        });
      },
    },
  ],
  build: { outDir: "dist", sourcemap: true, chunkSizeWarningLimit: 2000 },
  server: {
    proxy: {
      "/ws": { target: feed, ws: true },
      ...Object.fromEntries(
        [
          "/auth",
          "/drones",
          "/bases",
          "/pilots",
          "/events",
          "/replay",
          "/airspace",
          "/terrain",
          "/login",
          "/basemap",
          "/static",
          "/operators",
        ].map((path) => [path, api]),
      ),
    },
  },
});
