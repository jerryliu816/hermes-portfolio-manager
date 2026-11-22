import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Holdings } from "./pages/Holdings";
import { Accounts } from "./pages/Accounts";
import { PolicyPage } from "./pages/Policy";
import { Journal } from "./pages/Journal";
import { Imports } from "./pages/Imports";
import { Advisor } from "./pages/Advisor";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // Portfolio state changes on import, not on a timer.
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: false,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Dashboard />} />
            <Route path="holdings" element={<Holdings />} />
            <Route path="accounts" element={<Accounts />} />
            <Route path="policy" element={<PolicyPage />} />
            <Route path="advisor" element={<Advisor />} />
            <Route path="journal" element={<Journal />} />
            <Route path="imports" element={<Imports />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
