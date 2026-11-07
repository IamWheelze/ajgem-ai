import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { api, getUserId, setUserId } from "./api";
import { useResource } from "./hooks";

export interface Me {
  id: string;
  email: string;
  name: string;
  organisation: { id: string; name: string };
}

interface SessionValue {
  me: Me | undefined;
  loading: boolean;
  signIn: (userId: string) => void;
  signOut: () => void;
}

const Ctx = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [uid, setUid] = useState<string | null>(getUserId());
  const me = useResource(() => (uid ? api.get<Me>("/me") : Promise.resolve(undefined)), [uid]);
  const value = useMemo<SessionValue>(
    () => ({
      me: me.error || me.data?.id !== uid ? undefined : me.data,
      // A freshly chosen user counts as loading until /me answers for that user.
      loading: me.loading || (uid !== null && !me.error && me.data?.id !== uid),
      signIn: (id) => {
        setUserId(id);
        setUid(id);
      },
      signOut: () => {
        setUserId(null);
        setUid(null);
      },
    }),
    [me.data, me.error, me.loading, uid],
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
