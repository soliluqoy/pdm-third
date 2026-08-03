// PREDICT — login screen (shown when the server requires a password).
import { Activity, KeyRound, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

function BrandMark({ size = 40 }: { size?: number }) {
  return (
    <div
      className="rounded-xl bg-gradient-to-br from-accent via-teal-400 to-brand flex items-center justify-center text-white shadow-sm"
      style={{ width: size, height: size }}
    >
      <Activity size={size * 0.52} strokeWidth={2.6} />
    </div>
  );
}

export default function LoginPage() {
  const { authenticated, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Already logged in (or auth is disabled) → go to the app.
  if (authenticated) {
    const from = (location.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(password.trim());
      const from = (location.state as { from?: string } | null)?.from ?? "/";
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-dvh flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="card p-8">
          <div className="flex flex-col items-center text-center mb-7">
            <BrandMark />
            <h1 className="mt-4 text-xl font-bold text-slate-900 tracking-wide">PREDICT</h1>
            <p className="mt-1 text-sm text-muted">Car health & maintenance</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="label" htmlFor="password">Password</label>
              <div className="relative">
                <KeyRound
                  size={16}
                  className="absolute left-3 top-1/2 -translate-y-1/2 text-muted"
                />
                <input
                  id="password"
                  type="password"
                  autoFocus
                  autoComplete="current-password"
                  className="input !pl-9"
                  placeholder="Enter your password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={submitting}
                />
              </div>
            </div>

            {error && (
              <p className="text-sm text-bad bg-bad/5 border border-bad/15 rounded-xl px-3 py-2">
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={submitting || !password}>
              {submitting ? <Loader2 size={16} className="animate-spin" /> : null}
              {submitting ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}