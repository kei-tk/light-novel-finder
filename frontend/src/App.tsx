import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { Layout } from "./components/Layout";
import HomePage from "./pages/HomePage";
import SearchPage from "./pages/SearchPage";
import SeriesDetailPage from "./pages/SeriesDetailPage";
import AuthorPage from "./pages/AuthorPage";
import AuthPage from "./pages/AuthPage";
import AccountPage from "./pages/AccountPage";

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Layout>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/series/:id" element={<SeriesDetailPage />} />
            <Route path="/author/:name" element={<AuthorPage />} />
            <Route path="/auth" element={<AuthPage />} />
            <Route
              path="/account"
              element={
                <RequireAuth>
                  <AccountPage />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </AuthProvider>
  );
}

function RequireAuth({ children }: { children: React.ReactElement }) {
  const { user, loading } = useAuth();
  if (loading) return <FullScreenLoader />;
  if (!user) return <Navigate to="/auth" replace />;
  return children;
}

function FullScreenLoader() {
  return (
    <div className="min-h-[60vh] flex items-center justify-center">
      <div className="animate-bob text-5xl">📚</div>
    </div>
  );
}
