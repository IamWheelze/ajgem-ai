import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { completeLogin, getAuthConfig } from "../lib/auth";
import { useSession } from "../lib/session";
import { ErrorNotice, Loading } from "../components/ui";

/** Return point from the identity provider: exchange the code (PKCE), check state/nonce, then continue. */
export default function AuthCallback() {
  const nav = useNavigate();
  const { refresh } = useSession();
  const [err, setErr] = useState<unknown>();
  const ran = useRef(false);
  useEffect(() => {
    if (ran.current) return; // codes are single-use; StrictMode would run this twice
    ran.current = true;
    (async () => {
      const cfg = await getAuthConfig();
      if (cfg.mode !== "oidc") throw new Error("Organisation sign-in is not configured");
      const next = await completeLogin(cfg, window.location.search);
      refresh();
      nav(next, { replace: true });
    })().catch(setErr);
  }, [nav, refresh]);
  if (err) return <div className="login"><div className="card card-pad stack"><ErrorNotice error={err} title="Sign-in failed" /><a href="/login">Try again</a></div></div>;
  return <Loading what="Completing sign-in" />;
}
