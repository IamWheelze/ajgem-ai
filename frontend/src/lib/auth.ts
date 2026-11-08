/** OIDC sign-in for a public browser client: authorisation code + PKCE (S256), state and nonce checks. */
import { API_BASE } from "./api";

export type AuthConfig =
  | { mode: "dev"; dev_login: boolean }
  | { mode: "oidc"; issuer: string; client_id: string; scopes: string; authorization_endpoint: string; token_endpoint: string; end_session_endpoint?: string | null };

const TOKEN_KEY = "ajgem.idToken";
const PENDING_KEY = "ajgem.oidcPending";

const store = {
  get: (k: string) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k: string, v: string) => { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  del: (k: string) => { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

let cached: AuthConfig | null = null;
export async function getAuthConfig(): Promise<AuthConfig> {
  if (!cached) cached = (await (await fetch(`${API_BASE}/auth/config`)).json()) as AuthConfig;
  return cached;
}

/** Session-scoped ID token (never localStorage: it ends with the browser session). */
export function getIdToken(): string | null {
  const t = store.get(TOKEN_KEY);
  if (!t) return null;
  const exp = (decodeJwt(t)?.exp as number | undefined) ?? 0;
  if (exp * 1000 < Date.now() + 15_000) {
    store.del(TOKEN_KEY);
    return null;
  }
  return t;
}

export function clearIdToken(): void {
  store.del(TOKEN_KEY);
}

export function decodeJwt(token: string): Record<string, unknown> | null {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(decodeURIComponent(escape(atob(part.padEnd(part.length + ((4 - (part.length % 4)) % 4), "=")))));
  } catch {
    return null;
  }
}

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function random(n = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}
export async function pkceChallenge(verifier: string): Promise<string> {
  return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
}

const redirectUri = () => `${window.location.origin}/auth/callback`;

export async function startLogin(cfg: Extract<AuthConfig, { mode: "oidc" }>, returnTo = "/projects"): Promise<void> {
  const verifier = random(48);
  const state = random(16);
  const nonce = random(16);
  store.set(PENDING_KEY, JSON.stringify({ verifier, state, nonce, returnTo }));
  const q = new URLSearchParams({
    response_type: "code", client_id: cfg.client_id, redirect_uri: redirectUri(), scope: cfg.scopes,
    state, nonce, code_challenge: await pkceChallenge(verifier), code_challenge_method: "S256",
  });
  window.location.assign(`${cfg.authorization_endpoint}?${q}`);
}

/** Exchange the authorisation code for an ID token; verifies state and nonce. Returns the path to go to. */
export async function completeLogin(cfg: Extract<AuthConfig, { mode: "oidc" }>, search: string): Promise<string> {
  const params = new URLSearchParams(search);
  const pending = JSON.parse(store.get(PENDING_KEY) ?? "null") as { verifier: string; state: string; nonce: string; returnTo: string } | null;
  store.del(PENDING_KEY);
  if (params.get("error")) throw new Error(`Sign-in failed: ${params.get("error_description") ?? params.get("error")}`);
  if (!pending || params.get("state") !== pending.state) throw new Error("Sign-in response did not match this browser session; try again.");
  const res = await fetch(cfg.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", code: params.get("code") ?? "", redirect_uri: redirectUri(), client_id: cfg.client_id, code_verifier: pending.verifier }),
  });
  if (!res.ok) throw new Error(`Token exchange failed (${res.status})`);
  const body = (await res.json()) as { id_token?: string };
  if (!body.id_token) throw new Error("The identity provider returned no ID token");
  if (decodeJwt(body.id_token)?.nonce !== pending.nonce) throw new Error("Sign-in token nonce mismatch; try again.");
  store.set(TOKEN_KEY, body.id_token);
  return pending.returnTo || "/projects";
}

export async function signOutOidc(cfg: Extract<AuthConfig, { mode: "oidc" }>): Promise<void> {
  const hint = store.get(TOKEN_KEY);
  clearIdToken();
  if (cfg.end_session_endpoint) {
    const q = new URLSearchParams({ post_logout_redirect_uri: `${window.location.origin}/login`, client_id: cfg.client_id });
    if (hint) q.set("id_token_hint", hint);
    window.location.assign(`${cfg.end_session_endpoint}?${q}`);
  } else {
    window.location.assign("/login");
  }
}
