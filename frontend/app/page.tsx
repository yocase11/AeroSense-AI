"use client";

import dynamic from "next/dynamic";
import {
  Activity, Bot, BookOpen, Database, FileText, Flame, LayoutDashboard, Map,
  LogOut, MessageSquare, RefreshCw, Search, Send, TrendingUp,
} from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, apiBase } from "@/lib/api";

const MapPanel = dynamic(() => import("@/components/MapPanel"), { ssr: false });

type Workspace =
  | "overview" | "map" | "explorer" | "forecast" | "agents"
  | "anomalies" | "knowledge" | "assistant" | "reports";
type City = { id: string; name: string; latitude: number; longitude: number };
type Observation = {
  id: number; city_id: string; source_id: string; station_id: number | null;
  metric: string; value: number; unit: string; observed_at: string;
  latitude: number | null; longitude: number | null; quality_status: string;
};
type HorizonBacktest = {
  horizon_hours: number;
  status: string;
  evaluation_windows: number;
  available_windows: number;
  minimum_windows: number;
  mae?: number;
  rmse?: number;
  r_squared?: number | null;
  mae_improvement_percent?: number | null;
  persistence_baseline?: { mae: number; rmse: number; r_squared: number | null };
  seasonal_naive_baseline?: { mae: number; rmse: number; r_squared: number | null };
  mae_improvement_vs_seasonal_naive_percent?: number | null;
};
type ForecastRecord = {
  target_at: string;
  value: number;
  horizon_hours: number;
  horizon_evaluation?: HorizonBacktest | null;
  feature_importance?: Array<{ feature: string; mean_absolute_shap: number }> | null;
};
type ForecastResult = {
  status: string;
  available_samples?: number;
  training_samples?: number;
  horizon_evaluations?: HorizonBacktest[];
  feature_importance?: Array<{ feature: string; mean_absolute_shap: number }>;
};
type OverviewCity = {
  city: City;
  measurements: Observation[];
  estimated_india_aqi: {
    value: number | null; category: string; validity_status: string;
    basis: string; source_id: string | null; station_count: number;
    sample_count: number; pollutants_used?: string[];
  };
  anomalies: Array<{ id: number; metric: string; severity: string; description: string; detected_at: string }>;
  alerts: Array<{ id: number; severity: string; title: string; created_at: string }>;
  source_health: Array<{ id: string; name: string; configured: boolean; last_success_at: string | null; last_error: string | null }>;
};
type Overview = { generated_at: string; cities: OverviewCity[] };
type EventRecord = {
  id: number; city_id: string; source_id: string; event_type: string; title: string;
  description: string; url: string | null; observed_at: string;
  latitude: number | null; longitude: number | null; confidence: string | null;
};
type AuthUser = { id: number; email: string; role: string };

const navigation: Array<{ id: Workspace; label: string; icon: typeof Activity; section: string }> = [
  { id: "overview", label: "Overview", icon: LayoutDashboard, section: "Monitor" },
  { id: "map", label: "Live map", icon: Map, section: "Monitor" },
  { id: "explorer", label: "Data explorer", icon: Database, section: "Monitor" },
  { id: "forecast", label: "Forecasting", icon: TrendingUp, section: "Intelligence" },
  { id: "agents", label: "Agent center", icon: Bot, section: "Intelligence" },
  { id: "anomalies", label: "Events & anomalies", icon: Flame, section: "Intelligence" },
  { id: "knowledge", label: "Knowledge center", icon: BookOpen, section: "Research" },
  { id: "assistant", label: "Investigation", icon: MessageSquare, section: "Research" },
  { id: "reports", label: "Alerts & reports", icon: FileText, section: "Operations" },
];

function valueLabel(value: number | null | undefined, unit = "") {
  return value == null ? "Unavailable" : `${Number(value).toFixed(1)} ${unit}`.trim();
}

function freshness(value: string | null | undefined) {
  if (!value) return "No successful fetch";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60_000));
  return minutes < 60 ? `${minutes}m ago` : `${Math.round(minutes / 60)}h ago`;
}

function ErrorNotice({ error }: { error: Error | null }) {
  return error ? <div className="notice error" role="alert">{error.message}</div> : null;
}

export default function Home() {
  return <AuthGate />;
}

function AuthGate() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<"loading" | "signed_out" | "signed_in" | "error">("loading");
  const [error, setError] = useState("");
  const [authMode, setAuthMode] = useState<"sign_in" | "sign_up">("sign_in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    let active = true;
    const handleUnauthorized = () => {
      queryClient.clear();
      setUser(null);
      setStatus("signed_out");
    };
    window.addEventListener("aerosense:unauthorized", handleUnauthorized);
    async function loadSession() {
      try {
        await api<{ csrf_token: string }>("/auth/csrf");
        const currentUser = await api<AuthUser>("/auth/me");
        if (active) {
          setUser(currentUser);
          setStatus("signed_in");
        }
      } catch (reason) {
        if (!active) return;
        if (reason instanceof Error && reason.message === "Authentication required.") {
          setStatus("signed_out");
        } else {
          setError(reason instanceof Error ? reason.message : "Unable to connect to the API.");
          setStatus("error");
        }
      }
    }
    void loadSession();
    return () => {
      active = false;
      window.removeEventListener("aerosense:unauthorized", handleUnauthorized);
    };
  }, [queryClient]);

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (authMode === "sign_up" && password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const currentUser = await api<AuthUser>(
        authMode === "sign_up" ? "/auth/register" : "/auth/login",
        {
          method: "POST",
          body: JSON.stringify({ email, password }),
        },
      );
      queryClient.clear();
      setUser(currentUser);
      setStatus("signed_in");
      setPassword("");
      setConfirmPassword("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to continue.");
    } finally {
      setSubmitting(false);
    }
  }

  async function signOut() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-out request failed.");
    } finally {
      queryClient.clear();
      setUser(null);
      setStatus("signed_out");
    }
  }

  if (status === "loading") return <main className="auth-page"><div className="card">Checking your session…</div></main>;
  if (status === "signed_in" && user) {
    return <Dashboard user={user} onLogout={() => void signOut()} />;
  }

  return (
    <main className="auth-page">
      <form className="card auth-card" onSubmit={(event) => void submitAuth(event)}>
        <div className="brand"><span className="brand-mark"><Activity size={18} /></span>AeroSense AI</div>
        <h1>{authMode === "sign_up" ? "Create account" : "Sign in"}</h1>
        <p className="muted">
          {authMode === "sign_up"
            ? "New accounts start with viewer access."
            : "Sign in or create an account to access AeroSense."}
        </p>
        {error && <div className="notice error" role="alert">{error}</div>}
        <label className="caption" htmlFor="email">Email</label>
        <input id="email" className="input auth-input" type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} />
        <label className="caption" htmlFor="password">Password</label>
        <input id="password" className="input auth-input" type="password" autoComplete={authMode === "sign_up" ? "new-password" : "current-password"} minLength={authMode === "sign_up" ? 12 : undefined} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} />
        {authMode === "sign_up" && (
          <>
            <label className="caption" htmlFor="confirm-password">Confirm password</label>
            <input id="confirm-password" className="input auth-input" type="password" autoComplete="new-password" minLength={12} maxLength={128} required value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} />
          </>
        )}
        <button className="button button-primary" type="submit" disabled={submitting}>
          {submitting
            ? authMode === "sign_up" ? "Creating account…" : "Signing in…"
            : authMode === "sign_up" ? "Create account" : "Sign in"}
        </button>
        <button
          className="button"
          type="button"
          disabled={submitting}
          onClick={() => {
            setAuthMode(authMode === "sign_up" ? "sign_in" : "sign_up");
            setError("");
            setConfirmPassword("");
          }}
        >
          {authMode === "sign_up" ? "Already have an account? Sign in" : "Create account"}
        </button>
      </form>
    </main>
  );
}

function Dashboard({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [workspace, setWorkspace] = useState<Workspace>("overview");
  const [cityId, setCityId] = useState("");
  const [question, setQuestion] = useState("");
  const [investigationId, setInvestigationId] = useState<number | null>(null);
  const [searchText, setSearchText] = useState("");
  const [searchResults, setSearchResults] = useState<Array<Record<string, unknown>>>([]);
  const [liveConnected, setLiveConnected] = useState(false);
  const queryClient = useQueryClient();

  useEffect(() => {
    const stream = new EventSource(`${apiBase()}/dashboard/live-events`, { withCredentials: true });
    const refreshData = () => {
      setLiveConnected(true);
      queryClient.invalidateQueries();
    };
    stream.onopen = () => setLiveConnected(true);
    stream.onerror = () => setLiveConnected(false);
    [
      "observation",
      "event",
      "anomaly",
      "alert",
      "observation_ingested",
      "environmental_event",
      "ingestion_completed",
      "investigation_completed",
    ].forEach((eventName) => stream.addEventListener(eventName, refreshData));
    return () => stream.close();
  }, [queryClient]);

  const citiesQuery = useQuery({
    queryKey: ["cities"],
    queryFn: () => api<City[]>("/cities"),
  });
  const cities = citiesQuery.data ?? [];
  const selectedCity = cityId || cities[0]?.id || "";
  const overviewQuery = useQuery({
    queryKey: ["overview", selectedCity],
    queryFn: () => api<Overview>(`/dashboard/overview${selectedCity ? `?city_id=${encodeURIComponent(selectedCity)}` : ""}`),
    enabled: Boolean(selectedCity),
    refetchInterval: 30_000,
  });
  const overview = overviewQuery.data?.cities[0];
  const refresh = () => queryClient.invalidateQueries();

  const selectedTitle = navigation.find((item) => item.id === workspace)?.label ?? "Overview";
  const navSections = [...new Set(navigation.map((item) => item.section))];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark"><Activity size={18} /></span>AeroSense AI</div>
        {navSections.map((section) => (
          <div key={section}>
            <div className="nav-label">{section}</div>
            {navigation.filter((item) => item.section === section).map((item) => {
              const Icon = item.icon;
              return (
                <button key={item.id} className={`nav-button ${workspace === item.id ? "active" : ""}`} onClick={() => setWorkspace(item.id)}>
                  <Icon size={17} />{item.label}
                </button>
              );
            })}
          </div>
        ))}
        <div className="footer-note">Source-backed environmental intelligence. Missing data stays missing; modelled and station readings are kept distinct.</div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div>
            <p className="eyebrow">ENVIRONMENTAL INTELLIGENCE PLATFORM</p>
            <h1>{selectedTitle}</h1>
          </div>
          <div className="toolbar">
            <label className="caption" htmlFor="city">City</label>
            <select id="city" className="select" value={selectedCity} onChange={(event) => setCityId(event.target.value)}>
              {cities.map((city) => <option key={city.id} value={city.id}>{city.name}</option>)}
              {!cities.length && <option value="">No cities seeded</option>}
            </select>
            <span className={`status ${liveConnected ? "good" : "warn"}`}>{liveConnected ? "live stream connected" : "reconnecting"}</span>
            <button className="button" onClick={refresh} aria-label="Refresh dashboard"><RefreshCw size={16} /></button>
            <span className="caption">{user.email} · {user.role}</span>
            <button className="button" onClick={onLogout} aria-label="Sign out"><LogOut size={16} /></button>
          </div>
        </header>
        {citiesQuery.isError && <ErrorNotice error={citiesQuery.error} />}
        {overviewQuery.isError && <ErrorNotice error={overviewQuery.error} />}
        {!cities.length && !citiesQuery.isLoading && (
          <div className="notice">Backend data is not initialized. Run migrations and seed the city/source reference data.</div>
        )}
        {workspace === "overview" && overview && <OverviewWorkspace data={overview} onNavigate={setWorkspace} />}
        {workspace === "map" && <MapWorkspace cityId={selectedCity} />}
        {workspace === "explorer" && <ExplorerWorkspace cityId={selectedCity} />}
        {workspace === "forecast" && <ForecastWorkspace cityId={selectedCity} />}
        {workspace === "agents" && <AgentsWorkspace cityId={selectedCity} onInvestigate={(id) => { setInvestigationId(id); setWorkspace("assistant"); }} />}
        {workspace === "anomalies" && <AnomaliesWorkspace cityId={selectedCity} />}
        {workspace === "knowledge" && <KnowledgeWorkspace searchText={searchText} setSearchText={setSearchText} searchResults={searchResults} setSearchResults={setSearchResults} />}
        {workspace === "assistant" && (
          <AssistantWorkspace
            cityId={selectedCity}
            question={question}
            setQuestion={setQuestion}
            investigationId={investigationId}
            setInvestigationId={setInvestigationId}
          />
        )}
        {workspace === "reports" && <ReportsWorkspace cityId={selectedCity} />}
      </main>
    </div>
  );
}

function OverviewWorkspace({ data, onNavigate }: { data: OverviewCity; onNavigate: (workspace: Workspace) => void }) {
  const currentPm = data.measurements.filter((item) => item.metric === "PM2.5" || item.metric === "PM10");
  const temp = data.measurements.find((item) => item.metric === "Temperature");
  const aqi = data.estimated_india_aqi;
  const aqiDetail = aqi.basis === "citywide_ground_24h"
    ? `OpenAQ citywide · 24h · ${aqi.station_count} stations`
    : aqi.basis === "modeled_current"
      ? "Open-Meteo modeled · current estimate"
      : "No recent city PM data";
  return (
    <>
      <section className="grid">
        <MetricCard label="Estimated city AQI" value={aqi.value == null ? "Unavailable" : `${aqi.value} · ${aqi.category}`} detail={aqiDetail} />
        <MetricCard label="PM2.5 latest" value={valueLabel(currentPm.find((item) => item.metric === "PM2.5")?.value, "µg/m³")} detail={currentPm.find((item) => item.metric === "PM2.5")?.source_id ?? "No station reading"} />
        <MetricCard label="PM10 latest" value={valueLabel(currentPm.find((item) => item.metric === "PM10")?.value, "µg/m³")} detail={currentPm.find((item) => item.metric === "PM10")?.source_id ?? "No station reading"} />
        <MetricCard label="Temperature" value={valueLabel(temp?.value, temp?.unit)} detail={temp ? `Observed ${freshness(temp.observed_at)}` : "No weather reading"} />
      </section>
      <div className="grid-2">
        <section className="card">
          <h2>Latest measurements</h2>
          <ObservationTable rows={data.measurements} />
        </section>
        <section className="card">
          <h2>Data health</h2>
          {!data.source_health.length && <p className="muted">No provider attempts recorded yet.</p>}
          <div className="list">
            {data.source_health.map((source) => (
              <div className="list-item" key={source.id}>
                <div className="toolbar" style={{ justifyContent: "space-between" }}>
                  <strong>{source.name}</strong>
                  <span className={`status ${source.last_error ? "bad" : source.configured ? "good" : "warn"}`}>
                    {source.last_error ? "degraded" : source.configured ? "configured" : "not configured"}
                  </span>
                </div>
                <div className="caption">Last success: {freshness(source.last_success_at)}</div>
                {source.last_error && <div className="caption">{source.last_error}</div>}
              </div>
            ))}
          </div>
        </section>
      </div>
      <section className="grid-2">
        <div className="card">
          <h2>Open alerts</h2>
          {!data.alerts.length && <p className="muted">No active alerts in stored data.</p>}
          {data.alerts.map((alert) => <div className="list-item" key={alert.id}><span className="status warn">{alert.severity}</span><strong> {alert.title}</strong><div className="caption">{freshness(alert.created_at)}</div></div>)}
          <button className="button" onClick={() => onNavigate("reports")}>View alert history</button>
        </div>
        <div className="card">
          <h2>Recent anomalies</h2>
          {!data.anomalies.length && <p className="muted">No anomalies recorded for this city.</p>}
          {data.anomalies.map((item) => <div className="list-item" key={item.id}><span className="status warn">{item.severity}</span><strong> {item.metric}</strong><p>{item.description}</p></div>)}
          <button className="button" onClick={() => onNavigate("anomalies")}>Investigate signals</button>
        </div>
      </section>
      <p className="footer-note">City AQI is an estimate using India AQI PM breakpoints. It prefers a citywide 24-hour OpenAQ average when coverage is adequate; otherwise it uses current Open-Meteo modeled PM concentrations. The modeled estimate is not a ground-station reading or an official CPCB 24-hour AQI.</p>
    </>
  );
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="card"><div className="metric-label">{label}</div><div className="metric-value">{value}</div><div className="caption">{detail}</div></div>;
}

function ObservationTable({ rows }: { rows: Observation[] }) {
  if (!rows.length) return <p className="muted">No observations stored yet.</p>;
  return (
    <div className="table-wrap"><table><thead><tr><th>Metric</th><th>Value</th><th>Source / station</th><th>Observed</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={`${row.source_id}-${row.station_id}-${row.metric}`}><td>{row.metric}</td><td>{valueLabel(row.value, row.unit)}</td><td>{row.source_id}{row.station_id ? ` / #${row.station_id}` : ""}</td><td>{freshness(row.observed_at)}</td></tr>)}</tbody>
    </table></div>
  );
}

function MapWorkspace({ cityId }: { cityId: string }) {
  const stations = useQuery({ queryKey: ["stations", cityId], queryFn: () => api<Array<{ id: number; name: string; latitude: number | null; longitude: number | null; source_id: string }>>(`/air-quality/stations?city_id=${encodeURIComponent(cityId)}`), enabled: Boolean(cityId) });
  const events = useQuery({ queryKey: ["events", cityId], queryFn: () => api<EventRecord[]>(`/events?city_id=${encodeURIComponent(cityId)}&limit=200`), enabled: Boolean(cityId) });
  const points = useMemo(() => [
    ...(stations.data ?? []).filter((row) => row.latitude != null && row.longitude != null).map((row) => ({ id: row.id, latitude: row.latitude as number, longitude: row.longitude as number, title: row.name, detail: row.source_id, kind: "station" as const })),
    ...(events.data ?? []).filter((row) => row.latitude != null && row.longitude != null).map((row) => ({ id: row.id, latitude: row.latitude as number, longitude: row.longitude as number, title: row.title, detail: `${row.source_id} · ${new Date(row.observed_at).toLocaleString()}`, kind: "event" as const })),
  ], [stations.data, events.data]);
  return <section className="card"><h2>Stations and environmental events</h2><p className="muted">Map contains only stored, geolocated source records. Empty map means no geospatial data is available.</p><ErrorNotice error={stations.error ?? events.error} /><MapPanel points={points} /><p className="caption">{points.length} mapped records · OpenStreetMap contributors</p></section>;
}

function ExplorerWorkspace({ cityId }: { cityId: string }) {
  const sources = useQuery({ queryKey: ["sources"], queryFn: () => api<Array<Record<string, unknown>>>("/sources") });
  const runs = useQuery({ queryKey: ["runs", cityId], queryFn: () => api<Array<Record<string, unknown>>>(`/sources/ingestion-history?city_id=${encodeURIComponent(cityId)}&limit=100`) });
  return <>
    <section className="card"><h2>Provider availability and attribution</h2><ErrorNotice error={sources.error} /><div className="table-wrap"><table><thead><tr><th>Source</th><th>Status</th><th>Attribution</th><th>Last success</th></tr></thead><tbody>
      {(sources.data ?? []).map((source) => <tr key={String(source.id)}><td>{String(source.name)}</td><td><span className={`status ${source.configured ? "good" : "warn"}`}>{source.configured ? "configured" : "not configured"}</span></td><td>{String(source.attribution)}</td><td>{freshness(source.last_success_at as string | null)}</td></tr>)}
    </tbody></table></div></section>
    <section className="card"><h2>Ingestion history</h2><ErrorNotice error={runs.error} /><DataTable rows={runs.data ?? []} columns={["source_id", "status", "records_seen", "records_accepted", "records_rejected", "started_at", "error"]} /></section>
  </>;
}

function ForecastWorkspace({ cityId }: { cityId: string }) {
  const queryClient = useQueryClient();
  const [stationId, setStationId] = useState<number | null>(null);
  const stations = useQuery({
    queryKey: ["stations", cityId],
    queryFn: () => api<Array<{ id: number; name: string; source_id: string }>>(`/air-quality/stations?city_id=${encodeURIComponent(cityId)}&source_id=openaq`),
    enabled: Boolean(cityId),
  });
  useEffect(() => {
    if (stationId !== null && stations.data && !stations.data.some((station) => station.id === stationId)) {
      setStationId(null);
    }
  }, [cityId, stationId, stations.data]);
  const selectedStation = stationId ?? stations.data?.[0]?.id ?? null;
  const stationQuery = selectedStation == null ? "" : `&station_id=${selectedStation}`;
  const forecast = useQuery({
    queryKey: ["forecasts", cityId, selectedStation],
    queryFn: () => api<ForecastRecord[]>(
      `/forecasts?city_id=${encodeURIComponent(cityId)}&metric=PM2.5${stationQuery}`,
    ),
    enabled: selectedStation != null,
  });
  const actual = useQuery({
    queryKey: ["forecast-actual", cityId, selectedStation],
    queryFn: () => api<Observation[]>(
      `/air-quality/history?city_id=${encodeURIComponent(cityId)}&metric=PM2.5&hours=720&limit=1000${stationQuery}`,
    ),
    enabled: selectedStation != null,
  });
  const evaluation = useQuery({
    queryKey: ["model-evaluation", cityId, selectedStation],
    queryFn: () => api<Array<Record<string, unknown>>>(
      `/models/performance?city_id=${encodeURIComponent(cityId)}${stationQuery}`,
    ),
    enabled: selectedStation != null,
  });
  const mutation = useMutation({
    mutationFn: () => api<ForecastResult>(
      `/forecasts/generate?city_id=${encodeURIComponent(cityId)}&metric=PM2.5${stationQuery}`,
      { method: "POST" },
    ),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ["forecasts", cityId] }); queryClient.invalidateQueries({ queryKey: ["model-evaluation", cityId] }); },
  });
  const chartRows = [
    ...(actual.data ?? []).map((row) => ({ time: row.observed_at, actual: row.value, predicted: null as number | null })),
    ...(forecast.data ?? []).map((row) => ({ time: row.target_at, actual: null as number | null, predicted: row.value })),
  ].sort((left, right) => left.time.localeCompare(right.time));
  const horizonEvaluations = mutation.data?.horizon_evaluations
    ?? (forecast.data ?? []).flatMap((row) => (
      row.horizon_evaluation ? [row.horizon_evaluation] : []
    ));
  const horizonRows = horizonEvaluations.map((item) => ({
    horizon_hours: item.horizon_hours,
    status: item.status,
    evaluation_windows: item.evaluation_windows,
    available_windows: item.available_windows,
    mae: item.mae,
    rmse: item.rmse,
    persistence_mae: item.persistence_baseline?.mae,
    seasonal_naive_mae: item.seasonal_naive_baseline?.mae,
    mae_improvement_percent: item.mae_improvement_percent,
    mae_improvement_vs_seasonal_naive_percent: item.mae_improvement_vs_seasonal_naive_percent,
  }));
  const oneStepRows = (evaluation.data ?? []).filter(
    (row) => Number(row.horizon_hours) === 1 && row.evaluation_type === "one_step_holdout",
  );
  const featureImportance = mutation.data?.feature_importance
    ?? forecast.data?.find((row) => row.feature_importance)?.feature_importance
    ?? [];
  return <>
    <section className="card">
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <div>
          <h2>PM2.5 forecasts</h2>
          <p className="muted">One OpenAQ ground station per run; observations from different stations are never mixed.</p>
        </div>
        <div className="toolbar">
          <select className="select" value={selectedStation ?? ""} onChange={(event) => setStationId(event.target.value ? Number(event.target.value) : null)}>
            <option value="">Select a station</option>
            {(stations.data ?? []).map((station) => <option key={station.id} value={station.id}>{station.name} · {station.source_id}</option>)}
          </select>
          <button className="button button-primary" disabled={mutation.isPending || selectedStation == null} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Training…" : "Train / forecast"}
          </button>
        </div>
      </div>
      {stations.isError && <ErrorNotice error={stations.error} />}
      <ErrorNotice error={mutation.error ?? forecast.error ?? actual.error} />
      {mutation.data && <div className="notice">{mutation.data.status} · {mutation.data.available_samples ?? mutation.data.training_samples ?? 0} valid samples in the training window</div>}
      <p className="caption">{actual.data?.length ?? 0} actual records in the last 30 days · {forecast.data?.length ?? 0} saved forecast horizons</p>
      {!chartRows.length
        ? <div className="notice">No history or forecast is stored for this station yet. A forecast needs at least 48 valid station observations in the last 90 days.</div>
        : <div className="chart"><ResponsiveContainer width="100%" height="100%"><LineChart data={chartRows}><CartesianGrid stroke="#263743" /><XAxis dataKey="time" tick={{ fill: "#91a5b1", fontSize: 10 }} /><YAxis tick={{ fill: "#91a5b1" }} /><Tooltip /><Line dataKey="actual" name="Observed" stroke="#6ad8bb" connectNulls={false} dot={false} /><Line dataKey="predicted" name="Forecast" stroke="#f4b65f" connectNulls={false} dot /></LineChart></ResponsiveContainer></div>}
    </section>
    <section className="card">
      <h2>Rolling-origin horizon backtest</h2>
      <p className="muted">Each horizon is recursively forecast from held-out origins and compared with last-observation persistence and the previous-day same-time seasonal baseline. All scores use the same target windows and require at least 10 valid pairs.</p>
      {horizonRows.length
        ? <DataTable rows={horizonRows} columns={["horizon_hours", "status", "evaluation_windows", "available_windows", "mae", "rmse", "persistence_mae", "seasonal_naive_mae", "mae_improvement_percent", "mae_improvement_vs_seasonal_naive_percent"]} />
        : <p className="muted">No saved horizon backtest yet. Run Train / forecast after enough station history is available.</p>}
    </section>
    <section className="card">
      <h2>SHAP feature importance</h2>
      <p className="muted">Mean absolute TreeSHAP values for the trained model. Current inputs are PM2.5 lags and time-of-day/day-of-week features; weather is not an input and SHAP does not establish cause.</p>
      {featureImportance.length
        ? <DataTable rows={featureImportance} columns={["feature", "mean_absolute_shap"]} />
        : <p className="muted">Feature explanations are available after a successful forecast training run.</p>}
    </section>
    <section className="card">
      <h2>One-step model check</h2>
      <p className="muted">This chronological holdout checks the next observation only; it is separate from the multi-hour horizon backtests above.</p>
      <DataTable rows={oneStepRows} columns={["metric", "model", "version", "horizon_hours", "sample_count", "mae", "rmse", "r_squared", "persistence_baseline", "seasonal_naive_baseline", "evaluated_at"]} />
    </section>
  </>;
}

function AgentsWorkspace({ cityId, onInvestigate }: { cityId: string; onInvestigate: (id: number) => void }) {
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api<Array<{ name: string; role: string }>>("/agents") });
  const tasks = useQuery({ queryKey: ["agent-tasks"], queryFn: () => api<Array<Record<string, unknown>>>("/agents/tasks?limit=100"), refetchInterval: 5000 });
  const mutation = useMutation({
    mutationFn: (question: string) => api<{ investigation_id: number }>("/agents/investigate", { method: "POST", body: JSON.stringify({ question, city_id: cityId }) }),
    onSuccess: (result) => onInvestigate(result.investigation_id),
  });
  const [text, setText] = useState("");
  function submit(event: FormEvent) { event.preventDefault(); if (text.trim()) mutation.mutate(text.trim()); }
  return <>
    <section className="card"><h2>Investigation supervisor</h2><form onSubmit={submit} className="toolbar"><input className="input" style={{ flex: 1 }} value={text} onChange={(event) => setText(event.target.value)} placeholder="Ask a source-grounded environmental question" /><button className="button button-primary" disabled={mutation.isPending}><Send size={15} /> Start</button></form><ErrorNotice error={mutation.error} /><p className="caption">Specialists query persisted evidence in parallel; the Gemini synthesis runs only when configured.</p></section>
    <section className="grid">{(agents.data ?? []).map((agent) => <div className="card" key={agent.name}><span className="status">{agent.name.replaceAll("_", " ")}</span><p>{agent.role}</p><small className="muted">{(tasks.data ?? []).find((task) => task.agent === agent.name)?.status as string ?? "idle"}</small></div>)}</section>
    <section className="card"><h2>Recent task execution</h2><DataTable rows={tasks.data ?? []} columns={["investigation_id", "agent", "status", "started_at", "completed_at"]} /></section>
  </>;
}

function AnomaliesWorkspace({ cityId }: { cityId: string }) {
  const anomalies = useQuery({ queryKey: ["anomalies", cityId], queryFn: () => api<Array<Record<string, unknown>>>(`/anomalies?city_id=${encodeURIComponent(cityId)}`) });
  const events = useQuery({ queryKey: ["events", cityId], queryFn: () => api<EventRecord[]>(`/events?city_id=${encodeURIComponent(cityId)}&limit=100`) });
  return <div className="grid-2">
    <section className="card">
      <h2>Recorded anomalies</h2>
      <ErrorNotice error={anomalies.error} />
      {anomalies.data?.length
        ? <DataTable rows={anomalies.data} columns={["metric", "severity", "score", "description", "detected_at"]} />
        : !anomalies.isLoading && <p className="muted">No anomaly records are stored for this city. This does not imply the absence of risk; sparse station history can leave the detector without a usable baseline.</p>}
    </section>
    <section className="card">
      <h2>Satellite and news context</h2>
      <ErrorNotice error={events.error} />
      {events.data?.length
        ? <div className="list">{events.data.map((event) => <div className="list-item" key={event.id}><span className="status">{event.source_id}</span><strong> {event.title}</strong><p>{event.description}</p><small>{freshness(event.observed_at)} · {event.event_type}</small>{event.url && <p><a href={event.url} target="_blank" rel="noreferrer">Source reference</a></p>}</div>)}</div>
        : !events.isLoading && <p className="muted">No source event records are available for this city. Nearby news or map features are context, not proof of cause.</p>}
    </section>
  </div>;
}

function KnowledgeWorkspace({ searchText, setSearchText, searchResults, setSearchResults }: {
  searchText: string; setSearchText: (value: string) => void;
  searchResults: Array<Record<string, unknown>>;
  setSearchResults: (results: Array<Record<string, unknown>>) => void;
}) {
  const documents = useQuery({ queryKey: ["documents"], queryFn: () => api<Array<Record<string, unknown>>>("/knowledge/documents") });
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("Choose a PDF, TXT, or Markdown document.");
      const body = new FormData(); body.set("title", title || file.name); body.set("file", file);
      return api<Record<string, unknown>>("/knowledge/documents", { method: "POST", body });
    },
    onSuccess: () => documents.refetch(),
  });
  const search = useMutation({
    mutationFn: () => api<{ results: Array<Record<string, unknown>> }>("/knowledge/search", { method: "POST", body: JSON.stringify({ query: searchText }) }),
    onSuccess: (data) => setSearchResults(data.results ?? []),
  });
  return <>
    <section className="card">
      <h2>Document library</h2>
      <div className="toolbar"><input className="input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Document title" /><input type="file" accept=".pdf,.txt,.md,application/pdf,text/plain,text/markdown" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /><button className="button" disabled={upload.isPending} onClick={() => upload.mutate()}>Ingest document</button></div>
      <ErrorNotice error={upload.error} />
      <p className="caption">Uploads require ALLOW_DOCUMENT_UPLOADS and accept vetted PDF, TXT, or Markdown files up to 5 MB.</p>
      {documents.data?.length
        ? <DataTable rows={documents.data} columns={["title", "source_url", "created_at"]} />
        : !documents.isLoading && <p className="muted">No documents are indexed yet. Add a trusted source document to enable cited document retrieval; the system does not create placeholder documents.</p>}
    </section>
    <section className="card">
      <h2>Research search</h2>
      <form className="toolbar" onSubmit={(event) => { event.preventDefault(); search.mutate(); }}><input className="input" style={{ flex: 1 }} value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="Search indexed environmental documents" /><button className="button button-primary"><Search size={15} /> Search</button></form>
      <ErrorNotice error={search.error} />
      <div className="list">{searchResults.map((item, index) => <div className="list-item" key={`${String(item.document_id)}-${String(item.chunk_index)}-${index}`}><strong>{String(item.title)}</strong><p>{String(item.text)}</p><span className="caption">Page {String(item.page_number ?? "n/a")} · citation {String(item.document_id)}</span></div>)}</div>
      {search.isSuccess && !searchResults.length && <p className="muted">No matching passages were found in indexed documents.</p>}
    </section>
  </>;
}

function AssistantWorkspace({ cityId, question, setQuestion, investigationId, setInvestigationId }: {
  cityId: string; question: string; setQuestion: (value: string) => void;
  investigationId: number | null; setInvestigationId: (id: number | null) => void;
}) {
  const [liveProgress, setLiveProgress] = useState<Record<string, unknown> | null>(null);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => api<{ investigation_id: number }>("/assistant/chat", { method: "POST", body: JSON.stringify({ question, city_id: cityId }) }),
    onSuccess: (data) => { setInvestigationId(data.investigation_id); setLiveProgress(null); },
  });
  const status = useQuery({
    queryKey: ["investigation", investigationId],
    queryFn: () => api<Record<string, unknown>>(`/agents/investigations/${investigationId}`),
    enabled: investigationId != null,
    refetchInterval: (query) => ["completed", "failed"].includes(String(query.state.data?.status)) ? false : 2000,
  });
  useEffect(() => {
    if (investigationId == null) return;
    const stream = new EventSource(`${apiBase()}/agents/investigations/${investigationId}/events`);
    stream.addEventListener("progress", (message) => {
      try {
        const progress = JSON.parse((message as MessageEvent<string>).data) as Record<string, unknown>;
        setLiveProgress(progress);
        queryClient.invalidateQueries({ queryKey: ["investigation", investigationId] });
        if (progress.status === "completed" || progress.status === "failed") stream.close();
      } catch {
        stream.close();
      }
    });
    return () => stream.close();
  }, [investigationId, queryClient]);
  function submit(event: FormEvent) { event.preventDefault(); if (question.trim()) mutation.mutate(); }
  const investigation = status.data;
  return <>
    <section className="card"><h2>Evidence-based investigation</h2><p className="muted">Answers use stored provider data and document citations. News and geographic proximity are context, not proof of cause.</p><form onSubmit={submit}><textarea className="textarea" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Why did PM2.5 change? What evidence is available?" /><div className="toolbar" style={{ marginTop: 10 }}><button className="button button-primary" disabled={mutation.isPending}><Send size={15} /> Investigate</button>{investigationId && <span className="status">{String(liveProgress?.status ?? investigation?.status ?? "queued")}</span>}</div></form><ErrorNotice error={mutation.error ?? status.error} /></section>
    {investigation && <section className="card">
      <h2>Investigation {investigationId}</h2>
      <p>{String(investigation.answer ?? "Specialists are gathering stored evidence…")}</p>
      <h3>Agent tasks</h3>
      <DataTable rows={(investigation.tasks as Array<Record<string, unknown>>) ?? []} columns={["agent", "status", "completed_at"]} />
      <h3>Evidence records</h3>
      {(investigation.evidence as Array<Record<string, unknown>> | undefined)?.length
        ? <DataTable rows={investigation.evidence as Array<Record<string, unknown>>} columns={["observation_id", "event_id", "document_id", "metric", "title", "source_id", "observed_at"]} />
        : <p className="muted">No matching stored evidence was found. The answer cannot establish a cause without supporting records.</p>}
    </section>}
  </>;
}

function ReportsWorkspace({ cityId }: { cityId: string }) {
  const queryClient = useQueryClient();
  const alerts = useQuery({ queryKey: ["alerts", cityId], queryFn: () => api<Array<Record<string, unknown>>>(`/alerts?city_id=${encodeURIComponent(cityId)}`) });
  const reports = useQuery({ queryKey: ["reports"], queryFn: () => api<Array<Record<string, unknown>>>("/reports") });
  const ack = useMutation({ mutationFn: (id: number) => api(`/alerts/${id}/acknowledge`, { method: "POST" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alerts", cityId] }) });
  const generate = useMutation({ mutationFn: (kind: string) => api<Record<string, unknown>>(`/reports/generate?city_id=${encodeURIComponent(cityId)}&report_type=${kind}`, { method: "POST" }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["reports"] }) });
  return <>
    <section className="card">
      <div className="toolbar" style={{ justifyContent: "space-between" }}><h2>Alerts</h2><span className="caption">Only persisted threshold/anomaly records are shown.</span></div>
      <ErrorNotice error={alerts.error} />
      {alerts.data?.length
        ? <DataTable rows={alerts.data} columns={["title", "severity", "status", "created_at"]} />
        : !alerts.isLoading && <p className="muted">No alerts are stored for this city. Thresholds are not inferred when required observations are missing.</p>}
      <div className="list">{(alerts.data ?? []).filter((alert) => alert.status === "active").map((alert) => <div className="list-item" key={String(alert.id)}><strong>{String(alert.title)}</strong><button className="button" onClick={() => ack.mutate(Number(alert.id))}>Acknowledge</button></div>)}</div>
    </section>
    <section className="card">
      <div className="toolbar"><h2 style={{ marginRight: "auto" }}>Reports</h2><button className="button" onClick={() => generate.mutate("daily")}>Generate daily</button><button className="button" onClick={() => generate.mutate("weekly")}>Generate weekly</button></div>
      <ErrorNotice error={generate.error ?? reports.error} />
      {(reports.data ?? []).length
        ? <div className="list">{(reports.data ?? []).map((report) => <div className="list-item" key={String(report.id)}><strong>{String(report.title)}</strong><span className="caption"> · {freshness(report.created_at as string)}</span><a className="button" style={{ marginLeft: 12 }} href={`${apiBase()}/reports/${String(report.id)}/pdf`}>Download PDF</a></div>)}</div>
        : !reports.isLoading && <p className="muted">No generated reports yet. Reports use currently stored observations, alerts, and events.</p>}
    </section>
  </>;
}

function DataTable({ rows, columns }: { rows: Array<Record<string, unknown>>; columns: string[] }) {
  if (!rows.length) return <p className="muted">No stored records available.</p>;
  return <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={String(row.id ?? row.investigation_id ?? index)}>{columns.map((column) => <td key={column}>{row[column] == null ? "—" : typeof row[column] === "object" ? JSON.stringify(row[column]) : String(row[column])}</td>)}</tr>)}</tbody></table></div>;
}
