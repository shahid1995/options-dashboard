/**
 * Fail-closed production API URL configuration.
 *
 * The backend API base URL is injected into the client bundle at build time
 * via NEXT_PUBLIC_API_URL (single source of truth for REST, WebSocket, and
 * OAuth-redirect derivations — see lib/api.js). Historically next.config.js
 * substituted a hardcoded Railway URL when the variable was missing, so a
 * production build could silently point at a backend that no longer exists.
 *
 * Contract (enforced by requireApiUrl at the next.config.js boundary):
 *  - production builds REQUIRE NEXT_PUBLIC_API_URL: a missing/blank/invalid
 *    value fails the build with a clear configuration error.
 *  - production accepts only https:// URLs; http, localhost, and loopback
 *    literals are rejected (production frontends are served over TLS by the
 *    hosting platform, and the backend sets Secure cookies, so plain http
 *    backends cannot participate in the session contract).
 *  - the historical Railway URL is never acceptable configuration.
 *  - non-production builds/tests keep the intentional local behavior
 *    (no value -> undefined/empty; localhost/http allowed).
 */

/** Historical Railway backend URL — never valid configuration (may be dead). */
const FORBIDDEN_HOSTS = [
  "options-dashboard-production-fb47.up.railway.app",
];

function isBlank(value) {
  return typeof value !== "string" || value.trim().length === 0;
}

function isLocalhostHost(hostname) {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "[::1]" ||
    hostname.endsWith(".localhost")
  );
}

/**
 * Resolve the backend API URL for a build.
 *
 * @param {Object} inputs
 * @param {string|undefined} inputs.rawValue   Raw NEXT_PUBLIC_API_URL value.
 * @param {boolean} inputs.isProductionBuild   True for production builds.
 * @param {boolean} [inputs.warn]              Print a warning when the URL is
 *                                             accepted but not https (non-
 *                                             production only).
 * @returns {string|undefined} The configured API URL (trimmed), or undefined
 *                             when legitimately unset in non-production.
 * @throws {Error} When a production build is not safely configured.
 */
function requireApiUrl({ rawValue, isProductionBuild, warn }) {
  const value = typeof rawValue === "string" ? rawValue.trim() : undefined;

  if (isBlank(rawValue)) {
    if (isProductionBuild) {
      throw new Error(
        "NEXT_PUBLIC_API_URL is required for production builds. " +
          "No fallback backend URL is provided: set NEXT_PUBLIC_API_URL to " +
          "the production API origin (e.g. https://api.example.com) and " +
          "rebuild. Refusing to embed a default backend URL."
      );
    }
    return undefined;
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(
      "NEXT_PUBLIC_API_URL is not a valid URL: refusing to build. " +
        "Set it to an absolute origin such as https://api.example.com."
    );
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error(
      "NEXT_PUBLIC_API_URL must use http(s). Refusing to build with " +
        `protocol '${parsed.protocol}'.`
    );
  }

  if (FORBIDDEN_HOSTS.includes(parsed.hostname.toLowerCase())) {
    throw new Error(
      "NEXT_PUBLIC_API_URL points at the historical Railway backend " +
        "(options-dashboard-production-fb47.up.railway.app), which is no " +
        "longer valid configuration. Set NEXT_PUBLIC_API_URL to the current " +
        "production API origin."
    );
  }

  if (isProductionBuild) {
    if (parsed.protocol !== "https:") {
      throw new Error(
        "NEXT_PUBLIC_API_URL must use https for production builds " +
          "(the backend issues Secure, SameSite=None session cookies, so " +
          "plain-http backends cannot hold the session). Refusing to " +
          "build with a non-https production API URL."
      );
    }
    if (isLocalhostHost(parsed.hostname.toLowerCase())) {
      throw new Error(
        "NEXT_PUBLIC_API_URL must not point at localhost/loopback in a " +
          "production build. Refusing to build."
      );
    }
  } else if (parsed.protocol !== "https:" && warn) {
    console.warn(
      `[api-config] NEXT_PUBLIC_API_URL is non-https (${parsed.protocol}//${parsed.host}); ` +
        "acceptable for local development only."
    );
  }

  return value;
}

module.exports = { requireApiUrl, FORBIDDEN_HOSTS, isBlank, isLocalhostHost };
