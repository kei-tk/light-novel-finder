import type { ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";

interface LayoutProps {
  children: ReactNode;
}

export function Layout({ children }: LayoutProps) {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();

  const handleSignOut = async () => {
    await signOut();
    navigate("/");
  };

  return (
    <div className="min-h-screen flex flex-col bg-cream-50">
      <Header user={user} onSignOut={handleSignOut} />
      <main className="flex-1 w-full">{children}</main>
      <Footer />
    </div>
  );
}

function Header({ user, onSignOut }: { user: { email?: string | null } | null; onSignOut: () => void }) {
  return (
    <header className="sticky top-0 z-30 bg-white/85 backdrop-blur border-b border-cream-100 shadow-soft">
      <div className="max-w-6xl mx-auto px-4 md:px-8 h-16 flex items-center justify-between gap-4">
        <Link to="/" className="flex items-center gap-2 group">
          <span className="text-2xl animate-bob">📚</span>
          <span className="font-rounded font-extrabold text-xl text-ink group-hover:text-primary-500 transition">
            ラノベ<span className="text-primary-500">ファインダー</span>
          </span>
        </Link>

        <nav className="hidden md:flex items-center gap-1">
          <NavItem to="/" label="ホーム" />
          <NavItem to="/search" label="さがす" />
          {user && <NavItem to="/account" label="マイページ" />}
        </nav>

        <div className="flex items-center gap-2">
          {user ? (
            <>
              <Link
                to="/account"
                className="hidden sm:flex items-center justify-center w-10 h-10 rounded-full bg-pop-gradient text-white font-rounded font-bold shadow-pop hover:scale-105 transition"
                title={user.email ?? "アカウント"}
              >
                {(user.email ?? "?").charAt(0).toUpperCase()}
              </Link>
              <button
                onClick={onSignOut}
                className="text-sm font-rounded font-bold text-ink-muted hover:text-primary-500"
              >
                ログアウト
              </button>
            </>
          ) : (
            <Link to="/auth" className="btn-pop !py-2 !px-5 text-sm">
              ログイン
            </Link>
          )}
        </div>
      </div>
      {/* モバイル用のサブナビ */}
      <nav className="md:hidden flex items-center justify-center gap-1 pb-2 px-2">
        <NavItem to="/" label="ホーム" />
        <NavItem to="/search" label="さがす" />
        {user && <NavItem to="/account" label="マイページ" />}
      </nav>
    </header>
  );
}

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `px-4 py-2 rounded-full font-rounded font-bold text-sm transition ${
          isActive
            ? "bg-primary-100 text-primary-700"
            : "text-ink-soft hover:bg-cream-100"
        }`
      }
    >
      {label}
    </NavLink>
  );
}

function Footer() {
  return (
    <footer className="mt-20 border-t border-cream-100 bg-white py-8">
      <div className="max-w-6xl mx-auto px-4 md:px-8 text-center text-sm text-ink-muted font-rounded">
        <p>
          📚 ラノベファインダー — AI で読みたい1冊が見つかる
        </p>
        <p className="mt-2 text-xs">
          Powered by openBD &amp; hanmoto.com / Gemini で雰囲気スコア生成
        </p>
      </div>
    </footer>
  );
}
