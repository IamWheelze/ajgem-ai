import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, getUserId, setUserId } from "./api";
import { clearIdToken, getAuthConfig, getIdToken, signOutOidc } from "./auth";
import { useResource } from "./hooks";

export interface Me {
  id: string;
  email: string;
  name: string;
  organisation: { id: string; name: string };
  mfa?: boolean;
  auth_mode?: "dev" | "oidc";
}

interface SessionValue {
  me: Me | undefined;
  loading: boolean;
  /** Development sign-in: act as this user id. */
  signIn: (userId: string) => void;
  /** After an OIDC callback stored a token. */
  refresh: () => void;
  signOut: () => void;
}

const Ctx = createContext<SessionValue | null>(null);
const currentCredential = () => getIdToken() ?? getUserId();

export function SessionProvider({ children }: { children: ReactNode }) {
  const [cred, setCred] = useState<string | null>(currentCredential());
  const me = useResource(async () => (cred ? { key: cred, me: await api.get<Me>("/me") } : undefined), [cred]);
  useEffect(() => {
    const out = () => setCred(currentCredential());
    window.addEventListener("ajgem:signed-out", out);
    return () => window.removeEventListener("ajgem:signed-out", out);
  }, []);
  const value = useMemo<SessionValue>(
    () => ({
      me: me.error || !cred || me.data?.key !== cred ? undefined : me.data.me,
      // A freshly chosen credential counts as loading until /me answers for it.
      loading: me.loading || (cred !== null && !me.error && me.data?.key !== cred),
      signIn: (id) => {
        setUserId(id);
        setCred(id);
      },
      refresh: () => setCred(currentCredential()),
      signOut: () => {
        setUserId(null);
        if (getIdToken()) {
          getAuthConfig().then((cfg) => (cfg.mode === "oidc" ? signOutOidc(cfg) : clearIdToken()));
        }
        clearIdToken();
        setCred(null);
      },
    }),
    [me.data, me.error, me.loading, cred],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("SessionProvider missing");
  return v;
}

/** Map area selection shared between the map workspace and the assistant (scope chips). */
export interface AreaSelection {
  bbox: [number, number, number, number];
  sampleKeys: string[];
  sampleIds: string[];
}
interface ScopeValue {
  area: AreaSelection | null;
  setArea: (a: AreaSelection | null) => void;
}
const ScopeCtx = createContext<ScopeValue | null>(null);

export function ScopeProvider({ children }: { children: ReactNode }) {
  const [area, setArea] = useState<AreaSelection | null>(null);
  const v = useMemo(() => ({ area, setArea }), [area]);
  return <ScopeCtx.Provider value={v}>{children}</ScopeCtx.Provider>;
}

export function useScope(): ScopeValue {
  const v = useContext(ScopeCtx);
  if (!v) throw new Error("ScopeProvider missing");
  return v;
}
