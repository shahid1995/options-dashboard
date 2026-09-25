/** @type {import('next').NextConfig} */
const { requireApiUrl } = require("./lib/apiConfig");

// Fail-closed backend API configuration (frontend counterpart to the
// backend's ADR-014 guard): production builds REQUIRE an explicit
// NEXT_PUBLIC_API_URL pointing at a valid https origin. Missing/blank/
// invalid values — including the historical Railway URL, localhost, or
// plain http — abort the build with a clear configuration error. No default
// backend URL is ever embedded (the old hardcoded Railway fallback has been
// removed; it is historical infrastructure and no longer exists).
//
// Non-production builds (development, tests, preview) keep the intentional
// local behavior: an unset value yields undefined (relative-URL mode) and
// http/localhost APIs are allowed.
const isProductionBuild = process.env.NODE_ENV === "production";

const resolvedApiUrl = requireApiUrl({
  rawValue: process.env.NEXT_PUBLIC_API_URL,
  isProductionBuild,
  warn: true,
});

const nextConfig = {
  env: {
    // Backend API base URL — required at build time for production, optional
    // for local development/tests. Consumed by lib/api.js (REST baseURL,
    // loginUrl, chainWsUrl) and the settings page (OAuth redirect/origin).
    NEXT_PUBLIC_API_URL: resolvedApiUrl,
  },
};

module.exports = nextConfig;
