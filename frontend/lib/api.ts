export type JsonRecord = Record<string, unknown>;

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

let refreshInFlight: Promise<boolean> | null = null;

function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const cookie = document.cookie
    .split("; ")
    .find((item) => item.startsWith("aerosense_csrf="));
  return cookie ? decodeURIComponent(cookie.slice("aerosense_csrf=".length)) : null;
}

async function refreshSession(): Promise<boolean> {
  const token = csrfToken();
  if (!token) return false;
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": token },
      cache: "no-store",
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

function requestOptions(init?: RequestInit): RequestInit {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init?.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  return { ...init, credentials: "include", headers, cache: "no-store" };
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response = await fetch(`${API_BASE}${path}`, requestOptions(init));
  if (
    response.status === 401 &&
    ![
      "/auth/login",
      "/auth/register",
      "/auth/refresh",
      "/auth/logout",
      "/auth/csrf",
    ].includes(path)
  ) {
    if (await refreshSession()) {
      response = await fetch(`${API_BASE}${path}`, requestOptions(init));
    }
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new Event("aerosense:unauthorized"));
    }
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as
      | { detail?: string | { status?: string } }
      | null;
    const detail =
      typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function apiBase(): string {
  return API_BASE;
}
