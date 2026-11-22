import { NavLink, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

const NAV = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/holdings", label: "Holdings" },
  { to: "/accounts", label: "Accounts" },
  { to: "/policy", label: "Policy" },
  { to: "/advisor", label: "Advisor" },
  { to: "/journal", label: "Journal" },
  { to: "/imports", label: "Imports" },
];

function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark" | null>(() => {
    try {
      return (localStorage.getItem("theme") as "light" | "dark" | null) ?? null;
    } catch {
      return null;
    }
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme) {
      root.setAttribute("data-theme", theme);
      try {
        localStorage.setItem("theme", theme);
      } catch {
        /* private browsing; the OS setting still applies */
      }
    } else {
      root.removeAttribute("data-theme");
    }
  }, [theme]);

  const dark =
    theme === "dark" ||
    (theme === null && window.matchMedia?.("(prefers-color-scheme: dark)").matches);

  return (
    <button
      type="button"
      onClick={() => setTheme(dark ? "light" : "dark")}
      className="rounded-lg border border-line p-2 text-ink-2 hover:text-ink"
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {dark ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}

/** A standing reminder when accounts still need classifying. */
function SetupBadge() {
  const { data } = useQuery({
    queryKey: ["needs-classification"],
    queryFn: api.needsClassification,
    retry: false,
  });
  if (!data?.length) return null;
  return (
    <NavLink
      to="/accounts"
      className="rounded-md px-2 py-1 text-xs font-medium"
      style={{ background: "var(--status-warning-bg)", color: "var(--status-warning)" }}
    >
      {data.length} account{data.length === 1 ? "" : "s"} need classifying
    </NavLink>
  );
}

export function Layout() {
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-20 border-b border-line bg-surface/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-4 py-3">
          <span className="text-sm font-semibold tracking-tight">Portfolio</span>
          <nav className="flex flex-1 gap-1 overflow-x-auto" aria-label="Main">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-lg px-3 py-1.5 text-sm ${
                    isActive ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:text-ink"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <SetupBadge />
          <ThemeToggle />
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
